"""Unit tests for the review engine's source-link role rules.

Covers the new `source_link_roles_valid` gate (exactly one RAW per source,
valid roles only) and the RAW-awareness of the existing `sourcing` detector.
Both read the role-tagged `urls` ([{link, role}]) that jds_client attaches to
each source, and must remain lenient toward the legacy flat-`url` shape.
"""

from review.rules_engine import source_link_roles_valid, sourcing


def _ev(title, urls=None, url=None, source_type="OFFICIAL_GOVERNMENT", source_id=None):
    """Build one evidence item with a nested source dict."""
    src = {"title": title, "source_type": source_type}
    if urls is not None:
        src["urls"] = urls
    if url is not None:
        src["url"] = url
    return {"source_id": source_id or title, "source": src}


def _case(*evidence):
    return {"evidence": list(evidence)}


# ----------------------- source_link_roles_valid (gate) -----------------------


def test_single_raw_passes():
    case = _case(
        _ev("Press release", [{"link": "https://x/a.pdf", "role": "RAW"}]),
        _ev(
            "Verdict",
            [
                {"link": "https://x/b.pdf", "role": "RAW"},
                {"link": "https://web.archive.org/b", "role": "PERMALINK"},
            ],
        ),
    )
    score, issues = source_link_roles_valid(case)
    assert score == 100
    assert not issues


def test_multiple_raw_fails():
    case = _case(
        _ev(
            "PR",
            [
                {"link": "https://ciaa.gov.np/p/1", "role": "RAW"},
                {"link": "https://x/1.doc", "role": "RAW"},
                {"link": "https://x/1.pdf", "role": "RAW"},
            ],
        ),
    )
    score, issues = source_link_roles_valid(case)
    assert score == 0
    assert any("RAW links" in i for i in issues)


def test_no_raw_fails():
    case = _case(_ev("MD only", [{"link": "https://x/a.md", "role": "MARKDOWN"}]))
    score, issues = source_link_roles_valid(case)
    assert score == 0
    assert any("no canonical (RAW)" in i for i in issues)


def test_empty_links_fails():
    case = _case(_ev("Empty", []))
    score, issues = source_link_roles_valid(case)
    assert score == 0
    assert any("no links" in i for i in issues)


def test_invalid_role_fails():
    case = _case(
        _ev(
            "Weird",
            [
                {"link": "https://x/a.pdf", "role": "RAW"},
                {"link": "https://x/b", "role": "SECONDARY"},
            ],
        )
    )
    score, issues = source_link_roles_valid(case)
    assert score == 0
    assert any("invalid link role" in i for i in issues)


def test_missing_role_coerced_to_raw_passes():
    # A link with no/None role is normalized to RAW by the model
    # (normalize_url_list); the gate must treat it the same and not flag it as
    # an invalid role or a missing RAW. (Also guards against a sorted() crash
    # when a None role co-occurs with an invalid string role.)
    case = _case(_ev("No role", [{"link": "https://x/a.pdf"}]))
    score, issues = source_link_roles_valid(case)
    assert score == 100
    assert not issues


def test_missing_role_plus_invalid_role_does_not_crash():
    case = _case(
        _ev(
            "Mixed",
            [
                {"link": "https://x/a.pdf"},  # None role -> RAW
                {"link": "https://x/b", "role": "SECONDARY"},  # invalid
            ],
        )
    )
    score, issues = source_link_roles_valid(case)
    assert score == 0
    assert any("invalid link role" in i for i in issues)


def test_same_source_on_multiple_evidence_is_not_deduped():
    # The same source attached to two evidence rows is judged per attachment
    # (no dedupe), so two bad copies produce two findings.
    bad = _ev("Dup", [], source_id="dup")
    case = _case(bad, bad)
    score, issues = source_link_roles_valid(case)
    assert score == 0
    assert sum(1 for i in issues if "no links" in i) == 2


def test_legacy_flat_url_is_exempt():
    # A source still on the deprecated flat `url` list (no role-tagged `urls`)
    # is treated as a single RAW by normalize_url_list, so it must not fail.
    case = _case(_ev("Legacy", urls=None, url=["https://x/legacy.pdf"]))
    score, issues = source_link_roles_valid(case)
    assert score == 100
    assert not issues


def test_no_sources_passes():
    # Other rules gate on source presence; this one only judges shape.
    score, issues = source_link_roles_valid(_case())
    assert score == 100
    assert not issues


# ----------------------- sourcing detector (RAW-aware) -----------------------


def test_sourcing_counts_raw_not_just_any_url():
    # A markdown-only source has a link but no RAW -> flagged as lacking a
    # canonical document, lowering the URL component of the score.
    case = _case(
        _ev("Has RAW", [{"link": "https://x/a.pdf", "role": "RAW"}]),
        _ev("MD only", [{"link": "https://x/b.md", "role": "MARKDOWN"}]),
    )
    score, issues = sourcing(case)
    assert any("no canonical (RAW) document link" in i for i in issues)


def test_sourcing_legacy_flat_url_counts_as_raw():
    case = _case(
        _ev("L1", urls=None, url=["https://x/1.pdf"]),
        _ev("L2", urls=None, url=["https://x/2.pdf"]),
    )
    _, issues = sourcing(case)
    assert not any("RAW" in i for i in issues)
