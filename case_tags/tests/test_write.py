"""The write guards — the only enforcement between the model and the database.

There is no human tick, so these tests are not testing a nice-to-have. Each one covers a
rule the system prompt merely *asks* for, and the question each answers is "what happens
when the model ignores it".

The refusals are the point. A test suite where every case is the happy path would pass
against a function that writes whatever it is handed.
"""

import pytest

from case_tags.models import Tag, TagStatus
from case_tags.write import TAGGER_AXES, apply_tagger_output, tagger_vocabulary

pytestmark = pytest.mark.django_db

DESCRIPTION = (
    "ठूला करदाता कार्यालयको बोलपत्र प्रक्रियामा अनियमितता भएको र "
    "स्रोत नखुलेको सम्पत्ति आर्जन गरेको आरोप छ।"
)
ALLEGATION = "अस्पतालको उपकरण खरिदमा मिलेमतो भएको"


def _case(tags=None):
    from cases.models import Case

    return Case.objects.create(
        title="A case",
        slug="a-case",
        description=DESCRIPTION,
        key_allegations=[ALLEGATION],
        tags=tags or [],
        state="PUBLISHED",
    )


def _span(text=None):
    return text or "बोलपत्र प्रक्रियामा अनियमितता"


# ── the happy path ───────────────────────────────────────────────────────────────


def test_a_grounded_existing_term_is_applied():
    case = _case()
    out = apply_tagger_output(
        case,
        {"offence": [{"id": "procurement-irregularity", "span": _span()}]},
        detected_by="test",
    )
    assert out.applied == {"offence": ["procurement-irregularity"]}
    case.refresh_from_db()
    assert case.tags == ["procurement-irregularity"]


def test_multiple_axes_apply_together():
    case = _case()
    out = apply_tagger_output(
        case,
        {
            "offence": [{"id": "procurement-irregularity", "span": _span()}],
            "sector": [{"id": "health", "span": "अस्पतालको उपकरण खरिदमा"}],
        },
        detected_by="test",
    )
    assert set(out.applied) == {"offence", "sector"}


def test_a_span_may_be_reflowed_but_not_paraphrased():
    case = _case()
    # Whitespace differences are not fabrication; failing them would train us to ignore
    # this check.
    reflowed = "बोलपत्र   प्रक्रियामा\n  अनियमितता"
    out = apply_tagger_output(
        case, {"offence": [{"id": "bid-rigging", "span": reflowed}]}, detected_by="t"
    )
    assert out.applied == {"offence": ["bid-rigging"]}


# ── the refusals ─────────────────────────────────────────────────────────────────


def test_an_ungrounded_span_is_refused():
    """The strongest guard: a confident tag with no basis in the case."""
    case = _case()
    out = apply_tagger_output(
        case,
        {"offence": [{"id": "bribery", "span": "घुस लिएको स्पष्ट प्रमाण छ"}]},
        detected_by="t",
    )
    assert out.applied == {}
    assert out.rejected == [("bribery", "span is not quoted from the case text")]
    case.refresh_from_db()
    assert case.tags == []


def test_a_missing_span_is_refused():
    case = _case()
    out = apply_tagger_output(case, {"offence": [{"id": "bribery"}]}, detected_by="t")
    assert out.applied == {}


def test_a_term_outside_the_vocabulary_is_refused():
    case = _case()
    out = apply_tagger_output(
        case, {"offence": [{"id": "asset-concealment", "span": _span()}]}, detected_by="t"
    )
    assert out.rejected == [("asset-concealment", "not in the vocabulary")]


def test_a_term_on_the_wrong_axis_is_refused():
    case = _case()
    out = apply_tagger_output(
        case, {"sector": [{"id": "bribery", "span": _span()}]}, detected_by="t"
    )
    assert out.applied == {}
    assert "belongs to axis" in out.rejected[0][1]


@pytest.mark.parametrize(
    ("candidate", "fragment"),
    [
        ("081-CR-0098", "case number"),
        ("081 CR 0098", "case number"),
        ("1-crore-25-lakh", "amount"),
        ("corruption", "§9"),
        ("ciaa", "§9"),
        ("Witness Tampering", "slug"),
        ("witness_tampering", "slug"),
        ("साक्षी", "slug"),
    ],
)
def test_junk_ids_are_refused_with_a_reason(candidate, fragment):
    case = _case()
    out = apply_tagger_output(
        case, {"offence": [{"id": candidate, "span": _span()}]}, detected_by="t"
    )
    assert out.applied == {}
    assert fragment in out.rejected[0][1], out.rejected


def test_the_per_axis_cap_comes_from_the_axis_row():
    case = _case()
    # governance_level is 0..1, so the second value is refused rather than truncated
    # silently.
    out = apply_tagger_output(
        case,
        {
            "governance_level": [
                {"id": "local-government", "span": _span()},
                {"id": "federal-government", "span": _span()},
            ]
        },
        detected_by="t",
    )
    assert out.applied == {"governance_level": ["local-government"]}
    assert "at most 1" in out.rejected[0][1]


def test_axes_the_tagger_may_not_write_are_ignored():
    """status/verdict are court-derived; institution/person/geography come from entities."""
    case = _case()
    out = apply_tagger_output(
        case,
        {
            "status": [{"id": "sub-judice", "span": _span()}],
            "verdict": [{"id": "acquitted", "span": _span()}],
            "geography": [{"id": "bagmati", "span": _span()}],
        },
        detected_by="t",
    )
    assert out.applied == {}
    case.refresh_from_db()
    assert case.tags == []


def test_a_duplicate_within_one_axis_is_a_noop_not_an_error():
    case = _case()
    out = apply_tagger_output(
        case,
        {
            "offence": [
                {"id": "bribery", "span": _span()},
                {"id": "bribery", "span": _span()},
            ]
        },
        detected_by="t",
    )
    assert out.applied == {"offence": ["bribery"]}
    assert out.rejected == []


# ── new terms ────────────────────────────────────────────────────────────────────


def test_a_grounded_new_term_is_created_active_and_usable_immediately():
    case = _case()
    out = apply_tagger_output(
        case,
        {
            "new_terms": [
                {
                    "axis": "offence",
                    "id": "asset-concealment",
                    "label_en": "Asset Concealment",
                    "label_ne": "सम्पत्ति लुकाउने",
                    "rationale": "Concealment is not covered by illicit-enrichment.",
                    "span": _span(),
                }
            ],
            "offence": [{"id": "asset-concealment", "span": _span()}],
        },
        detected_by="t",
    )
    assert out.created_terms == ["asset-concealment"]
    tag = Tag.objects.get(id="asset-concealment")
    assert tag.status == TagStatus.ACTIVE
    assert tag.label_ne == "सम्पत्ति लुकाउने"
    # Created and then USED in the same response — new_terms is processed first for
    # exactly this reason.
    assert out.applied == {"offence": ["asset-concealment"]}


def test_a_refused_new_term_also_refuses_the_tag_that_referenced_it():
    case = _case()
    out = apply_tagger_output(
        case,
        {
            "new_terms": [
                {"axis": "offence", "id": "Bad Slug", "label_en": "Bad", "span": _span()}
            ],
            "offence": [{"id": "Bad Slug", "span": _span()}],
        },
        detected_by="t",
    )
    assert out.created_terms == []
    assert out.applied == {}
    assert not Tag.objects.filter(label_en="Bad").exists()


def test_a_new_term_on_a_non_enumerated_axis_is_refused():
    case = _case()
    out = apply_tagger_output(
        case,
        {
            "new_terms": [
                {"axis": "geography", "id": "dhanusha", "label_en": "Dhanusha", "span": _span()}
            ]
        },
        detected_by="t",
    )
    assert not Tag.objects.filter(id="dhanusha").exists()
    assert "may not create" in out.rejected[0][1]


def test_a_new_term_without_a_grounded_span_is_refused():
    case = _case()
    out = apply_tagger_output(
        case,
        {
            "new_terms": [
                {
                    "axis": "offence",
                    "id": "invented",
                    "label_en": "Invented",
                    "span": "nothing like this appears in the case",
                }
            ]
        },
        detected_by="t",
    )
    assert not Tag.objects.filter(id="invented").exists()
    assert out.rejected == [("new_term 'invented'", "span is not quoted from the case text")]


# ── what happens to what was already there ───────────────────────────────────────


def test_unresolved_existing_values_survive_a_retag():
    """Geography, offices and people belong to axes the tagger was never asked to write.

    Deleting them would be the tagger overstepping its mandate.
    """
    case = _case(tags=["Kathmandu Valley", "NITC"])
    apply_tagger_output(
        case, {"offence": [{"id": "bribery", "span": _span()}]}, detected_by="t"
    )
    case.refresh_from_db()
    assert case.tags == ["bribery", "Kathmandu Valley", "NITC"]


def test_a_resolvable_existing_value_is_replaced_not_accumulated():
    case = _case(tags=["bribery"])
    apply_tagger_output(
        case,
        {"offence": [{"id": "procurement-irregularity", "span": _span()}]},
        detected_by="t",
    )
    case.refresh_from_db()
    # The old vocabulary tag is gone: the tagger's answer REPLACES its axes rather than
    # appending, or a re-run would only ever grow the list.
    assert case.tags == ["procurement-irregularity"]


def test_empty_output_leaves_the_case_untouched():
    case = _case(tags=["Kathmandu Valley"])
    out = apply_tagger_output(case, {}, detected_by="t")
    assert out.applied == {}
    case.refresh_from_db()
    assert case.tags == ["Kathmandu Valley"]


# ── the enum handed to the model ─────────────────────────────────────────────────


def test_the_vocabulary_offered_covers_only_the_writable_axes():
    vocab = tagger_vocabulary()
    assert {a["id"] for a in vocab} == set(TAGGER_AXES)
    offence = next(a for a in vocab if a["id"] == "offence")
    assert offence["max_per_case"] == 3
    assert len(offence["terms"]) == 18
    assert {"id", "label_en", "label_ne"} <= set(offence["terms"][0])


def test_a_term_the_tagger_created_is_offered_for_reuse_next_time():
    """The reason the vocabulary lives in a table rather than in the prompt."""
    before = len(next(a for a in tagger_vocabulary() if a["id"] == "offence")["terms"])
    Tag.objects.create(
        id="asset-concealment",
        axis_id="offence",
        label_en="Asset Concealment",
        status=TagStatus.ACTIVE,
    )
    after = next(a for a in tagger_vocabulary() if a["id"] == "offence")["terms"]
    assert len(after) == before + 1
    assert "asset-concealment" in {t["id"] for t in after}


def test_inactive_terms_are_not_offered():
    Tag.objects.create(
        id="retired", axis_id="offence", label_en="Retired", status=TagStatus.DEPRECATED
    )
    offered = {t["id"] for a in tagger_vocabulary() for t in a["terms"]}
    assert "retired" not in offered
