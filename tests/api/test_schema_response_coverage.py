"""The published OpenAPI schema describes these endpoints' response bodies.

``/api/schema/`` is served publicly and is what anyone generating a client reads.
When drf-spectacular cannot work out a view's serializer it logs "unable to guess
serializer … Ignoring view for now" and emits the path with NO response body —
the route is advertised and its shape is not, which is worse than silence
because a generated client compiles and then has nothing to deserialize into.

These tests pin the endpoints whose response types were added deliberately. They
fail if a decorator is dropped, if a serializer is removed, or if someone moves
``@extend_schema`` below ``@api_view`` — which looks harmless, satisfies the type
checker, and silently empties the schema (see the note in materials/views.py).
"""

import pytest
from drf_spectacular.generators import SchemaGenerator


@pytest.fixture(scope="module")
def schema():
    return SchemaGenerator().get_schema(request=None, public=True)


def _json_200(schema, path, method="get"):
    """The 200 response's JSON schema for a path, or None if it has no body."""
    operation = schema["paths"][path][method]
    content = operation.get("responses", {}).get("200", {}).get("content", {})
    return content.get("application/json", {}).get("schema")


@pytest.mark.parametrize(
    "path",
    [
        "/api/search/",
        "/api/materials/{source}/{ident}/extraction/",
        "/api/materials/{source}/{ident}/extraction/tables/{key}/",
    ],
)
def test_endpoint_is_present_in_the_schema(schema, path):
    assert path in schema["paths"], (
        f"{path} is missing from the published schema entirely. "
        f"Known paths: {sorted(schema['paths'])[:5]}…"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/search/",
        "/api/materials/{source}/{ident}/extraction/",
        "/api/materials/{source}/{ident}/extraction/tables/{key}/",
    ],
)
def test_endpoint_describes_its_200_body(schema, path):
    body = _json_200(schema, path)

    assert body, (
        f"{path} publishes no 200 response body. drf-spectacular could not "
        f"resolve a serializer — check that @extend_schema(responses=...) is "
        f"present AND sits above @api_view."
    )


def test_search_response_carries_the_envelope_keys(schema):
    ref = _json_200(schema, "/api/search/")["$ref"].rsplit("/", 1)[-1]
    props = schema["components"]["schemas"][ref]["properties"]

    # The envelope SearchService.search returns. Not exhaustive on purpose —
    # these are the keys a client cannot work without.
    for key in ("query", "count", "counts", "facets", "results", "next_cursor"):
        assert key in props, f"search response schema lost '{key}'"


def test_extraction_manifest_carries_provenance_tables_and_figures(schema):
    ref = _json_200(schema, "/api/materials/{source}/{ident}/extraction/")[
        "$ref"
    ].rsplit("/", 1)[-1]
    props = schema["components"]["schemas"][ref]["properties"]

    for key in ("material", "provenance", "counts", "tables", "figures"):
        assert key in props, f"extraction manifest schema lost '{key}'"


def test_extracted_table_schema_includes_the_markdown(schema):
    ref = _json_200(
        schema, "/api/materials/{source}/{ident}/extraction/tables/{key}/"
    )["$ref"].rsplit("/", 1)[-1]
    props = schema["components"]["schemas"][ref]["properties"]

    # The whole reason the table endpoint is split out from the manifest.
    assert "markdown" in props
    assert "key" in props


# ---------------------------------------------------------------------------
# Type assertions.
#
# The first version of this file asserted property NAMES only. That is how four
# real drifts shipped green: `header` declared a string while the model is a
# JSONField(default=list), `verified` declared non-nullable while the model is
# null=True, `search_id` missing entirely, and `type` pinned to a closed enum the
# service does not honour. A name-only check cannot see any of those, and a
# wrong schema is worse than an absent one — a generated client compiles and
# then fails at runtime.
# ---------------------------------------------------------------------------


def _component(schema, path, method="get"):
    ref = _json_200(schema, path, method)["$ref"].rsplit("/", 1)[-1]
    return schema["components"]["schemas"][ref]


def test_extracted_table_header_is_an_array(schema):
    """ExtractedTable.header is JSONField(default=list), not a string."""
    stub = schema["components"]["schemas"]["ExtractionTableStub"]

    assert stub["properties"]["header"]["type"] == "array"


def test_extracted_figure_verified_is_nullable(schema):
    """BooleanField(null=True) — null means 'not checked', not 'false'."""
    verified = schema["components"]["schemas"]["ExtractionFigure"]["properties"][
        "verified"
    ]

    assert verified["type"] == "boolean"
    assert verified.get("nullable") is True


def test_search_response_carries_search_id(schema):
    """POST /api/search/click requires it as the join key."""
    props = _component(schema, "/api/search/")["properties"]

    assert "search_id" in props, (
        "search_id is set on every search response and is REQUIRED by the click "
        "beacon; omitting it from the schema makes the beacon unreachable from a "
        "generated client."
    )


def test_search_result_type_is_not_a_closed_enum(schema):
    """_serialize_hit falls back to source_app / 'unknown' off the known set."""
    result_type = schema["components"]["schemas"]["SearchResult"]["properties"]["type"]

    assert "enum" not in result_type, (
        "SearchResult.type is declared as a closed enum, but _serialize_hit "
        "emits the document's source_app (or 'unknown') when the index name does "
        "not map to a known type. A strict client would reject valid responses."
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/materials/{source}/{ident}/extraction/",
        "/api/materials/{source}/{ident}/extraction/tables/{key}/",
    ],
)
def test_extraction_endpoints_declare_their_400(schema, path):
    """Reachable: the URL ident pattern is wider than MATERIAL_IRI_RE."""
    responses = schema["paths"][path]["get"]["responses"]

    assert "400" in responses


def test_search_400_is_not_declared_as_a_detail_only_object(schema):
    """400 has two shapes; pinning it to {detail} would misdescribe the common one."""
    four_hundred = schema["paths"]["/api/search/"]["get"]["responses"]["400"]
    body = four_hundred["content"]["application/json"]["schema"]

    assert "$ref" not in body, (
        "The 400 is declared as a concrete serializer, but a rejected query "
        "parameter comes back as a DRF field-error map, not {detail}."
    )
    assert four_hundred.get("description")
