"""Adversarial: the IRI / reference resolution "join key" attack surface.

Threat model. The platform join key is a schema.org ``@id`` IRI. Three read
surfaces parse *untrusted path input* into that key before it ever touches the
DB:

  * ``GET /api/entities/<ref>``   → ``entities.views._resolve_ref``
  * ``GET /api/materials/<source>/<ident>`` and ``?iri=`` →
    ``materials.views._resolve_material`` / ``_normalize_iri_param`` +
    ``build_material_iri`` / ``is_valid_material_iri``
  * the shared grammar in ``jawafdehi_shared.entities.ids``.

A ``ref`` is an *opaque, attacker-controlled string*. If the resolver can be
coaxed into (a) re-keying onto a foreign authority so a request addresses a
different tenant/resource, (b) escaping the ``/entity/`` or ``/material/``
namespace via traversal, (c) decoding inconsistently, (d) overflowing the
join-key column width, (e) smuggling a NUL/control char into a DB lookup, or
(f) accepting a ``javascript:``/``file:``/``data:`` scheme as an ``@id``, the
join key is forgeable.

Every test below asserts the SECURE behavior against the CURRENT code: the
input is rejected (``None`` / ``ValueError`` / 400 / 404) or SAFELY canonicalized
onto the one canonical authority (path preserved, host discarded) — it never
resolves to a foreign/unexpected resource, never 500s, never traverses.

These are almost all pure-function assertions (cheap, no DB); a handful of
endpoint tests drive the full request path.

Run with: ``uv run pytest -m security tests/security/test_iri_resolution_security.py``
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from rest_framework.test import APIClient

import entities.views as ev
import materials.views as mv
from jawafdehi_shared.entities.ids import (
    MAX_IRI_LENGTH,
    build_material_iri,
    canonicalize_case_iri,
    canonicalize_entity_iri,
    canonicalize_material_iri,
    is_valid_case_iri,
    is_valid_entity_iri,
    is_valid_material_iri,
    iri_base,
    parse_entity_iri,
    parse_material_iri,
)

pytestmark = pytest.mark.security

CANONICAL = "https://jawafdehi.org"


# ===========================================================================
# 1. SSRF-style host re-keying — a FOREIGN host in the @id/ref must never
#    address a different tenant/resource. The contract: keep ONLY the path
#    grammar, re-emit on the one canonical authority. So a foreign host can
#    at most spell the SAME canonical resource, never reach a foreign one.
# ===========================================================================


class TestForeignHostRekeying:
    @pytest.mark.parametrize(
        "hostile",
        [
            "http://evil.com/entity/person/ram",
            "https://evil.com/entity/person/ram",
            "https://jawafdehi.org.evil.com/entity/person/ram",  # suffix trick
            "https://x:8443/entity/person/ram",  # rogue port
            "http://127.0.0.1/entity/person/ram",  # loopback (SSRF classic)
            "http://169.254.169.254/entity/person/ram",  # cloud metadata host
            "http://jawafdehi.org@evil.com/entity/person/ram",  # userinfo confusion
        ],
    )
    def test_foreign_host_canonicalizes_to_one_authority_same_path(self, hostile):
        # Canonicalization keeps ONLY the /entity/<prefix>/<slug> path and
        # re-emits it on the canonical authority. The hostile host is discarded.
        out = canonicalize_entity_iri(hostile)
        assert out == f"{CANONICAL}/entity/person/ram"
        # The path is what's preserved — the attacker cannot pivot the resource.
        assert parse_entity_iri(out) == parse_entity_iri(hostile)

    @pytest.mark.parametrize(
        "hostile",
        [
            "http://evil.com/entity/person/ram",
            "https://jawafdehi.org.evil.com/entity/person/ram",
            "http://169.254.169.254/entity/person/ram",
        ],
    )
    def test_strict_validator_rejects_foreign_host(self, hostile):
        # A join-key validator must REJECT a non-canonical host outright.
        assert is_valid_entity_iri(hostile) is False
        # ...while the lenient shape-only check accepts the path grammar.
        assert is_valid_entity_iri(hostile, any_host=True) is True

    def test_material_and_case_strict_validators_reject_foreign_host(self):
        assert is_valid_material_iri("http://evil.com/material/court/x") is False
        assert is_valid_case_iri("http://evil.com/case/foo") is False
        # Canonicalizers re-key to the same-path canonical resource.
        assert (
            canonicalize_material_iri("http://evil.com/material/court/x")
            == f"{CANONICAL}/material/court/x"
        )
        assert canonicalize_case_iri("http://evil.com/case/foo") == f"{CANONICAL}/case/foo"

    def test_resolve_ref_rekeys_foreign_host_to_canonical_path(self):
        # The entity detail resolver: a foreign-host ref resolves to the
        # canonical authority + SAME path — never a foreign resource.
        assert (
            ev._resolve_ref("http://evil.com/entity/person/ram")
            == f"{CANONICAL}/entity/person/ram"
        )
        # A canonical-host ref resolves to itself (idempotent).
        assert (
            ev._resolve_ref(f"{CANONICAL}/entity/person/ram")
            == f"{CANONICAL}/entity/person/ram"
        )

    def test_material_iri_param_foreign_host_kept_then_strict_rejected(self):
        # _normalize_iri_param only prefixes bare /material/ paths; a full
        # foreign-host IRI is passed through UNCHANGED and then the strict
        # is_valid_material_iri gate in the view rejects it (→ 400).
        foreign = "http://evil.com/material/court/x"
        assert mv._normalize_iri_param(foreign) == foreign
        assert is_valid_material_iri(foreign) is False


# ===========================================================================
# 2. Path traversal — ../, encoded ..%2F, %2e%2e, absolute paths — must never
#    escape the /entity/ or /material/ namespace.
# ===========================================================================


class TestPathTraversal:
    @pytest.mark.parametrize(
        "ref",
        [
            "../../etc/passwd",
            "..%2F..%2Fetc%2Fpasswd",
            "%2e%2e/%2e%2e/x",
            "..%2f..%2fetc",
            "/etc/passwd",  # absolute path
            "person/../../../admin",
            "..\\..\\windows",  # backslash traversal
            "....//....//etc",  # doubled-dot bypass attempt
        ],
    )
    def test_entity_ref_traversal_rejected(self, ref):
        # None means "no valid join key" → the view returns 400/404, never a
        # filesystem/foreign resource.
        assert ev._resolve_ref(ref) is None

    @pytest.mark.parametrize(
        "source,ident",
        [
            ("../etc", "x"),
            ("court", "../etc"),
            ("court", ".."),
            ("..", ".."),
            ("court", "./x"),
        ],
    )
    def test_material_traversal_rejected(self, source, ident):
        # build_material_iri validates the grammar; traversal segments do not
        # match the source/ident classes → ValueError.
        with pytest.raises(ValueError):
            build_material_iri(source, ident)

    def test_material_bare_traversal_iri_invalid(self):
        # A bare /material/ path with traversal normalizes but fails the strict
        # validator (path is not the source/ident grammar) → 400 at the view.
        normalized = mv._normalize_iri_param("/material/../../etc")
        assert is_valid_material_iri(normalized) is False


# ===========================================================================
# 3. URL-encoding tricks — %2F (encoded slash), double-encoding, mixed. The
#    resolver must decode PREDICTABLY (single unquote) or reject; it must never
#    double-decode into a different key.
# ===========================================================================


class TestEncodingTricks:
    def test_single_encoded_slash_decodes_predictably(self):
        # _resolve_ref does exactly one unquote: %2F → '/', so an encoded
        # prefix/slug resolves to the SAME key as its literal form.
        assert (
            ev._resolve_ref("person%2Fram-bahadur")
            == ev._resolve_ref("person/ram-bahadur")
            == f"{CANONICAL}/entity/person/ram-bahadur"
        )

    @pytest.mark.parametrize(
        "ref",
        [
            "person%252Fram-bahadur",  # double-encoded slash
            "%252e%252e/x",  # double-encoded ..
            "person%00/ram",  # encoded NUL survives single-decode → invalid slug
        ],
    )
    def test_double_or_residual_encoding_not_re_decoded(self, ref):
        # After a SINGLE unquote the leftover literal '%25.../%00' is not a
        # valid IRI/path → None. Crucially it is NOT decoded a second time.
        assert ev._resolve_ref(ref) is None

    def test_uppercase_scheme_not_treated_as_iri(self):
        # The http(s):// prefix check is case-sensitive; an uppercased scheme is
        # not an IRI and 'HTTPS:...' has no '/' prefix path → None (not a 500).
        assert ev._resolve_ref("HTTPS://jawafdehi.org/entity/person/x") is None


# ===========================================================================
# 4. Over-length — an IRI longer than MAX_IRI_LENGTH (300) must be REJECTED,
#    never truncated-then-matched (which could collide onto a stored key).
# ===========================================================================


class TestOverLength:
    def test_max_iri_length_is_300(self):
        assert MAX_IRI_LENGTH == 300

    def test_overlength_entity_iri_rejected_by_validator(self):
        huge = f"{CANONICAL}/entity/person/" + ("a" * 400)
        assert len(huge) > MAX_IRI_LENGTH
        assert is_valid_entity_iri(huge) is False
        assert is_valid_entity_iri(huge, any_host=True) is False

    def test_overlength_ref_does_not_resolve(self):
        huge = f"{CANONICAL}/entity/person/" + ("a" * 400)
        # canonicalize raises (canonical result would exceed MAX) → resolver
        # swallows to None; no truncated match.
        assert ev._resolve_ref(huge) is None

    def test_overlength_material_iri_rejected(self):
        huge = f"{CANONICAL}/material/court/" + ("a" * 400)
        assert is_valid_material_iri(huge) is False
        with pytest.raises(ValueError):
            build_material_iri("court", "a" * 400)


# ===========================================================================
# 5. NUL byte / control chars — must be rejected at the contract boundary,
#    never handed to the DB (SQLite/Postgres reject NUL; the point is we stop
#    it before the query).
# ===========================================================================


class TestNullAndControlChars:
    @pytest.mark.parametrize(
        "ref",
        [
            "person/ram\x00bad",
            "person\x00/ram",
            "person/ram\nbad",
            "person/ram\tbad",
            "person/ram\rbad",
            "\x00",
        ],
    )
    def test_control_char_ref_rejected(self, ref):
        assert ev._resolve_ref(ref) is None

    def test_control_char_iri_not_valid(self):
        assert is_valid_entity_iri(f"{CANONICAL}/entity/person/ram\x00bad") is False

    def test_control_char_material_ident_rejected(self):
        with pytest.raises(ValueError):
            build_material_iri("court", "x\x00y")
        with pytest.raises(ValueError):
            parse_material_iri(f"{CANONICAL}/material/court/x\x00y")


# ===========================================================================
# 6. Scheme confusion — javascript:/file://data: as an @id must be rejected.
# ===========================================================================


class TestSchemeConfusion:
    @pytest.mark.parametrize(
        "ref",
        [
            "javascript:alert(1)",
            "file:///etc/passwd",
            "data:text/html,<script>alert(1)</script>",
            "ftp://evil.com/entity/person/x",
            "gopher://evil/entity/person/x",
            "  javascript:alert(1)",  # leading whitespace
        ],
    )
    def test_non_http_scheme_ref_rejected(self, ref):
        # _resolve_ref only treats http(s):// as an IRI; anything else with no
        # '/' path segment → None. (file:///... has slashes but rpartition on
        # 'file:/etc/passwd' yields a prefix that fails the slug grammar.)
        assert ev._resolve_ref(ref) is None

    @pytest.mark.parametrize(
        "iri",
        [
            "javascript:alert(1)",
            "file:///etc/passwd",
            "data:text/html,x",
            "ftp://evil.com/entity/person/x",
        ],
    )
    def test_non_http_scheme_not_a_valid_iri(self, iri):
        assert is_valid_entity_iri(iri, any_host=True) is False
        assert is_valid_material_iri(iri, any_host=True) is False
        assert is_valid_case_iri(iri, any_host=True) is False


# ===========================================================================
# 7. Multi-segment prefix correctness (NOT an attack — a guard that the
#    hardening didn't break legit nested refs). A 1-4 segment prefix must
#    resolve to the RIGHT (prefix, slug), and a hyphenated court number must
#    round-trip through the material ident (partition on first '.').
# ===========================================================================


class TestMultiSegmentCorrectness:
    def test_nested_prefix_resolves_to_right_prefix_and_slug(self):
        # rpartition splits off ONLY the final segment as the slug.
        iri = ev._resolve_ref("govt/office/ciaa/head-office")
        assert iri == f"{CANONICAL}/entity/govt/office/ciaa/head-office"
        parsed = parse_entity_iri(iri)
        assert parsed.prefix == "govt/office/ciaa"
        assert parsed.slug == "head-office"

    def test_two_segment_prefix(self):
        parsed = parse_entity_iri(ev._resolve_ref("govt/office/head-office"))
        assert (parsed.prefix, parsed.slug) == ("govt/office", "head-office")

    def test_full_iri_ref_nested_prefix(self):
        iri = ev._resolve_ref(f"{CANONICAL}/entity/govt/office/ciaa/head-office")
        assert parse_entity_iri(iri).prefix == "govt/office/ciaa"

    def test_hyphenated_court_number_round_trips_in_material_ident(self):
        # A court material IRI is /material/court/<court>.<case_number>; the
        # derivation partitions on the FIRST '.' so a hyphenated case number
        # (082-OA-0503, lowercased) survives intact.
        ident = "special.082-oa-0503"
        iri = build_material_iri("court", ident)
        assert iri == f"{CANONICAL}/material/court/special.082-oa-0503"
        parsed = parse_material_iri(iri)
        assert parsed.source == "court"
        assert parsed.ident == ident
        # Mirror the resolver's partition-on-first-'.' step.
        court_identifier, _, case_number = parsed.ident.partition(".")
        assert court_identifier == "special"
        assert case_number == "082-oa-0503"

    def test_iri_base_is_the_one_canonical_authority(self):
        assert iri_base() == CANONICAL


# ===========================================================================
# Endpoint-level smoke: the full request path must land on 400/404 for every
# hostile ref (never 500, never a foreign/unexpected resource) and must
# correctly re-key a foreign host onto the caller's OWN seeded resource.
# ===========================================================================


@pytest.mark.django_db(databases="__all__")
class TestEndpointHardening:
    @pytest.mark.parametrize(
        "ref",
        [
            "http://evil.com/entity/person/does-not-exist",  # foreign, absent
            "../../etc/passwd",
            "..%2F..%2Fetc%2Fpasswd",
            "%2e%2e/%2e%2e/x",
            "javascript:alert(1)",
            "file:///etc/passwd",
        ],
    )
    def test_entity_detail_hostile_ref_is_400_or_404_never_500(self, ref):
        resp = APIClient().get("/api/entities/" + quote(ref, safe=""))
        assert resp.status_code in (400, 404), resp.content
        assert resp.status_code < 500

    def test_entity_detail_overlength_ref_not_500(self):
        huge = f"{CANONICAL}/entity/person/" + ("a" * 400)
        resp = APIClient().get("/api/entities/" + quote(huge, safe=""))
        assert resp.status_code in (400, 404), resp.content

    def test_entity_detail_nullbyte_ref_not_500(self):
        resp = APIClient().get("/api/entities/" + quote("person/ram\x00bad", safe=""))
        assert resp.status_code in (400, 404), resp.content

    def test_foreign_host_rekeys_onto_own_seeded_resource(self):
        # Seed a real entity, then request it via a FOREIGN-host @id with the
        # SAME path. It must resolve to the caller's own canonical resource —
        # proving the foreign host is dropped, not honored, and cannot be used
        # to pivot to a different resource.
        from entities.services.publication import PublicationService
        from entities.write_validation import normalize_authoring_payload

        PublicationService().create_entity(
            doc=normalize_authoring_payload(
                {
                    "prefix": "person",
                    "slug": "seeded-target",
                    "type": "Person",
                    "name": {"en": "Seeded Target"},
                }
            ),
            author_id="oidc:seed",
            change_description="seed",
        )
        client = APIClient()
        foreign = client.get(
            "/api/entities/"
            + quote("http://evil.com/entity/person/seeded-target", safe="")
        )
        assert foreign.status_code == 200, foreign.content
        assert foreign.json()["@id"] == f"{CANONICAL}/entity/person/seeded-target"

        # A foreign host with a DIFFERENT (absent) path must 404 — the host
        # buys the attacker nothing.
        absent = client.get(
            "/api/entities/"
            + quote("http://evil.com/entity/person/not-a-real-slug", safe="")
        )
        assert absent.status_code == 404

    @pytest.mark.parametrize(
        "iri",
        [
            "http://evil.com/material/court/x",  # foreign host → 400
            "https://jawafdehi.org/material/court/" + ("a" * 400),  # overlength → 400
            "javascript:alert(1)",
        ],
    )
    def test_material_by_iri_hostile_param_400(self, iri):
        resp = APIClient().get("/api/materials/?iri=" + quote(iri, safe=""))
        assert resp.status_code == 400, resp.content

    def test_material_detail_traversal_ident_not_500(self):
        # ident '..' matches the URL's [^/]+ but fails build_material_iri → 400.
        resp = APIClient().get("/api/materials/court/..")
        assert resp.status_code in (400, 404), resp.content
