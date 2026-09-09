"""The entity public-search visibility gate: ``case_count`` and its triggers.

An entity is publicly searchable iff a PUBLISHED Jawafdehi case cites it. The
count is DERIVED from the binds and promoted onto the search document, so these
tests are about two things:

1. the arithmetic (``entities.search_visibility``), including the two directions
   an entity can move and the distinct-case rule;
2. the TRIGGERS (``cases.signals``) — the requirement is that an archived entity
   becomes visible with no operator step the moment a published case binds it,
   so every write path that changes a bind has to reach the index.

The trigger tests assert on ``index_by_iri`` calls rather than on OpenSearch,
which is stubbed: the contract under test is "the indexer was asked to write the
right count for the right IRI", and the indexer itself is covered elsewhere.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import TestCase

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    RelationshipType,
)
from entities.models import StoredEntity
from entities.search_visibility import (
    case_counts,
    clear_cache,
    entity_case_count,
    entity_public_visible,
    referenced_iris,
)

IRI_BASE = "https://jawafdehi.org/entity"
PERSON = f"{IRI_BASE}/person/gate-test-subject"
OTHER = f"{IRI_BASE}/person/gate-test-bystander"
PLACE = f"{IRI_BASE}/location/district/gate-test-district"


def _entity(iri: str, *, is_deleted: bool = False) -> StoredEntity:
    prefix, _, slug = iri.removeprefix(f"{IRI_BASE}/").rpartition("/")
    return StoredEntity.objects.create(
        iri=iri,
        entity_type="Person",
        prefix=prefix,
        slug=slug,
        data={"@id": iri, "@type": "Person", "name": {"en": slug}},
        is_deleted=is_deleted,
    )


def _case(state: str = CaseState.PUBLISHED, title: str = "Gate test") -> Case:
    return Case.objects.create(title=title, case_type=CaseType.CORRUPTION, state=state)


def _bind(case: Case, iri: str, role: str = RelationshipType.ACCUSED):
    return CaseEntityRelationship.objects.create(
        case=case, nes_id=iri, relationship_type=role
    )


class VisibilityArithmeticTests(TestCase):
    """``case_counts`` / ``entity_case_count`` / ``referenced_iris``."""

    databases = "__all__"

    def setUp(self):
        cache.clear()
        for target in (
            "entities.search_index.index",
            "entities.search_index.delete",
            "entities.search_index.index_by_iri",
            "cases.search_index.index",
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_uncited_entity_has_no_count(self):
        _entity(PERSON)
        assert entity_case_count(PERSON) == 0
        assert case_counts() == {}
        assert referenced_iris() == frozenset()

    def test_published_bind_makes_an_entity_cited(self):
        _entity(PERSON)
        _bind(_case(), PERSON)
        assert entity_case_count(PERSON) == 1
        assert case_counts() == {PERSON: 1}
        assert referenced_iris(refresh=True) == frozenset({PERSON})

    def test_draft_bind_does_not_count(self):
        """Mirrors the case index's own PUBLISHED-only rule: an unpublished case
        must not surface the people it names."""
        _entity(PERSON)
        _bind(_case(state=CaseState.DRAFT), PERSON)
        assert entity_case_count(PERSON) == 0
        assert case_counts() == {}

    def test_location_role_counts(self):
        """Decision 2026-09-09: every bind role counts, locations included."""
        _entity(PLACE)
        _bind(_case(), PLACE, role=RelationshipType.LOCATION)
        assert entity_case_count(PLACE) == 1

    def test_counts_distinct_cases_not_binds(self):
        """Two roles in ONE case is one citation. "In 2 cases" must mean two
        cases, so the annotate counts distinct cases."""
        _entity(PERSON)
        case = _case()
        _bind(case, PERSON, role=RelationshipType.ACCUSED)
        _bind(case, PERSON, role=RelationshipType.RELATED)
        assert CaseEntityRelationship.objects.filter(nes_id=PERSON).count() == 2
        assert entity_case_count(PERSON) == 1
        assert case_counts() == {PERSON: 1}

    def test_three_cases(self):
        _entity(PERSON)
        for n in range(3):
            _bind(_case(title=f"Gate test {n}"), PERSON)
        assert entity_case_count(PERSON) == 3
        assert case_counts() == {PERSON: 3}

    def test_unpublishing_one_of_two_leaves_the_entity_visible(self):
        """The A2 edge case: never decrement, always recompute. An entity cited
        by two cases must survive one of them being unpublished."""
        _entity(PERSON)
        first, second = _case(title="First"), _case(title="Second")
        _bind(first, PERSON)
        _bind(second, PERSON)
        assert entity_case_count(PERSON) == 2

        first.state = CaseState.DRAFT
        first.save()
        assert entity_case_count(PERSON) == 1
        assert entity_public_visible(_entity_row(PERSON)) is True

        second.state = CaseState.DRAFT
        second.save()
        assert entity_case_count(PERSON) == 0
        assert entity_public_visible(_entity_row(PERSON)) is False

    def test_removing_the_last_bind_archives_the_entity(self):
        _entity(PERSON)
        bind = _bind(_case(), PERSON)
        assert entity_case_count(PERSON) == 1
        bind.delete()
        assert entity_case_count(PERSON) == 0

    def test_soft_deleted_entity_is_never_visible_even_when_cited(self):
        """The two conditions compose with AND. A soft-deleted entity is off the
        read plane, so a citation must not resurrect it in search."""
        entity = _entity(PERSON, is_deleted=True)
        _bind(_case(), PERSON)
        assert entity_case_count(PERSON) == 1
        assert entity_public_visible(entity) is False

    def test_cache_is_invalidated_rather_than_going_stale(self):
        _entity(PERSON)
        assert referenced_iris() == frozenset()
        _bind(_case(), PERSON)
        # Still cached from the call above — this is the drift the signal closes.
        assert referenced_iris() == frozenset()
        clear_cache()
        assert referenced_iris() == frozenset({PERSON})


class VisibilityTriggerTests(TestCase):
    """``cases.signals``: every bind write must reach the entity index.

    The auto-unarchive requirement lives here. A caseworker binds an entity and
    the entity becomes publicly visible with no further action — so the trigger
    has to fire from the bind, not from a ``Case`` save, because the bind-rewrite
    path does not save the case.
    """

    databases = "__all__"

    def setUp(self):
        cache.clear()
        for target in (
            "entities.search_index.index",
            "entities.search_index.delete",
            "cases.search_index.index",
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        reindex_patcher = patch("entities.search_index.index_by_iri")
        self.reindex = reindex_patcher.start()
        self.addCleanup(reindex_patcher.stop)

    def _on_commit(self, write):
        """Run ``write`` and then its ``on_commit`` callbacks.

        The signals defer every cross-app re-index to ``transaction.on_commit``
        so a rolled-back case write never touches the index. ``TestCase`` wraps
        each test in a transaction that is never committed, so without this the
        callbacks are collected and discarded and every assertion below would
        pass vacuously against an unused mock.
        """
        with self.captureOnCommitCallbacks(execute=True):
            return write()

    def _counts_written(self) -> dict[str, int]:
        """``{iri: last case_count written}`` across every reindex call."""
        written: dict[str, int] = {}
        for call in self.reindex.call_args_list:
            iri = call.args[0] if call.args else call.kwargs["iri"]
            written[iri] = call.kwargs.get("case_count")
        return written

    def test_binding_a_published_case_unarchives_the_entity(self):
        """THE requirement: no operator step, no management command."""
        _entity(PERSON)
        self.reindex.reset_mock()
        self._on_commit(lambda: _bind(_case(), PERSON))
        assert self._counts_written() == {PERSON: 1}

    def test_binding_a_draft_case_reindexes_but_keeps_it_archived(self):
        """The trigger still fires (the count has to be re-asserted), but the
        answer is 0 until the case publishes."""
        _entity(PERSON)
        self.reindex.reset_mock()
        self._on_commit(lambda: _bind(_case(state=CaseState.DRAFT), PERSON))
        assert self._counts_written() == {PERSON: 0}

    def test_publishing_a_case_unarchives_the_entities_it_already_binds(self):
        """The other order: bind on a draft, then publish. The Case ``post_save``
        leg is what covers this, since no bind row changes."""
        _entity(PERSON)
        case = _case(state=CaseState.DRAFT)
        _bind(case, PERSON)
        self.reindex.reset_mock()

        def publish():
            case.state = CaseState.PUBLISHED
            case.save()

        self._on_commit(publish)
        assert self._counts_written() == {PERSON: 1}

    def test_unbinding_reindexes_the_entity(self):
        _entity(PERSON)
        bind = _bind(_case(), PERSON)
        self.reindex.reset_mock()
        self._on_commit(bind.delete)
        assert self._counts_written() == {PERSON: 0}

    def test_whole_list_rewrite_reaches_every_affected_entity(self):
        """``cases.api_views._rewrite_entity_binds`` deletes the whole list and
        recreates it WITHOUT saving the case. ``QuerySet.delete()`` emits
        ``post_delete`` per row, which is what makes that path observable."""
        _entity(PERSON)
        _entity(OTHER)
        case = _case()
        _bind(case, PERSON)
        self.reindex.reset_mock()

        def rewrite():
            # Drop the whole list, then write the new one — the shape
            # ``_rewrite_entity_binds`` uses, with no ``Case.save()``.
            case.entity_relationships.all().delete()
            _bind(case, OTHER)

        self._on_commit(rewrite)
        written = self._counts_written()
        assert written[PERSON] == 0
        assert written[OTHER] == 1

    def test_hard_deleting_a_case_archives_its_entities(self):
        """The A3 edge case. ``CaseEntityRelationship`` rows CASCADE away before
        ``post_delete``, so the IRIs come from the ``pre_delete`` snapshot."""
        _entity(PERSON)
        case = _case()
        _bind(case, PERSON)
        self.reindex.reset_mock()
        self._on_commit(Case.objects.filter(pk=case.pk).delete)
        assert self._counts_written() == {PERSON: 0}


def _entity_row(iri: str) -> StoredEntity:
    return StoredEntity.objects.get(iri=iri)


@pytest.mark.django_db(databases="__all__")
def test_build_doc_promotes_the_count():
    """The indexer writes the number the gate filters on."""
    from entities import search_index

    with patch("entities.search_index.index"), patch("entities.search_index.delete"):
        entity = _entity(PERSON)
    assert search_index.build_doc(entity, case_count=3)["case_count"] == 3
    # Unsupplied → resolved for this one entity (the live post_save path).
    assert search_index.build_doc(entity)["case_count"] == 0


class MergeVisibilityTests(TestCase):
    """A merge must move visibility from the retired IRIs onto the survivor.

    ``merge.references`` repoints ``CaseEntityRelationship.nes_id`` onto the
    survivor, so the survivor's citation count rises and the retired IRIs' fall
    to 0. Neither ``StoredEntity`` row's own signal can see that — the binds live
    in a different app and a different database — so before this was wired the
    survivor stayed archived out of public search while its tombstones stayed
    visible.
    """

    databases = "__all__"

    def setUp(self):
        cache.clear()
        for target in (
            "entities.search_index.index",
            "entities.search_index.delete",
            "cases.search_index.index",
            "cases.search_index.index_now",
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        reindex_patcher = patch("entities.search_index.index_by_iri")
        self.reindex = reindex_patcher.start()
        self.addCleanup(reindex_patcher.stop)

    def test_merge_reindexes_both_the_survivor_and_the_retired(self):
        from entities.services.merge import EntityMergeService

        _entity(PERSON)
        _entity(OTHER)
        _bind(_case(), OTHER)  # the duplicate holds the citation
        self.reindex.reset_mock()

        EntityMergeService().merge(
            survivor_iri=PERSON,
            duplicate_iris=[OTHER],
            author_id="oidc:test",
        )

        touched = {
            (call.args[0] if call.args else call.kwargs["iri"])
            for call in self.reindex.call_args_list
        }
        assert PERSON in touched, "survivor must be re-indexed: it just gained a citation"
        assert OTHER in touched, "retired IRI must be re-indexed: it just lost one"
