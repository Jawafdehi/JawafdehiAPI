"""T22/T23 — the vocabulary models and the seed.

The seed assertions are deliberately specific (56 terms, 18 offence, offence min 0, no
nickname axis) rather than "some rows exist": the whole value of a transcribed vocabulary
is that it matches its source, and a test that only checks non-emptiness would pass just
as happily against a half-loaded table.
"""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from case_tags.models import (
    AxisMembers,
    ProposalKind,
    ProposalStatus,
    Tag,
    TagAlias,
    TagAxis,
    TagProposal,
    TagStatus,
)

pytestmark = pytest.mark.django_db


# ── T23: the seed ────────────────────────────────────────────────────────────────


def test_seed_loads_every_axis_and_term():
    assert TagAxis.objects.count() == 9
    assert Tag.objects.count() == 56


def test_seed_term_counts_per_axis():
    # From vocabulary.yml, which was verified term-by-term against policy.md §4.1,
    # §5.1, §5.3, §6.1, §6.2, §6.3, §8.1, §8.2, §8.3 with 0 mismatches.
    expected = {
        "status": 9,  # §4.1 ladder (4) + §6.2 pre-prosecution (3) + §6.3 arbitration (2)
        "verdict": 6,  # §5.1 core (3) + §5.3 non-prosecution (3)
        "offence": 18,
        "sector": 14,
        "governance_level": 4,
        "nature": 5,
    }
    for axis_id, count in expected.items():
        assert Tag.objects.filter(axis_id=axis_id).count() == count, axis_id


def test_seed_carries_the_nepali_labels_verbatim():
    # Spot-checks on the three §7.4 terms whose Nepali wording was itself an editorial
    # decision measured against our published case texts — the ones most likely to be
    # silently "corrected" by a well-meaning later edit.
    assert Tag.objects.get(id="illicit-enrichment").label_ne == "स्रोत नखुलेको सम्पत्ति आर्जन"
    assert Tag.objects.get(id="bribery").label_ne == "घुस रिसवत"
    assert Tag.objects.get(id="bid-rigging").label_ne == "बोलपत्रमा मिलेमतो"
    assert Tag.objects.get(id="abuse-of-public-office").label_ne == "पदको दुरुपयोग"


def test_first_instance_decided_has_no_single_nepali_label():
    # policy §4.2 composes it from the deciding court. A non-null label_ne here would
    # mean somebody invented one.
    tag = Tag.objects.get(id="first-instance-decided")
    assert tag.label_ne is None
    assert tag.label_ne_composed == {
        "special": "विशेष अदालतको फैसला",
        "district": "जिल्ला अदालतको फैसला",
        "high": "उच्च अदालतको फैसला",
    }


def test_offence_minimum_is_zero_not_one():
    # The 2026-08-23 divergence from policy §3. §3 required >=1 while §8.1 may not cover
    # every case, which made a fitting-nothing case unsaveable.
    offence = TagAxis.objects.get(id="offence")
    assert offence.min_per_case == 0
    assert offence.max_per_case == 3


def test_no_nickname_axis():
    # The other 2026-08-23 divergence. policy §8.7 defines one; it moves to
    # `case_aliases` instead, so tags are 100% controlled vocabulary.
    assert not TagAxis.objects.filter(id="nickname").exists()


def test_seed_creates_no_aliases():
    # The T3a/T23 invariant: policy.md contains no alias lists, so seeding any would be
    # authorship rather than transcription. They arrive via the review queue.
    assert TagAlias.objects.count() == 0


def test_axes_without_enumerated_members_legitimately_have_no_terms():
    for axis_id in ("institution", "geography", "person"):
        axis = TagAxis.objects.get(id=axis_id)
        assert axis.members != AxisMembers.ENUMERATED
        assert axis.tags.count() == 0


def test_status_and_verdict_are_the_highlighted_axes():
    highlighted = set(TagAxis.objects.filter(highlighted=True).values_list("id", flat=True))
    assert highlighted == {"status", "verdict"}


def test_every_seeded_term_is_active_and_unmerged():
    assert not Tag.objects.exclude(status=TagStatus.ACTIVE).exists()
    assert not Tag.objects.filter(merged_into__isnull=False).exists()


def test_seed_is_idempotent():
    """Re-running must be a no-op, not a duplicate-key crash.

    This is not hypothetical: the seed re-runs on every rebuilt test database, and will
    re-run in production if the migration is ever replayed after a rollback.

    Calls the migration's own ``seed`` rather than a re-implementation, so the thing
    under test is the thing that ships. ``importlib`` because the module name starts
    with a digit and cannot be a normal import.
    """
    import importlib  # noqa: PLC0415

    from django.apps import apps as global_apps  # noqa: PLC0415

    migration = importlib.import_module("case_tags.migrations.0002_seed_vocabulary")

    before = (TagAxis.objects.count(), Tag.objects.count())
    migration.seed(global_apps, None)
    assert (TagAxis.objects.count(), Tag.objects.count()) == before


# ── T22: model behaviour ─────────────────────────────────────────────────────────


def test_canonical_returns_self_for_an_ordinary_tag():
    tag = Tag.objects.get(id="bribery")
    assert tag.canonical().id == "bribery"


def test_canonical_follows_a_merge_chain():
    # A retired slug must stay resolvable: stored Case.tags values and ?tags= URLs
    # minted before the merge have to keep working (design.md §12 lifecycle).
    target = Tag.objects.get(id="illicit-enrichment")
    mid = Tag.objects.create(
        id="illegal-wealth", axis_id="offence", label_en="Illegal Wealth",
        status=TagStatus.MERGED, merged_into=target,
    )
    outer = Tag.objects.create(
        id="illegal-property", axis_id="offence", label_en="Illegal Property",
        status=TagStatus.MERGED, merged_into=mid,
    )
    assert outer.canonical().id == "illicit-enrichment"


def test_canonical_raises_on_a_merge_cycle_rather_than_hanging():
    # The merge chain is human-editable through the admin, so a cycle is an ordinary
    # mistake. A raise is a far better symptom than a spinning request.
    a = Tag.objects.create(id="cycle-a", axis_id="offence", label_en="A")
    b = Tag.objects.create(
        id="cycle-b", axis_id="offence", label_en="B", status=TagStatus.MERGED, merged_into=a
    )
    Tag.objects.filter(id="cycle-a").update(status=TagStatus.MERGED, merged_into=b)
    with pytest.raises(ValidationError, match="cycle"):
        Tag.objects.get(id="cycle-a").canonical()


def test_merged_requires_a_target_and_non_merged_forbids_one():
    tag = Tag(id="dangling", axis_id="offence", label_en="X", status=TagStatus.MERGED)
    with pytest.raises(ValidationError, match="merged_into"):
        tag.full_clean()

    other = Tag.objects.get(id="bribery")
    tag2 = Tag(
        id="pointing", axis_id="offence", label_en="Y",
        status=TagStatus.ACTIVE, merged_into=other,
    )
    with pytest.raises(ValidationError, match="merged_into"):
        tag2.full_clean()


def test_database_rejects_merged_without_target_even_bypassing_clean():
    # full_clean() is not in the path for admin bulk edits, shell writes or raw ORM
    # updates, so the invariant is enforced in the DB too.
    with pytest.raises(IntegrityError), transaction.atomic():
        Tag.objects.create(
            id="raw-dangling", axis_id="offence", label_en="Z", status=TagStatus.MERGED
        )


def test_alias_value_is_unique_across_the_table():
    # One raw string cannot resolve to two different terms — that ambiguity is exactly
    # what the corpus already suffers from.
    TagAlias.objects.create(value="assets beyond known income", tag_id="illicit-enrichment")
    with pytest.raises(IntegrityError), transaction.atomic():
        TagAlias.objects.create(value="assets beyond known income", tag_id="embezzlement")


def test_label_falls_back_rather_than_returning_blank():
    composed = Tag.objects.get(id="first-instance-decided")
    # No Nepali label exists, so `ne` must fall back to English, never to "".
    assert composed.label("ne") == "Decided at first instance"
    assert Tag.objects.get(id="bribery").label("ne") == "घुस रिसवत"
    assert Tag.objects.get(id="bribery").label("en") == "Bribery"


def test_axis_max_cannot_be_below_min():
    with pytest.raises(IntegrityError), transaction.atomic():
        TagAxis.objects.create(
            id="broken", label_ne="x", label_en="X", min_per_case=3, max_per_case=1
        )


# ── T22: the proposal queue ──────────────────────────────────────────────────────


def _proposal(**kw):
    defaults = {
        "kind": ProposalKind.ALIAS_EQUIVALENCE,
        "payload": {"raw_value": "ncell", "proposed_tag_id": "tax-evasion"},
        "confidence": 0.9,
        "detected_by": "consumer:test",
        "dedup_key": "alias:ncell",
    }
    return TagProposal.objects.create(**{**defaults, **kw})


def test_dedup_key_is_unique_so_a_rejection_stays_sticky():
    """Without this the proposer refills the queue every run with refused rows.

    That is how a review queue becomes something people stop opening, which closes the
    escape hatch in practice and pushes everyone back to free text.
    """
    _proposal()
    with pytest.raises(IntegrityError), transaction.atomic():
        _proposal(confidence=0.1)


def test_proposal_defaults_to_pending_and_is_not_decided():
    p = _proposal()
    assert p.status == ProposalStatus.PENDING
    assert p.is_decided is False


def test_confidence_is_bounded_in_the_database():
    # The queue is ORDERED by confidence, and the admin and shell both bypass the
    # serializer, so the range is enforced where nothing can route around it.
    with pytest.raises(IntegrityError), transaction.atomic():
        _proposal(dedup_key="alias:bad", confidence=1.4)


def test_proposals_sort_most_confident_first():
    _proposal(dedup_key="a", confidence=0.4)
    _proposal(dedup_key="b", confidence=0.95)
    _proposal(dedup_key="c", confidence=0.7)
    assert [p.dedup_key for p in TagProposal.objects.all()] == ["b", "c", "a"]


# ── PR #463 review fixes ─────────────────────────────────────────────────────────


def test_alias_value_is_normalized_on_save_whatever_the_writer():
    """``apply`` is not the only writer — admin, shell and migrations reach the model.

    A raw-cased row written that way satisfies the unique constraint and then never
    resolves, because the resolver looks up the normalized form. It would sit in the
    table looking correct and doing nothing.
    """
    alias = TagAlias.objects.create(value="  Assets Beyond KNOWN Income.  ", tag_id="illicit-enrichment")
    alias.refresh_from_db()
    assert alias.value == "assets beyond known income"


def test_normalization_on_save_makes_casing_variants_collide():
    """Two rows differing only in casing are now one alias, not two."""
    TagAlias.objects.create(value="Ncell", tag_id="tax-evasion")
    with pytest.raises(IntegrityError), transaction.atomic():
        TagAlias.objects.create(value="ncell", tag_id="tax-evasion")


def test_canonical_resolves_a_chain_longer_than_the_old_hop_cap():
    """A 12-link merge chain must resolve, not be dropped as if corrupt.

    A fixed depth cap of 10 used to sit here. Cycle detection already guarantees
    termination, so the cap's only distinct effect was to reject legitimate chains.
    """
    target = Tag.objects.get(id="illicit-enrichment")
    previous = target
    for i in range(12):
        previous = Tag.objects.create(
            id=f"legacy-{i}", axis_id="offence", label_en=f"Legacy {i}",
            status=TagStatus.MERGED, merged_into=previous,
        )
    assert previous.canonical().id == "illicit-enrichment"


def test_unseed_survives_an_operator_added_term_under_a_seeded_axis():
    """``Tag.axis`` is PROTECT, so a term the review queue created would block rollback.

    Reverse migrations are exactly what gets run under pressure; one that raises
    partway through is worse than useless.
    """
    import importlib

    from django.apps import apps as global_apps

    migration = importlib.import_module("case_tags.migrations.0002_seed_vocabulary")
    Tag.objects.create(id="operator-added", axis_id="offence", label_en="Operator Added")

    migration.unseed(global_apps, None)  # must not raise

    # The seeded terms are gone; the operator's term and its axis survive.
    assert not Tag.objects.filter(id="bribery").exists()
    assert Tag.objects.filter(id="operator-added").exists()
    assert TagAxis.objects.filter(id="offence").exists()
