"""Tests for the ResourceSync documents (sqlite).

Covers the MVP trio — Source Description (/.well-known/resourcesync), Capability
List, Resource List — for well-formedness, the rs: namespace + capability
metadata, the canonical IRIs as loc, and the describedby JSON-LD links.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from xml.etree import ElementTree as ET

from django.test import TestCase

from cases.models import Case, CaseState, CaseType
from discovery import resourcesync
from entities.models import StoredEntity
from courts.models import Court, CourtCase

SM = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
RS = "{http://www.openarchives.org/rs/terms/}"


def _published_case(title="Pub"):
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


class SourceDescriptionTests(TestCase):
    databases = "__all__"

    def test_well_formed_and_capability_description(self):
        resp = self.client.get("/.well-known/resourcesync")
        assert resp.status_code == 200, resp.content
        assert "xml" in resp["Content-Type"]
        root = ET.fromstring(resp.content)
        assert root.tag == f"{SM}urlset"
        # rs:md capability="description" at the document level.
        md = root.find(f"{RS}md")
        assert md is not None
        assert md.get("capability") == "description"
        # Points at the capability list.
        locs = [el.text for el in root.iter(f"{SM}loc")]
        assert any("capabilitylist.xml" in loc for loc in locs)


class CapabilityListTests(TestCase):
    databases = "__all__"

    def test_well_formed_advertises_resourcelist(self):
        resp = self.client.get("/resourcesync/capabilitylist.xml")
        assert resp.status_code == 200, resp.content
        root = ET.fromstring(resp.content)
        assert root.tag == f"{SM}urlset"
        md = root.find(f"{RS}md")
        assert md is not None and md.get("capability") == "capabilitylist"
        # Advertises the resourcelist capability + links up to the description.
        child_caps = [
            el.get("capability") for el in root.iter(f"{RS}md")
        ]
        assert "resourcelist" in child_caps
        ups = [el for el in root.iter(f"{RS}ln") if el.get("rel") == "up"]
        assert ups and ".well-known/resourcesync" in ups[0].get("href")


class ResourceListTests(TestCase):
    databases = "__all__"

    def test_well_formed_resourcelist_with_iris_and_describedby(self):
        _entity(slug="ram-bahadur")
        court = Court.objects.create(
            identifier="kathmandudc", court_type="district", full_name_nepali="ज"
        )
        CourtCase.objects.create(
            case_number="082-oa-0503", court=court,
            registration_date_ad=date(2026, 1, 11),
        )
        case = _published_case()

        resp = self.client.get("/resourcesync/resourcelist.xml")
        assert resp.status_code == 200, resp.content
        root = ET.fromstring(resp.content)
        assert root.tag == f"{SM}urlset"
        # Document-level rs:md capability="resourcelist" with an `at` timestamp.
        md = root.find(f"{RS}md")
        assert md is not None
        assert md.get("capability") == "resourcelist"
        assert md.get("at")

        locs = [el.text for el in root.iter(f"{SM}loc")]
        assert "https://jawafdehi.org/entity/person/ram-bahadur" in locs
        assert "https://jawafdehi.org/courtcase/kathmandudc/082-oa-0503" in locs
        assert f"https://jawafdehi.org/case/{case.slug}" in locs

        # describedby links carry the schema.org JSON-LD URL for the types that
        # have one (entity, courtcase); the case has none.
        describedby = [
            el.get("href")
            for el in root.iter(f"{RS}ln")
            if el.get("rel") == "describedby"
        ]
        assert any("/api/nes/entities/person/ram-bahadur" in h for h in describedby)
        assert any(
            "/api/ngm/materials/court/kathmandudc.082-oa-0503" in h
            for h in describedby
        )

    def test_resourcelist_excludes_draft_cases(self):
        draft = Case(case_type=CaseType.CORRUPTION, title="Draft")
        draft.save()  # DRAFT
        published = _published_case("Published")
        xml = resourcesync.resource_list()
        assert f"https://jawafdehi.org/case/{published.slug}" in xml
        assert f"https://jawafdehi.org/case/{draft.slug}" not in xml

    def test_resourcelist_is_a_sitemaps_extension(self):
        # ResourceSync docs ARE sitemap urlsets in the sitemaps namespace,
        # augmented with the rs: namespace — verify both namespaces are present.
        xml = resourcesync.resource_list(resources=[])
        assert resourcesync.SITEMAP_NS in xml
        assert resourcesync.RS_NS in xml
        root = ET.fromstring(xml)
        assert root.tag == f"{SM}urlset"


class RobotsTxtTests(TestCase):
    databases = "__all__"

    def test_robots_points_at_sitemap(self):
        resp = self.client.get("/robots.txt")
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Sitemap: https://jawafdehi.org/sitemap.xml" in body
        assert "resourcesync" in body.lower()
