"""The published response shapes for ``…/content`` and ``…/search`` are true.

``materials/content_schema.py`` describes those bodies with serializers that
never serialize anything — the responses are built as plain dicts by
``materials.content_api``, which defers the heavy columns and prefetches the
figure points, and routing them through a ``Serializer`` would undo both. That
split is the whole risk this file exists to cover: a field added to
``_page_stub`` and not to ``DocumentPageStubSerializer`` leaves the published
schema quietly wrong, which is worse for a client than the "No response body"
it replaced, because a wrong schema is believed and an absent one is not.

So every shape the two routes can serve is fetched here for real and checked
against the component the schema says it is — key for key, type for type, and
every enum against the live value. The comparison is EXACT in both directions:
a key in the body that the schema omits fails, and so does a key the schema
publishes that the body never sends.

The sibling test in ``test_content_api.py`` pins the same contract from the
other end — that every accepted *argument* is documented.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_content_schema.py
"""

from __future__ import annotations

from django.conf import settings
from rest_framework import status
from rest_framework.test import APITestCase

from materials.content_api import (
    CONTENT_TYPES,
    MATCH_KINDS,
    MAX_SEARCH_HITS,
    Span,
)

from .test_content_api import IDENT, SOURCE, _seed_content, _seed_material

CONTENT_PATH = "/api/materials/{source}/{ident}/content/"
SEARCH_PATH = "/api/materials/{source}/{ident}/search/"

#: JSON types, as OpenAPI names them.
_PYTHON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _build_schema():
    from drf_spectacular.generators import SchemaGenerator

    return SchemaGenerator().get_schema(request=None, public=True)


class SchemaConformanceMixin:
    """Compare a real response body against the component that describes it."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Generated once per class: it walks every view in the project and is
        # nowhere near cheap enough to build per test.
        cls.schema = _build_schema()
        cls.components = cls.schema["components"]["schemas"]

    def _deref(self, node: dict) -> dict:
        """Follow ``$ref`` and unwrap the single-``allOf`` description wrapper.

        drf-spectacular wraps a ``$ref`` in ``allOf`` whenever it has to hang a
        per-property ``description`` off it — which it does for every enum field
        here — so a plain ``$ref`` walk would stop one level short and the enum
        would never be checked.
        """
        for _ in range(10):
            if "$ref" in node:
                node = self.components[node["$ref"].rsplit("/", 1)[1]]
            elif len(node.get("allOf", ())) == 1:
                wrapper = {k: v for k, v in node.items() if k != "allOf"}
                return {**wrapper, **self._deref(node["allOf"][0])}
            else:
                return node
        raise AssertionError("cyclic $ref")

    def assertConforms(self, body, component_name: str, where: str = ""):
        """``body`` is exactly what ``component_name`` says it is."""
        self.assertIn(component_name, self.components)
        self._check(body, self.components[component_name], where or component_name)

    def _check(self, value, schema: dict, where: str):
        schema = self._deref(schema)

        if value is None:
            self.assertTrue(
                schema.get("nullable"),
                f"{where}: null, but the schema does not allow it",
            )
            return

        declared = schema.get("type")
        if declared in _PYTHON_TYPES:
            expected = _PYTHON_TYPES[declared]
            # bool is a subclass of int; an integer field must not accept one.
            if declared in ("integer", "number"):
                self.assertNotIsInstance(value, bool, f"{where}: bool for {declared}")
            self.assertIsInstance(value, expected, f"{where}: not a {declared}")

        if "enum" in schema:
            self.assertIn(value, schema["enum"], f"{where}: outside the published enum")

        if declared == "array":
            for i, item in enumerate(value):
                self._check(item, schema.get("items", {}), f"{where}[{i}]")
            return

        if declared != "object" or "properties" not in schema:
            return

        properties = schema["properties"]
        required = set(schema.get("required", ()))
        present = set(value)

        undocumented = present - set(properties)
        self.assertFalse(
            undocumented,
            f"{where}: the body carries {sorted(undocumented)}, which the "
            "published schema does not. Add them to materials/content_schema.py "
            "— a client reading the schema would not know they exist.",
        )
        missing = required - present
        self.assertFalse(
            missing,
            f"{where}: the schema marks {sorted(missing)} required and the body "
            "does not send them. A client would treat their absence as a bug.",
        )

        for name, child in properties.items():
            if name in value:
                self._check(value[name], child, f"{where}.{name}")


class ContentResponseShapeTests(SchemaConformanceMixin, APITestCase):
    """Every body ``…/content`` can serve matches its published component."""

    databases = "__all__"

    def setUp(self):
        self.material = _seed_material()
        _seed_content(self.material)
        self.url = f"/api/materials/{SOURCE}/{IDENT}/content"

    def _variants(self) -> dict:
        """The component the oneOf offers for each request shape."""
        return {
            "MaterialPageList": {"type": "page"},
            "MaterialPage": {"type": "page", "num": 30},
            "MaterialPageRange": {"type": "page", "from": 30, "to": 31},
            "MaterialTableList": {"type": "table"},
            "MaterialTable": {"type": "table", "id": "p0030-t1"},
            "MaterialFigureList": {"type": "figure"},
            "MaterialFigure": {"type": "figure", "id": "p0030-f1"},
        }

    def test_every_shape_the_route_serves_matches_its_component(self):
        for component, params in self._variants().items():
            with self.subTest(component=component):
                resp = self.client.get(self.url, params)
                self.assertEqual(resp.status_code, status.HTTP_200_OK)
                self.assertConforms(resp.json(), component)

    def test_the_oneof_offers_exactly_those_shapes_and_no_others(self):
        # The union is what a client branches on. An envelope added to the route
        # without being added here would leave that client with a body no
        # member of the union describes.
        offered = {
            ref["$ref"].rsplit("/", 1)[1]
            for ref in self.components["MaterialContentResponse"]["oneOf"]
        }
        self.assertEqual(offered, set(self._variants()))

    def test_the_narrowed_listing_echoes_q_as_the_schema_says_it_may(self):
        # `q` is the one optional key in a listing envelope, and the whole point
        # of it is that its ABSENCE is meaningful. Both states have to conform.
        narrowed = self.client.get(self.url, {"type": "table", "q": "उजुरी"}).json()
        self.assertEqual(narrowed["q"], "उजुरी")
        self.assertConforms(narrowed, "MaterialTableList")

        plain = self.client.get(self.url, {"type": "table"}).json()
        self.assertNotIn("q", plain)
        self.assertConforms(plain, "MaterialTableList")

    def test_a_page_we_could_not_read_still_conforms(self):
        # The honesty fields are the ones a client is most likely to meet for
        # the first time on a failed page, so the empty-text case is pinned
        # rather than left to the happy path.
        body = self.client.get(self.url, {"type": "page", "num": 31}).json()
        self.assertEqual(body["item"]["text"], "")
        self.assertEqual(body["item"]["coverage_state"], "ocr_unread")
        self.assertConforms(body, "MaterialPage")

    def test_a_figure_with_an_unset_verified_flag_conforms(self):
        # `verified` is nullable and null means NOT CHECKED, which is a
        # different claim from false. The schema has to allow it.
        from materials.models import DocumentFigure

        DocumentFigure.objects.update(verified=None)
        body = self.client.get(self.url, {"type": "figure"}).json()
        self.assertIsNone(body["items"][0]["verified"])
        self.assertConforms(body, "MaterialFigureList")

    def test_a_point_with_no_value_conforms(self):
        from materials.models import DocumentFigurePoint

        DocumentFigurePoint.objects.update(value=None)
        body = self.client.get(self.url, {"type": "figure"}).json()
        self.assertIsNone(body["items"][0]["points"][0]["value"])
        self.assertConforms(body, "MaterialFigureList")

    def test_the_type_constants_are_the_ones_the_route_accepts(self):
        published = set()
        for ref in self.components["MaterialContentResponse"]["oneOf"]:
            envelope = self._deref(ref)
            published |= set(self._deref(envelope["properties"]["type"])["enum"])
        self.assertEqual(published, set(CONTENT_TYPES))

    def test_the_enum_name_overrides_cover_every_content_type(self):
        # Without one override per value drf-spectacular names these by a hash
        # of the value (`Type8d9Enum`) and warns three times each. A fourth
        # content type added without a fourth entry would reintroduce that.
        names = settings.SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]
        for kind in CONTENT_TYPES:
            name = f"MaterialContent{kind.capitalize()}Enum"
            self.assertIn(name, names, f"no ENUM_NAME_OVERRIDES entry for {kind!r}")
            self.assertEqual(names[name], [kind])
            self.assertIn(name, self.components)


class PublishedSurfaceTests(SchemaConformanceMixin, APITestCase):
    """What the two routes publish, taken as a whole rather than body by body."""

    databases = "__all__"

    def _reachable(self) -> set:
        """Every component the two operations' responses can reach.

        Derived rather than listed, so a shape added to either route is covered
        by the assertions below the moment it is published — a hand-kept list
        would go stale in exactly the way this whole file exists to prevent.
        """
        seen: set = set()
        stack = [
            self.schema["paths"][path]["get"]["responses"]
            for path in (CONTENT_PATH, SEARCH_PATH)
        ]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                ref = node.get("$ref")
                if isinstance(ref, str):
                    name = ref.rsplit("/", 1)[1]
                    if name not in seen:
                        seen.add(name)
                        stack.append(self.components[name])
                stack.extend(v for k, v in node.items() if k != "$ref")
            elif isinstance(node, list):
                stack.extend(node)
        return seen

    def test_the_routes_publish_exactly_the_components_we_think_they_do(self):
        self.assertEqual(
            self._reachable(),
            {
                # the ``…/content`` 200, and the seven bodies it is a union of
                "MaterialContentResponse",
                "MaterialPageList",
                "MaterialPage",
                "MaterialPageRange",
                "MaterialTableList",
                "MaterialTable",
                "MaterialFigureList",
                "MaterialFigure",
                # the items those carry
                "DocumentPageStub",
                "DocumentPage",
                "DocumentTableStub",
                "DocumentTable",
                "DocumentFigure",
                "DocumentFigurePoint",
                # the ``…/search`` 200 and its discriminated hits
                "MaterialContentSearch",
                "DocumentSearchHit",
                "DocumentSearchPageHit",
                "DocumentSearchTableHit",
                "DocumentSearchFigureHit",
                # the shared 400/404 body
                "MaterialContentError",
                # the pinned constants and vocabularies
                "MaterialContentPageEnum",
                "MaterialContentTableEnum",
                "MaterialContentFigureEnum",
                "DocumentSearchPageHitFieldEnum",
                "DocumentSearchTableHitFieldEnum",
                "DocumentSearchFigureHitFieldEnum",
                "MatchEnum",
            },
        )

    def test_the_conditional_get_headers_are_published_and_real(self):
        # The conditional GET is the one part of this plane a client has to opt
        # into, so an undocumented ETag is a feature nobody uses. Asserted
        # against a live response as well as the schema, because a header the
        # document promises and the route does not send is the worse half.
        _seed_content(_seed_material())
        url = f"/api/materials/{SOURCE}/{IDENT}/content"
        live = self.client.get(url, {"type": "table"})
        self.assertIn("ETag", live)
        self.assertIn("Authorization", live["Vary"])

        for path in (CONTENT_PATH, SEARCH_PATH):
            responses = self.schema["paths"][path]["get"]["responses"]
            for code in ("200", "304"):
                self.assertIn("ETag", responses[code].get("headers", {}), f"{path} {code}")
            for code in responses:
                self.assertIn("Vary", responses[code].get("headers", {}), f"{path} {code}")

    def test_only_the_three_meaningful_absences_are_optional(self):
        # `_check` catches a key the schema omits and a required key the body
        # omits. What it cannot catch is a property published as OPTIONAL that
        # the route never sends at all — nothing fails, and a client is left
        # handling a field that does not exist. There are exactly three, each
        # optional because its ABSENCE carries meaning, and each has a test
        # above exercising both states. A fourth should get the same treatment
        # rather than inheriting the silence.
        optional = set()
        for name in self._reachable():
            component = self.components[name]
            required = set(component.get("required", ()))
            for prop in component.get("properties", {}):
                if prop not in required:
                    optional.add(f"{name}.{prop}")
        self.assertEqual(
            optional,
            {
                # Present only when the listing was narrowed.
                "MaterialTableList.q",
                "MaterialFigureList.q",
                # Present only on "we have not read this document".
                "MaterialContentError.extractable",
            },
        )


class SearchResponseShapeTests(SchemaConformanceMixin, APITestCase):
    """``…/search`` and its three hit shapes match their components."""

    databases = "__all__"

    def setUp(self):
        self.material = _seed_material()
        _seed_content(self.material)
        self.url = f"/api/materials/{SOURCE}/{IDENT}/search"

    def test_the_envelope_matches_its_component(self):
        resp = self.client.get(self.url, {"q": "उजुरी"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertTrue(body["hits"], "the fixture must produce at least one hit")
        self.assertConforms(body, "MaterialContentSearch")

    def test_each_hit_matches_the_component_its_kind_maps_to(self):
        # The hit union is discriminated on `kind`, so the mapping is a promise:
        # a client switches on `kind` and decodes as the component named there.
        mapping = self.components["DocumentSearchHit"]["discriminator"]["mapping"]
        self.assertEqual(set(mapping), set(CONTENT_TYPES))

        seen = set()
        # One query per kind, so all three hit shapes are actually exercised
        # rather than whichever the fixture happens to match first.
        for query in ("उजुरी", "31", "संख्या"):
            for hit in self.client.get(self.url, {"q": query}).json()["hits"]:
                component = mapping[hit["kind"]].rsplit("/", 1)[1]
                self.assertConforms(hit, component, f"{query}/{hit['kind']}")
                seen.add(hit["kind"])
        self.assertEqual(seen, set(CONTENT_TYPES), f"only exercised {sorted(seen)}")

    def test_the_match_grounds_are_the_ones_the_matcher_can_report(self):
        # `match` tells a client how strong a hit is, and a value the schema
        # does not list would be rendered as an unknown rather than as the
        # weaker claim it is. Checked against what `Span` actually constructs,
        # not just against the constant both sides import.
        self.assertEqual(set(self.components["MatchEnum"]["enum"]), set(MATCH_KINDS))
        constructed = {
            Span("literal", 0, 1).how,
            Span("phonetic").how,
            Span("folio").how,
        }
        self.assertEqual(constructed, set(MATCH_KINDS))

    def test_the_documented_limit_is_the_one_enforced(self):
        operation = self.schema["paths"][SEARCH_PATH]["get"]
        described = next(
            p["description"] for p in operation["parameters"] if p["name"] == "limit"
        )
        self.assertIn(str(MAX_SEARCH_HITS), described)


class ErrorResponseShapeTests(SchemaConformanceMixin, APITestCase):
    """The 400s and 404s are typed too — they are most of what a client meets."""

    databases = "__all__"

    def setUp(self):
        self.material = _seed_material()
        _seed_content(self.material)
        self.content_url = f"/api/materials/{SOURCE}/{IDENT}/content"
        self.search_url = f"/api/materials/{SOURCE}/{IDENT}/search"

    def test_the_bad_requests_match_the_error_component(self):
        cases = {
            "no type": {},
            "unknown type": {"type": "sausage"},
            "q on pages": {"type": "page", "q": "x"},
            "half a range": {"type": "page", "from": 30},
            "page args on a table": {"type": "table", "num": 30},
            "id and q together": {"type": "table", "id": "p0030-t1", "q": "x"},
            "oversized range": {"type": "page", "from": 1, "to": 999},
        }
        for label, params in cases.items():
            with self.subTest(case=label):
                resp = self.client.get(self.content_url, params)
                self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertConforms(resp.json(), "MaterialContentError")
                self.assertNotIn("extractable", resp.json())

    def test_a_missing_item_is_an_error_without_extractable(self):
        # We have read this document; this page is simply not in it. Reporting
        # `extractable` here would invite a client to wait for an extraction
        # that has already happened.
        for params in ({"type": "page", "num": 9999}, {"type": "table", "id": "nope"}):
            with self.subTest(params=params):
                resp = self.client.get(self.content_url, params)
                self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
                self.assertConforms(resp.json(), "MaterialContentError")
                self.assertNotIn("extractable", resp.json())

    def test_an_unread_document_is_an_error_with_extractable(self):
        _seed_material(ident="unread-doc")
        for url in (
            f"/api/materials/{SOURCE}/unread-doc/content?type=table",
            f"/api/materials/{SOURCE}/unread-doc/search?q=x",
        ):
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
                body = resp.json()
                self.assertIn("extractable", body)
                self.assertConforms(body, "MaterialContentError")

    def test_both_routes_publish_the_same_error_component(self):
        for path in (CONTENT_PATH, SEARCH_PATH):
            responses = self.schema["paths"][path]["get"]["responses"]
            for code in ("400", "404"):
                ref = responses[code]["content"]["application/json"]["schema"]["$ref"]
                self.assertEqual(ref.rsplit("/", 1)[1], "MaterialContentError")
            # 304 is a status a conditional GET really returns, and a body-less
            # response is still a documented one.
            self.assertIn("304", responses)
            self.assertNotIn("content", responses["304"])

    def test_neither_route_still_says_no_response_body(self):
        # The exact state this work replaced. Pinned so a refactor that drops
        # the `responses=` argument fails loudly rather than silently reverting
        # the published schema to a shrug.
        for path in (CONTENT_PATH, SEARCH_PATH):
            ok = self.schema["paths"][path]["get"]["responses"]["200"]
            self.assertIn("content", ok, f"{path} publishes no 200 body")
