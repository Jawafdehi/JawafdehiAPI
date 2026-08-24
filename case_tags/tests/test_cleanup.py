"""The cleanup pass over the existing free-text tags.

Every value asserted on here is a real one from the live corpus
(``research/corpus-analysis.md``). That is deliberate: a cleanup tested against invented
inputs proves it handles inputs nobody has, while the whole point is the 144 values we
actually hold.
"""

import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from case_tags.cleanup import classify, plan
from case_tags.models import Tag, TagAlias

pytestmark = pytest.mark.django_db


# ── classification ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    ["081-CR-0098", "081-CR-0111", "080-cr-0190", "081 - CR - 0044"],
)
def test_court_case_numbers_are_deleted(raw):
    action, reason = classify(raw)
    assert action == "delete"
    assert "case number" in reason


@pytest.mark.parametrize(
    "raw",
    ["~1 Crore 25 Lakh", "Rs 3.5 Crore", "50 लाख", "२ करोड", "1 Arab 20 Crore"],
)
def test_bigo_amounts_are_deleted(raw):
    action, reason = classify(raw)
    assert action == "delete"
    assert "amount" in reason


def test_the_two_most_used_tags_in_the_corpus_are_deleted_not_mapped():
    """`CIAA` (53 cases) and `Corruption` (49) are the corpus's top two.

    Both are removed rather than folded into a term: 79 of 82 cases are already
    `case_type: CORRUPTION` and the CIAA is the filer in nearly all of them, so they
    group everything, which discriminates nothing.
    """
    assert classify("CIAA")[0] == "delete"
    assert classify("Corruption")[0] == "delete"
    assert classify("corruption")[0] == "delete"  # casing does not matter


def test_editorial_judgement_tags_are_deleted():
    # Decision D9: a tag states a fact about a case, never our assessment of it.
    for raw in (
        "Unsubstantiated Claim",
        "Stalled Investigation",
        "Corruption Allegation",
        "Hospital related",
        "national issue",
        "Irregular Amount",
        "Political Corruption",
    ):
        assert classify(raw)[0] == "delete", raw


def test_the_seven_way_illicit_enrichment_split_maps_to_one_term():
    for raw in (
        "Illegal Property Acquisition",
        "Assets Beyond Known Income",
        "Illicit Enrichment",
        "Illegal Enrichment",
        "Illegal enrichment",
        "Illegal Property",
        "Illegal Wealth",
    ):
        assert classify(raw) == ("remap", "illicit-enrichment"), raw


def test_procurement_folds_but_bid_rigging_keeps_its_own_term():
    # §8.1 reserves bid-rigging for where collusion is specifically alleged, so folding
    # it into procurement-irregularity would lose a real distinction.
    for raw in ("Procurement Irregularities", "Procurement", "Public Procurement"):
        assert classify(raw) == ("remap", "procurement-irregularity"), raw
    assert classify("Bid Rigging") == ("remap", "bid-rigging")


def test_cross_script_duplicates_map_to_one_term():
    # Nothing derives these. They resolve only because the table says so.
    assert classify("Tax Evasion") == ("remap", "tax-evasion")
    assert classify("tax evasion") == ("remap", "tax-evasion")
    assert classify("कर छली") == ("remap", "tax-evasion")


def test_the_acronym_sector_tag_survives_as_a_sector():
    # `IT` is live (x2). Blind case-folding would make it the English word "it"; the
    # normalizer's allow-list is what keeps it reachable here.
    assert classify("IT") == ("remap", "information-technology")


def test_trailing_punctuation_does_not_prevent_a_match():
    assert classify("Abuse of Power.") == ("remap", "abuse-of-public-office")


def test_unmatched_values_are_kept_not_guessed():
    """The honest bucket. Geography, offices and people belong to other axes.

    Deleting them would lose real information to make a number look better.
    """
    for raw in ("Kathmandu Valley", "Bagmati", "NITC", "sashikanta jha", "TERAMOCS CASE"):
        assert classify(raw)[0] == "keep", raw


def test_banned_beats_remap_when_a_value_could_be_both():
    # Order matters: deleting a banned value is never wrong, but mapping one would
    # reintroduce it under a canonical name.
    action, reason = classify("Special Court")
    assert action == "delete"
    assert "§9" in reason


# ── planning ─────────────────────────────────────────────────────────────────────


def test_plan_deduplicates_and_partitions():
    result = plan(["CIAA", "CIAA", "Procurement", "Kathmandu Valley", "081-CR-0098"])
    assert set(result.delete) == {"CIAA", "081-CR-0098"}
    assert result.remap == {"Procurement": "procurement-irregularity"}
    assert result.keep == ["Kathmandu Valley"]
    assert result.touched == 3


def test_every_remap_target_exists_in_the_seeded_vocabulary():
    """Guards the map against drift.

    A target that does not exist would make the command raise at runtime; catching it
    here means a bad edit to FRAGMENTATION fails in CI instead.
    """
    from case_tags.cleanup import FRAGMENTATION

    known = set(Tag.objects.values_list("id", flat=True))
    missing = sorted(set(FRAGMENTATION.values()) - known)
    assert not missing, f"cleanup.FRAGMENTATION targets non-existent terms: {missing}"


# ── the command ──────────────────────────────────────────────────────────────────


def _case(slug, tags):
    from cases.models import Case

    return Case.objects.create(title=slug, slug=slug, tags=tags, state="PUBLISHED")


def test_dry_run_writes_nothing():
    case = _case("a-case", ["CIAA", "Procurement", "081-CR-0098"])
    call_command("clean_case_tags")
    case.refresh_from_db()
    assert case.tags == ["CIAA", "Procurement", "081-CR-0098"]
    assert TagAlias.objects.count() == 0


def test_apply_requires_a_snapshot_path():
    _case("a-case", ["Procurement"])
    with pytest.raises(CommandError, match="snapshot"):
        call_command("clean_case_tags", "--apply")


def test_apply_deletes_remaps_and_keeps(tmp_path):
    case = _case("a-case", ["CIAA", "Procurement", "Public Procurement", "Kathmandu Valley"])
    snap = tmp_path / "snap.json"
    call_command("clean_case_tags", "--apply", f"--snapshot={snap}")

    case.refresh_from_db()
    # CIAA deleted; the two procurement variants collapse to ONE term, not two;
    # the unmatched geography value survives untouched.
    assert case.tags == ["procurement-irregularity", "Kathmandu Valley"]


def test_apply_writes_a_restorable_snapshot(tmp_path):
    _case("a-case", ["CIAA", "Procurement"])
    snap = tmp_path / "snap.json"
    call_command("clean_case_tags", "--apply", f"--snapshot={snap}")

    data = json.loads(snap.read_text(encoding="utf-8"))
    assert data["cases"]["a-case"] == ["CIAA", "Procurement"]
    assert data["taken_at"]


def test_apply_records_the_mapping_as_aliases(tmp_path):
    """So the resolver reaches the old value too, and the mapping survives the run."""
    _case("a-case", ["Assets Beyond Known Income"])
    call_command("clean_case_tags", "--apply", f"--snapshot={tmp_path / 's.json'}")

    alias = TagAlias.objects.get(value="assets beyond known income")
    assert alias.tag_id == "illicit-enrichment"
    assert alias.approved_by == "clean_case_tags"


def test_apply_is_idempotent(tmp_path):
    case = _case("a-case", ["CIAA", "Procurement"])
    for _ in range(2):
        call_command("clean_case_tags", "--apply", f"--snapshot={tmp_path / 's.json'}")
    case.refresh_from_db()
    assert case.tags == ["procurement-irregularity"]


def test_a_case_whose_every_tag_is_banned_ends_up_empty_not_broken(tmp_path):
    # Reachable: a case tagged only `Corruption` + `CIAA` + its case number. An empty
    # list is correct — offence min_per_case is 0 — and the tagger will fill it.
    case = _case("a-case", ["Corruption", "CIAA", "081-CR-0098"])
    call_command("clean_case_tags", "--apply", f"--snapshot={tmp_path / 's.json'}")
    case.refresh_from_db()
    assert case.tags == []


def test_unpublished_cases_are_untouched(tmp_path):
    from cases.models import Case

    draft = Case.objects.create(title="d", slug="d", tags=["CIAA"], state="DRAFT")
    _case("a-case", ["CIAA"])
    call_command("clean_case_tags", "--apply", f"--snapshot={tmp_path / 's.json'}")
    draft.refresh_from_db()
    assert draft.tags == ["CIAA"]
