"""Published response shapes for the material content read plane.

``…/content`` and ``…/search`` are plain ``@api_view`` functions that build
dicts by hand (see ``materials.content_api``), so drf-spectacular has no
serializer to introspect and published them as *"No response body"*. Their
query surface was already declared — ``CONTENT_PARAMETERS`` / ``SEARCH_PARAMETERS``
in ``materials.views`` — which left the schema in the worst of both states: it
told a client exactly how to ASK and nothing about what came back. These are the
answers, declared the same way and for the same reason.

**Nothing here serializes anything.** Every class in this module is
documentation: the response bodies are still built by the payload functions in
``materials.content_api``, which defer the heavy columns and prefetch the figure
points, and routing a response through a ``Serializer`` would quietly undo both.
The cost of that split is drift — a field added to ``_page_stub`` and not to
``DocumentPageStubSerializer`` would make the published schema a lie, which is
worse for a client than the silence it replaces, because a wrong schema is
believed. ``materials/tests/test_content_schema.py`` is the thing that stops it:
it compares these field sets against the keys the real payload functions emit,
so the two cannot diverge without a red test.

**The 200 is a ``oneOf``, not one object with everything optional.** The route
serves seven bodies and the difference between two of them is load-bearing:
``?type=page`` lists page STUBS with no ``text`` and ``?type=page&from=&to=``
returns the same pages WITH it. Collapsing those into one schema with an
optional ``text`` would tell a client the listing might carry the transcript,
and the whole reason the listing exists is that it must not. The envelopes are
distinguishable by ``type`` plus whether they carry ``item`` or ``items``.
"""

from __future__ import annotations

from drf_spectacular.extensions import OpenApiSerializerFieldExtension
from drf_spectacular.utils import PolymorphicProxySerializer, inline_serializer
from rest_framework import serializers

from .content_api import (
    MATCH_KINDS,
    MAX_LABEL_CHARS,
    MAX_PAGE_SPAN,
    MAX_SEARCH_HITS,
)


class EnumField(serializers.CharField):
    """A string field whose published schema is a closed set of values.

    Not a ``ChoiceField``. That one is a *validation* construct, and this module
    validates nothing — it also derives its own ``description`` from the choice
    labels (``* `page` - page``), which would have to be overridden at every one
    of the ten sites below to say anything useful.

    Two things about it are load-bearing rather than taste:

    The values are a CONSTRUCTOR argument, not something set on the instance
    afterwards. DRF deep-copies declared fields on every ``.fields`` access and
    ``Field.__deepcopy__`` rebuilds from ``_args``/``_kwargs`` instead of copying
    the object, so an attribute added later — an ``extend_schema_field``
    annotation, notably — is silently gone by the time the generator reads the
    field, and the enum just does not appear. A constructor argument survives.

    And the enums it emits are inline, but they do not STAY inline:
    drf-spectacular's postprocessing hoists every enum into a named component.
    Where the value set is reached through one property name it is named after
    it (``MatchEnum``) or after its component (``DocumentSearchTableHitFieldEnum``);
    the one-value sets below are reached through both ``type`` and ``kind``, and
    those are named in ``SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]`` — see the
    note there.
    """

    def __init__(self, values, description: str = "", **kwargs) -> None:
        self.values = list(values)
        self.description = description
        super().__init__(**kwargs)


class EnumFieldExtension(OpenApiSerializerFieldExtension):
    """Publishes :class:`EnumField` as an inline string enum."""

    target_class = "materials.content_schema.EnumField"

    def map_serializer_field(self, auto_schema, direction) -> dict:
        schema = {"type": "string", "enum": self.target.values}
        if self.target.description:
            schema["description"] = self.target.description
        return schema


def _literal_field(value: str, description: str) -> EnumField:
    """A string field pinned to one value — an envelope's ``type``, a hit's ``kind``."""
    return EnumField([value], description)


def _material_field():
    return serializers.CharField(
        help_text="The material IRI these contents were read out of."
    )


# ── items ────────────────────────────────────────────────────────────────────


class DocumentPageStubSerializer(serializers.Serializer):
    """A page without its text — what ``?type=page`` lists.

    ``coverage_state`` rides on the STUB and not only on the full page: a client
    drawing a 446-row table of contents has to be able to see which pages failed
    OCR before it fetches one, or it renders a page we could not read as a page
    with nothing on it.
    """

    page_no = serializers.IntegerField(
        help_text=(
            "The printed folio, which is the address this page is fetched by. "
            "Not an index into our rows. `0` means the upstream reported no "
            "usable folio for the page."
        )
    )
    uid = serializers.CharField(help_text="The upstream's own id for this page.")
    coverage_state = serializers.CharField(
        allow_blank=True,
        help_text=(
            "How well the page was recovered, in the upstream's vocabulary "
            "(observed: `anchored`, `blank`, `folded`, `gap`, `ocr_vision`, "
            "`unread`). Free-form by design — it is another project's "
            "vocabulary and may gain values. Read `text` together with this, "
            "never alone: an empty `text` on an unread page is a statement "
            "about us, not about the page."
        ),
    )
    anchor_fidelity = serializers.CharField(allow_blank=True)
    ocr_source = serializers.CharField(
        allow_blank=True,
        help_text=(
            "Where the text came from — a PDF text layer, or a vision model "
            "reading the page as an image. The second is a materially weaker "
            "claim than the first."
        ),
    )
    landscape = serializers.BooleanField(
        help_text="The page is printed sideways."
    )
    n_table_blocks = serializers.IntegerField(
        help_text="How many table blocks sit on this page."
    )
    chars = serializers.IntegerField(
        help_text="Characters of recovered text; `0` on a page we could not read."
    )


class DocumentPageSerializer(DocumentPageStubSerializer):
    """One page in full — the stub plus the recovered text."""

    text = serializers.CharField(
        allow_blank=True,
        help_text=(
            "The recovered text. Blank is legitimate and ambiguous on its own: "
            "check `coverage_state` before presenting it as an empty page. This "
            "is the REDACTED, published text — the unredacted original is not "
            "served by this route at any permission level."
        ),
    )


class DocumentTableStubSerializer(serializers.Serializer):
    """A table without its markdown — what ``?type=table`` lists."""

    ordinal = serializers.IntegerField(
        help_text=(
            "Presentation order within the document. Recomputed on every "
            "ingest — never address a table by it; use `key`."
        )
    )
    key = serializers.CharField(
        help_text=(
            "The stable handle (`p0030-t1`) that `?type=table&id=` takes. "
            "Survives the upstream gaining or losing tables elsewhere."
        )
    )
    uid = serializers.CharField(help_text="The upstream's own id for this table.")
    page_no = serializers.IntegerField(help_text="The folio this table is printed on.")
    index_on_page = serializers.IntegerField(
        help_text="Which table this is among those on its page, from 1."
    )
    caption = serializers.CharField(
        allow_blank=True,
        help_text=(
            "The prose introducing the table, where it has any. Frequently "
            "blank — 17% of this corpus's tables carry no caption at all — in "
            "which case `page_no` is the only handle a reader has."
        ),
    )
    header = serializers.ListField(
        child=serializers.CharField(allow_blank=True),
        help_text=(
            "The header row as the source gives it. Ragged and repetitive: "
            "merged header cells arrive as empty strings, and it is kept that "
            "way rather than normalised into something that loses the column "
            "count."
        ),
    )
    n_rows = serializers.IntegerField()
    n_cols = serializers.IntegerField()
    fidelity = serializers.CharField(
        allow_blank=True,
        help_text=(
            "The upstream's rating of this table's fidelity (`tight`, "
            "`ocr_vision`). Free-form, as `coverage_state` is."
        ),
    )


class DocumentTableSerializer(DocumentTableStubSerializer):
    """One table in full — the stub plus the markdown a client renders."""

    markdown = serializers.CharField(
        help_text=(
            "The table as markdown. Carried only on a single-table fetch and "
            "never in the listing: one report's tables come to ~830 KiB and a "
            "single table reaches ~170 KiB."
        )
    )


class DocumentFigurePointSerializer(serializers.Serializer):
    """One datum of one chart."""

    point_index = serializers.IntegerField()
    label = serializers.CharField(allow_blank=True)
    series = serializers.CharField(
        allow_blank=True, help_text="Which series this point belongs to."
    )
    value = serializers.FloatField(
        allow_null=True,
        help_text="Null where the chart names a category but prints no value for it.",
    )
    estimated = serializers.BooleanField(
        help_text=(
            "The value was read off the chart image rather than from a printed "
            "figure — a materially weaker claim, and one a client must not "
            "render identically to a printed one. It rides on every POINT "
            "rather than on the figure because a single chart routinely mixes "
            "the two."
        )
    )


class DocumentFigureSerializer(serializers.Serializer):
    """One chart and its full series.

    Figures carry their points inline in the LISTING too, unlike tables. That
    asymmetry is measured rather than stylistic: the entire 35-report corpus is
    6,116 points.
    """

    ordinal = serializers.IntegerField(
        help_text="Presentation order; recomputed on every ingest. Address by `key`."
    )
    key = serializers.CharField(
        help_text="The stable handle (`p0030-f1`) that `?type=figure&id=` takes."
    )
    uid = serializers.CharField()
    page_no = serializers.IntegerField()
    index_on_page = serializers.IntegerField()
    title = serializers.CharField(allow_blank=True)
    chart_type = serializers.CharField(
        allow_blank=True,
        help_text=(
            "The upstream's own chart vocabulary, kept verbatim and NOT "
            "normalised — it contains both `stacked bar` and `stacked_bar`."
        ),
    )
    unit = serializers.CharField(allow_blank=True)
    x_axis = serializers.CharField(allow_blank=True)
    y_axis = serializers.CharField(allow_blank=True)
    notes = serializers.CharField(
        allow_blank=True,
        help_text=(
            "The transcriber's reasoning about this chart — how a disputed "
            "value was resolved, which total it reconciles against. The audit "
            "trail behind every number in `points`."
        ),
    )
    verify_note = serializers.CharField(allow_blank=True)
    verified = serializers.BooleanField(
        allow_null=True,
        help_text=(
            "The upstream's verification flag. Null means NOT CHECKED, which "
            "is a different claim from `false` (checked and found wanting)."
        ),
    )
    points = DocumentFigurePointSerializer(many=True)


# ── the ``…/content`` envelopes ──────────────────────────────────────────────


def _listing(name: str, kind: str, item, *, what: str, narrowable: bool):
    """A ``count`` + ``items`` envelope for one content kind."""
    fields = {
        "material": _material_field(),
        "type": _literal_field(kind, f"Always `{kind}` on this body."),
        "count": serializers.IntegerField(help_text=f"How many {what} are listed."),
        "items": item(many=True),
    }
    if narrowable:
        # Echoed only when the request carried one, so an empty result is
        # readable as "nothing here matched" rather than as a document that
        # holds none of this kind at all.
        fields["q"] = serializers.CharField(
            required=False,
            help_text=(
                "Echoed back when the listing was narrowed by `q`. Absent "
                "otherwise — which is how an empty narrowed result is told "
                f"apart from a document with no {what} in it."
            ),
        )
    return inline_serializer(name=name, fields=fields)


def _single(name: str, kind: str, item):
    """A single-``item`` envelope for one content kind."""
    return inline_serializer(
        name=name,
        fields={
            "material": _material_field(),
            "type": _literal_field(kind, f"Always `{kind}` on this body."),
            "item": item(),
        },
    )


MaterialPageListSerializer = _listing(
    "MaterialPageList",
    "page",
    DocumentPageStubSerializer,
    what="pages",
    narrowable=False,
)

MaterialPageSerializer = _single("MaterialPage", "page", DocumentPageSerializer)

#: ``from`` is a Python keyword, so this envelope cannot be a class body — which
#: is the other reason the envelopes are built with ``inline_serializer``.
MaterialPageRangeSerializer = inline_serializer(
    name="MaterialPageRange",
    fields={
        "material": _material_field(),
        "type": _literal_field("page", "Always `page` on this body."),
        "from": serializers.IntegerField(
            help_text="First folio of the range, inclusive."
        ),
        "to": serializers.IntegerField(
            help_text="Last folio of the range, inclusive."
        ),
        "count": serializers.IntegerField(
            help_text=(
                "Pages actually returned. May be fewer than `to - from + 1` — "
                "a range may span folios the document does not have — but is "
                "never a truncation: a span over "
                f"{MAX_PAGE_SPAN} pages is refused with a 400 rather than cut."
            )
        ),
        "items": DocumentPageSerializer(many=True),
    },
)

MaterialTableListSerializer = _listing(
    "MaterialTableList",
    "table",
    DocumentTableStubSerializer,
    what="tables",
    narrowable=True,
)

MaterialTableSerializer = _single("MaterialTable", "table", DocumentTableSerializer)

MaterialFigureListSerializer = _listing(
    "MaterialFigureList",
    "figure",
    DocumentFigureSerializer,
    what="figures",
    narrowable=True,
)

MaterialFigureSerializer = _single("MaterialFigure", "figure", DocumentFigureSerializer)

#: Every 200 ``…/content`` can serve. Ordered listing-then-item per kind, which
#: is the order the route's own documentation introduces them in.
CONTENT_RESPONSE_SERIALIZERS = [
    MaterialPageListSerializer,
    MaterialPageSerializer,
    MaterialPageRangeSerializer,
    MaterialTableListSerializer,
    MaterialTableSerializer,
    MaterialFigureListSerializer,
    MaterialFigureSerializer,
]

MaterialContentResponse = PolymorphicProxySerializer(
    component_name="MaterialContentResponse",
    serializers=CONTENT_RESPONSE_SERIALIZERS,
    # No discriminator property: `type` alone does not separate a listing from a
    # single item, and OpenAPI's `discriminator` takes exactly one field. A
    # client branches on `type` and then on `item` vs `items`.
    resource_type_field_name=None,
    many=False,
)


# ── the ``…/search`` envelope ────────────────────────────────────────────────


def _hit_fields(kind: str, field_names: tuple[str, ...], **extra):
    """The fields every search hit carries, plus the ones only ``kind`` does."""
    return {
        "kind": _literal_field(kind, f"Always `{kind}` on this hit."),
        "page_no": serializers.IntegerField(
            help_text="The folio the hit is printed on; `?type=page&num=` takes it."
        ),
        "key": serializers.CharField(
            allow_null=True,
            help_text=(
                "The handle `?type=…&id=` takes, or null on a page — pages are "
                "addressed by `page_no`. Present and null rather than absent, "
                "so a client can read `key` off every hit without branching on "
                "`kind` first."
            ),
        ),
        "field": EnumField(
            field_names,
            "Which field of the row matched. One hit per row, never one per "
            "field, and the order is most-citable-first — so a table whose "
            "caption and markdown both match reports `caption`.",
        ),
        "match": EnumField(
            MATCH_KINDS,
            "On what grounds this matched: `literal` (the query is in the "
            "text), `phonetic` (it is there in sound only — `ujuri` matching "
            "उजुरी), or `folio` (the query was a page number, and this is NOT "
            "a claim about the text at all). The three are different strengths "
            "of evidence and must not be rendered identically.",
        ),
        "snippet": serializers.CharField(
            allow_blank=True,
            help_text=(
                "~190 characters of the matched field, whitespace collapsed to "
                "one line. A leading or trailing `…` marks an end that was cut, "
                "so a snippet that IS the whole field can be told from a window "
                "into something longer. Blank where the field has no text — a "
                "folio hit on a page we could not read."
            ),
        ),
        **extra,
    }


DocumentSearchPageHitSerializer = inline_serializer(
    name="DocumentSearchPageHit",
    fields=_hit_fields(
        "page",
        ("text",),
        coverage_state=serializers.CharField(
            allow_blank=True,
            help_text=(
                "As on the page stub. A hit on an unread page is held-but-"
                "unread rather than blank."
            ),
        ),
        ocr_source=serializers.CharField(allow_blank=True),
    ),
)

DocumentSearchTableHitSerializer = inline_serializer(
    name="DocumentSearchTableHit",
    fields=_hit_fields(
        "table",
        ("caption", "markdown"),
        caption=serializers.CharField(
            allow_blank=True,
            help_text=(
                f"The table's caption, clipped to {MAX_LABEL_CHARS} characters "
                "with the cut marked. Carried whichever field matched, as the "
                "label for the hit."
            ),
        ),
        fidelity=serializers.CharField(allow_blank=True),
    ),
)

DocumentSearchFigureHitSerializer = inline_serializer(
    name="DocumentSearchFigureHit",
    fields=_hit_fields(
        "figure",
        ("title", "notes"),
        title=serializers.CharField(
            allow_blank=True,
            help_text=f"The figure's title, clipped to {MAX_LABEL_CHARS} characters.",
        ),
    ),
)

#: The three hit shapes, keyed by the ``kind`` that distinguishes them.
#:
#: Annotated wider than it is built. ``PolymorphicProxySerializer`` accepts
#: either instances or classes, and ``dict`` is invariant in its value type, so
#: an inferred ``dict[str, Serializer]`` is not assignable to the
#: ``dict[str, Serializer | type[Serializer]]`` it declares.
SEARCH_HIT_SERIALIZERS: dict[
    str, serializers.Serializer | type[serializers.Serializer]
] = {
    "page": DocumentSearchPageHitSerializer,
    "table": DocumentSearchTableHitSerializer,
    "figure": DocumentSearchFigureHitSerializer,
}

#: Unlike the content envelope this one HAS a single discriminator — ``kind`` —
#: so clients get a properly tagged union and code generators emit a switch
#: rather than a struct with every optional field on it.
DocumentSearchHits = PolymorphicProxySerializer(
    component_name="DocumentSearchHit",
    serializers=SEARCH_HIT_SERIALIZERS,
    resource_type_field_name="kind",
    many=True,
)

MaterialContentSearchSerializer = inline_serializer(
    name="MaterialContentSearch",
    fields={
        "material": _material_field(),
        "q": serializers.CharField(
            help_text=(
                "The query, echoed. This is what makes an empty `hits` readable "
                "as *this document does not say that* rather than as a request "
                "that never carried a query."
            )
        ),
        "total": serializers.IntegerField(
            help_text=(
                "How many hits the document really holds, counted BEFORE the "
                "limit is applied. A caller narrowing a common term can see "
                "that it is common rather than believing it found everything."
            )
        ),
        "truncated": serializers.BooleanField(
            help_text="`total` exceeded the limit, so `hits` is a prefix of it."
        ),
        "count": serializers.IntegerField(
            help_text=f"Length of `hits`; at most {MAX_SEARCH_HITS}."
        ),
        "hits": DocumentSearchHits,
    },
)


# ── errors ───────────────────────────────────────────────────────────────────


class MaterialContentErrorSerializer(serializers.Serializer):
    """The 400 and 404 bodies on both content routes."""

    detail = serializers.CharField(
        help_text="What was wrong with the request, or what was not found."
    )
    extractable = serializers.BooleanField(
        required=False,
        help_text=(
            "Only on the 404 that means *this material has no stored content "
            "at all*, which is the common case — most materials are a single "
            "scraped page, not a transcribed report. True when the document is "
            "one the pipeline can read, so a client knows whether it is worth "
            "coming back for: the difference between *nobody has opened this "
            "yet* and *there is nothing here to open*. Nothing is asked of the "
            "caller either way — documents are queued on arrival. Absent on "
            "every other error, including the 404 for an unknown page, table "
            "or figure WITHIN a document we have read."
        ),
    )
