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
        # `cib` is the standing unlabelled fixture. It is not a token that
        # merely HAPPENS to be unnamed yet — it cannot be named: the
        # authoritative source inventory uses "CIB" for both the Nepal Police
        # Central Investigation Bureau and the Credit Information Bureau, and
        # its rows are not publicly readable to break the tie. See the
        # deliberately-unlabelled block in `materials/sources.py`. That makes it
        # a stable fixture — unlike `dfmis`, which stood here until it was
        # confirmed and labelled on 2026-10-29.
        {"source": "cib", "count": 91},
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
        self.assertIn("Attorney General", rows["ag"]["publisher"]["en"])
        self.assertIn("Auditor General", rows["official_report"]["publisher"]["en"])

    def test_every_publisher_claim_is_sourced_or_absent(self):
        # The failure this guards against is an INVENTED office. A publisher
        # here must come from the materials' own publisher field or from the
        # reviewed series registry; sources with neither carry none, because a
        # reader cannot tell an invented attribution from a checked one.
        from materials.sources import SOURCE_LABELS

        # Three reasons a token carries no publisher, all of them deliberate:
        # the records name none (`kanun_patrika`); the issuer varies per record
        # and is on the record (`court_order`, `court_filing`, `news`,
        # `document`, `legal_corpus`, `social_media`); or the records fix the
        # government LEVEL but never name the office (`province/koshi`).
        unsourced = {
            "kanun_patrika",
            "court_order",
            "court_filing",
            "news",
            "document",
            "legal_corpus",
            "social_media",
            "province/koshi",
        }
        for token in unsourced:
            self.assertNotIn(
                "publisher", SOURCE_LABELS[token], f"{token} has no citable publisher"
            )
        for token in ("ag", "official_report", "nkp", "ppmo", "bolpatra"):
            self.assertIsInstance(SOURCE_LABELS[token]["publisher"], dict)

    def test_a_publisher_read_off_the_records_own_field_is_used_verbatim(self):
        # The three US tokens are the only ones whose materials carry a
        # populated schema.org `publisher`. That field is the strongest
        # evidence class available, so the label must restate it rather than
        # paraphrase it into something shorter and wrong.
        from materials.sources import SOURCE_LABELS

        self.assertEqual(
            SOURCE_LABELS["us_court"]["publisher"]["en"],
            "United States District Court for the District of Columbia",
        )
        self.assertEqual(
            SOURCE_LABELS["us_sec"]["publisher"]["en"],
            "United States Securities and Exchange Commission",
        )
        self.assertEqual(
            SOURCE_LABELS["us_doj"]["publisher"]["en"],
            "United States Department of Justice",
        )

    def test_a_publisher_known_only_from_the_attached_document_is_still_labelled(self):
        # `news_shilaptra` names no outlet in any metadata field. It is labelled
        # because its `associatedMedia` RAW link points at shilapatra.com, whose
        # page title matches the record's own Nepali name. Metadata silence is
        # not the end of the evidence — the attached document usually carries
        # the original URL, and that domain names the publisher.
        #
        # The label follows the DOMAIN, not the token, which misspells it.
        from materials.sources import SOURCE_LABELS

        self.assertEqual(SOURCE_LABELS["news_shilaptra"]["publisher"]["en"], "Shilapatra")

    def test_an_unexpanded_acronym_stays_unexpanded(self):
        # `occrp`, `nrb`, `sebon` and `dmli` are named by their own records, but
        # nothing in this repo, the sourcing README or the reviewed frontend
        # registry spells the acronyms out. Recording the acronym fixes the
        # token to a named body; expanding it here would be invention, which is
        # the one thing this module must not do.
        from materials.sources import SOURCE_LABELS

        for token in ("occrp", "nrb", "sebon", "dmli"):
            publisher = SOURCE_LABELS[token]["publisher"]["en"]
            self.assertEqual(publisher, publisher.upper(), f"{token} publisher was expanded")

    def test_every_label_carries_a_name_and_a_description(self):
        from materials.sources import SOURCE_LABELS

        for token, label in SOURCE_LABELS.items():
            self.assertTrue(label.get("name", {}).get("en"), f"{token} has no English name")
            self.assertTrue(label.get("description"), f"{token} has no description")

    def test_nkp_is_attributed_to_the_court_not_the_journal(self):
        # The materials' own publisher field says सर्वोच्च अदालत. The journal
        # publishes these precedents; the Supreme Court issues them.
        from materials.sources import SOURCE_LABELS

        self.assertIn("Supreme Court", SOURCE_LABELS["nkp"]["publisher"]["en"])

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
        # `cib` cannot be named from any evidence available (see the fixture
        # note above). Inventing a plausible label on an accountability archive
        # is worse than the raw token, because a reader cannot tell an invented
        # label from a reviewed one.
        _snapshot(MATERIALS)
        rows = {r["source"]: r for r in self.client.get(URL).json()["sources"]}
        self.assertFalse(rows["cib"]["labelled"])
        self.assertIsNone(rows["cib"]["name"])
        self.assertTrue(rows["ag"]["labelled"])

    def test_a_pending_merge_token_is_labelled_and_says_it_is_pending(self):
        # `chargesheet` misspells `charge_sheet` and `court_order_0133` let a
        # case number leak in. They hold real documents, so they ARE labelled,
        # but the description has to carry the pending merge or a tidy name
        # quietly hides the defect it exists to fix.
        #
        # `charge_sheet` and `press_release` are deliberately NOT in this list:
        # sourcing a case upload by its material_type is the documented
        # convention (#344), so they are legitimate sources, not accidents.
        from materials.sources import SOURCE_LABELS

        for token in ("chargesheet", "court_order_0133"):
            label = SOURCE_LABELS[token]
            self.assertTrue(label["name"]["en"])
            self.assertIn(
                "pending",
                label["description"].lower(),
                f"{token} is labelled but does not disclose the pending merge",
            )

    def test_a_duplicate_token_reuses_the_reviewed_attribution(self):
        # The point of labelling a duplicate is that it should be
        # indistinguishable in CONTENT from the token it duplicates — same
        # documents, same publisher. Re-deriving an attribution here is how the
        # two drift apart and start making different claims about one shelf.
        from materials.sources import SOURCE_LABELS as S

        self.assertEqual(S["charge_sheet"]["publisher"], S["ag"]["publisher"])
        self.assertEqual(S["chargesheet"]["publisher"], S["charge_sheet"]["publisher"])
        self.assertEqual(S["press_release"]["publisher"], S["ciaa_press_release"]["publisher"])
        self.assertEqual(
            S["court_order_0133"]["publisher"], S["special_court_order"]["publisher"]
        )

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


class NotReadyTests(APITestCase):
    """Before the first refresh the answer is "I do not know yet", not "empty".

    The refresh runs hourly, so this window is real. A 200 with an empty list
    would be indistinguishable from an archive that holds nothing — the same
    unknown-vs-zero conflation the payload avoids for ``readable``.
    """

    databases = "__all__"

    def test_no_snapshot_yet_is_503_not_an_empty_archive(self):
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertFalse(resp.json()["ready"])
        self.assertEqual(resp["Cache-Control"], "no-store")
        self.assertEqual(resp["Retry-After"], "300")

    def test_a_placeholder_snapshot_is_also_not_ready(self):
        # A placeholder's zeroed blocks are not an answer either.
        _snapshot({"by_source": []}, placeholder=True)
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertFalse(resp.json()["ready"])

    def test_a_real_snapshot_is_ready_and_briefly_cacheable(self):
        _snapshot(MATERIALS)
        resp = self.client.get(URL)
        self.assertTrue(resp.json()["ready"])
        self.assertIn("max-age=60", resp["Cache-Control"])
        # No s-maxage: the Cloudflare ruleset bypasses this path by default, so
        # an edge directive would describe caching that does not happen.
        self.assertNotIn("s-maxage", resp["Cache-Control"])


class BuilderUnitTests(APITestCase):
    databases = "__all__"

    def test_empty_materials_block_is_an_empty_catalogue(self):
        self.assertEqual(build_catalogue({}), [])


class ReadabilityAggregateTests(APITestCase):
    """``by_readability`` counts documents we have READ, not ones with the key.

    ``data__has_key("text")`` is true for ``""``, ``{}`` and JSON ``null`` — all
    of which are materials whose conversion produced nothing. Counting them
    would overstate how much of the archive is quotable, which is the one number
    this aggregate exists to report honestly.
    """

    databases = "__all__"

    def _material(self, ident, data_extra):
        from materials.jsonld import MATERIAL_CONTEXT
        from materials.models import Material

        iri = f"https://jawafdehi.org/material/probe/{ident}"
        return Material.objects.create(
            iri=iri,
            material_type="document",
            source="probe",
            ident=ident,
            data={
                "@context": MATERIAL_CONTEXT,
                "@type": "DigitalDocument",
                "@id": iri,
                "name": {"ne": "परीक्षण"},
                **data_extra,
            },
        )

    def test_only_real_text_counts_as_read(self):
        from cases.services.statistics import _materials_metrics

        self._material("with-text", {"text": "वास्तविक पाठ"})
        self._material("bilingual", {"text": {"ne": "पाठ"}})
        self._material("empty-string", {"text": ""})
        self._material("empty-object", {"text": {}})
        self._material("json-null", {"text": None})
        self._material("no-key", {})

        readable = {
            row["source"]: row["count"]
            for row in _materials_metrics()["by_readability"]
        }
        self.assertEqual(readable.get("probe"), 2, "only the two real transcripts")

    def test_page_level_content_counts_even_with_no_inline_text(self):
        # The CIAA annual reports keep their transcript as DocumentPage rows and
        # carry no data["text"] at all. Counting only the inline field would
        # report the one corpus with page-level text as 0% readable.
        from cases.services.statistics import _materials_metrics
        from materials.models import DocumentContent

        material = self._material("paged", {})
        DocumentContent.objects.create(
            material=material,
            doc_id="probe-1",
            dataset="probe",
            page_count=10,
            pages_with_text=9,
        )
        readable = {
            row["source"]: row["count"]
            for row in _materials_metrics()["by_readability"]
        }
        self.assertEqual(readable.get("probe"), 1)
