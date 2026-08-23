"""T15 — the vocabulary endpoint. T25 — the review queue API.

The single most important test in here is
``test_the_machine_role_cannot_approve_its_own_proposal``. Every other guard in this app
is defence in depth behind it: if automation can approve, then an LLM can put a term in
front of the public on its own authority, and the human-in-the-loop design is decoration.
"""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework.test import APIClient

from case_tags.models import ProposalKind, Tag, TagAlias, TagAxis, TagProposal, TagStatus

pytestmark = pytest.mark.django_db

VOCAB_URL = "/api/case-tags/"
PROPOSALS_URL = "/api/case-tag-proposals/"


def make_user(role):
    """role: 'Caseworker' | 'ReadOnly' | 'JobPoller' | 'superuser'."""
    User = get_user_model()
    user = User.objects.create_user(username=f"u-{role}", password="x")
    if role == "superuser":
        user.is_superuser = True
        user.is_staff = True
        user.save()
    else:
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
    return user


def client_for(role):
    client = APIClient()
    client.force_authenticate(user=make_user(role))
    return client


def alias_payload(raw="Assets Beyond Known Income", tag="illicit-enrichment", key=None):
    return {
        "kind": ProposalKind.ALIAS_EQUIVALENCE,
        "payload": {
            "raw_value": raw,
            "proposed_tag_id": tag,
            "case_count": 8,
            "example_case_slugs": ["a-case", "another-case"],
        },
        "confidence": 0.92,
        "detected_by": "consumer:alias-proposer",
        "dedup_key": key or f"alias:{raw.lower()}",
    }


def new_term_payload(slug="asset-concealment", key="term:asset-concealment"):
    return {
        "kind": ProposalKind.NEW_TERM,
        "payload": {
            "axis": "offence",
            "proposed_slug": slug,
            "label_ne": "सम्पत्ति लुकाउने",
            "label_en": "Asset Concealment",
            "rationale": "No existing offence term covers concealment as charged.",
            "quoted_span": "…सम्पत्ति लुकाएको आरोप…",
            "case_slug": "a-case",
        },
        "confidence": 0.55,
        "detected_by": "consumer:tagger",
        "dedup_key": key,
    }


# ── T15: the vocabulary endpoint ─────────────────────────────────────────────────


def test_vocabulary_is_public_and_returns_every_axis():
    r = APIClient().get(VOCAB_URL)
    assert r.status_code == 200
    assert len(r.data) == 9


def test_vocabulary_carries_bounds_so_a_client_need_not_hardcode_policy():
    r = APIClient().get(f"{VOCAB_URL}?axis=offence&counts=false")
    axis = r.data[0]
    assert axis["id"] == "offence"
    assert (axis["min_per_case"], axis["max_per_case"]) == (0, 3)
    assert len(axis["terms"]) == 18


def test_vocabulary_returns_both_labels():
    r = APIClient().get(f"{VOCAB_URL}?axis=offence&counts=false")
    by_id = {t["id"]: t for t in r.data[0]["terms"]}
    assert by_id["bribery"]["label_ne"] == "घुस रिसवत"
    assert by_id["bribery"]["label_en"] == "Bribery"


def test_composed_label_is_exposed_as_a_map_not_a_string():
    r = APIClient().get(f"{VOCAB_URL}?axis=status&counts=false")
    term = next(t for t in r.data[0]["terms"] if t["id"] == "first-instance-decided")
    assert term["label_ne"] is None
    assert term["label_ne_composed"]["special"] == "विशेष अदालतको फैसला"


def test_non_enumerated_axes_return_an_empty_term_list():
    # A client must not read this as "no legal values" — these come from the entities
    # relation and the official district list respectively.
    r = APIClient().get(VOCAB_URL)
    by_id = {a["id"]: a for a in r.data}
    for axis_id in ("institution", "geography", "person"):
        assert by_id[axis_id]["terms"] == []
        assert by_id[axis_id]["members"] != "enumerated"


def test_highlighted_axes_are_flagged():
    r = APIClient().get(VOCAB_URL)
    flagged = {a["id"] for a in r.data if a["highlighted"]}
    assert flagged == {"status", "verdict"}


def test_proposed_terms_are_hidden_by_default_and_visible_with_include_all():
    Tag.objects.create(
        id="not-yet", axis_id="offence", label_en="Not Yet", status=TagStatus.PROPOSED
    )
    default = APIClient().get(f"{VOCAB_URL}?axis=offence&counts=false")
    assert "not-yet" not in {t["id"] for t in default.data[0]["terms"]}

    everything = APIClient().get(f"{VOCAB_URL}?axis=offence&counts=false&include=all")
    assert "not-yet" in {t["id"] for t in everything.data[0]["terms"]}


def test_counts_can_be_skipped():
    r = APIClient().get(f"{VOCAB_URL}?axis=offence&counts=false")
    assert all(t["case_count"] == 0 for t in r.data[0]["terms"])


# ── T25: create ──────────────────────────────────────────────────────────────────


def test_automation_may_file_a_proposal():
    # The machine role is the PRIMARY producer, so create must be open to it.
    r = client_for("JobPoller").post(PROPOSALS_URL, alias_payload(), format="json")
    assert r.status_code == 201
    assert r.data["status"] == "pending"


def test_a_client_cannot_self_approve_by_posting_a_status():
    body = alias_payload()
    body["status"] = "approved"
    r = client_for("Caseworker").post(PROPOSALS_URL, body, format="json")
    assert r.status_code == 201
    assert r.data["status"] == "pending"  # read-only field, silently ignored
    assert TagAlias.objects.count() == 0


def test_duplicate_dedup_key_is_rejected_so_a_rejection_stays_sticky():
    c = client_for("JobPoller")
    assert c.post(PROPOSALS_URL, alias_payload(), format="json").status_code == 201
    again = c.post(PROPOSALS_URL, alias_payload(), format="json")
    assert again.status_code == 400


def test_new_term_without_a_quoted_span_is_rejected():
    body = new_term_payload()
    del body["payload"]["quoted_span"]
    r = client_for("JobPoller").post(PROPOSALS_URL, body, format="json")
    assert r.status_code == 400
    assert "quoted_span" in str(r.data)


def test_alias_proposal_missing_its_target_is_rejected_at_create_time():
    body = alias_payload()
    del body["payload"]["proposed_tag_id"]
    r = client_for("JobPoller").post(PROPOSALS_URL, body, format="json")
    assert r.status_code == 400


def test_confidence_out_of_range_is_a_400_not_a_500():
    body = alias_payload()
    body["confidence"] = 1.5
    r = client_for("JobPoller").post(PROPOSALS_URL, body, format="json")
    assert r.status_code == 400


# ── T25: decide ──────────────────────────────────────────────────────────────────


def test_the_machine_role_cannot_approve_its_own_proposal():
    """The load-bearing guard of the whole app.

    Automation files the proposals (T27 files one per unmapped tag value). If it could
    also approve them, an LLM would be able to publish a term or an alias on its own
    authority and every other safeguard here would be decoration.
    """
    c = client_for("JobPoller")
    created = c.post(PROPOSALS_URL, alias_payload(), format="json")
    pid = created.data["id"]

    r = c.post(f"{PROPOSALS_URL}{pid}/approve/", {}, format="json")
    assert r.status_code == 403
    assert TagAlias.objects.count() == 0


def test_readonly_cannot_decide():
    p = TagProposal.objects.create(**alias_payload())
    r = client_for("ReadOnly").post(f"{PROPOSALS_URL}{p.id}/approve/", {}, format="json")
    assert r.status_code == 403


def test_approving_an_alias_creates_it_with_attribution():
    p = TagProposal.objects.create(**alias_payload())
    r = client_for("Caseworker").post(
        f"{PROPOSALS_URL}{p.id}/approve/", {"notes": "same concept"}, format="json"
    )
    assert r.status_code == 200
    assert r.data["status"] == "approved"

    alias = TagAlias.objects.get()
    # Stored NORMALIZED, not raw: a raw-cased alias would be unreachable for every
    # other casing, which is the Ncell/ncell defect recreated inside its own fix.
    assert alias.value == "assets beyond known income"
    assert alias.tag_id == "illicit-enrichment"
    assert alias.approved_by.startswith("caseworker:")
    assert alias.approved_at is not None


def test_approving_an_alias_does_not_touch_stored_case_tags():
    """Approval changes the vocabulary, never the cases.

    This is what makes a wrong tick recoverable: aliases are applied when the search
    document is built, so un-approving and reindexing reverts it with no migration.
    """
    from cases.models import Case

    case = Case.objects.create(
        title="A case", slug="a-case", tags=["Assets Beyond Known Income"]
    )
    p = TagProposal.objects.create(**alias_payload())
    client_for("Caseworker").post(f"{PROPOSALS_URL}{p.id}/approve/", {}, format="json")

    case.refresh_from_db()
    assert case.tags == ["Assets Beyond Known Income"]


def test_approving_a_new_term_creates_an_active_tag():
    p = TagProposal.objects.create(**new_term_payload())
    r = client_for("Caseworker").post(f"{PROPOSALS_URL}{p.id}/approve/", {}, format="json")
    assert r.status_code == 200

    tag = Tag.objects.get(id="asset-concealment")
    assert tag.status == TagStatus.ACTIVE
    assert tag.axis_id == "offence"
    assert tag.label_ne == "सम्पत्ति लुकाउने"


def test_rejecting_changes_nothing_but_records_the_decision():
    p = TagProposal.objects.create(**alias_payload())
    r = client_for("Caseworker").post(
        f"{PROPOSALS_URL}{p.id}/reject/", {"notes": "different concept"}, format="json"
    )
    assert r.status_code == 200
    assert r.data["status"] == "rejected"
    assert r.data["review_notes"] == "different concept"
    assert TagAlias.objects.count() == 0


def test_deciding_twice_is_a_409_not_a_double_write():
    p = TagProposal.objects.create(**alias_payload())
    c = client_for("Caseworker")
    assert c.post(f"{PROPOSALS_URL}{p.id}/approve/", {}, format="json").status_code == 200
    again = c.post(f"{PROPOSALS_URL}{p.id}/approve/", {}, format="json")
    assert again.status_code == 409
    assert TagAlias.objects.count() == 1


def test_approving_an_alias_onto_an_unknown_tag_is_a_400():
    p = TagProposal.objects.create(
        **alias_payload(tag="no-such-term", key="alias:bad-target")
    )
    r = client_for("Caseworker").post(f"{PROPOSALS_URL}{p.id}/approve/", {}, format="json")
    assert r.status_code == 400
    p.refresh_from_db()
    assert p.status == "pending"  # a failed apply must not mark it decided


def test_approving_an_alias_onto_a_merged_term_is_refused():
    # It would work — the resolver follows merges — but it bakes in an indirection
    # nobody asked for. Point at the replacement instead.
    target = Tag.objects.get(id="illicit-enrichment")
    Tag.objects.create(
        id="illegal-wealth", axis_id="offence", label_en="Illegal Wealth",
        status=TagStatus.MERGED, merged_into=target,
    )
    p = TagProposal.objects.create(
        **alias_payload(tag="illegal-wealth", key="alias:onto-merged")
    )
    r = client_for("Caseworker").post(f"{PROPOSALS_URL}{p.id}/approve/", {}, format="json")
    assert r.status_code == 400
    assert "merged" in str(r.data)


def test_a_new_term_on_a_non_enumerated_axis_is_refused():
    body = new_term_payload(slug="dhanusha", key="term:dhanusha")
    body["payload"]["axis"] = "geography"
    p = TagProposal.objects.create(**body)
    r = client_for("Caseworker").post(f"{PROPOSALS_URL}{p.id}/approve/", {}, format="json")
    assert r.status_code == 400
    assert not Tag.objects.filter(id="dhanusha").exists()


def test_a_new_term_with_an_unslugged_id_is_refused():
    # apply refuses to mint a slug: transliterating महालेखा परीक्षक is not deterministic,
    # so two approvals would produce two slugs for one concept.
    body = new_term_payload(slug="Asset Concealment", key="term:unslugged")
    p = TagProposal.objects.create(**body)
    r = client_for("Caseworker").post(f"{PROPOSALS_URL}{p.id}/approve/", {}, format="json")
    assert r.status_code == 400


# ── T25: retarget ────────────────────────────────────────────────────────────────


def test_a_reviewer_can_retarget_an_alias_before_approving():
    """Without this the reviewer's only options are approve-wrong or reject.

    And rejection is sticky, so rejecting would leave the value permanently unresolved.
    """
    p = TagProposal.objects.create(**alias_payload(tag="embezzlement"))
    c = client_for("Caseworker")
    fixed = dict(p.payload, proposed_tag_id="illicit-enrichment")
    r = c.patch(f"{PROPOSALS_URL}{p.id}/payload/", {"payload": fixed}, format="json")
    assert r.status_code == 200

    c.post(f"{PROPOSALS_URL}{p.id}/approve/", {}, format="json")
    assert TagAlias.objects.get().tag_id == "illicit-enrichment"


def test_a_decided_proposal_cannot_be_retargeted():
    p = TagProposal.objects.create(**alias_payload())
    c = client_for("Caseworker")
    c.post(f"{PROPOSALS_URL}{p.id}/approve/", {}, format="json")
    r = c.patch(f"{PROPOSALS_URL}{p.id}/payload/", {"payload": p.payload}, format="json")
    assert r.status_code == 409


def test_the_machine_role_cannot_retarget():
    p = TagProposal.objects.create(**alias_payload())
    r = client_for("JobPoller").patch(
        f"{PROPOSALS_URL}{p.id}/payload/", {"payload": p.payload}, format="json"
    )
    assert r.status_code == 403


# ── T25: filtering, for the queue UI ─────────────────────────────────────────────


def test_the_queue_filters_by_kind_and_status():
    TagProposal.objects.create(**alias_payload())
    TagProposal.objects.create(**new_term_payload())
    c = client_for("Caseworker")
    assert c.get(f"{PROPOSALS_URL}?kind=alias_equivalence").data["count"] == 1
    assert c.get(f"{PROPOSALS_URL}?status=pending").data["count"] == 2


def test_axes_are_ordered_for_display_with_highlighted_first():
    r = APIClient().get(f"{VOCAB_URL}?counts=false")
    ids = [a["id"] for a in r.data]
    assert ids[:2] == ["status", "verdict"]
    assert TagAxis.objects.get(id="status").sort_order < TagAxis.objects.get(
        id="offence"
    ).sort_order


# ── PR #463 review fixes ─────────────────────────────────────────────────────────


def test_editing_a_payload_into_an_unapprovable_shape_is_refused():
    """The queue is for decisions, not litter.

    ``TagProposalPayloadEditSerializer`` only asserts "is an object", so without a
    kind-aware re-check a reviewer could save a payload missing the fields its own kind
    requires — a row that looks reviewable and 400s the moment anyone approves it.
    """
    p = TagProposal.objects.create(**alias_payload())
    r = client_for("Caseworker").patch(
        f"{PROPOSALS_URL}{p.id}/payload/",
        {"payload": {"raw_value": "something"}},  # no proposed_tag_id
        format="json",
    )
    assert r.status_code == 400
    p.refresh_from_db()
    assert p.payload["proposed_tag_id"] == "illicit-enrichment"  # unchanged


def test_approving_an_alias_that_already_exists_on_the_same_term_is_idempotent():
    """Covers the get_or_create path: losing a race to an identical proposal is a no-op.

    Two proposals for the same raw string approved concurrently both reach `apply`; the
    read-then-write version let both insert and one got a unique-violation 500.
    """
    TagAlias.objects.create(
        value="assets beyond known income", tag_id="illicit-enrichment"
    )
    p = TagProposal.objects.create(**alias_payload())
    r = client_for("Caseworker").post(f"{PROPOSALS_URL}{p.id}/approve/", {}, format="json")
    assert r.status_code == 200
    assert TagAlias.objects.count() == 1


def test_approving_an_alias_that_exists_on_a_different_term_is_a_deterministic_400():
    TagAlias.objects.create(value="assets beyond known income", tag_id="embezzlement")
    p = TagProposal.objects.create(**alias_payload())
    r = client_for("Caseworker").post(f"{PROPOSALS_URL}{p.id}/approve/", {}, format="json")
    assert r.status_code == 400
    assert "already resolves to" in str(r.data)
    assert TagAlias.objects.get().tag_id == "embezzlement"  # untouched
