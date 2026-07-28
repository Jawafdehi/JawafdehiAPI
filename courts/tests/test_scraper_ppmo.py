"""ppmo_blacklist port: parse the company-list JSON API + the command client.

Pure parse tests run without a network or DB. The command test injects a fake
source transport AND a fake ingestion client (via the ``build_*`` seams) and
asserts the command fetches, parses, and POSTs the right payloads — the ORM
upsert itself is exercised server-side in ``test_ingestion_api.py``.
"""

import json
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase

from courts.scraper import ppmo as P

CMD = "courts.management.commands.scrape_ppmo_blacklist"

# The public feed's envelope: {"success", "data": [ {company}, ... ]}. Includes a
# firm with a JV/end date, a firm with no owner/end date, and two skip cases
# (no company name; no start date).
_API_JSON = {
    "success": True,
    "data": [
        {
            "id": 1,
            "company_name": "श्री ग्लोबल ट्रेडिङ्ग प्रा.लि.",
            "address": "का.म.न.पा.-01, काठमाडौं",
            "owner": "श्री आशिष अग्रवाल",
            "public_entity_name": "श्री नेपाल वायुसेवा निगम",
            "remark": "<p>सार्वजनिक खरिद ऐन, २०६३ को दफा ६३ बमोजिम कालोसुचीमा राखिएको।</p>",
            "start_date": "2026-07-28",
            "end_date": "2027-07-27",
            "status": "Active",
        },
        {
            "id": 2,
            "company_name": "ABC Builders Pvt Ltd",
            "address": "Lalitpur",
            "owner": "",
            "public_entity_name": "Road Division",
            "remark": "",
            "start_date": "2025-01-10",
            "end_date": None,
        },
        {"id": 3, "company_name": "", "start_date": "2025-01-10"},   # no name → skip
        {"id": 4, "company_name": "No Date Co", "start_date": ""},    # no start → skip
    ],
}


# ── pure parse ───────────────────────────────────────────────────────────────


def test_parse_company_list_maps_and_skips():
    firms = P.parse_company_list(_API_JSON)
    names = [f.firm_name for f in firms]
    # rows 3 (no name) and 4 (no start_date) are skipped
    assert names == ["श्री ग्लोबल ट्रेडिङ्ग प्रा.लि.", "ABC Builders Pvt Ltd"]

    g = firms[0]
    assert g.proprietor_name == "आशिष अग्रवाल"          # श्री honorific stripped
    assert g.recommending_office == "नेपाल वायुसेवा निगम"  # श्री stripped
    assert "<p>" not in g.reason and "दफा ६३" in g.reason  # HTML flattened
    assert g.blacklist_date_ad.isoformat() == "2026-07-28"
    assert g.blacklist_date_bs == "2083-04-12"            # AD→BS derived
    assert g.effective_until_ad.isoformat() == "2027-07-27"
    assert g.effective_until_bs is not None
    assert g.duration == f"{g.blacklist_date_bs} to {g.effective_until_bs}"


def test_parse_single_date_firm_has_no_until():
    abc = P.parse_company_list(_API_JSON)[1]
    assert abc.firm_name == "ABC Builders Pvt Ltd"
    assert abc.proprietor_name is None      # empty owner
    assert abc.effective_until_bs is None   # end_date null
    assert abc.duration == abc.blacklist_date_bs


def test_parse_bare_list_and_empty():
    assert P.parse_company_list([]) == []
    assert P.parse_company_list({"success": True, "data": None}) == []


def test_to_payload_omits_none_and_isoformats_dates():
    firm = P.parse_company_list(_API_JSON)[1]  # ABC, no until
    payload = P.to_payload(firm)
    assert payload["firm_name"] == "ABC Builders Pvt Ltd"
    assert payload["blacklist_date_bs"] == firm.blacklist_date_bs
    assert isinstance(payload["blacklist_date_ad"], str)
    assert "proprietor_name" not in payload      # None omitted
    assert "effective_until_bs" not in payload


# ── command (fake source + fake ingestion client) ─────────────────────────────


class _FakeSource:
    def __init__(self, payload):
        self._body = json.dumps(payload)

    def get(self, url):
        return 200, self._body


class _FakeIngestion:
    def __init__(self):
        self.batches = []

    def post_firms(self, items):
        self.batches.append(items)
        return {"created": len(items), "updated": 0, "unchanged": 0, "failed": 0, "results": []}


class PpmoCommandClientTests(SimpleTestCase):
    def _run(self, source, ingestion, *extra):
        with patch(f"{CMD}.build_source_client", return_value=source), patch(
            f"{CMD}.build_ingestion_client", return_value=ingestion
        ), patch("time.sleep"):
            call_command(
                "scrape_ppmo_blacklist", "--delay", "0",
                "--api-token", "t", "--api-base", "http://api", *extra,
            )

    def test_write_posts_parsed_firms(self):
        ing = _FakeIngestion()
        self._run(_FakeSource(_API_JSON), ing, "--write")
        posted = [item for batch in ing.batches for item in batch]
        assert {p["firm_name"] for p in posted} == {
            "श्री ग्लोबल ट्रेडिङ्ग प्रा.लि.", "ABC Builders Pvt Ltd",
        }
        g = next(p for p in posted if p["firm_name"].startswith("श्री ग्लोबल"))
        assert g["blacklist_date_bs"] == "2083-04-12"
        assert g["address"] == "का.म.न.पा.-01, काठमाडौं"

    def test_dry_run_posts_nothing(self):
        ing = _FakeIngestion()
        self._run(_FakeSource(_API_JSON), ing)  # no --write
        assert ing.batches == []

    def test_ingestion_batch_failure_is_not_fatal(self):
        class _Raising:
            def post_firms(self, items):
                raise RuntimeError("boom 503")

        # The command must finish (count the batch failed), not raise.
        self._run(_FakeSource(_API_JSON), _Raising(), "--write")
