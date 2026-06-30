"""Tests for the IRI-driven Sitemaps (sqlite).

Covers: /sitemap.xml is a well-formed sitemap INDEX; child sitemaps contain the
expected canonical IRIs verbatim (NOT re-prefixed by a Site domain); and the
large-corpus case paginates into multiple child sitemaps (index has > one entry
for a type that exceeds the 50k cap, simulated by patching the limit).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest import mock
from xml.etree import ElementTree as ET

from django.test import TestCase, override_settings

from cases.models import Case, CaseState, CaseType
from monolith.discovery import corpus, sitemaps
from nes_service.entities.models import StoredEntity
from ngm_service.courts.models import Court, CourtCase

SM_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _published_case(title="Pub case"):
    case = Case(case_type=CaseType.CORRUPTION, title=title)
    case.save()
    case.state = CaseState.PUBLISHED
    case.save()
    return case


def _entity(prefix="person", slug="ram"):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    iri = f"https://jawafdehi.org/entity/{prefix}/{slug}"
    return StoredEntity.objects.create(
        iri=iri, entity_type="Person", prefix=prefix, slug=slug,
        data={"@id": iri, "@type": "Person", "name": "Ram"},
        version=1, created_at=now, updated_at=now,
    )


class SitemapIndexTests(TestCase):
    databases = "__all__"

    def test_sitemap_index_is_well_formed_and_lists_sections(self):
        _published_case()
        _entity()
        resp = self.client.get("/sitemap.xml")
        assert resp.status_code == 200, resp.content
        root = ET.fromstring(resp.content)
        assert root.tag == f"{SM_NS}sitemapindex"
        locs = [el.text for el in root.iter(f"{SM_NS}loc")]
        # Sections that HAVE items appear (entity + case here).
        assert any("sitemap-entity.xml" in loc for loc in locs)
        assert any("sitemap-case.xml" in loc for loc in locs)

    def test_child_sitemap_contains_canonical_iri_verbatim(self):
        case = _published_case()
        resp = self.client.get("/sitemap-case.xml")
        assert resp.status_code == 200, resp.content
        root = ET.fromstring(resp.content)
        assert root.tag == f"{SM_NS}urlset"
        locs = [el.text for el in root.iter(f"{SM_NS}loc")]
        # The loc is the canonical @id IRI, NOT a Site-prefixed example.com URL.
        assert f"https://jawafdehi.org/case/{case.slug}" in locs
        for loc in locs:
            assert "example.com" not in loc
        # lastmod is emitted from updated_at.
        assert any(el.text for el in root.iter(f"{SM_NS}lastmod"))

    def test_entity_child_sitemap_contains_entity_iri(self):
        _entity(slug="ram-bahadur")
        resp = self.client.get("/sitemap-entity.xml")
        assert resp.status_code == 200
        root = ET.fromstring(resp.content)
        locs = [el.text for el in root.iter(f"{SM_NS}loc")]
        assert "https://jawafdehi.org/entity/person/ram-bahadur" in locs

    def test_draft_case_absent_from_sitemap(self):
        draft = Case(case_type=CaseType.CORRUPTION, title="Draft")
        draft.save()  # DRAFT
        # The case section yields an empty (but well-formed) urlset — the public
        # -only guarantee: the DRAFT case's IRI must not appear anywhere.
        resp = self.client.get("/sitemap-case.xml")
        assert resp.status_code == 200, resp.content
        root = ET.fromstring(resp.content)
        assert root.tag == f"{SM_NS}urlset"
        locs = [el.text for el in root.iter(f"{SM_NS}loc")]
        assert locs == []
        assert f"https://jawafdehi.org/case/{draft.slug}" not in locs


class SitemapPaginationTests(TestCase):
    """Large-corpus handling: a type exceeding the per-sitemap limit paginates
    into multiple child sitemaps referenced from the index (?p=N)."""

    databases = "__all__"

    def test_large_corpus_paginates_into_multiple_child_sitemaps(self):
        # Create a handful of courtcases, then shrink the per-page limit so they
        # split across pages — exercising the sitemap-index pagination path
        # without inserting 50k rows.
        court = Court.objects.create(
            identifier="kathmandudc", court_type="district", full_name_nepali="ज"
        )
        for i in range(5):
            CourtCase.objects.create(
                case_number=f"082-oa-{i:04d}",
                court=court,
                registration_date_ad=date(2026, 1, 11),
            )

        original = sitemaps._CorpusSitemap.limit
        try:
            # 2 per page -> 5 courtcases -> 3 pages.
            sitemaps.CourtCaseSitemap.limit = 2
            resp = self.client.get("/sitemap.xml")
            assert resp.status_code == 200
            root = ET.fromstring(resp.content)
            locs = [el.text for el in root.iter(f"{SM_NS}loc")]
            courtcase_pages = [loc for loc in locs if "sitemap-courtcase.xml" in loc]
            # First page is unsuffixed; subsequent pages carry ?p=2, ?p=3.
            assert len(courtcase_pages) == 3, courtcase_pages
            assert any("p=2" in loc for loc in courtcase_pages)
            assert any("p=3" in loc for loc in courtcase_pages)

            # And each paginated child sitemap is fetchable + well-formed.
            page2 = self.client.get("/sitemap-courtcase.xml?p=2")
            assert page2.status_code == 200
            page2_root = ET.fromstring(page2.content)
            assert page2_root.tag == f"{SM_NS}urlset"
            assert len(list(page2_root.iter(f"{SM_NS}url"))) == 2
        finally:
            sitemaps.CourtCaseSitemap.limit = original


class SitemapIndexEfficiencyTests(TestCase):
    """The index must compute page counts from a cheap COUNT(*) (count_resources)
    WITHOUT materializing items() — the DoS fix."""

    databases = "__all__"

    def test_index_page_count_uses_count_resources_not_items(self):
        _entity()
        _published_case()
        # If the index materialized items(), iter_resources would be called to
        # build the list. The fix routes paging through count_resources instead.
        with mock.patch.object(
            sitemaps.corpus, "count_resources", wraps=sitemaps.corpus.count_resources
        ) as count_spy, mock.patch.object(
            sitemaps.corpus, "iter_resources", wraps=sitemaps.corpus.iter_resources
        ) as iter_spy:
            resp = self.client.get("/sitemap.xml")
            assert resp.status_code == 200, resp.content
            # Page count came from count_resources...
            assert count_spy.called
            # ...and the index did NOT materialize the corpus via iter_resources.
            assert not iter_spy.called, "index should not load items() to count"

    def test_index_get_latest_lastmod_uses_aggregate_not_items(self):
        _entity()
        with mock.patch.object(
            sitemaps.corpus, "max_lastmod", wraps=sitemaps.corpus.max_lastmod
        ) as max_spy, mock.patch.object(
            sitemaps.corpus, "iter_resources", wraps=sitemaps.corpus.iter_resources
        ) as iter_spy:
            resp = self.client.get("/sitemap.xml")
            assert resp.status_code == 200
            assert max_spy.called
            assert not iter_spy.called


@override_settings(ALLOWED_HOSTS=["*"])
class SitemapIndexCanonicalHostTests(TestCase):
    """The index child-sitemap <loc> links must be built on the canonical IRI
    host, NOT the (spoofable) request Host header — cache-poisoning fix.

    ALLOWED_HOSTS=["*"] is set so the hostile Host header reaches the VIEW (in a
    real deploy ALLOWED_HOSTS is the first line of defense and would 400 it; here
    we deliberately bypass that outer gate to prove the *inner* defense — the
    view pinning child <loc> to the canonical authority regardless of Host)."""

    databases = "__all__"

    def test_child_links_use_canonical_host_not_request_host(self):
        _entity()
        # Send a hostile Host header; the child <loc> must still be jawafdehi.org.
        resp = self.client.get("/sitemap.xml", HTTP_HOST="evil.example.com")
        assert resp.status_code == 200, resp.content
        root = ET.fromstring(resp.content)
        locs = [el.text for el in root.iter(f"{SM_NS}loc")]
        assert locs, "index should list at least the entity section"
        for loc in locs:
            assert loc.startswith("https://jawafdehi.org/"), loc
            assert "evil.example.com" not in loc


@override_settings(TESTING=False, DISCOVERY_CACHE_IN_TESTS=False)
class SitemapCachingTests(TestCase):
    """Server-side caching: a second request reuses the rendered document instead
    of re-scanning the corpus."""

    databases = "__all__"

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)

    def test_resourcelist_second_request_does_not_rescan(self):
        _entity()
        with mock.patch(
            "monolith.discovery.resourcesync.corpus.iter_resources",
            wraps=corpus.iter_resources,
        ) as iter_spy:
            r1 = self.client.get("/resourcesync/resourcelist.xml")
            assert r1.status_code == 200
            calls_after_first = iter_spy.call_count
            assert calls_after_first >= 1
            r2 = self.client.get("/resourcesync/resourcelist.xml")
            assert r2.status_code == 200
            # Second request served from cache — no further corpus scan.
            assert iter_spy.call_count == calls_after_first
            assert r2.content == r1.content

    def test_cache_busts_when_corpus_changes(self):
        _entity(slug="first")
        r1 = self.client.get("/resourcesync/resourcelist.xml")
        assert b"/entity/person/first" in r1.content
        # A new public resource changes the corpus-version stamp -> fresh render.
        _entity(slug="second")
        r2 = self.client.get("/resourcesync/resourcelist.xml")
        assert b"/entity/person/second" in r2.content
