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
* the alias proposer (T27) turns it into a ``TagProposal`` for a human to tick.
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

        self._by_alias: dict[str, str] = dict(
            TagAlias.objects.values_list("value", "tag_id")
        )

    def _follow_merges(self, tag_id: str) -> str | None:
        """Resolve a merged id to its replacement, bounded against cycles.

        Mirrors ``Tag.canonical`` but over the snapshot, so it costs no queries. Returns
        ``None`` on a cycle rather than raising: a bad merge chain is a data problem for
        somebody to fix, and it should not abort an entire index rebuild over one tag.
        """
        seen = {tag_id}
        current = tag_id
        for _ in range(10):
            nxt = self._merged.get(current)
            if nxt is None:
                return current
            if nxt in seen:
                return None
            seen.add(nxt)
            current = nxt
        return None

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
