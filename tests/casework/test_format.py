"""Tests for the shared prompt-context formatters (casework/common/format.py).

These read the LIVE payload shape, and getting that wrong is a silent failure:
a formatter reading keys that do not exist returns a tidy list of blank bullets,
the prompt looks well-formed, and the model simply never learns any name in the
case. So the entity tests assert against the shape
`cases/services/nes_resolver.py::build_entity_binds` actually produces, and one
test pins that shape by calling the real shaper.
"""
import subprocess
from pathlib import Path

import pytest

from casework.common.format import format_bigo, format_entities, format_list

REPO_ROOT = Path(__file__).resolve().parents[2]
DONOR_COMMIT = "0321a85"

# The live shape: nes_id / display_name / entity_type / type / outcome / notes.
# `type` is the RELATIONSHIP type -- that is what gets bracketed.
LIVE_ENTITY = {
    "nes_id": "person/kamal-raj-gautam",
    "display_name": "कमल राज गौतम",
    "entity_type": "person",
    "type": "accused",
    "outcome": "convicted",
    "notes": "तत्कालीन प्रमुख जिल्ला अधिकारी",
}


class TestFormatBigo:
    def test_thousands_separated(self):
        assert format_bigo(330000000) == "330,000,000"

    def test_a_numeric_string_is_accepted(self):
        assert format_bigo("10403941") == "10,403,941"

    def test_none_and_zero_and_junk_are_all_unknown(self):
        assert format_bigo(None) == "(unknown)"
        assert format_bigo(0) == "(unknown)"
        assert format_bigo("") == "(unknown)"
        assert format_bigo("अज्ञात") == "(unknown)"

    def test_a_negative_bigo_is_unknown_not_negative(self):
        """A negative disputed amount is data corruption, not a fact to print
        into a public-facing prompt."""
        assert format_bigo(-500) == "(unknown)"


class TestFormatList:
    def test_bullets(self):
        assert format_list(["पहिलो", "दोस्रो"]) == "- पहिलो\n- दोस्रो"

    def test_empty_and_none(self):
        assert format_list([]) == "(none provided)"
        assert format_list(None) == "(none provided)"


class TestFormatEntities:
    def test_the_relationship_type_and_display_name_are_rendered(self):
        out = format_entities([LIVE_ENTITY])
        assert out.startswith("- [accused] कमल राज गौतम")

    def test_notes_are_appended_when_present(self):
        assert "तत्कालीन प्रमुख जिल्ला अधिकारी" in format_entities([LIVE_ENTITY])

    def test_blank_notes_add_no_dash(self):
        out = format_entities([dict(LIVE_ENTITY, notes="", outcome="")])
        assert out == "- [accused] कमल राज गौतम"

    def test_the_per_entity_outcome_reaches_the_prompt(self):
        """The structured answer to "who was convicted and who was cleared".

        Both prompts require a per-defendant outcome -- `enrich_description`'s
        section ग and `enrich_card`'s split-verdict teaser rule -- and dropping
        this key forced both models to re-derive the split from prose, which is
        what produced the misreported verdicts.
        """
        out = format_entities([dict(LIVE_ENTITY, notes="", outcome="सफाई")])
        assert out == "- [accused] कमल राज गौतम — फैसला: सफाई"

    def test_a_blank_outcome_adds_no_label(self):
        for blank in ("", "   ", None):
            out = format_entities([dict(LIVE_ENTITY, notes="", outcome=blank)])
            assert out == "- [accused] कमल राज गौतम", f"outcome={blank!r}"

    def test_an_unresolved_entity_still_gets_a_line(self):
        """`build_entity_binds` sets display_name to None when NES cannot
        resolve the id. That must render as a line with a visible relationship
        type, not vanish -- a dropped accused is a dropped fact."""
        out = format_entities([
            {"nes_id": "person/unknown", "display_name": None,
             "entity_type": None, "type": "accused", "notes": ""},
        ])
        assert out == "- [accused] "

    def test_a_non_dict_entry_is_skipped_not_crashed(self):
        out = format_entities(["गलत", None, dict(LIVE_ENTITY, notes="", outcome="")])
        assert out == "- [accused] कमल राज गौतम"

    def test_only_malformed_entries_falls_back_to_none_provided(self):
        assert format_entities(["गलत", 42]) == "(none provided)"

    def test_empty_and_none(self):
        assert format_entities([]) == "(none provided)"
        assert format_entities(None) == "(none provided)"

    def test_the_keys_read_are_the_keys_the_real_shaper_emits(self):
        """The pin that catches a payload-shape drift.

        Calls `build_entity_binds` itself rather than trusting this file's
        fixture, so a rename in `nes_resolver` fails here instead of quietly
        turning every entity line blank.
        """
        from cases.services.nes_resolver import build_entity_binds

        class _Rel:
            nes_id = "person/kamal-raj-gautam"
            relationship_type = "accused"
            outcome = "convicted"
            notes = "तत्कालीन प्रमुख"

        binds = build_entity_binds(
            [_Rel()],
            {"person/kamal-raj-gautam": {"display_name": "कमल राज गौतम",
                                         "entity_type": "person"}},
            include_notes=True,
        )
        assert format_entities(binds) == (
            "- [accused] कमल राज गौतम — फैसला: convicted — तत्कालीन प्रमुख")


def test_the_donors_all_shared_these_three_functions():
    """Why they live in `common/` rather than in one enricher.

    All three donor scripts imported the same three names from the shared
    `casework/common.py`. A per-enricher copy is a fork, and a forked formatter
    means two prompts that disagree about what a missing बिगो looks like.
    """
    for donor in ("enrich_description.py", "enrich_card.py", "enrich_title.py"):
        proc = subprocess.run(
            ["git", "show", f"{DONOR_COMMIT}:casework/{donor}"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            pytest.skip(f"donor commit {DONOR_COMMIT} not in local history")
        assert "format_bigo" in proc.stdout, donor
        assert "format_list" in proc.stdout, donor
