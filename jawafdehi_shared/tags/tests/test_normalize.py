"""Tag normalizer tests — mechanical hygiene only, asserted at codepoint level.

Every corpus value in :data:`CORPUS_SAMPLE` was verified from a real source, not
invented: the live production tags facet (``search_control_plane``, retrieved
2026-08-23), two live case ``card.tags`` payloads, or a documented value in
``management/policies/case-tagging/research/corpus-analysis.md`` §5-§7. Provenance
is marked per block so a reader can re-derive any of them.
"""

from __future__ import annotations

import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from jawafdehi_shared.tags.normalize import ACRONYMS, normalize_tag

# The fault pair and its repair, named so no test has to index into a string to
# talk about them. ाे is U+093E U+0947; ो is U+094B.
FAULT_PAIR_O = "ाे"
CORRECT_O = "ो"
FAULT_PAIR_AU = "ाै"
CORRECT_AU = "ौ"

# The two encoding-fault tags, spelled with the FAULT (ा + े) on purpose. Written
# as explicit escapes so the fault survives an editor, a copy-paste or a
# well-meaning "fix" — the whole defect is that it renders identically to the
# correct form.
FAULT_MACHHAPOKHARI = "माछापाेखरी"
GOOD_MACHHAPOKHARI = "माछापोखरी"
FAULT_PUBLIC_LOSS = (
    "सार्वजनिक "
    "सम्पत्ति "
    "हानी "
    "नाेकसानी"
)

# ── The verified corpus sample ─────────────────────────────────────────────────
#
# NOT all 144 distinct live tags: the production facet truncates at 50 (the very
# defect T1/T2 fix, still undeployed), so the ~94-value singleton tail is not
# reachable through a read API today. These are the values that WERE verifiable.

# 50 values from the live production tags facet, ?type=case, 2026-08-23.
_FROM_LIVE_FACET = (
    "CORRUPTION", "CIAA", "Corruption", "Public Office Abuse", "Local Government",
    "Illegal Property Acquisition", "Procurement Irregularities", "Land Management",
    "Embezzlement", "Forged Documents", "Procurement", "Kathmandu Valley",
    "Assets Beyond Known Income", "Bagmati", "Public Procurement", "Revenue Leakage",
    "Lalitpur", "Madhesh", "Finance", "Forestry", "Revenue", "Special Court",
    "Construction", "Lumbini", "nphl", "Bid Rigging", "Infrastructure", "Karnali",
    "NITC", "Public Works", "TAX_EVASION", "Water Supply", "कर छली", "Civil Servant",
    "Education", "Gandaki", "Health", "IT", "Illegal Enrichment", "Illicit Enrichment",
    "Kathmandu", "Land", "Money Laundering", "Nagarjun Municipality", "Ncell",
    "Political Corruption", "Supreme Court", "Tax Evasion", "एनसेल", "081-CR-0098",
)

# From two live case cards (budhigandaki-hydropower-projec-77f597,
# giribandhu-tea-estate-land-swap), same session.
_FROM_LIVE_CARDS = (
    "Hydropower", "Corruption Allegation", "Unsubstantiated Claim",
    "Budhigandaki Hydropower Project", "Policy Corruption", "Land Grab",
    "Abuse of Authority", "Jhapa", "K.P. Sharma Oli", "Giribandhu",
)

# Values named in corpus-analysis.md §5-§7 and TASKS.md's own defect inventory.
# This is where the hygiene defects live — the tail the facet cannot reach.
_FROM_CORPUS_ANALYSIS = (
    "ncell", "tax evasion", "Illegal enrichment",          # §7 casing collisions
    "Abuse of Power.", "Conflict of Interest.",            # §7 trailing punctuation
    "Hulak Saving  Bank Case",                             # §7 double space
    "land-deal",                                           # §7 kebab-case
    FAULT_MACHHAPOKHARI, FAULT_PUBLIC_LOSS,                # §7 encoding fault
    "Land Scandel", "Danusha",                             # §7 typos
    "Hospital related", "national issue", "Irregular Amount",  # §7 vague
    "TERAMOCS CASE", "Pashupati Jalahari",                 # §3 nicknames
    "sashikanta jha", "RSP", "महालेखा परीक्षक",              # §3 person/institution
    "Madesh Province", "Sudurpashchim", "Sudurpashchim Province",
    "Province 1", "Koshi", "गण्डकी प्रदेश",                  # §6 province variants
    "Transport", "Transportation", "Civil Servants",
    "Money-laundering",  # §6 also-variants ("Money Laundering" is in the facet block)
    "Illegal Property", "Illegal Wealth", "Procurement Splitting",
    "Stalled Investigation", "Bagmati Mayor Involvement", "High Specification",
    "081-CR-0111", "~1 Crore 25 Lakh",                     # §3 banned forms
)

CORPUS_SAMPLE = _FROM_LIVE_FACET + _FROM_LIVE_CARDS + _FROM_CORPUS_ANALYSIS


def _codepoints(value: str) -> list[str]:
    """U+XXXX list — the only honest way to assert on these strings, since the
    fault and the repair render almost identically."""
    return [f"U+{ord(ch):04X}" for ch in value]


# ── the encoding fault (policy §7.2, decision D12) ─────────────────────────────


def test_devanagari_o_fault_is_repaired_at_codepoint_level():
    """``ा + े`` (U+093E U+0947) → ``ो`` (U+094B). Asserted on codepoints, never by
    eye: the two forms are visually near-identical, which is exactly why this
    survived editorial review in the live corpus."""
    out = normalize_tag(FAULT_MACHHAPOKHARI)
    assert _codepoints(out) == _codepoints(GOOD_MACHHAPOKHARI)
    assert FAULT_PAIR_O not in out  # the ा+े pair is gone entirely
    assert CORRECT_O in out  # ...replaced by the single ो


def test_nfc_alone_does_not_repair_the_fault():
    """Documents WHY the explicit substitution exists, so nobody "simplifies" it
    into a normalize() call later.

    ``ा + े`` is not a canonical decomposition of ``ो`` — U+094B has no
    decomposition at all — so NFC is a no-op here and reaching for it merely looks
    like a fix (policy §7.2, decision D12)."""
    assert unicodedata.decomposition(CORRECT_O) == ""
    assert unicodedata.normalize("NFC", FAULT_MACHHAPOKHARI) != GOOD_MACHHAPOKHARI
    assert unicodedata.normalize("NFKC", FAULT_MACHHAPOKHARI) != GOOD_MACHHAPOKHARI
    # ...and the normalizer, which does the substitution explicitly, does fix it.
    assert normalize_tag(FAULT_MACHHAPOKHARI) == GOOD_MACHHAPOKHARI


def test_devanagari_au_fault_is_repaired():
    """The ौ half of policy §7.2's rule: ``ा + ै`` (U+093E U+0948) → ``ौ`` (U+094C)."""
    assert normalize_tag("क" + FAULT_PAIR_AU) == "क" + CORRECT_AU


def test_second_fault_tag_is_repaired_without_touching_its_spelling():
    """``नाेकसानी`` → ``नोकसानी``: the ENCODING fault only.

    policy §7.3 says the correct spellings are ``नोक्सानी`` and ``हानि``, but those
    are editorial spelling corrections, not mechanical ones — they belong to the
    alias table, not here. This module must not quietly do vocabulary work."""
    out = normalize_tag(FAULT_PUBLIC_LOSS)
    assert FAULT_PAIR_O not in out
    assert "न" + CORRECT_O + "कसानी" in out  # नोकसानी — encoding repaired
    assert "हानी" in out  # §7.3 would spell this हानि; not this module's job


# ── casing collisions (§7) ─────────────────────────────────────────────────────


def test_casing_collisions_collapse_to_one_value():
    """The defect that makes a filter find half the cases."""
    assert normalize_tag("Ncell") == normalize_tag("ncell")
    assert normalize_tag("Tax Evasion") == normalize_tag("tax evasion")
    assert normalize_tag("Illegal Enrichment") == normalize_tag("Illegal enrichment")


def test_acronyms_survive_case_folding():
    """Blind case-folding turns the sector tag ``IT`` into the English word "it"."""
    assert normalize_tag("IT") == "IT"
    assert normalize_tag("NITC") == "NITC"
    assert normalize_tag("CIAA") == "CIAA"


def test_acronym_casing_variants_collapse_onto_the_allow_list_spelling():
    """The live corpus carries ``nphl`` lowercase (×4) while the allow-list spells
    it ``NPHL``. Matching case-insensitively and emitting the allow-list spelling is
    what collapses that pair — the same defect as Ncell/ncell."""
    assert normalize_tag("nphl") == "NPHL"
    assert normalize_tag("NPHL") == "NPHL"
    assert normalize_tag("Nphl") == "NPHL"


def test_acronym_list_is_exactly_the_five_the_spec_names():
    """The allow-list is human-supplied vocabulary. Extending it is an editorial
    act (you cannot infer that ``nphl`` is the National Public Health Laboratory),
    so it stays pinned to the five TASKS.md names until a human adds more."""
    assert ACRONYMS == frozenset({"IT", "NITC", "CIAA", "RSP", "NPHL"})


# ── whitespace and punctuation (§7.1 rules 3-4) ────────────────────────────────


def test_trailing_punctuation_is_stripped():
    """policy §7.1 rule 3. Case-folding still applies — see the module docstring on
    the one place TASKS.md's illustration and its own step list disagree."""
    assert normalize_tag("Abuse of Power.") == "abuse of power"
    assert normalize_tag("Conflict of Interest.") == "conflict of interest"
    assert normalize_tag("ठगी ।") == "ठगी"


def test_internal_whitespace_collapses_and_edges_are_trimmed():
    """policy §7.1 rule 4 — the ``Hulak Saving  Bank Case`` double space."""
    assert normalize_tag("Hulak Saving  Bank Case") == "hulak saving bank case"
    assert normalize_tag("  Forestry  ") == "forestry"
    assert normalize_tag("Water\tSupply") == "water supply"


def test_internal_punctuation_is_preserved():
    """Only TRAILING punctuation is a defect. ``K.P. Sharma Oli`` keeps its stops."""
    assert normalize_tag("K.P. Sharma Oli") == "k.p. sharma oli"


def test_blank_input_normalizes_to_empty():
    for blank in ("", "   ", "\t\n", "."):
        assert normalize_tag(blank) == "", repr(blank)


# ── digits (step 5) ────────────────────────────────────────────────────────────


def test_devanagari_digits_fold_to_ascii():
    """One form, and ASCII is the only direction consistent with policy §7.1 rule
    1 (ASCII slugs). Neither NFC nor NFKC does this — verified in the same test."""
    assert normalize_tag("24९") == "249"
    assert normalize_tag("२०७८") == "2078"
    assert unicodedata.normalize("NFKC", "24९") != "249"


# ── the properties that make this safe to put in front of an alias table ───────


def test_normalizer_is_idempotent_over_every_corpus_value():
    """T6 will feed this into an alias lookup; a normalizer that keeps moving would
    make that lookup order-dependent."""
    for value in CORPUS_SAMPLE:
        once = normalize_tag(value)
        assert normalize_tag(once) == once, _codepoints(value)


def test_no_corpus_value_normalizes_to_empty():
    """A tag that normalizes away would silently vanish from a case on write."""
    for value in CORPUS_SAMPLE:
        assert normalize_tag(value) != "", _codepoints(value)


def test_normalizer_never_introduces_the_encoding_fault():
    for value in CORPUS_SAMPLE:
        assert FAULT_PAIR_O not in normalize_tag(value), _codepoints(value)
        assert FAULT_PAIR_AU not in normalize_tag(value), _codepoints(value)


def test_corpus_sample_is_a_documented_subset_not_the_full_144():
    """Guards the honesty of the sample: it is what was verifiable, and the count
    is asserted so nobody later reads it as "all 144 live tags"."""
    assert len(set(CORPUS_SAMPLE)) == len(CORPUS_SAMPLE), "duplicate corpus value"
    assert len(CORPUS_SAMPLE) < 144


# ── the module contract: pure, no Django ───────────────────────────────────────


def test_module_imports_without_django():
    """T4 requires a standalone pure function. Proven by importing it in a fresh
    interpreter with no DJANGO_SETTINGS_MODULE and no django.setup() — an
    in-process assertion could not tell, since pytest has already configured
    Django by the time this runs."""
    # Repo root derived from this file, not the cwd: PYTHONPATH="." would depend
    # on where pytest was invoked from and fail from any subdirectory.
    repo_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import jawafdehi_shared.tags.normalize as m;"
            "print(m.normalize_tag('  Tax Evasion. '))",
        ],
        capture_output=True,
        text=True,
        # A deliberately bare env: no DJANGO_SETTINGS_MODULE, so an accidental
        # Django import in the module under test would fail here rather than
        # passing on the ambient settings pytest has already configured.
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(repo_root)},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "tax evasion"
    assert "django" not in result.stderr.lower()


@pytest.mark.parametrize("bad", [None, 42, ["Forestry"]])
def test_non_string_input_raises_rather_than_guessing(bad):
    """No fuzzy coercion: T13 is the layer that rejects bad input with a 400, and it
    cannot do that if this silently stringifies whatever it is handed."""
    with pytest.raises((AttributeError, TypeError)):
        normalize_tag(bad)
