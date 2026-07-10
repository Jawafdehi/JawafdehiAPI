"""Tests for the public-corpus enumerator (the @id envelope, sqlite).

Asserts the public-only guarantee — especially that DRAFT / IN_REVIEW / CLOSED
cases are ABSENT — and that each record type yields its canonical IRI with a
lastmod and the right describedby JSON-LD URL.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest import mock

from django.test import TestCase

from cases.models import Case, CaseState, CaseType
from discovery import corpus
from entities.models import StoredEntity
from courts.models import Court, CourtCase
from materials.jsonld import MATERIAL_CONTEXT, MaterialType
from materials.models import Material


def _make_entity(prefix="person", slug="ram-bahadur"):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    iri = f"https://jawafdehi.org/entity/{prefix}/{slug}"
    return StoredEntity.objects.create(
        iri=iri,
        entity_type="Person",
        prefix=prefix,
        slug=slug,
        data={"@id": iri, "@type": "Person", "name": "Ram"},
        version=1,
        created_at=now,
        updated_at=now,
    )


def _make_material():
    doc = {
        "@context": MATERIAL_CONTEXT,
        "@type": "Legislation",
        "@id": "https://jawafdehi.org/material/nkp/2080-act-1",
        "name": {"ne": "ऐन"},
    }
    m = Material.from_jsonld(doc, material_type=MaterialType.LEGAL_CORPUS)
    m.save()
    return m


def _make_courtcase():
    court = Court.objects.create(
        identifier="kathmandudc", court_type="district", full_name_nepali="ज"
    )
    return CourtCase.objects.create(
        case_number="082-OA-0503",
        court=court,
        case_type="भ्रष्टाचार",
        registration_date_ad=date(2026, 1, 11),
    )


class CorpusEnumeratorTests(TestCase):
    databases = "__all__"

    def test_entity_resource_shape(self):
        _make_entity()
        resources = list(corpus.iter_resources((corpus.TYPE_ENTITY,)))
        assert len(resources) == 1
        r = resources[0]
        assert r.iri == "https://jawafdehi.org/entity/person/ram-bahadur"
        assert r.type == corpus.TYPE_ENTITY
        assert r.lastmod is not None
        assert r.jsonld_url == "/api/entities/person/ram-bahadur"

    def test_material_resource_shape(self):
        _make_material()
        resources = list(corpus.iter_resources((corpus.TYPE_MATERIAL,)))
        assert len(resources) == 1
        r = resources[0]
        assert r.iri == "https://jawafdehi.org/material/nkp/2080-act-1"
        assert r.jsonld_url == "/api/materials/nkp/2080-act-1"

    def test_nonlisted_materials_absent_from_iter_count_and_lastmod(self):
        # A LISTED material is public; UNLISTED/PRIVATE/soft-deleted must be
        # excluded from iter_resources, count_resources, AND max_lastmod (else a
        # non-public material leaks into the sitemap / drives its lastmod).
        from materials.models import Visibility

        listed = _make_material()
        private = Material.from_jsonld(
            {
                "@context": MATERIAL_CONTEXT,
                "@type": "Legislation",
                "@id": "https://jawafdehi.org/material/nkp/2080-act-2",
                "name": {"ne": "गोप्य"},
            },
            material_type=MaterialType.LEGAL_CORPUS,
        )
        private.visibility = Visibility.PRIVATE
        private.save()
        # Force the private row to have the newest updated_at.
        Material.objects.filter(pk=private.pk).update(
            updated_at=datetime(2030, 1, 1, tzinfo=timezone.utc)
        )

        iris = {r.iri for r in corpus.iter_resources((corpus.TYPE_MATERIAL,))}
        assert iris == {listed.iri}
        assert corpus.count_resources((corpus.TYPE_MATERIAL,)) == 1
        # lastmod must come from the LISTED row, not the newer PRIVATE one.
        assert corpus.max_lastmod((corpus.TYPE_MATERIAL,)) != datetime(
            2030, 1, 1, tzinfo=timezone.utc
        )

    def test_soft_deleted_entity_absent_from_iter_count_and_lastmod(self):
        # A soft-deleted entity (is_deleted=True) has vanished from every read
        # plane (list/detail/search) but its canonical @id must NOT keep
        # appearing as a live <loc> in the public sitemap / ResourceSync.
        live = _make_entity(slug="ram-bahadur")
        gone = _make_entity(prefix="person", slug="deleted-official")
        gone.is_deleted = True
        gone.save(update_fields=["is_deleted"])
        StoredEntity.objects.filter(pk=gone.pk).update(
            updated_at=datetime(2030, 1, 1, tzinfo=timezone.utc)
        )

        iris = {r.iri for r in corpus.iter_resources((corpus.TYPE_ENTITY,))}
        assert iris == {live.iri}
        assert corpus.count_resources((corpus.TYPE_ENTITY,)) == 1
        # The newer soft-deleted row must not drive the public lastmod.
        assert corpus.max_lastmod((corpus.TYPE_ENTITY,)) != datetime(
            2030, 1, 1, tzinfo=timezone.utc
        )

    def test_soft_deleted_courtcase_absent_from_iter_count_and_lastmod(self):
        # Same tombstone guarantee for court cases: a soft-deleted court case is
        # gone from the read plane and must not remain in public discovery.
        court = Court.objects.create(
            identifier="kathmandudc", court_type="district", full_name_nepali="ज"
        )
        live = CourtCase.objects.create(
            case_number="082-OA-0503", court=court, case_type="भ्रष्टाचार",
            registration_date_ad=date(2026, 1, 11),
        )
        gone = CourtCase.objects.create(
            case_number="082-OA-9999", court=court, case_type="भ्रष्टाचार",
            registration_date_ad=date(2026, 1, 11),
        )
        gone.is_deleted = True
        gone.save(update_fields=["is_deleted"])
        CourtCase.objects.filter(pk=gone.pk).update(
            updated_at=datetime(2030, 1, 1, tzinfo=timezone.utc)
        )

        iris = {r.iri for r in corpus.iter_resources((corpus.TYPE_COURTCASE,))}
        assert live.case_number.lower() in " ".join(iris)
        assert "082-oa-9999" not in " ".join(iris)
        assert corpus.count_resources((corpus.TYPE_COURTCASE,)) == 1
        assert corpus.max_lastmod((corpus.TYPE_COURTCASE,)) != datetime(
            2030, 1, 1, tzinfo=timezone.utc
        )

    def test_courtcase_resource_shape(self):
        _make_courtcase()
        resources = list(corpus.iter_resources((corpus.TYPE_COURTCASE,)))
        assert len(resources) == 1
        r = resources[0]
        assert r.iri == "https://jawafdehi.org/courtcase/kathmandudc/082-oa-0503"
        # describedby points at the court-case MATERIAL JSON-LD.
        assert r.jsonld_url == "/api/materials/court/kathmandudc.082-oa-0503"

    def test_published_case_is_public(self):
        case = Case(case_type=CaseType.CORRUPTION, title="A published case")
        case.save()
        case.state = CaseState.PUBLISHED
        case.save()
        resources = list(corpus.iter_resources((corpus.TYPE_CASE,)))
        assert len(resources) == 1
        assert resources[0].iri == f"https://jawafdehi.org/case/{case.slug}"
        assert resources[0].jsonld_url is None  # cases have no standalone JSON-LD

    def test_draft_and_in_review_cases_are_absent(self):
        # The public-only guarantee: only PUBLISHED cases appear.
        draft = Case(case_type=CaseType.CORRUPTION, title="A DRAFT case")
        draft.save()  # state defaults to DRAFT
        in_review = Case(case_type=CaseType.CORRUPTION, title="An IN_REVIEW case")
        in_review.save()
        # Force IN_REVIEW directly (bypass submit()'s strict validation).
        Case.objects.filter(pk=in_review.pk).update(state=CaseState.IN_REVIEW)

        published = Case(case_type=CaseType.CORRUPTION, title="Published")
        published.save()
        published.state = CaseState.PUBLISHED
        published.save()

        resources = list(corpus.iter_resources((corpus.TYPE_CASE,)))
        iris = {r.iri for r in resources}
        assert resources, "the published case should be present"
        assert f"https://jawafdehi.org/case/{published.slug}" in iris
        assert f"https://jawafdehi.org/case/{draft.slug}" not in iris
        assert f"https://jawafdehi.org/case/{in_review.slug}" not in iris

    def test_iter_all_types_and_count(self):
        _make_entity()
        _make_material()
        _make_courtcase()
        case = Case(case_type=CaseType.CORRUPTION, title="Pub")
        case.save()
        case.state = CaseState.PUBLISHED
        case.save()

        all_resources = list(corpus.iter_resources())
        types = {r.type for r in all_resources}
        assert types == set(corpus.ALL_TYPES)
        assert len(all_resources) == 4
        assert corpus.count_resources() == 4


class BadRowResilienceTests(TestCase):
    """One un-addressable row must be SKIPPED, never 500 the whole enumeration."""

    databases = "__all__"

    def test_courtcase_value_error_row_is_skipped_not_raised(self):
        # Two courtcases; the describedby material-IRI derivation raises
        # ValueError for the first only. Enumeration must yield the second, not
        # abort.
        court = Court.objects.create(
            identifier="kathmandudc", court_type="district", full_name_nepali="ज"
        )
        bad = CourtCase.objects.create(
            case_number="082-OA-BAD", court=court,
            registration_date_ad=date(2026, 1, 11),
        )
        CourtCase.objects.create(
            case_number="082-OA-GOOD", court=court,
            registration_date_ad=date(2026, 1, 11),
        )

        from materials.jsonld import court_case_material_iri

        def fake(court_arg, case_number_arg):
            if case_number_arg == bad.case_number:
                raise ValueError("MAX_IRI_LENGTH: ident too long")
            return court_case_material_iri(court_arg, case_number_arg)

        with mock.patch(
            "materials.jsonld.court_case_material_iri", side_effect=fake
        ):
            resources = list(corpus.iter_resources((corpus.TYPE_COURTCASE,)))

        iris = [r.iri for r in resources]
        assert any("082-oa-good" in i.lower() for i in iris)
        assert not any("082-oa-bad" in i.lower() for i in iris)

    def test_case_value_error_row_is_skipped_not_raised(self):
        good = Case(case_type=CaseType.CORRUPTION, title="Good")
        good.save()
        good.state = CaseState.PUBLISHED
        good.save()
        bad = Case(case_type=CaseType.CORRUPTION, title="Bad")
        bad.save()
        bad.state = CaseState.PUBLISHED
        bad.save()

        real_public_iri = type(good).public_iri

        def fake_public_iri(self):
            if self.pk == bad.pk:
                raise ValueError("Invalid slug for case IRI")
            return real_public_iri.fget(self)

        with mock.patch.object(
            type(good), "public_iri", property(fake_public_iri)
        ):
            resources = list(corpus.iter_resources((corpus.TYPE_CASE,)))

        iris = {r.iri for r in resources}
        assert f"https://jawafdehi.org/case/{good.slug}" in iris
        assert f"https://jawafdehi.org/case/{bad.slug}" not in iris
