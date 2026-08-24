"""The alias resolver.

The interesting assertions here are the negative ones. Most of what makes this resolver
correct is what it refuses to do: no fuzzy match, no slugification, no guessing at a
plausible target. Each of those has a test, because each is a thing a later "improvement"
would plausibly add.
"""

import pytest

from case_tags.models import Tag, TagAlias, TagStatus
from case_tags.resolve import TagResolver, resolve_tag

pytestmark = pytest.mark.django_db


def _alias(value: str, tag_id: str) -> None:
    TagAlias.objects.create(value=value, tag_id=tag_id)


# ── the happy path ───────────────────────────────────────────────────────────────


def test_a_canonical_id_resolves_to_itself():
    assert resolve_tag("bribery") == "bribery"


def test_a_canonical_id_resolves_regardless_of_casing():
    # normalize_tag lowercases, so the stored slug is reached from any casing.
    assert resolve_tag("Bribery") == "bribery"
    assert resolve_tag("  BRIBERY  ") == "bribery"


def test_an_approved_alias_resolves_to_its_term():
    _alias("assets beyond known income", "illicit-enrichment")
    assert resolve_tag("Assets Beyond Known Income") == "illicit-enrichment"


def test_the_seven_way_illicit_enrichment_split_collapses():
    """corpus-analysis §6: one concept stored seven ways across 31 applications."""
    for raw in (
        "Illegal Property Acquisition",
        "Assets Beyond Known Income",
        "Illicit Enrichment",
        "Illegal Enrichment",
        "Illegal enrichment",
        "Illegal Property",
        "Illegal Wealth",
    ):
        from jawafdehi_shared.tags.normalize import normalize_tag  # noqa: PLC0415

        TagAlias.objects.get_or_create(
            value=normalize_tag(raw), defaults={"tag_id": "illicit-enrichment"}
        )

    resolver = TagResolver()
    resolved = {
        resolver.resolve(r)
        for r in (
            "Illegal Property Acquisition",
            "Assets Beyond Known Income",
            "Illicit Enrichment",
            "Illegal Enrichment",
            "Illegal enrichment",
            "Illegal Property",
            "Illegal Wealth",
        )
    }
    assert resolved == {"illicit-enrichment"}


def test_cross_script_duplicates_resolve_to_one_term():
    # Ncell/एनसेल and Tax Evasion/कर छली. No algorithm relates these — they resolve
    # only because both aliases exist in the table — which is why the cleanup pass
    # seeds them from the measured corpus rather than leaving them to a rule.
    _alias("ncell", "tax-evasion")
    _alias("एनसेल", "tax-evasion")
    resolver = TagResolver()
    assert resolver.resolve("Ncell") == "tax-evasion"
    assert resolver.resolve("ncell") == "tax-evasion"
    assert resolver.resolve("एनसेल") == "tax-evasion"


def test_the_devanagari_encoding_fault_resolves_through_normalization():
    """माछापाेखरी (ा+े) must reach the alias stored under माछापोखरी (ो).

    This is the fault NFC does not repair (policy §7.2, decision D12). If normalization
    ran after the lookup instead of before, this alias would never be found.
    """
    _alias("माछापोखरी", "land-grab")  # stored with U+094B
    assert resolve_tag("माछापाेखरी") == "land-grab"  # queried with U+093E U+0947


# ── the refusals ─────────────────────────────────────────────────────────────────


def test_an_unknown_value_resolves_to_none_not_a_guess():
    assert resolve_tag("Something Nobody Has Ever Tagged") is None


def test_no_fuzzy_fallback_on_a_near_miss():
    # "briberry" is one edit from a real term. A fuzzy resolver would map it; this one
    # must not, because the same leniency turns `Illegal Property` into whatever it is
    # nearest to rather than what a human decided it means.
    assert resolve_tag("briberry") is None
    assert resolve_tag("bribery ") == "bribery"  # trailing space IS mechanical


def test_no_slugification_of_a_display_string():
    # "Tax Evasion" normalizes to "tax evasion" (space), not "tax-evasion" (hyphen), so
    # it does NOT accidentally hit the canonical id. It needs an approved alias.
    # normalize_tag documents this: minting slugs would silently invent canonical ids.
    assert resolve_tag("Tax Evasion") is None
    _alias("tax evasion", "tax-evasion")
    assert resolve_tag("Tax Evasion") == "tax-evasion"


def test_banned_values_resolve_to_none():
    # policy §9. These have no alias and must never acquire one, so they simply do not
    # resolve — and therefore never reach a facet.
    for raw in ("Corruption", "CIAA", "081-CR-0098", "~1 Crore 25 Lakh"):
        assert resolve_tag(raw) is None, raw


def test_blank_and_non_string_input_resolve_to_none():
    assert resolve_tag("") is None
    assert resolve_tag("   ") is None
    assert resolve_tag(".") is None  # punctuation-only normalizes to ""
    assert TagResolver().resolve(None) is None  # type: ignore[arg-type]


# ── merges ───────────────────────────────────────────────────────────────────────


def test_a_merged_term_resolves_to_its_replacement():
    Tag.objects.create(
        id="illegal-wealth",
        axis_id="offence",
        label_en="Illegal Wealth",
        status=TagStatus.MERGED,
        merged_into=Tag.objects.get(id="illicit-enrichment"),
    )
    assert resolve_tag("illegal-wealth") == "illicit-enrichment"


def test_an_alias_pointing_at_a_merged_term_follows_through():
    Tag.objects.create(
        id="illegal-wealth",
        axis_id="offence",
        label_en="Illegal Wealth",
        status=TagStatus.MERGED,
        merged_into=Tag.objects.get(id="illicit-enrichment"),
    )
    _alias("illegal wealth", "illegal-wealth")
    assert resolve_tag("Illegal Wealth") == "illicit-enrichment"


def test_a_merge_cycle_resolves_to_none_rather_than_aborting():
    # Deliberately different from Tag.canonical(), which raises. A bad merge chain is
    # somebody's data error; it must not take down an entire index rebuild over one tag.
    a = Tag.objects.create(id="cycle-a", axis_id="offence", label_en="A")
    b = Tag.objects.create(
        id="cycle-b", axis_id="offence", label_en="B", status=TagStatus.MERGED, merged_into=a
    )
    Tag.objects.filter(id="cycle-a").update(status=TagStatus.MERGED, merged_into=b)
    assert TagResolver().resolve("cycle-a") is None


# ── bulk ─────────────────────────────────────────────────────────────────────────


def test_resolve_all_drops_unresolved_and_deduplicates_preserving_order():
    _alias("procurement irregularities", "procurement-irregularity")
    _alias("procurement", "procurement-irregularity")
    resolver = TagResolver()
    out = resolver.resolve_all(
        ["Procurement Irregularities", "Nonsense", "bribery", "Procurement"]
    )
    # Both procurement variants collapse onto one id, emitted once, in first-seen order.
    assert out == ["procurement-irregularity", "bribery"]


def test_resolver_snapshot_does_not_shift_mid_run():
    """An indexing run must see one consistent vocabulary from start to finish."""
    resolver = TagResolver()
    assert resolver.resolve("Assets Beyond Known Income") is None
    _alias("assets beyond known income", "illicit-enrichment")
    # Same resolver: still None. A new one picks it up.
    assert resolver.resolve("Assets Beyond Known Income") is None
    assert TagResolver().resolve("Assets Beyond Known Income") == "illicit-enrichment"


# ── hardening ─────────────────────────────────────────────────────────


def test_the_snapshot_normalizes_keys_so_a_bulk_written_alias_still_resolves():
    """``bulk_create`` bypasses model ``save``, so a raw-cased row can exist.

    Without read-side normalization it would sit in the table looking correct and never
    resolve, because ``resolve`` looks up the normalized form.
    """
    TagAlias.objects.bulk_create(
        [TagAlias(value="Assets Beyond KNOWN Income.", tag_id="illicit-enrichment")]
    )
    assert TagAlias.objects.get().value == "Assets Beyond KNOWN Income."  # unnormalized
    assert resolve_tag("assets beyond known income") == "illicit-enrichment"
    assert resolve_tag("Assets Beyond Known Income") == "illicit-enrichment"


def test_two_rows_normalizing_to_one_key_but_different_terms_resolve_to_none():
    """Refuse rather than guess — the same rule as the no-fuzzy-fallback one.

    First-wins would depend on row order, so the identical query could answer
    differently on two replicas. None is a result the callers already handle, and it
    surfaces as a gap somebody investigates.
    """
    TagAlias.objects.bulk_create(
        [
            TagAlias(value="Illegal Wealth", tag_id="illicit-enrichment"),
            TagAlias(value="illegal wealth", tag_id="embezzlement"),
        ]
    )
    resolver = TagResolver()
    assert resolver.resolve("Illegal Wealth") is None
    assert "illegal wealth" in resolver.ambiguous_aliases


def test_a_merge_chain_longer_than_the_old_hop_cap_resolves():
    """12 links. A fixed cap of 10 used to drop this as if it were corrupt."""
    previous = Tag.objects.get(id="illicit-enrichment")
    for i in range(12):
        previous = Tag.objects.create(
            id=f"legacy-{i}", axis_id="offence", label_en=f"Legacy {i}",
            status=TagStatus.MERGED, merged_into=previous,
        )
    assert TagResolver().resolve(previous.id) == "illicit-enrichment"
