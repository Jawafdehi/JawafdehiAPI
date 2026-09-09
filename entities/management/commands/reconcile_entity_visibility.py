"""``reconcile_entity_visibility`` — repair drift in entity ``case_count``.

An entity is publicly searchable iff a PUBLISHED Jawafdehi case cites it, and the
citation count is promoted onto the search document as ``case_count`` (see
``entities.search_visibility``). The live path that keeps it current is
best-effort: ``cases.signals._refresh_bound_entities`` swallows an OpenSearch
failure so a search blip cannot fail a case write. This command is the backstop
for exactly those swallowed failures, plus the one catchup gap
``reindex_entities`` documents (a bind deleted mid-rebuild on an unsaved case).

Bounded work: it only inspects entities that are cited or that the index claims
are cited, which is ~1,544 documents against a 187k corpus. Cheap enough to run
hourly, and unlike ``reindex_entities --rebuild`` it neither creates a generation
nor swaps an alias.

``--dry-run`` reports what it would change and writes nothing.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from jawafdehi_shared.search.opensearch import ENTITY_INDEX, make_client
from entities import search_index
from entities.models import StoredEntity
from entities.search_visibility import case_counts


class Command(BaseCommand):
    help = "Re-index NES entities whose indexed case_count disagrees with the DB."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the corrections without writing to the index.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        # Raises on a cases-DB failure rather than reporting "nothing is cited",
        # which would otherwise archive every entity this command touches.
        counts = case_counts()
        client = make_client()

        indexed = _indexed_case_counts(client)
        # Union both directions. ``counts`` alone would miss an entity the index
        # still shows as cited after its last case was unpublished — the
        # over-visible direction, which is the one a citation-driven scan cannot
        # see from the database side.
        candidates = set(counts) | set(indexed)

        corrected = 0
        missing: list[str] = []
        for iri in sorted(candidates):
            expected = counts.get(iri, 0)
            if indexed.get(iri) == expected:
                continue
            entity = StoredEntity.objects.filter(iri=iri).first()
            if entity is None or entity.is_deleted:
                # A bind pointing at an entity the store no longer serves. Evict
                # the document (matching the entities.signals rule) and report the
                # dangling IRI: it means a bind outlived its entity, which the
                # merge path is supposed to prevent.
                missing.append(iri)
                if not dry_run:
                    search_index.delete_by_iri(iri, client=client)
                corrected += 1
                continue
            if not dry_run:
                search_index.index(entity, client=client, case_count=expected)
            corrected += 1
            self.stdout.write(
                f"  {iri}: indexed={indexed.get(iri)!r} -> {expected}"
            )

        verb = "would correct" if dry_run else "corrected"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {corrected} of {len(candidates)} candidate entities "
                f"({len(counts)} cited in the DB)"
            )
        )
        if missing:
            self.stdout.write(
                self.style.WARNING(
                    f"{len(missing)} bind(s) point at an entity the store does not "
                    f"serve: {', '.join(missing[:10])}"
                    + ("…" if len(missing) > 10 else "")
                )
            )


def _indexed_case_counts(client) -> dict[str, int]:
    """``{iri: case_count}`` for every indexed entity the index shows as cited.

    Scans only documents with ``case_count >= 1``. That is the small side (~1.5k
    of 187k) and it is the only side the DB scan cannot infer, so pairing it with
    ``case_counts()`` covers both drift directions without walking the corpus.
    """
    body = {
        "query": {"range": {"case_count": {"gte": 1}}},
        "_source": ["case_count"],
        "size": 1000,
        "sort": [{"iri": {"order": "asc"}}],
    }
    out: dict[str, int] = {}
    search_after = None
    while True:
        page = dict(body)
        if search_after is not None:
            page["search_after"] = search_after
        response = client.search(index=ENTITY_INDEX, body=page)
        hits = (response.get("hits") or {}).get("hits") or []
        if not hits:
            return out
        for hit in hits:
            source = hit.get("_source") or {}
            out[hit.get("_id")] = int(source.get("case_count") or 0)
        search_after = hits[-1].get("sort")
        if search_after is None:
            return out
