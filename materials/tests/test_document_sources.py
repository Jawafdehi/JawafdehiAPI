"""The document-source catalogue, ``GET /api/document-sources/``.

What the archive holds by source, how much of each is readable, and the series
inside a source. The cases that matter are the ones a consumer cannot recover
from: an unlabelled token presented as though it were named, a missing
readability figure presented as zero, and a series list that silently drops a
bucket the upstream has just minted.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_document_sources.py
"""

from __future__ import annotations

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from cases.api_views import STATISTICS_SNAPSHOT_KEY
from cases.models import StatisticsSnapshot
from materials.sources import build_catalogue

URL = "/api/document-sources/"


def _snapshot(materials: dict, *, placeholder: bool = False) -> StatisticsSnapshot:
    return StatisticsSnapshot.objects.create(
        key=STATISTICS_SNAPSHOT_KEY,
        data={"materials": materials},
        computed_at=timezone.now(),
        is_placeholder=placeholder,
    )


MATERIALS = {
    "by_source": [
        {"source": "ag", "count": 99750},
        {"source": "official_report", "count": 223},
        {"source": "dfmis", "count": 2117},
    ],
    "by_dataset_bucket": [
        {"source": "official_report", "dataset_bucket": "report_annual-report", "count": 18},
        {"source": "official_report", "dataset_bucket": "report_province-report", "count": 56},
    ],
    "by_readability": [
        {"source": "official_report", "count": 223},
    ],
}


class CatalogueShapeTests(APITestCase):
    databases = "__all__"

    def setUp(self):
        _snapshot(MATERIALS)

    def test_lists_every_source_with_its_count(self):
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = {r["source"]: r for r in resp.json()["sources"]}
        self.assertEqual(rows["ag"]["count"], 99750)
        self.assertEqual(rows["official_report"]["count"], 223)

    def test_the_two_general_offices_are_told_apart(self):
        # The error this endpoint exists to prevent: `ag` is the ATTORNEY
        # General and `official_report` is the AUDITOR General. A caller picking
        # by token spelling gets charge sheets when it wanted audit reports.
        rows = {r["source"]: r for r in self.client.get(URL).json()["sources"]}
        self.assertIn("Attorney General", rows["ag"]["publisher"])
        self.assertIn("Auditor General", rows["official_report"]["publisher"])

    def test_series_are_nested_under_their_source(self):
        rows = {r["source"]: r for r in self.client.get(URL).json()["sources"]}
        series = {s["bucket"]: s for s in rows["official_report"]["series"]}
        self.assertEqual(series["report_annual-report"]["count"], 18)
        self.assertEqual(series["report_annual-report"]["label"], "Annual reports")
        # A source with no buckets reports an empty list, not a null.
        self.assertEqual(rows["ag"]["series"], [])

    def test_readable_is_reported_apart_from_count(self):
        # Holding a document and having read it are different facts. 100% of the
        # charge sheets are in the archive and none is transcribed.
        rows = {r["source"]: r for r in self.client.get(URL).json()["sources"]}
        self.assertEqual(rows["official_report"]["readable"], 223)
        self.assertEqual(rows["ag"]["readable"], 0)


class HonestyTests(APITestCase):
    databases = "__all__"

    def test_an_unlabelled_source_says_so_rather_than_guessing(self):
        # `dfmis` has no confirmed label anywhere in the codebase. Inventing a
        # plausible one on an accountability archive is worse than the raw
        # token, because a reader cannot tell an invented label from a reviewed
        # one.
        _snapshot(MATERIALS)
        rows = {r["source"]: r for r in self.client.get(URL).json()["sources"]}
        self.assertFalse(rows["dfmis"]["labelled"])
        self.assertIsNone(rows["dfmis"]["name"])
        self.assertTrue(rows["ag"]["labelled"])

    def test_a_snapshot_without_readability_reports_unknown_not_zero(self):
        # An older snapshot predates the aggregate. Defaulting its absence to 0
        # would claim we have read nothing, which is false and unrecoverable
        # from the payload.
        stale = {k: v for k, v in MATERIALS.items() if k != "by_readability"}
        _snapshot(stale)
        rows = {r["source"]: r for r in self.client.get(URL).json()["sources"]}
        self.assertIsNone(rows["ag"]["readable"])

    def test_an_unlabelled_series_still_appears(self):
        # Derived from the data, not from the label map: a bucket the upstream
        # mints tomorrow must show up today, or a whole shelf is invisible.
        _snapshot(
            {
                "by_source": [{"source": "official_report", "count": 5}],
                "by_dataset_bucket": [
                    {"source": "official_report", "dataset_bucket": "report_brand-new", "count": 5}
                ],
                "by_readability": [],
            }
        )
        series = self.client.get(URL).json()["sources"][0]["series"]
        self.assertEqual(series[0]["bucket"], "report_brand-new")
        self.assertIsNone(series[0]["label"])
        self.assertFalse(series[0]["labelled"])


class CachingTests(APITestCase):
    databases = "__all__"

    def test_a_real_snapshot_is_publicly_cacheable(self):
        _snapshot(MATERIALS)
        self.assertIn("s-maxage", self.client.get(URL)["Cache-Control"])

    def test_a_placeholder_snapshot_is_never_cached(self):
        # Its zeroed blocks would otherwise be pinned at the edge for a full TTL
        # and the archive would advertise itself as empty worldwide.
        _snapshot({"by_source": []}, placeholder=True)
        self.assertEqual(self.client.get(URL)["Cache-Control"], "no-store")

    def test_no_snapshot_yet_is_empty_and_uncached(self):
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["sources"], [])
        self.assertEqual(resp["Cache-Control"], "no-store")


class BuilderUnitTests(APITestCase):
    databases = "__all__"

    def test_empty_materials_block_is_an_empty_catalogue(self):
        self.assertEqual(build_catalogue({}), [])
