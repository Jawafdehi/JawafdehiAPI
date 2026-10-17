"""The shape a producer hands the writer: one document's extracted content.

Every producer — the parquet ingest, the PDF extractor, and whatever reads the
next corpus — builds a :class:`ContentBundle`, and
``materials.document_content.store_document_content`` is the only thing that
turns one into rows. That indirection exists because the two jobs have different
owners: a producer knows another project's column names and NaN conventions, and
the writer knows ours (field widths, the delete-then-insert contract, the
material's ETag). Collapsing them is how the archive ended up with keys computed
in a management command.

**This is also a wire format.** ``as_dict`` / ``from_dict`` round-trip through
JSON, so an extraction can be produced in one process and written in another, or
dumped to disk for review before a single row is written.

Vocabularies
------------
``coverage_state``, ``anchor_fidelity``, ``ocr_source``, ``text_source`` and the
two verdicts are free-form on the model on purpose: the first corpus brought its
own vocabulary and pinning another project's verdict strings to a ``TextChoices``
would turn their next added value into our 500. The values *this package* emits
are fixed, and listed here because the API hands them to clients verbatim:

``coverage_state``
    ``anchored`` text came off the page itself · ``ocr_vision`` text was read
    from an image of the page by a model · ``blank`` the page carries nothing at
    all · ``unread`` the page carries something we did not read · ``gap``
    extracting this page failed.
``anchor_fidelity``
    ``tight`` the text layer decoded cleanly · ``repaired`` it came through a
    legacy-font remap · ``suspect`` the text layer decoded to something that does
    not read as Nepali · ``ocr_vision`` read off an image.
``transcript_verdict``
    ``clean`` every page we hold was read or is genuinely blank · ``partial``
    some pages are ``unread`` · ``unread`` no page produced text.
``figures_verdict``
    ``none_detected`` no page looks like it carries a chart · ``not_attempted``
    pages do, and nothing has read them · ``read`` the vision tier ran.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

#: ``dataset`` for content this package extracted, as opposed to a corpus loaded
#: from someone else's dataset. Versioned: a change to how pages or tables are
#: derived is a new value, so a stored row says which extractor produced it
#: without anyone having to date it against a deploy.
PDF_EXTRACTOR_DATASET = "jawafdehi:pdf-extract/v1"


def content_key(page_no: int, index_on_page: int, kind: str) -> str:
    """The stable, URL-safe handle for a table or figure: ``p0018-t1``.

    **The one definition.** This is what ``…/content?type=table&id=`` resolves,
    so it is a public URL and may not drift between producers. It is built from
    where the thing physically sits in the document, which is what makes it
    survive the next revision: ``ordinal`` is presentation order, recomputed on
    every ingest, so a revision that recovers a table missed earlier in the
    report shifts every later one — and a reader's saved link then resolves to a
    *different* table. A 200 with the wrong content is worse than a 404.
    """
    return f"p{page_no:04d}-{kind}{index_on_page}"


def _coerce(cls, raw: Any):
    """Build ``cls`` from a dict, ignoring keys it does not declare.

    Unknown keys are dropped rather than raised on: this type is the boundary
    between a producer and the writer, and a bundle written by a newer extractor
    must degrade to "the fields we both understand" instead of failing a whole
    corpus on a field nobody reads yet.
    """
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (raw or {}).items() if k in known})


@dataclass(slots=True)
class PageRecord:
    """One page of the document and the text recovered from it.

    ``page_no`` is the printed folio as the producer reports it, not a position
    in our rows: it is what makes a citation checkable against the published PDF.

    ``text`` empty is ambiguous on its own and must be read with
    ``coverage_state`` — ``blank`` says the page has nothing, ``unread`` says we
    did not read it. A client that renders the two identically presents a failed
    read as a blank page.
    """

    page_no: int
    uid: str = ""
    text: str = ""
    coverage_state: str = ""
    anchor_fidelity: str = ""
    ocr_source: str = ""
    landscape: bool = False
    n_table_blocks: int = 0
    chars: int = 0


@dataclass(slots=True)
class TableRecord:
    """One table, kept as markdown because the published artefact is a rendered
    table and a second cell-grid representation would be one more thing to keep
    in step for no reader benefit.

    ``index_on_page`` is 1-based and, with ``page_no``, is what
    :func:`content_key` builds the public handle from.
    """

    page_no: int
    index_on_page: int = 1
    uid: str = ""
    caption: str = ""
    header: list = field(default_factory=list)
    n_rows: int = 0
    n_cols: int = 0
    fidelity: str = ""
    markdown: str = ""


@dataclass(slots=True)
class FigurePointRecord:
    """One datum of one chart.

    ``estimated`` is the load-bearing field: a value read off an axis is a weaker
    claim than one printed on the mark, and it rides per point because a single
    chart routinely mixes the two.
    """

    point_index: int
    label: str = ""
    series: str = ""
    value: float | None = None
    estimated: bool = False

    @classmethod
    def from_dict(cls, raw: dict) -> FigurePointRecord:
        return _coerce(cls, raw)


@dataclass(slots=True)
class FigureRecord:
    """One chart and its series."""

    page_no: int
    index_on_page: int = 1
    uid: str = ""
    title: str = ""
    chart_type: str = ""
    unit: str = ""
    x_axis: str = ""
    y_axis: str = ""
    notes: str = ""
    verify_note: str = ""
    verified: bool | None = None
    points: list[FigurePointRecord] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict) -> FigureRecord:
        figure = _coerce(cls, raw)
        figure.points = [
            p if isinstance(p, FigurePointRecord) else FigurePointRecord.from_dict(p)
            for p in (raw or {}).get("points") or []
        ]
        return figure


@dataclass(slots=True)
class DeclaredCounts:
    """What the producer's own source says this document contains.

    Only set when a producer has an independent witness — the parquet ingest has
    one (the dataset's ``documents`` row), and the PDF extractor does not, being
    the only thing that has looked. When it is set the writer stores *these*
    numbers rather than counting the rows it wrote, so that a shortfall in our
    ingest cannot redefine itself as the truth; the caller compares the two and
    reports the gap.
    """

    tables: int = 0
    figures: int = 0
    points: int = 0
    pages_with_text: int = 0


@dataclass(slots=True)
class ContentBundle:
    """One document's extracted content, ready to be written.

    ``chart_pages`` and ``redactions`` are the exceptions: they are facts about
    the run rather than content, and are NOT persisted. It records which pages look like they carry a chart so the vision
    tier has a bounded list to work through. Persisting them as figures with no
    points would be the lie this schema exists to avoid — a chart listed with an
    empty series reads as "we read this and it had no data".
    """

    doc_id: str
    dataset: str
    dataset_revision: str = ""
    page_count: int = 0
    text_source: str = ""
    transcript_verdict: str = ""
    figures_verdict: str = ""
    pages: list[PageRecord] = field(default_factory=list)
    tables: list[TableRecord] = field(default_factory=list)
    figures: list[FigureRecord] = field(default_factory=list)
    declared: DeclaredCounts | None = None
    chart_pages: list[int] = field(default_factory=list)
    #: Personal-data removals per class, from ``materials.extraction.redact``.
    #: Counts only — never a match and never a location — so this is safe to log
    #: and to put in a job result. Not persisted, for the same reason
    #: ``chart_pages`` is not: it describes the RUN, not the document.
    redactions: dict = field(default_factory=dict)

    @property
    def pages_with_text(self) -> int:
        """How much of the document was actually READ — not how much we hold."""
        return sum(1 for page in self.pages if page.text.strip())

    @property
    def n_points(self) -> int:
        return sum(len(figure.points) for figure in self.figures)

    def summary(self) -> dict[str, int]:
        """Row counts, for a log line or a job result. Cheap enough to call."""
        return {
            "pages": len(self.pages),
            "pages_with_text": self.pages_with_text,
            "tables": len(self.tables),
            "figures": len(self.figures),
            "points": self.n_points,
            "chart_pages": len(self.chart_pages),
        }

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> ContentBundle:
        bundle = _coerce(cls, raw)
        bundle.pages = [
            p if isinstance(p, PageRecord) else _coerce(PageRecord, p)
            for p in (raw or {}).get("pages") or []
        ]
        bundle.tables = [
            t if isinstance(t, TableRecord) else _coerce(TableRecord, t)
            for t in (raw or {}).get("tables") or []
        ]
        bundle.figures = [
            f if isinstance(f, FigureRecord) else FigureRecord.from_dict(f)
            for f in (raw or {}).get("figures") or []
        ]
        declared = (raw or {}).get("declared")
        bundle.declared = (
            declared
            if isinstance(declared, DeclaredCounts) or declared is None
            else _coerce(DeclaredCounts, declared)
        )
        return bundle
