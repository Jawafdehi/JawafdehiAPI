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
