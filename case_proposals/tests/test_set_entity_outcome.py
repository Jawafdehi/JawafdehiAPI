"""The ``set_entity_outcome`` intent: the one enrichment ``raw_patch`` cannot express.

An August 2026 sweep of the published corpus found 71 accused rows still reading
``charged`` on cases the Special Court had already decided — including a case where
all 18 defendants had been acquitted, and one where the stored outcome was
``acquitted`` for a defendant the court convicted. Both directions are factual
errors on a public page about a named person, and neither had a reviewable fix:
``raw_patch`` cannot reach ``entities`` at all.

The negative tests here are the load-bearing ones. This intent writes a field the
DB guards with a CHECK constraint, so "rejected with a 400" and "rejected with a
500 IntegrityError" are very different outcomes, and only one of them is correct.
"""

import pytest
from rest_framework.exceptions import ValidationError

from case_proposals.apply import apply_intent
from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseType,
    RelationshipOutcome,
    RelationshipType,
)

ACCUSED_A = "https://jawafdehi.org/entity/person/sunil-paudel-a1b2c3"
ACCUSED_B = "https://jawafdehi.org/entity/person/nim-bahadur-bali-d4e5f6"
WITNESS = "https://jawafdehi.org/entity/person/a-witness-999999"
STRANGER = "https://jawafdehi.org/entity/person/not-on-this-case-000000"


def make_case(slug="ntc-081-cr-0111", title="NTC billing-system contract"):
    return Case.objects.create(title=title, case_type=CaseType.CORRUPTION, slug=slug)


def bind(case, nes_id, role=RelationshipType.ACCUSED, outcome=None):
    return CaseEntityRelationship.objects.create(
        case=case, nes_id=nes_id, relationship_type=role, outcome=outcome
    )


def outcome_intent(*pairs):
    return {
        "type": "set_entity_outcome",
        "outcomes": [{"nes_id": nes_id, "outcome": outcome} for nes_id, outcome in pairs],
    }


@pytest.mark.django_db
class TestTheAcquittedStopReadingAsCharged:
    def test_a_full_acquittal_moves_every_accused_off_charged(self):
        case = make_case()
        a = bind(case, ACCUSED_A)
        b = bind(case, ACCUSED_B)
        assert a.outcome == RelationshipOutcome.CHARGED  # the model's default

        result = apply_intent(
            case,
            outcome_intent(
                (ACCUSED_A, RelationshipOutcome.ACQUITTED),
                (ACCUSED_B, RelationshipOutcome.ACQUITTED),
            ),
        )

        a.refresh_from_db()
        b.refresh_from_db()
        assert a.outcome == RelationshipOutcome.ACQUITTED
        assert b.outcome == RelationshipOutcome.ACQUITTED
        assert len(result["outcomes_changed"]) == 2

    def test_a_partial_verdict_can_convict_one_and_acquit_another(self):
        """``आंशिक ठहर`` is one fact with two different consequences per person."""
        case = make_case()
        a = bind(case, ACCUSED_A)
        b = bind(case, ACCUSED_B)

        apply_intent(
            case,
            outcome_intent(
                (ACCUSED_A, RelationshipOutcome.CONVICTED),
                (ACCUSED_B, RelationshipOutcome.ACQUITTED),
            ),
        )

        a.refresh_from_db()
        b.refresh_from_db()
        assert a.outcome == RelationshipOutcome.CONVICTED
        assert b.outcome == RelationshipOutcome.ACQUITTED

    def test_it_corrects_the_inverse_error_too(self):
        """Case 90's defect: stored ``acquitted`` for a defendant who was convicted."""
        case = make_case()
        rel = bind(case, ACCUSED_A, outcome=RelationshipOutcome.ACQUITTED)

        apply_intent(case, outcome_intent((ACCUSED_A, RelationshipOutcome.CONVICTED)))

        rel.refresh_from_db()
        assert rel.outcome == RelationshipOutcome.CONVICTED

    def test_reporting_distinguishes_changed_from_already_correct(self):
        case = make_case()
        bind(case, ACCUSED_A, outcome=RelationshipOutcome.ACQUITTED)
        bind(case, ACCUSED_B)

        result = apply_intent(
            case,
            outcome_intent(
                (ACCUSED_A, RelationshipOutcome.ACQUITTED),  # already right
                (ACCUSED_B, RelationshipOutcome.ACQUITTED),
            ),
        )

        assert result["unchanged"] == 1
        assert [c["nes_id"] for c in result["outcomes_changed"]] == [ACCUSED_B]
        assert result["outcomes_changed"][0]["from"] == RelationshipOutcome.CHARGED


@pytest.mark.django_db
class TestItCannotReachWhatItWasNotGiven:
    def test_an_entity_not_bound_to_this_case_is_a_400_not_a_silent_skip(self):
        case = make_case()
        bind(case, ACCUSED_A)

        with pytest.raises(ValidationError) as exc:
            apply_intent(case, outcome_intent((STRANGER, RelationshipOutcome.ACQUITTED)))
        assert "not an entity of case" in str(exc.value)

    def test_a_non_accused_role_is_rejected_with_the_role_named(self):
        """The DB CHECK would raise IntegrityError (a 500); this must be a 400."""
        case = make_case()
        bind(case, WITNESS, role=RelationshipType.WITNESS)

        with pytest.raises(ValidationError) as exc:
            apply_intent(case, outcome_intent((WITNESS, RelationshipOutcome.ACQUITTED)))
        assert "not accused" in str(exc.value)

    def test_an_entity_on_a_DIFFERENT_case_stays_out_of_reach(self):
        """Resolution is scoped to this case's own binds, not the whole table."""
        theirs = make_case(slug="someone-elses-case", title="Another case")
        bind(theirs, ACCUSED_A)
        mine = make_case()
        bind(mine, ACCUSED_B)

        with pytest.raises(ValidationError):
            apply_intent(mine, outcome_intent((ACCUSED_A, RelationshipOutcome.ACQUITTED)))

        rel = CaseEntityRelationship.objects.get(case=theirs, nes_id=ACCUSED_A)
        assert rel.outcome == RelationshipOutcome.CHARGED


@pytest.mark.django_db
class TestTheVocabularyIsNarrowerThanTheColumn:
    def test_charged_is_not_proposable(self):
        """Un-deciding a decided case is a correction for a human, not an enrichment."""
        case = make_case()
        bind(case, ACCUSED_A, outcome=RelationshipOutcome.ACQUITTED)

        with pytest.raises(ValidationError) as exc:
            apply_intent(case, outcome_intent((ACCUSED_A, RelationshipOutcome.CHARGED)))
        assert "not proposable" in str(exc.value)

    def test_an_invented_outcome_is_rejected(self):
        case = make_case()
        bind(case, ACCUSED_A)

        with pytest.raises(ValidationError):
            apply_intent(case, outcome_intent((ACCUSED_A, "exonerated")))

    def test_abated_is_allowed(self):
        case = make_case()
        rel = bind(case, ACCUSED_A)

        apply_intent(case, outcome_intent((ACCUSED_A, RelationshipOutcome.ABATED)))

        rel.refresh_from_db()
        assert rel.outcome == RelationshipOutcome.ABATED


@pytest.mark.django_db
class TestHalfAVerdictIsNeverApplied:
    def test_one_bad_entry_rolls_back_the_whole_batch(self):
        """Otherwise a case ends up asserting some of the acquitted are still charged."""
        case = make_case()
        good = bind(case, ACCUSED_A)

        with pytest.raises(ValidationError):
            apply_intent(
                case,
                outcome_intent(
                    (ACCUSED_A, RelationshipOutcome.ACQUITTED),  # valid
                    (STRANGER, RelationshipOutcome.ACQUITTED),  # not on this case
                ),
            )

        good.refresh_from_db()
        assert good.outcome == RelationshipOutcome.CHARGED, "the valid half must not persist"

    def test_an_empty_outcomes_list_is_rejected(self):
        case = make_case()
        with pytest.raises(ValidationError):
            apply_intent(case, {"type": "set_entity_outcome", "outcomes": []})

    def test_a_malformed_entry_is_rejected(self):
        case = make_case()
        bind(case, ACCUSED_A)
        with pytest.raises(ValidationError):
            apply_intent(case, {"type": "set_entity_outcome", "outcomes": [{"nes_id": ACCUSED_A}]})


@pytest.mark.django_db
class TestTheCaseRowIsTouchedSoCachesInvalidate:
    def test_updated_at_moves_on_a_relationship_only_write(self):
        case = make_case()
        bind(case, ACCUSED_A)
        before = Case.objects.get(pk=case.pk).updated_at

        apply_intent(case, outcome_intent((ACCUSED_A, RelationshipOutcome.ACQUITTED)))

        assert Case.objects.get(pk=case.pk).updated_at > before

    def test_updated_at_is_left_alone_when_nothing_changed(self):
        case = make_case()
        bind(case, ACCUSED_A, outcome=RelationshipOutcome.ACQUITTED)
        before = Case.objects.get(pk=case.pk).updated_at

        result = apply_intent(case, outcome_intent((ACCUSED_A, RelationshipOutcome.ACQUITTED)))

        assert result["outcomes_changed"] == []
        assert Case.objects.get(pk=case.pk).updated_at == before
