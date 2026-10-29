"""Serialization for the material content read plane (``…/content``, ``…/search``).

Two routes. ``…/content?type=…`` addresses the document: three content kinds,
two shapes each — a LIST of stubs, or ONE item in full. ``…/search?q=…`` is the
way IN when you do not yet know what to address: it searches one document's own
pages, tables and figures and returns mixed hits with snippets, each carrying
the page number and key that ``…/content`` takes.

The split is deliberate. ``…/content`` requires a ``type`` and answers with one
kind; a search that mixed kinds could only be expressed there by making ``type``
conditionally optional and the envelope polymorphic. The original design note
(``docs/2026-10-11-material-content-api``) reserved ``search`` as a second route
for exactly this, and reserved the WORD too — see ``materials/urls.py``.

The list/item split on ``…/content`` is forced by size, not taste: one CIAA
report's table markdown comes to 830 KiB and a single table reaches 170 KiB,
and its pages hold the whole transcript, so a listing that inlined either would
be unservable. Figures are the exception and carry their full series in the
list, because the entire 35-report corpus is 6,116 points.

Keys are snake_case — the platform's convention for its non-JSON-LD endpoints
(cf. ``/api/cases/``). This is a projection of a material, not a JSON-LD document
in its own right, so it carries no ``@context``.

What this replaces: API#514's ``/extraction`` manifest, which answered three
questions in one payload (*how much is in here*, *what tables*, *what charts*).
The two listings became their own addressable resources here; the counts are
denormalised onto ``DocumentContent`` ready for the material detail payload,
which is a FOLLOW-UP and not yet wired. Until it lands a client still has to
probe this route to learn whether a document has any content, which
is the one thing the split has not yet bought back.

**No query here pins its database alias.** The router sends ``materials`` to
``ngm`` already, and — unlike an explicit ``using()`` — it also honours the
read-replica flag that ``ReadReplicaRoutingMiddleware`` sets for anonymous
reads. These are the heaviest public payloads in the archive, so they are
exactly the reads that should be eligible for a replica rather than exempt
from it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from django.db.models import Prefetch

from jawafdehi_shared.search.transliterate import (
    matches_search,
    search_fold,
    search_key,
    search_key_uncached,
)

from .models import (
    DocumentContent,
    DocumentFigure,
    DocumentFigurePoint,
    DocumentPage,
    DocumentTable,
)

#: The ``type`` values ``…/content`` accepts. ``page`` is addressed by ``num``
#: (the printed folio) or by a ``from``/``to`` range; ``table`` and ``figure`` by
#: ``id`` (their stable key).
CONTENT_TYPES = ("page", "table", "figure")

#: How many pages one ``from``/``to`` request may return.
#:
#: A page range is the only call on this plane that can grow without bound: the
#: CIAA corpus averages ~1.3 KB of text per page (13 MB over 10,015 pages), so 50
#: pages is ~65 KB — the same order as the 129 KiB worst case the old manifest
#: already served — while the whole 446-page report would be ~580 KB.
#:
#: Over the cap the response is a 400 that names it, NOT a silent truncation. A
#: caller that asked for pages 30-200 and received 30-79 without being told has no
#: way to know its answer is missing three quarters of the evidence, and anything
#: summarising that result will report the gap as an absence of findings.
MAX_PAGE_SPAN = 50


# ── pages ────────────────────────────────────────────────────────────────────


def _page_stub(page: DocumentPage) -> dict:
    """A page without its text — enough to navigate, and to know what is missing.

    ``coverage_state`` and ``chars`` ride on the stub deliberately. A client
    listing 446 pages needs to see which of them we could not read BEFORE it
    fetches one and gets an empty string back, or it will present a failed OCR
    as a blank page.
    """
    return {
        "page_no": page.page_no,
        "uid": page.uid,
        "coverage_state": page.coverage_state,
        "anchor_fidelity": page.anchor_fidelity,
        "ocr_source": page.ocr_source,
        "landscape": page.landscape,
        "n_table_blocks": page.n_table_blocks,
        "chars": page.chars,
    }


def page_payload(page: DocumentPage) -> dict:
    """One page in full: the stub plus the recovered text."""
    return {**_page_stub(page), "text": page.text}


# ── tables ───────────────────────────────────────────────────────────────────


def _table_stub(table: DocumentTable) -> dict:
    """A table without its markdown — enough to list, label and link it."""
    return {
        # ``ordinal`` is presentation order and is recomputed on every ingest;
        # ``key`` is what a client must address a table by. See the model.
        "ordinal": table.ordinal,
        "key": table.key,
        "uid": table.uid,
        "page_no": table.page_no,
        "index_on_page": table.index_on_page,
        "caption": table.caption,
        "header": table.header,
        "n_rows": table.n_rows,
        "n_cols": table.n_cols,
        "fidelity": table.fidelity,
    }


def table_payload(table: DocumentTable) -> dict:
    """One table in full: the stub plus the markdown a client renders."""
    return {**_table_stub(table), "markdown": table.markdown}


# ── figures ──────────────────────────────────────────────────────────────────


def figure_payload(figure: DocumentFigure) -> dict:
    """One chart and its series.

    ``estimated`` rides on every point rather than being summarised at the figure
    level: a single chart routinely mixes printed values with ones read off the
    image, and collapsing that to one per-figure flag would misreport both kinds.
    """
    return {
        "ordinal": figure.ordinal,
        "key": figure.key,
        "uid": figure.uid,
        "page_no": figure.page_no,
        "index_on_page": figure.index_on_page,
        "title": figure.title,
        "chart_type": figure.chart_type,
        "unit": figure.unit,
        "x_axis": figure.x_axis,
        "y_axis": figure.y_axis,
        "notes": figure.notes,
        "verify_note": figure.verify_note,
        "verified": figure.verified,
        "points": [
            {
                "point_index": point.point_index,
                "label": point.label,
                "series": point.series,
                "value": point.value,
                "estimated": point.is_estimated,
            }
            # Pre-fetched by the queryset below; iterating the related manager
            # here would be an N+1 across every figure in the listing.
            for point in figure.points.all()
        ],
    }


# ── queries ──────────────────────────────────────────────────────────────────


def content_for(iri: str) -> DocumentContent | None:
    """The content row for ``iri``, or None.

    Callers MUST have resolved the material's visibility first — this applies no
    gate of its own (see ``materials.views.material_content``).
    """
    return DocumentContent.objects.filter(pk=iri).first()


def list_pages(iri: str) -> list[dict]:
    """Every page stub. ``defer("text")`` is the point: without it this reads the
    document's entire transcript out of Postgres to serialize none of it."""
    rows = DocumentPage.objects.filter(content_id=iri).defer("text")
    return [_page_stub(p) for p in rows.order_by("page_no")]


def get_page(iri: str, page_no: int) -> DocumentPage | None:
    return DocumentPage.objects.filter(content_id=iri, page_no=page_no).first()


def get_page_range(iri: str, first: int, last: int) -> list[dict]:
    """Pages ``first``..``last`` inclusive, each with its text.

    Inclusive because a page range in a citation is: "pages 30–32" is three
    pages, and a reader asking for one page as ``from=30&to=30`` should get it.
    The caller is responsible for bounding the span — see ``MAX_PAGE_SPAN``.
    """
    rows = DocumentPage.objects.filter(
        content_id=iri, page_no__gte=first, page_no__lte=last
    )
    return [page_payload(p) for p in rows.order_by("page_no")]


def list_tables(iri: str, query: str | None = None) -> list[dict]:
    """Every table stub.

    ``defer("markdown")`` is load-bearing, not a micro-optimisation: a bare query
    would pull every table's markdown — ~830 KiB for the largest CIAA report —
    out of the database only for the serializer to drop it again.
    """
    rows = DocumentTable.objects.filter(content_id=iri).defer("markdown")
    return [
        _table_stub(t)
        for t in rows.order_by("ordinal")
        if _matches(t.caption, t.page_no, query)
    ]


def get_table(iri: str, key: str) -> DocumentTable | None:
    return DocumentTable.objects.filter(content_id=iri, key=key).first()


def _figures_queryset(iri: str):
    """Figures for one document, with their points pre-fetched.

    The Prefetch names an explicitly ORDERED queryset, not an alias: points are
    rendered in series order and a bare prefetch would leave that to the
    database. Routing is the router's job — see the module docstring.
    """
    points = DocumentFigurePoint.objects.order_by("point_index", "id")
    return (
        DocumentFigure.objects.filter(content_id=iri)
        .prefetch_related(Prefetch("points", queryset=points))
        .order_by("ordinal")
    )


def list_figures(iri: str, query: str | None = None) -> list[dict]:
    """Every figure with its full series inline.

    Inlined because they are small — 6,116 points across the entire 35-report
    corpus — which is why #514's manifest inlined them too. The asymmetry with
    tables is deliberate and measured.
    """
    return [
        figure_payload(f)
        for f in _figures_queryset(iri)
        if _matches(f.title, f.page_no, query)
    ]


def get_figure(iri: str, key: str) -> DocumentFigure | None:
    return _figures_queryset(iri).filter(key=key).first()


def _matches(text: str, page_no: int, query: str | None) -> bool:
    """Does one listing row answer to ``query``?

    **Applied in Python, deliberately.** The match is phonetic (see
    ``jawafdehi_shared.search.transliterate``) and no amount of SQL will tell
    Postgres that "vivaran" is विवरण. The listings this runs over are bounded by
    one document — 250 tables and 49 figures in the largest report in the
    archive — so the set is small and already being walked.

    Checked BEFORE the row is serialized, so a narrowed figure listing does not
    build the points of the 48 figures it is about to discard.

    **The page number matches too.** 17% of tables carry no caption at all (61%
    in the 35th report), and for those the folio is the only handle a reader
    has; excluding them from every text query would put a sixth of the archive
    out of reach of the one control on screen. Exact, not a substring: in a
    table of contents a number means "take me to that page", and "3" matching
    pages 3, 13, 30 and 300 answers a question nobody asked.

    Delegates to :func:`locate_match`, which decides the same question and also
    reports *where* it matched. One definition rather than two: the listing
    filter and the document search have to agree about what a hit is, or ``q``
    would narrow a caption listing to rows that ``…/search`` then says do not
    match.
    """
    if query is None or not query.strip():
        return True
    return locate_match(text, page_no, query) is not None


# ── matching ─────────────────────────────────────────────────────────────────


#: Every value :attr:`Span.how` can take, and therefore every value the ``match``
#: field of a search hit can carry. Named here rather than in the schema module
#: so the published enum is read off the matcher rather than restated beside it.
MATCH_KINDS = ("literal", "phonetic", "folio")


@dataclass(frozen=True)
class Span:
    """Where in a string a query matched, and on what grounds.

    ``start``/``end`` are offsets into the ORIGINAL string, so a snippet cut
    with them is text the document really contains. ``-1`` means the row
    matched but no span in its text did — a bare-folio hit matches the page
    *number*, and pointing at a span of prose would claim the words are there.
    """

    #: ``literal`` (the query is in the text), ``phonetic`` (it is there in
    #: sound only — "ujuri" matching उजुरी), or ``folio`` (the page number).
    how: str
    start: int = -1
    end: int = -1

    @property
    def located(self) -> bool:
        return self.start >= 0


#: Splits on whitespace, for walking a haystack word by word with offsets.
_WORDS = re.compile(r"\S+")

#: Any run of whitespace, collapsed when a snippet is cut. Page text carries the
#: document's own line breaks and a table carries markdown pipes-and-newlines;
#: a snippet is one line of context, not a re-rendering of the layout.
_WHITESPACE = re.compile(r"\s+")


def _literal_span(haystack: str, needle: str) -> Span | None:
    """The first case-insensitive occurrence of ``needle``, or None.

    Mirrors :func:`matches_search`'s own literal test (``casefold`` on both
    sides) so the two cannot disagree about whether there is a literal hit.

    ``casefold`` can CHANGE LENGTH — "ß" folds to "ss" — and an offset into the
    folded string then does not address the same character in the original, so
    a snippet cut with it would be off by one per expansion. Where that has
    happened a case-sensitive find is tried instead, and failing that the hit
    is returned UNLOCATED rather than downgraded to ``phonetic``: it really is
    a literal match, and calling it a sound-alike would understate it. Only the
    span is lost, and the snippet falls back to the opening of the field.
    Devanagari is caseless and ASCII folds one-to-one, so this guards the rest
    of Unicode rather than any path this corpus takes.
    """
    folded = haystack.casefold()
    folded_needle = needle.casefold()
    if folded_needle not in folded:
        return None
    if len(folded) != len(haystack):
        at = haystack.find(needle)
        return Span("literal", at, at + len(needle)) if at >= 0 else Span("literal")
    at = folded.find(folded_needle)
    return Span("literal", at, at + len(folded_needle))


def _phonetic_span(haystack: str, needle: str) -> Span:
    """Where a sound-match landed — the first word that answers to the query.

    ``matches_search`` has already said this string matches; the folded form it
    compared has no offsets back into the original (transliteration is not
    length-preserving), so the span is recovered by folding the haystack a word
    at a time until one carries a query token. ``search_key`` is LRU-cached and
    this runs only over strings already known to match, so it is paid once per
    hit rather than once per row.

    Falls back to an unlocated span rather than guessing: a token can match
    across a word boundary, and a snippet centred on the wrong word is worse
    than one that says it is the opening of the match.
    """
    tokens = [t for t in search_fold(needle).split() if t]
    if tokens:
        for word in _WORDS.finditer(haystack):
            key = search_key(word.group())
            if any(token in key for token in tokens):
                return Span("phonetic", word.start(), word.end())
    return Span("phonetic")


def _span_for(haystack: str, needle: str) -> Span | None:
    """``needle`` located in ``haystack``, literally or in sound, else None."""
    located = _literal_span(haystack, needle)
    if located is not None:
        return located
    if matches_search(haystack, needle):
        return _phonetic_span(haystack, needle)
    return None


def locate_match(text: str, page_no: int, query: str | None) -> Span | None:
    """Where ``query`` matches ``text`` (or its page number), else None.

    The single definition of what a hit is on this plane — see :func:`_matches`,
    which is this with the span thrown away, and ``search_document``, which
    keeps it to cut a snippet.

    The order is the one ``_matches`` has always had, and it is load-bearing:
    an exact folio first (so "3" means page 3), then the text, then the
    word-plus-folio form. Only an EXACT folio token is taken that way, so the
    precision of a bare number survives.
    """
    if query is None or not query.strip():
        return None
    needle = query.strip()
    haystack = text or ""
    folio = str(page_no)
    if needle == folio:
        return Span("folio")

    located = _span_for(haystack, needle)
    if located is not None:
        return located

    # "ujuri 31" — a word and a folio, which is how someone narrows a long
    # listing they have already filtered once.
    tokens = needle.split()
    if len(tokens) < 2 or folio not in tokens:
        return None
    rest = [t for t in tokens if t != folio]
    return _span_for(haystack, " ".join(rest)) if rest else None


# ── search inside one document ───────────────────────────────────────────────
#
# **Postgres, not OpenSearch, and that is a decision rather than an expedient.**
# The index would have to be a fragment index — one document per page, table and
# figure — and three things make that the wrong price for searching inside ONE
# document: a new string field must be PUT into the mapping before the indexer
# writes it or the whole of unified search 503s, and that failure is one-way;
# the indices are aliases over generations, so the mapping work has to precede
# any indexer change; and ``reindex_materials`` is already the operationally
# expensive job in the estate. None of that is worth paying to scan a corpus
# bounded by one document — ~700 pages at the archive's worst.
#
# What that costs is honest to state: the scan reads the matched document's
# transcript out of Postgres, ~1.9 MB for the largest report, to return ~10 KB
# of snippets. It is streamed (see ``_page_hits``) so the memory is a chunk at a
# time, and it is the whole document rather than a prefix because a hit on page
# 400 of a 446-page report is exactly the hit a reader cannot find any other way.

#: How many hits one search returns. Over the cap the response says so and
#: reports the true ``total``, so a caller narrowing a common term can see that
#: it is common rather than believing it found everything.
MAX_SEARCH_HITS = 50

#: Characters of context kept either side of the matching span. A snippet is
#: ~190 characters, which is a sentence of Devanagari — enough to judge whether
#: a hit is the one you want, and nowhere near enough to be a page.
SNIPPET_CONTEXT = 90

#: Pages pulled per round trip while scanning. Bounds the memory a scan holds:
#: the default of 2,000 would be ~5 MB of transcript resident at once.
PAGE_SCAN_CHUNK = 100

#: Reported order when two hits sit on the same page — the page first, then what
#: is printed on it. A reader who searched for a term wants the prose that
#: mentions it before the table it is tabulated in.
_KIND_ORDER = {"page": 0, "table": 1, "figure": 2}

#: Longest ``caption``/``title`` carried alongside a hit. The field is
#: unbounded in the model (``DocumentTable.caption`` records an observed 1.3 kB,
#: though this corpus's largest is 88 characters), and the MCP tool declines a
#: text ceiling on the grounds that a search response is small by construction.
#: That is only true if everything on a hit is bounded, so it is bounded here
#: rather than asserted there.
MAX_LABEL_CHARS = SNIPPET_CONTEXT * 2


def _label(text: str) -> str:
    """A caption or title, clipped to ``MAX_LABEL_CHARS`` with the cut marked."""
    body = (text or "").strip()
    if len(body) <= MAX_LABEL_CHARS:
        return body
    return body[:MAX_LABEL_CHARS] + "…"


class DocumentScan:
    """One query, applied across one document's rows.

    Built once per request rather than per row, for two reasons that are both
    about the LONG fields — a page's transcript and a table's markdown, which
    are a thousand times the size of the captions this matching was written for.

    **It does not fold long text through the shared cache.**
    ``search_key``'s ``lru_cache`` is bounded at 8192 ENTRIES, not bytes, and is
    sized for short captions. Folding pages through it costs ~6 kB a slot, so
    ~18 large documents fill it permanently — and it evicts the caption working
    set the listing filter depends on. The walk below keeps its own ``dict``
    instead, which holds one document's vocabulary of short words and is freed
    with the request.

    **It folds a word at a time, not the whole field.** Measured on 50 real
    pages of the 35th CIAA report: ``matches_search`` over whole pages is 2.77 s
    extrapolated to the full 446, the walk is 1.18 s, and the two agree on every
    query tried. The win is that words repeat — 3,745 distinct across 50 pages —
    so the transliteration is paid once each rather than once per page.

    A query with no Latin in it never reaches any of this: ``search_fold`` of
    pure Devanagari is empty, so there are no phonetic tokens and the literal
    pass is the whole search (~0.01 s for the same 446 pages).
    """

    def __init__(self, query: str) -> None:
        self.query = query.strip()
        tokens = self.query.split()
        # **An ASCII number is a page, not a string to find in the prose.**
        # ``_matches`` says this about a LISTING and means it: a caption is a
        # label, and "तालिका ४.२" carrying a number is part of the label. Page
        # text is not a label — it is prose full of incidental digits, and
        # ``q=31`` substring-matching "रु. 31,45,000" buries the page the
        # reader asked for under every currency figure in the report. So on
        # THIS route a number addresses a folio and nothing else.
        #
        # ASCII-only: ``str.isdigit()`` is true of १९४८८ too, and a Devanagari
        # numeral can never equal ``str(page_no)``. Treating it as a folio
        # would silently make the archive's own numerals unsearchable.
        self.folios = {t for t in tokens if t.isascii() and t.isdigit()}
        self.words = " ".join(t for t in tokens if t not in self.folios)
        self.tokens = [t for t in search_fold(self.words).split() if t]
        self._folds: dict[str, str] = {}

    @property
    def is_empty(self) -> bool:
        return not self.query

    def _fold(self, word: str) -> str:
        key = self._folds.get(word)
        if key is None:
            key = self._folds[word] = search_key_uncached(word)
        return key

    def _walk(self, haystack: str) -> Span | None:
        """All query tokens present in SOME word, else None.

        Token-AND like :func:`matches_search`, and order-free for the same
        reason: "ujuri badhi" finding nothing while "badhi ujuri" found
        eighteen rows reads to a reader as the archive not holding it.

        The span is the FIRST word that answered, which is where a snippet
        should be centred.
        """
        remaining = set(self.tokens)
        first = None
        for word in _WORDS.finditer(haystack):
            key = self._fold(word.group())
            hit = {token for token in remaining if token in key}
            if not hit:
                continue
            if first is None:
                first = word
            remaining -= hit
            if not remaining:
                return Span("phonetic", first.start(), first.end())
        return None

    def _in_text(self, haystack: str) -> Span | None:
        """``self.words`` located in ``haystack`` — literally, then by sound."""
        located = _literal_span(haystack, self.words)
        if located is not None:
            return located
        return self._walk(haystack) if self.tokens else None

    def locate(self, text: str, page_no: int) -> Span | None:
        """Where this query matches one row, or None.

        Three shapes, and the folio rule decides between them:

        * no number in the query → find the words in the text
        * a number alone → that page, and nothing about its text
        * words and a number → those words, on that page only

        The third is the ``ujuri 31`` form, and here it means what it says. On
        the listing filter it degrades to "either", because a caption listing
        has nowhere else to put the constraint; on a search over prose,
        "either" would return every page that happens to mention the word.
        """
        if self.is_empty:
            return None
        if self.folios:
            if str(page_no) not in self.folios:
                return None
            return self._in_text(text or "") if self.words else Span("folio")
        return self._in_text(text or "")


def snippet_for(text: str, span: Span, context: int = SNIPPET_CONTEXT) -> str:
    """A short, single-line window of ``text`` around ``span``.

    Whitespace is collapsed because the stored text carries the document's own
    line breaks (and a table carries markdown rules), and a snippet is one line
    of context rather than a re-rendering of the page.

    An ellipsis marks each end that was cut, so a caller can tell a snippet that
    IS the whole field from one that is a window into something longer — which
    is the difference between quoting a table caption and quoting 190 characters
    of a 4,000-character page.
    """
    body = text or ""
    if not body:
        return ""
    if span.located:
        start = max(0, span.start - context)
        end = min(len(body), span.end + context)
    else:
        # No span to centre on (a folio match, or a sound match we could not
        # pin to a word). The opening of the field is honest about being the
        # opening; a window somewhere in the middle would read as the match.
        start, end = 0, min(len(body), context * 2)
    piece = _WHITESPACE.sub(" ", body[start:end]).strip()
    if not piece:
        return ""
    return f"{'…' if start > 0 else ''}{piece}{'…' if end < len(body) else ''}"


def _hit(kind: str, page_no: int, key: str | None, field: str, span: Span,
         text: str, **extra) -> dict:
    """One search result: what matched, where it is, and how to fetch it whole.

    ``match`` rides on every hit for the same reason ``estimated`` rides on
    every figure point: a phonetic hit is a weaker claim than a literal one and
    a folio hit is not a claim about the text at all, and a client that renders
    the three identically presents a guess as a quotation.
    """
    return {
        "kind": kind,
        "page_no": page_no,
        # Pages have no key — they are addressed by their printed folio. Present
        # and null rather than absent, so a client can read ``key`` off every
        # hit without branching on the kind first.
        "key": key,
        "field": field,
        "match": span.how,
        "snippet": snippet_for(text, span),
        **extra,
    }


def _first_hit(kind: str, page_no: int, key: str | None, fields: list[tuple[str, str]],
               scan: DocumentScan, **extra) -> dict | None:
    """The first of ``fields`` that answers to ``scan``, as a hit.

    One hit per row, not one per field: a table whose caption and markdown both
    mention the term is one table, and reporting it twice would make a listing
    of 12 tables look like 24 findings. The field order is the caller's and is
    most-citable-first — a caption is a better quotation than the markdown row
    it introduces.
    """
    for name, text in fields:
        span = scan.locate(text, page_no)
        if span is not None:
            return _hit(kind, page_no, key, name, span, text, **extra)
    return None


def _page_hits(iri: str, scan: DocumentScan) -> list[dict]:
    """Every page whose TEXT answers to ``query``.

    This is the gap the endpoint exists to close: a caller could already find a
    table by its caption and could not find a paragraph by what it says.

    ``only()`` + ``iterator()`` rather than a plain queryset — the opposite of
    the ``defer("text")`` the page LISTING uses. The listing must not read the
    transcript because it serializes none of it; this must read it because the
    text is the thing being searched, and the streaming is what keeps a
    446-page report from being resident all at once.
    """
    rows = (
        DocumentPage.objects.filter(content_id=iri)
        .only("page_no", "text", "coverage_state", "ocr_source")
        .order_by("page_no")
    )
    hits = []
    for page in rows.iterator(chunk_size=PAGE_SCAN_CHUNK):
        hit = _first_hit(
            "page",
            page.page_no,
            None,
            [("text", page.text)],
            scan,
            # Both ride along for the reason the page stub carries them: a hit
            # on a page read by a vision model is a weaker claim than one read
            # from the PDF's own text layer, and an empty-text page that
            # matched its folio is held-but-unread rather than blank.
            coverage_state=page.coverage_state,
            ocr_source=page.ocr_source,
        )
        if hit is not None:
            hits.append(hit)
    return hits


def _table_hits(iri: str, scan: DocumentScan) -> list[dict]:
    """Every table whose caption OR markdown answers to ``query``.

    The markdown is searched, which is what puts a figure inside a table within
    reach — ``घुस`` in a bribery table's rows is a hit even when its caption
    says only "तालिका ४.२". The caption is tried first because it is the better
    quotation of the two.
    """
    rows = (
        DocumentTable.objects.filter(content_id=iri)
        .only("key", "page_no", "caption", "markdown", "fidelity")
        .order_by("ordinal")
    )
    hits = []
    for table in rows.iterator(chunk_size=PAGE_SCAN_CHUNK):
        hit = _first_hit(
            "table",
            table.page_no,
            table.key,
            [("caption", table.caption), ("markdown", table.markdown)],
            scan,
            caption=_label(table.caption),
            fidelity=table.fidelity,
        )
        if hit is not None:
            hits.append(hit)
    return hits


def _figure_hits(iri: str, scan: DocumentScan) -> list[dict]:
    """Every figure whose title OR notes answer to ``query``.

    Deliberately NOT ``_figures_queryset``: that one prefetches every point to
    serialize the series, and a search returns a snippet rather than a chart.

    ``notes`` is searched because it is the transcriber's audit trail — how a
    disputed value was resolved, which total it reconciles against — and that
    is frequently where a chart's subject is actually named. The points are not:
    a label is a word or two with no context to snippet, and a figure whose
    series mentions the term is reachable through its title or its page.
    """
    rows = (
        DocumentFigure.objects.filter(content_id=iri)
        .only("key", "page_no", "title", "notes")
        .order_by("ordinal")
    )
    hits = []
    for figure in rows.iterator(chunk_size=PAGE_SCAN_CHUNK):
        hit = _first_hit(
            "figure",
            figure.page_no,
            figure.key,
            [("title", figure.title), ("notes", figure.notes)],
            scan,
            title=_label(figure.title),
        )
        if hit is not None:
            hits.append(hit)
    return hits


def search_document(iri: str, query: str, *, limit: int = MAX_SEARCH_HITS) -> dict:
    """Search one document's own content. Mixed hits, in document order.

    Pages, tables and figures come back TOGETHER rather than as three listings,
    because the question a reader asks is "where does this document discuss
    bribery", and the answer is a paragraph on page 294 and the table beside it.
    Three separate calls would make the caller reassemble that, and a caller
    that only knew to ask for tables would never learn the paragraph exists.

    Ordered by page, not by relevance. There is no scorer here and inventing a
    rank order would be a claim we cannot support; document order is one a
    reader can check against the printed PDF, and it puts a hit and the table
    that belongs to it next to each other.

    Counted BEFORE truncation. ``total`` is how many hits the document really
    holds and ``hits`` is at most ``limit`` of them — a truncated result that
    reported the truncated count would tell a caller searching a common term
    that it is rare.

    **The redacted text is what is searched**, and there is nothing to opt out
    of: ``DocumentPage.text`` and ``DocumentTable.markdown`` ARE the stored,
    published text, and the unredacted original lives in ``Material.source_text``
    behind ``materials.view_unredacted``. Nothing here reads that column, so a
    search cannot surface an identifier that ``?type=page`` would not already
    have served.
    """
    scan = DocumentScan(query)
    hits = _page_hits(iri, scan) + _table_hits(iri, scan) + _figure_hits(iri, scan)
    hits.sort(key=lambda h: (h["page_no"], _KIND_ORDER[h["kind"]], h["key"] or ""))
    total = len(hits)
    return {
        "total": total,
        "truncated": total > limit,
        "count": min(total, limit),
        "hits": hits[:limit],
    }


def _variant_digest(variant: str) -> str:
    """A short ASCII digest of the request shape, for the ETag.

    Hashed rather than embedded verbatim because an ETag is an HTTP header
    value and ``q`` carries Devanagari: Django MIME-encodes a non-ASCII header
    (``=?utf-8?b?…?=``), which is not a valid entity-tag, and a client echoing
    that form back would never match the raw tag we recompute — so conditional
    GET would silently stop working for exactly the queries this endpoint
    exists to serve. A digest is also bounded, so a long query cannot grow the
    header, and it keeps a reader's search terms out of proxy logs.

    Not a security boundary: a collision costs one stale 304 between two
    representations of the same document, so 64 bits is ample.
    """
    return hashlib.blake2b(variant.encode("utf-8"), digest_size=8).hexdigest()


def version_token(content: DocumentContent, variant: str = "") -> str:
    """A weak ETag for one content RESPONSE.

    ``ingested_at`` covers the document: the ingest replaces a document's
    content wholesale inside one transaction, so nothing about any of its rows
    can change without that timestamp moving.

    ``variant`` covers *which* response. One URL serves many representations —
    the table listing, the figure listing, one page, a span of pages, and now a
    narrowed listing per ``q`` — and an ETag identifies a representation, not a
    resource (RFC 9110 §8.8.1). Keyed on the timestamp alone, a caller echoing
    the tag from ``?type=table`` got a 304 for ``?type=figure`` and reused the
    tables as if they were the charts. Browsers key their cache by URL and so
    were mostly spared; anything echoing a tag by hand was not.
    """
    suffix = f":{_variant_digest(variant)}" if variant else ""
    return f'W/"{content.ingested_at.timestamp():.6f}{suffix}"'


def content_summary(content: DocumentContent) -> dict:
    """What is inside this document, for the material detail payload.

    Reports the DENORMALISED counters, which come from the upstream document row
    rather than from ``COUNT`` over ours — see ``DocumentContent``. That is what
    keeps this cheap enough to sit on the hottest read in the archive: one LEFT
    JOIN, no aggregate, and nothing extra at all for the ~346k materials that
    have no stored content.

    ``pages_read`` is not ``pages``. A page we hold but could not transcribe is
    still a page of the document, and a reader comparing the two sees how much of
    the document is actually quotable. Collapsing them would present an
    untranscribed report as an empty one.
    """
    return {
        "pages": content.page_count,
        "pages_read": content.pages_with_text,
        "tables": content.n_tables,
        "figures": content.n_figures,
        "points": content.n_points,
        "provenance": {
            "doc_id": content.doc_id,
            "dataset": content.dataset,
            "dataset_revision": content.dataset_revision,
            "text_source": content.text_source,
            "transcript_verdict": content.transcript_verdict,
            "figures_verdict": content.figures_verdict,
            "ingested_at": content.ingested_at.isoformat(),
        },
    }
