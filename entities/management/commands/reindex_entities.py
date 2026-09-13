"""``reindex_entities`` — bulk-(re)index NES entities into ``nes-entities``.

Streams the SEARCHABLE ``StoredEntity`` set through the entity indexer's
``build_doc`` into OpenSearch. ``--rebuild`` builds a new generation and swaps
the alias onto it (mapping changes). The DB router pins ``StoredEntity`` reads to
the ``nes`` DB automatically.

Only ``is_deleted=False`` rows are indexed — the SAME gate the live ``post_save``
signal applies (``entities.signals``: a soft-deleted row is EVICTED, not indexed,
because DELETE flips the flag rather than removing the row). Streaming ``.all()``
here instead RESURRECTS every tombstone: an entity deleted from the read plane
comes back in anonymous unified search on the next reindex. Mirrors the identical
gate in ``reindex_materials``.

CASE-COUNT PROMOTION. Every document carries ``case_count``, the number of
PUBLISHED Jawafdehi cases citing it, which is the public-search visibility gate
(``entities.search_visibility``). It is loaded ONCE as a grouped map (~1,544
rows) before streaming: resolving it per document would issue one query per
entity across ~187k rows. A cases-DB failure raises out of ``case_counts()``
rather than yielding an empty map, because an empty map indexes the whole corpus
as uncited and archives all of it in one pass.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from jawafdehi_shared.search.opensearch import ENTITY_INDEX
from jawafdehi_shared.search.reindex import reindex, summary
from entities import search_index
from entities.models import StoredEntity
from entities.search_visibility import case_counts


class Command(BaseCommand):
    help = "Bulk-(re)index NES entities into the nes-entities OpenSearch index."

    def add_arguments(self, parser):
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Build a new generation and swap the alias onto it.",
        )

    def handle(self, *args, **options):
        # Load the citation counts up front. This RAISES on a cases-DB failure
        # (see the module docstring): a silent empty map would archive every
        # entity from public search in one swap. ``RebuildAborted`` in the shared
        # driver is the second line of defence, but it only triggers on a wholly
        # empty generation — a gate that mis-answered would still pass it.
        counts = case_counts()
        self.stdout.write(f"loaded case counts for {len(counts)} cited entities")

        def build(entity):
            """``build_doc`` with the citation count supplied from the bulk map.

            ``.get(iri, 0)`` is correct rather than fail-closed guesswork: the map
            holds an entry for every entity a published case cites, so an absent
            IRI genuinely has no published citation.
            """
            return search_index.build_doc(
                entity, case_count=counts.get(entity.iri, 0)
            )

        # Mirror the live indexer's gate (entities.signals): index ONLY the rows
        # still on the read plane. Without this a reindex re-adds soft-deleted
        # entities to public search — the exact rows the signal evicted.
        def changed(since):
            """``(iri, entity_or_None)`` for everything written during the build.

            THREE sources, because an entity's document depends on data in two
            databases and only the first source touches the entity row itself:

            1. ``StoredEntity.updated_at`` — the entity was re-published.
               Unfiltered on is_deleted so a DELETE landing mid-build arrives as a
               TOMBSTONE; NES delete is a soft delete, so without this the swap
               resurrects the very tombstone the signal evicted. ``updated_at`` is
               NOT auto_now here; entities.persistence sets it on every
               re-publish, which is exactly the write we need to catch.
            2. A bind CREATED mid-build — a newly cited entity whose
               ``case_count`` went from 0 to non-zero. Nothing stamps the entity
               row, so source 1 cannot see it, and missing it would leave a
               just-cited entity invisible until the next reconcile.
            3. A case SAVED mid-build — its state may have changed, which flips
               the visibility of every entity it binds in either direction.
               ``Case.updated_at`` is ``auto_now``, so any save is caught.

            Residual gap, closed by ``reconcile_entity_visibility`` rather than
            here: a bind DELETED mid-build on a case that is not itself saved.
            ``CaseEntityRelationship`` has no ``updated_at`` and a deleted row
            leaves no timestamp to scan, so it is unobservable from the tables.
            The bind-rewrite path issues its deletes inside a case PATCH, which
            does save the case, so source 3 covers the common shape; the leftover
            drift is an entity shown as cited slightly too long, which is the
            safer direction.
            """
            seen: set[str] = set()
            for e in StoredEntity.objects.filter(updated_at__gte=since).iterator():
                seen.add(e.iri)
                yield e.iri, (None if e.is_deleted else e)

            for iri in _iris_touched_since(since):
                if iri in seen:
                    continue
                seen.add(iri)
                entity = StoredEntity.objects.filter(iri=iri).first()
                yield iri, (None if (entity is None or entity.is_deleted) else entity)

        result = reindex(
            index=ENTITY_INDEX,
            records=StoredEntity.objects.filter(is_deleted=False).iterator(),
            build_doc=build,
            rebuild=options["rebuild"],
            catchup=changed,
        )
        self.stdout.write(self.style.SUCCESS(summary("nes-entities", result)))


def _iris_touched_since(since) -> set[str]:
    """Entity IRIs whose citation count may have moved since ``since``.

    Sources 2 and 3 of the catchup (see ``changed``): binds created in the
    window, and binds held by a case saved in the window. Both are cross-app
    reads into the default DB, so a failure is swallowed — the catchup is a
    best-effort narrowing of an already-complete full pass, and the reconcile
    command is the backstop.
    """
    try:
        from cases.models import Case, CaseEntityRelationship

        fresh_binds = CaseEntityRelationship.objects.filter(created_at__gte=since)
        touched_cases = Case.objects.filter(updated_at__gte=since)
        binds_on_touched = CaseEntityRelationship.objects.filter(
            case__in=touched_cases
        )
        return set(
            fresh_binds.values_list("nes_id", flat=True)
        ) | set(binds_on_touched.values_list("nes_id", flat=True))
    except Exception:  # noqa: BLE001 — best-effort narrowing; reconcile backstops.
        return set()
