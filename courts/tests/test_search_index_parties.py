"""DB-backed shaping of ``raw.parties`` for the court-case search index.

``search/tests/test_indexers.py`` covers the no-DB path, where the shaping falls
back to the case's own ``plaintiff``/``defendant`` strings. Everything here
needs real ``CaseEntity`` rows: the side split, the side values the scrapers
actually write, deduplication, and the name cap with an uncapped total.

    DJANGO_SETTINGS_MODULE=config.settings_test TESTING=true \\
        uv run pytest -q courts/tests/test_search_index_parties.py
"""

from __future__ import annotations

from django.test import TestCase

from courts.models import CaseEntity, Court, CourtCase
from courts.search_index import PARTY_NAME_CAP, build_doc


class PartiesBySideTests(TestCase):
    # The DB router pins the NGM ``courts`` models to the ``ngm`` alias, and the
    # test runner only sets up ``default`` unless a test declares otherwise —
    # same reason ``courts/tests/test_api.py`` enrolls every alias.
    databases = "__all__"

    @classmethod
    def setUpTestData(cls):
        cls.court = Court.objects.create(
            identifier="kathmandudc",
            court_type="district",
            full_name_nepali="जिल्ला अदालत काठमाडौं",
            full_name_english="District Court Kathmandu",
        )

    def _case(self, case_number: str, **kwargs) -> CourtCase:
        return CourtCase.objects.create(
            case_number=case_number,
            court=self.court,
            case_type="भ्रष्टाचार",
            case_status="चालु",
            **kwargs,
        )

    def _entity(self, case: CourtCase, side: str, name: str) -> None:
        CaseEntity.objects.create(
            case_number=case.case_number, court=self.court, side=side, name=name
        )

    def test_splits_parties_by_side(self):
        case = self._case("082-C1-0001", plaintiff="ignored", defendant="ignored")
        self._entity(case, "plaintiff", "नेपाल सरकार")
        self._entity(case, "defendant", "राम बहादुर")
        self._entity(case, "defendant", "सीता देवी")

        parties = build_doc(case)["raw"]["parties"]

        assert parties["plaintiff"] == {"names": ["नेपाल सरकार"], "total": 1}
        assert parties["defendant"]["total"] == 2
        assert parties["defendant"]["names"] == ["राम बहादुर", "सीता देवी"]

    def test_entity_rows_win_over_the_free_text_party(self):
        """The case-level string is only a fallback. Where rows exist they are
        the resolved, per-person truth, and the string is often a whole clause
        ("X को जाहेरीले नेपाल सरकार") rather than one party."""
        case = self._case(
            "082-C1-0002",
            plaintiff="नारद अवस्थीको जाहेरीले नेपाल सरकार",
            defendant="कुन्ता भाट",
        )
        self._entity(case, "plaintiff", "नारद अवस्थी")

        parties = build_doc(case)["raw"]["parties"]

        assert parties["plaintiff"] == {"names": ["नारद अवस्थी"], "total": 1}
        # Defendant has no rows, so that side alone falls back to the string.
        assert parties["defendant"] == {"names": ["कुन्ता भाट"], "total": 1}

    def test_accepts_the_devanagari_side_values_the_scrapers_write(self):
        """``CaseEntity.side`` is a free CharField, not a choice field, and the
        scrapers write Devanagari as well as English. Dropping those would
        silently empty both sides on a chunk of the corpus."""
        case = self._case("082-C1-0003")
        self._entity(case, "वादी", "नेपाल सरकार")
        self._entity(case, "प्रतिवादी", "राम बहादुर")

        parties = build_doc(case)["raw"]["parties"]

        assert parties["plaintiff"]["names"] == ["नेपाल सरकार"]
        assert parties["defendant"]["names"] == ["राम बहादुर"]

    def test_side_is_matched_case_insensitively_and_untrimmed(self):
        case = self._case("082-C1-0004")
        self._entity(case, " Plaintiff ", "नेपाल सरकार")
        self._entity(case, "DEFENDANT", "राम बहादुर")

        parties = build_doc(case)["raw"]["parties"]

        assert parties["plaintiff"]["names"] == ["नेपाल सरकार"]
        assert parties["defendant"]["names"] == ["राम बहादुर"]

    def test_drops_an_unrecognised_side_rather_than_guessing(self):
        """A row whose side is neither party ("witness", a typo, empty) must not
        be filed under one of them — a wrong attribution on a court record is
        worse than a missing one. It also must not fall back to the free-text
        string, which would misreport a party that has rows."""
        case = self._case("082-C1-0005", plaintiff="नेपाल सरकार")
        self._entity(case, "plaintiff", "नेपाल सरकार")
        self._entity(case, "witness", "साक्षी एक")
        self._entity(case, "", "अज्ञात")

        parties = build_doc(case)["raw"]["parties"]

        assert parties["plaintiff"] == {"names": ["नेपाल सरकार"], "total": 1}
        assert parties["defendant"] == {"names": [], "total": 0}

    def test_deduplicates_repeated_names_on_one_side(self):
        case = self._case("082-C1-0006")
        self._entity(case, "defendant", "राम बहादुर")
        self._entity(case, "defendant", "राम बहादुर")
        self._entity(case, "defendant", "सीता देवी")

        parties = build_doc(case)["raw"]["parties"]

        assert parties["defendant"]["names"] == ["राम बहादुर", "सीता देवी"]
        assert parties["defendant"]["total"] == 2

    def test_caps_the_names_but_not_the_total(self):
        """The cap keeps the doc small; the total is what makes "+N others"
        correct, so it must count every party, not just the ones shipped."""
        case = self._case("082-C1-0007")
        for i in range(PARTY_NAME_CAP + 4):
            self._entity(case, "defendant", f"प्रतिवादी {i}")

        parties = build_doc(case)["raw"]["parties"]

        assert len(parties["defendant"]["names"]) == PARTY_NAME_CAP
        assert parties["defendant"]["total"] == PARTY_NAME_CAP + 4
        # The kept names are the first ones, not an arbitrary slice.
        assert parties["defendant"]["names"][0] == "प्रतिवादी 0"

    def test_parties_do_not_disturb_the_flattened_recall_bag(self):
        """``body``/``keywords`` still carry every party name regardless of
        side, so a party-name query keeps matching."""
        case = self._case("082-C1-0008", plaintiff="नेपाल सरकार")
        self._entity(case, "defendant", "राम बहादुर")

        doc = build_doc(case)

        assert "राम बहादुर" in doc["keywords"]
        assert "नेपाल सरकार" in doc["body"]
