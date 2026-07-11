"""Unit tests for the NKP page parser (``materials.sourcing.nkp.parse``).

Pure HTML→dict parsing, no DB/network — locks the field extraction against a
representative ``full_detail`` fixture and the listing pagination shape.
"""

from __future__ import annotations

from pathlib import Path

from django.test import SimpleTestCase

from materials.sourcing.nkp.parse import (
    extract_listing,
    parse_browse,
    parse_detail,
    parse_year_months,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "nkp_full_detail_sample.html"


class NkpDetailParseTests(SimpleTestCase):
    def setUp(self):
        self.html = _FIXTURE.read_text(encoding="utf-8")

    def test_parses_core_metadata(self):
        item = parse_detail(self.html, "8389", "https://nkp.gov.np/full_detail/8389")
        self.assertEqual(item["decision_no"], "9346")
        self.assertEqual(item["decision_no_bs"], "९३४६")
        self.assertEqual(item["case_number"], "067-CR-1288")
        self.assertEqual(item["case_name"], "मानव बेचबिखन, बालविवाह, जबर्जस्ती करणी")
        self.assertEqual(item["court"], "सर्वोच्च अदालत")
        self.assertEqual(item["bench"], "संयुक्त इजलास")
        self.assertEqual(item["year_bs"], "2072")
        self.assertEqual(item["decision_date_bs"], "2071-10-19")

    def test_judges_and_headnotes(self):
        item = parse_detail(self.html, "8389")
        self.assertEqual(len(item["judges"]), 2)
        self.assertIn("सुशीला कार्की", item["judges"])
        self.assertTrue(item["headnotes"])
        self.assertEqual(item["headnotes"][0]["prakaran"], "5")

    def test_full_text_present_and_utf8(self):
        item = parse_detail(self.html, "8389")
        self.assertIn("full_text", item)
        # No mojibake markers; real Devanagari present.
        self.assertNotIn("Ã", item["full_text"])
        self.assertNotIn("à¤", item["full_text"])

    def test_no_content_column_returns_none(self):
        self.assertIsNone(parse_detail("<html><body>nope</body></html>", "1"))


class NkpListingParseTests(SimpleTestCase):
    def test_listing_scopes_to_result_rows_and_reads_total(self):
        html = """
        <div class="main-listing"><h2>२२ खोजी नतिजाहरु</h2>
          <article class="format-standard"><a href="/full_detail/111">निर्णय नं. १ - x</a></article>
          <article class="format-standard"><a href="/full_detail/222">निर्णय नं. २ - y</a></article>
        </div>
        <div class="col-md-4"><a href="/full_detail/999">sidebar recently-published</a></div>
        """
        r = extract_listing(html)
        # sidebar (999) excluded; only the two article rows counted.
        self.assertEqual(r["detail_ids"], ["111", "222"])
        self.assertEqual(r["total"], 22)

    def test_empty_overrun_page_yields_no_ids(self):
        # Past-the-last-page: no article rows, only sidebar → zero result ids.
        html = '<div class="col-md-4"><a href="/full_detail/999">sidebar</a></div>'
        self.assertEqual(extract_listing(html)["detail_ids"], [])


class NkpBrowseParseTests(SimpleTestCase):
    def test_browse_years_and_counts(self):
        html = """
        <a href="/browse_monthly/?Submit=Yes&year=2082">बि.स. २०८२ (५९ थान)</a>
        <a href="/browse_monthly/?Submit=Yes&year=2081">बि.स. २०८१ (१५० थान)</a>
        """
        years = parse_browse(html)
        self.assertEqual([y["year"] for y in years], ["2082", "2081"])
        self.assertEqual(years[0]["expected"], 59)
        self.assertEqual(years[1]["expected"], 150)

    def test_year_months(self):
        html = """
        <a href="/advance_search/?Submit=Yes&year=2082&month=1">बैशाख</a>
        <a href="/advance_search/?Submit=Yes&year=2082&month=3">असार</a>
        """
        self.assertEqual(parse_year_months(html), ["1", "3"])
