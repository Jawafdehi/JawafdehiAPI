"""Tests for the shared entity/material id contract (canonical-authority join key).

Pure-python (no Django). Covers the canonicalize functions (re-key any
valid-shaped IRI onto the canonical authority), STRICT-by-default validation
(reject non-canonical host/scheme/port), the optional lenient ``any_host`` shape
check, and the MAX_IRI_LENGTH bound.
"""

import pytest

from jawafdehi_shared.entities.ids import (
    MAX_IRI_LENGTH,
    build_case_iri,
    build_courtcase_iri,
    canonicalize_case_iri,
    canonicalize_courtcase_iri,
    canonicalize_entity_iri,
    canonicalize_material_iri,
    is_valid_case_iri,
    is_valid_courtcase_iri,
    is_valid_entity_iri,
    is_valid_material_iri,
    iri_base,
    parse_case_iri,
    parse_courtcase_iri,
)

CANON = "https://jawafdehi.org"


# ── canonicalize: re-key any valid-shaped IRI onto the canonical authority ───

@pytest.mark.parametrize(
    "given,expected",
    [
        ("http://evil.com/entity/person/ram", f"{CANON}/entity/person/ram"),
        ("https://x:8443/entity/person/ram", f"{CANON}/entity/person/ram"),
        (f"{CANON}/entity/person/ram", f"{CANON}/entity/person/ram"),
        ("http://jawafdehi.org/entity/org/some-ministry", f"{CANON}/entity/org/some-ministry"),
    ],
)
def test_canonicalize_entity_iri_rewrites_authority(given, expected):
    assert canonicalize_entity_iri(given) == expected


@pytest.mark.parametrize(
    "given,expected",
    [
        ("http://evil.com/material/court/sc.123", f"{CANON}/material/court/sc.123"),
        ("https://x:8443/material/ciaa/pr-9", f"{CANON}/material/ciaa/pr-9"),
        (f"{CANON}/material/court/sc.123", f"{CANON}/material/court/sc.123"),
    ],
)
def test_canonicalize_material_iri_rewrites_authority(given, expected):
    assert canonicalize_material_iri(given) == expected


@pytest.mark.parametrize("bad", ["", "not-an-iri", f"{CANON}/wrong/person/ram", f"{CANON}/entity/"])
def test_canonicalize_rejects_malformed(bad):
    with pytest.raises(ValueError):
        canonicalize_entity_iri(bad)


def test_canonicalize_is_idempotent():
    once = canonicalize_entity_iri("http://evil.com/entity/person/ram")
    assert canonicalize_entity_iri(once) == once


# ── strict validation: reject non-canonical authority ───────────────────────

@pytest.mark.parametrize(
    "value",
    [
        "http://evil.com/entity/person/ram",
        "https://x:8443/entity/person/ram",
        "http://jawafdehi.org/entity/person/ram",  # wrong scheme
    ],
)
def test_is_valid_entity_iri_strict_rejects_noncanonical_host(value):
    assert not is_valid_entity_iri(value)
    # ...but the lenient shape-only check still accepts the path grammar.
    assert is_valid_entity_iri(value, any_host=True)


def test_is_valid_entity_iri_accepts_canonical():
    assert is_valid_entity_iri(f"{CANON}/entity/person/ram")


@pytest.mark.parametrize(
    "value",
    [
        "http://evil.com/material/court/sc.123",
        "https://x:8443/material/court/sc.123",
    ],
)
def test_is_valid_material_iri_strict_rejects_noncanonical_host(value):
    assert not is_valid_material_iri(value)
    assert is_valid_material_iri(value, any_host=True)


def test_is_valid_material_iri_accepts_canonical():
    assert is_valid_material_iri(f"{CANON}/material/court/sc.123")


# ── MAX_IRI_LENGTH bound ─────────────────────────────────────────────────────

def test_max_iri_length_rejected_by_validators():
    long_slug = "a" * MAX_IRI_LENGTH  # guarantees the full IRI exceeds the cap
    long_entity = f"{CANON}/entity/person/{long_slug}"
    assert len(long_entity) > MAX_IRI_LENGTH
    assert not is_valid_entity_iri(long_entity)
    assert not is_valid_entity_iri(long_entity, any_host=True)
    long_material = f"{CANON}/material/court/{long_slug}"
    assert not is_valid_material_iri(long_material)


def test_iri_base_is_canonical_default():
    assert iri_base() == CANON


# ── case IRIs (Jawafdehi cases, /case/<slug>) ────────────────────────────────

def test_build_case_iri_canonical():
    assert build_case_iri("case-078-wc-0123-abc123") == (
        f"{CANON}/case/case-078-wc-0123-abc123"
    )


def test_parse_case_iri_roundtrip():
    iri = build_case_iri("my-case-slug")
    assert parse_case_iri(iri).slug == "my-case-slug"


def test_case_iri_grammar_matches_validate_slug():
    # The case IRI slug grammar MUST match the authoritative validate_slug
    # (^[a-zA-Z][a-zA-Z0-9-]{0,49}$): UPPERCASE allowed, underscores FORBIDDEN.
    # (Case slug generation strips underscores, so valid data never has them.)
    iri = build_case_iri("case-078-WC-0123-sunil-poudel")  # uppercase ok
    assert iri == f"{CANON}/case/case-078-WC-0123-sunil-poudel"
    assert is_valid_case_iri(iri)
    assert parse_case_iri(iri).slug == "case-078-WC-0123-sunil-poudel"
    # Underscores are not valid slug chars → rejected.
    assert not is_valid_case_iri(f"{CANON}/case/has_underscore")
    with pytest.raises(ValueError):
        build_case_iri("has_underscore")


@pytest.mark.parametrize(
    "given,expected",
    [
        ("http://evil.com/case/ram-slug", f"{CANON}/case/ram-slug"),
        ("https://x:8443/case/ram-slug", f"{CANON}/case/ram-slug"),
        (f"{CANON}/case/ram-slug", f"{CANON}/case/ram-slug"),
    ],
)
def test_canonicalize_case_iri_rewrites_authority(given, expected):
    assert canonicalize_case_iri(given) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://evil.com/case/ram-slug",
        "https://x:8443/case/ram-slug",
        "http://jawafdehi.org/case/ram-slug",  # wrong scheme
    ],
)
def test_is_valid_case_iri_strict_rejects_noncanonical_host(value):
    assert not is_valid_case_iri(value)
    assert is_valid_case_iri(value, any_host=True)


def test_is_valid_case_iri_accepts_canonical():
    assert is_valid_case_iri(f"{CANON}/case/some-slug")


@pytest.mark.parametrize(
    "bad",
    ["", "not-an-iri", f"{CANON}/wrong/slug", f"{CANON}/case/", f"{CANON}/case/-bad"],
)
def test_is_valid_case_iri_rejects_malformed(bad):
    assert not is_valid_case_iri(bad)
    with pytest.raises(ValueError):
        parse_case_iri(bad)


def test_build_case_iri_rejects_invalid_slug():
    # Uppercase / leading non-letter are not valid slug grammar.
    with pytest.raises(ValueError):
        build_case_iri("Bad Slug")


def test_case_iri_max_length():
    long_slug = "a" * MAX_IRI_LENGTH
    assert not is_valid_case_iri(f"{CANON}/case/{long_slug}")
    with pytest.raises(ValueError):
        build_case_iri(long_slug)


# ── court-case IRIs (NGM CourtCase, /courtcase/<court>/<case_number>) ─────────

def test_build_courtcase_iri_canonical():
    assert build_courtcase_iri("supreme", "081-CR-0081") == (
        f"{CANON}/courtcase/supreme/081-cr-0081"
    )


def test_build_courtcase_iri_lowercases():
    # court + case_number are lowercased for a stable, reconstructable key.
    assert build_courtcase_iri("SPECIAL", "076-CR-0456") == (
        f"{CANON}/courtcase/special/076-cr-0456"
    )


def test_parse_courtcase_iri_roundtrip():
    iri = build_courtcase_iri("special", "076-cr-0456")
    parsed = parse_courtcase_iri(iri)
    assert (parsed.court, parsed.case_number) == ("special", "076-cr-0456")


@pytest.mark.parametrize(
    "given,expected",
    [
        ("http://evil.com/courtcase/supreme/081-cr-0081", f"{CANON}/courtcase/supreme/081-cr-0081"),
        ("https://x:8443/courtcase/supreme/081-cr-0081", f"{CANON}/courtcase/supreme/081-cr-0081"),
        (f"{CANON}/courtcase/supreme/081-cr-0081", f"{CANON}/courtcase/supreme/081-cr-0081"),
    ],
)
def test_canonicalize_courtcase_iri_rewrites_authority(given, expected):
    assert canonicalize_courtcase_iri(given) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://evil.com/courtcase/supreme/081-cr-0081",
        "https://x:8443/courtcase/supreme/081-cr-0081",
        "http://jawafdehi.org/courtcase/supreme/081-cr-0081",  # wrong scheme
    ],
)
def test_is_valid_courtcase_iri_strict_rejects_noncanonical_host(value):
    assert not is_valid_courtcase_iri(value)
    assert is_valid_courtcase_iri(value, any_host=True)


def test_is_valid_courtcase_iri_accepts_canonical():
    assert is_valid_courtcase_iri(f"{CANON}/courtcase/supreme/081-cr-0081")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-an-iri",
        f"{CANON}/courtcase/supreme",  # missing case_number
        f"{CANON}/courtcase//081-cr-0081",  # missing court
        f"{CANON}/wrong/supreme/081-cr-0081",
    ],
)
def test_is_valid_courtcase_iri_rejects_malformed(bad):
    assert not is_valid_courtcase_iri(bad)
    with pytest.raises(ValueError):
        parse_courtcase_iri(bad)


def test_courtcase_iri_max_length():
    long_num = "a" * MAX_IRI_LENGTH
    assert not is_valid_courtcase_iri(f"{CANON}/courtcase/supreme/{long_num}")
    with pytest.raises(ValueError):
        build_courtcase_iri("supreme", long_num)
