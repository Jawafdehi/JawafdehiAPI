"""The vocabulary read endpoint."""

import pytest
from rest_framework.test import APIClient

from case_tags.models import Tag, TagAxis, TagStatus

pytestmark = pytest.mark.django_db

VOCAB_URL = "/api/case-tags/"


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


def test_axes_are_ordered_for_display_with_highlighted_first():
    r = APIClient().get(f"{VOCAB_URL}?counts=false")
    ids = [a["id"] for a in r.data]
    assert ids[:2] == ["status", "verdict"]
    assert TagAxis.objects.get(id="status").sort_order < TagAxis.objects.get(
        id="offence"
    ).sort_order
