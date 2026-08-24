"""T6 — turn a raw tag string into a canonical :class:`~case_tags.models.Tag` id.

Two steps, in this order and no others: mechanical normalization
(:func:`jawafdehi_shared.tags.normalize.normalize_tag`), then an exact lookup. There is
no third step, and in particular **no fuzzy fallback**.

That absence is the design. The corpus's fragmentation is not mostly misspelling — it is
different words for one concept (``Illegal Property Acquisition`` / ``Assets Beyond Known
Income`` / ``Illicit Enrichment``, ``research/corpus-analysis.md`` §6), and cross-script
duplicates (``Ncell``/``एनसेल``) that no edit-distance metric relates at all. So a
nearest-match would be wrong precisely where it looked most helpful, and a wrong silent
mapping is worse than an unresolved value: the unresolved one shows up as a gap somebody
investigates, while the wrong one becomes a public filter chip nobody questions.

``None`` is therefore a first-class result, not a failure. Its three callers each do
something different and sensible with it:

* the indexer (T24) omits the value from ``tag_ids`` but leaves it in ``keywords``, so
  nothing becomes unsearchable;
* write validation (T13) turns it into a ``400`` naming the offending value;
* the tagger treats it as "no existing term matched", and either picks a different
  one or creates a term (:mod:`case_tags.write`).
"""

from __future__ import annotations

from jawafdehi_shared.tags.normalize import normalize_tag

from case_tags.models import Tag, TagAlias, TagStatus


class TagResolver:
    """Resolves many values against one in-memory snapshot of the vocabulary.

    Built for the indexer, which resolves every tag on every case in a rebuild — a
    per-value query there is thousands of round trips for a table of a few hundred rows.
    Load once, resolve in memory.

    The snapshot is taken at construction and never refreshed, deliberately: an indexing
    run should see one consistent vocabulary from start to finish rather than shifting
    under itself if somebody approves an alias mid-rebuild. Construct a new resolver per
    run.
    """

    def __init__(self) -> None:
        # Canonical ids first. A value that IS already a canonical id must resolve to
        # itself regardless of what the alias table says — see the precedence note in
        # `resolve`.
        self._by_id: dict[str, str] = {}
        self._merged: dict[str, str] = {}
        for tag in Tag.objects.all().select_related("merged_into"):
            self._by_id[tag.id] = tag.id
            if tag.status == TagStatus.MERGED and tag.merged_into_id:
                self._merged[tag.id] = tag.merged_into_id

        # Keys are NORMALIZED on the way into the snapshot, even though
        # ``TagAlias.save`` normalizes on the way into the table. ``bulk_create``,
        # ``QuerySet.update`` and data migrations all bypass model ``save``, so a
        # raw-cased row can exist; without this it would sit in the table looking
        # correct and never resolve, because ``resolve`` looks up the normalized form.
        # ``normalize_tag`` is idempotent, so re-normalizing an already-clean value
        # costs a function call and changes nothing.
        #
        # A collision — two stored rows normalizing to one key but pointing at
        # different terms — is DROPPED rather than resolved first-wins. Which row won
        # would depend on row order, so resolving would make the same query answer
        # differently on two replicas. Dropping it means the value resolves to None,
        # which the callers already handle and which surfaces as a gap somebody
        # investigates. Same rule as the no-fuzzy-fallback one above: refuse rather
        # than guess.
        by_alias: dict[str, str] = {}
        ambiguous: set[str] = set()
        for value, tag_id in TagAlias.objects.values_list("value", "tag_id"):
            key = normalize_tag(value)
            if not key:
                continue
            if key in by_alias and by_alias[key] != tag_id:
                ambiguous.add(key)
            by_alias.setdefault(key, tag_id)
        for key in ambiguous:
            del by_alias[key]
        self._by_alias = by_alias
        self.ambiguous_aliases = ambiguous

    def _follow_merges(self, tag_id: str) -> str | None:
        """Resolve a merged id to its replacement, bounded against cycles.

        Mirrors ``Tag.canonical`` but over the snapshot, so it costs no queries. Returns
        ``None`` on a cycle rather than raising: a bad merge chain is a data problem for
        somebody to fix, and it should not abort an entire index rebuild over one tag.

        Termination comes from ``seen``. A fixed hop cap used to sit here too and was
        removed — it cannot catch anything ``seen`` does not already catch, and its only
        distinct effect is to silently drop a *legitimate* chain longer than the cap,
        turning a resolvable tag into an unresolved one for no reason.
        """
        seen = {tag_id}
        current = tag_id
        while True:
            nxt = self._merged.get(current)
            if nxt is None:
                return current
            if nxt in seen:
                return None
            seen.add(nxt)
            current = nxt

    def resolve(self, raw: str) -> str | None:
        """The canonical tag id for ``raw``, or ``None`` if nothing matches exactly.

        Precedence is canonical id, then alias. If a value is both — which the schema
        permits and which would be a data error — the canonical reading wins, because a
        term shadowing its own slug is the less surprising of two bad outcomes and the
        alternative silently redirects a valid id somewhere else.
        """
        if not isinstance(raw, str):
            return None
        value = normalize_tag(raw)
        if not value:
            return None

        tag_id = self._by_id.get(value) or self._by_alias.get(value)
        if tag_id is None:
            return None
        return self._follow_merges(tag_id)

    def resolve_all(self, raws: list[str]) -> list[str]:
        """Canonical ids for ``raws``, dropping unresolved values and de-duplicating.

        Order-preserving, because the first tag on a case is often the most salient one
        and a caseworker's ordering is information. De-duplicating because two raw values
        collapsing onto one term is the entire point — ``Procurement Irregularities`` and
        ``Procurement`` must not produce that term twice.
        """
        out: list[str] = []
        for raw in raws:
            tag_id = self.resolve(raw)
            if tag_id is not None and tag_id not in out:
                out.append(tag_id)
        return out


def resolve_tag(raw: str) -> str | None:
    """One-shot convenience wrapper. Builds a resolver per call.

    Fine for a write-validation path handling a handful of values per request; wrong for
    the indexer, which should hold a :class:`TagResolver` for the whole run.
    """
    return TagResolver().resolve(raw)
