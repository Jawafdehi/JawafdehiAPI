"""Tag roll-up at index time.

``Case.tags`` stores only the specific tag a case was given. The search document
carries that PLUS every broader tag, so ``?tags=land`` is a single term query that
matches a case tagged only ``land-grab`` — no query-time rewriting, no vocabulary
loaded in the query layer.

The labels go somewhere else on purpose. Ids are slugs; a reader searching free text
types "भूमि प्रशासन", not "land-administration".
"""

from __future__ import annotations

from typing import Any

import pytest

from case_tags.models import Tag, TagStatus
from cases.search_index import _expand_tags

pytestmark = pytest.mark.django_db


@pytest.fixture
def vocabulary() -> None:
    land = Tag.objects.create(
        id="land", label_ne="भूमि", label_en="Land", status=TagStatus.ACTIVE
    )
    Tag.objects.create(
        id="land-administration",
        label_ne="भूमि प्रशासन",
        label_en="Land Administration",
        status=TagStatus.ACTIVE,
        broader=land,
    )
    Tag.objects.create(
        id="land-grab",
        label_ne="सरकारी जग्गा हडप",
        label_en="Land Grab",
        status=TagStatus.ACTIVE,
        broader=land,
    )
    bagmati = Tag.objects.create(
        id="bagmati",
        label_ne="बागमती प्रदेश",
        label_en="Bagmati Province",
        status=TagStatus.ACTIVE,
    )
    Tag.objects.create(
        id="lalitpur",
        label_ne="ललितपुर",
        label_en="Lalitpur",
        status=TagStatus.ACTIVE,
        broader=bagmati,
    )


def test_adds_the_broader_tag(vocabulary: None) -> None:
    ids, _ = _expand_tags(["land-grab"])
    assert ids == ["land-grab", "land"]


def test_districts_roll_up_to_their_province(vocabulary: None) -> None:
    """The reason the province facet is usable at all: 41 of 82 cases had geography
    before curation, and most of what they had was a district."""
    ids, _ = _expand_tags(["lalitpur"])
    assert ids == ["lalitpur", "bagmati"]


def test_a_broader_tag_is_added_once_for_two_children(vocabulary: None) -> None:
    """A shared parent appears exactly once — a duplicated term would inflate that
    tag's facet count for this one document."""
    ids, _ = _expand_tags(["land-administration", "land-grab"])
    # Each tag is followed by its parent, so `land` lands after the FIRST child
    # that pulled it in and is not re-added by the second.
    assert ids == ["land-administration", "land", "land-grab"]
    assert ids.count("land") == 1


def test_already_having_the_parent_is_not_duplicated(vocabulary: None) -> None:
    ids, _ = _expand_tags(["land", "land-grab"])
    assert ids == ["land", "land-grab"]


def test_specific_order_is_preserved(vocabulary: None) -> None:
    """Parents append after the tags that pulled them in, so the case's own tags stay
    first — the order a display would want."""
    ids, _ = _expand_tags(["lalitpur", "land-grab"])
    assert ids == ["lalitpur", "bagmati", "land-grab", "land"]


def test_labels_carry_both_scripts(vocabulary: None) -> None:
    """Someone typing Devanagari and someone typing Latin must both hit the case."""
    _, labels = _expand_tags(["land-administration"])
    assert "भूमि प्रशासन" in labels
    assert "Land Administration" in labels


def test_labels_exclude_the_parent(vocabulary: None) -> None:
    """A land-grab case must not become a free-text hit for "भूमि प्रशासन" merely
    because both roll up to land. The roll-up is for FILTERING, not for text."""
    _, labels = _expand_tags(["land-grab"])
    assert "भूमि प्रशासन" not in labels
    assert "Land Administration" not in labels
    # The parent's own label is also not smuggled in.
    assert "भूमि" not in labels


def test_unknown_id_passes_through_in_both_lists(vocabulary: None) -> None:
    """A tag can only be unknown here if the vocabulary lost a term the cases still
    carry. Dropping it silently would hide that; keeping it means the case stays
    findable by the one string it has."""
    ids, labels = _expand_tags(["land-grab", "some-orphan"])
    assert "some-orphan" in ids
    assert "some-orphan" in labels


def test_empty_is_empty(vocabulary: None) -> None:
    assert _expand_tags([]) == ([], [])


def test_one_query_regardless_of_tag_count(
    vocabulary: None, django_assert_num_queries: Any
) -> None:
    """Reindexing walks every case, so a per-tag lookup would be an N+1 across the
    whole corpus."""
    with django_assert_num_queries(1):
        _expand_tags(["land-grab", "land-administration", "lalitpur"])


def test_document_carries_ids_in_tags_and_labels_in_keywords(vocabulary: None) -> None:
    """The end-to-end shape, through ``build_indexed_doc``.

    ``build_doc`` itself stays pure — no DB — which is a deliberate property of this
    module (see its docstring) and what keeps the 15 doc-shape tests in
    search/tests/test_indexers.py DB-free. The vocabulary lookup lives in the
    indexed wrapper, exactly where entity resolution already does.

    ``keywords`` is analyzed text and also holds case_type; faceting tags off it is
    what returned CORRUPTION as a tag."""
    from cases.models import Case, CaseState, CaseType
    from cases.search_index import build_indexed_doc

    case = Case.objects.create(
        title="जग्गा प्रकरण",
        slug="rollup-doc-shape",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        tags=["land-grab", "lalitpur"],
    )
    doc = build_indexed_doc(case)

    assert doc["tags"] == ["land-grab", "land", "lalitpur", "bagmati"]
    assert "भूमि" not in doc["keywords"]  # parent label not promoted
    assert "सरकारी जग्गा हडप" in doc["keywords"]
    assert "Lalitpur" in doc["keywords"]
    assert "CORRUPTION" in doc["keywords"]  # case_type still rides along
    # No slug leaks into the analyzed text field.
    assert "land-grab" not in doc["keywords"]
    # raw.tags stays the case's OWN tags, un-expanded — it is the record, not the index.
    assert doc["raw"]["tags"] == ["land-grab", "lalitpur"]
