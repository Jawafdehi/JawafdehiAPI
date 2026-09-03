"""The shipped ``curation.yml``.

Every entry is an editorial claim about a real legal case, justified by a `why`.
These check the claims are well-formed — that the tags exist, that no entry
contradicts another, and that each `why` actually says something. Whether a claim
is *right* is a human review question; this is what can be checked mechanically.

Slugs are deliberately NOT checked here: that needs the corpus, and
``rebuild_case_tags`` already refuses to run on an unresolvable one.
"""

from __future__ import annotations

import collections
import pathlib
from typing import Any, cast

import pytest
import yaml

CURATION = pathlib.Path("case_tags/curation.yml")
VOCABULARY = pathlib.Path("case_tags/vocabulary.yml")


@pytest.fixture(scope="module")
def entries() -> list[dict[str, Any]]:
    document = cast("dict[str, Any]", yaml.safe_load(CURATION.read_text("utf-8")))
    return cast("list[dict[str, Any]]", document["cases"])


@pytest.fixture(scope="module")
def vocabulary() -> dict[str, dict[str, Any]]:
    document = cast("dict[str, Any]", yaml.safe_load(VOCABULARY.read_text("utf-8")))
    return {t["id"]: t for t in document["tags"]}


def test_slugs_are_unique(entries: list[dict[str, Any]]) -> None:
    """Two entries for one case would make the file mean different things depending
    on read order. The command rejects it; this catches it in review."""
    counts = collections.Counter(e["slug"] for e in entries)
    assert [s for s, n in counts.items() if n > 1] == []


def test_every_entry_does_something(entries: list[dict[str, Any]]) -> None:
    no_op = [e["slug"] for e in entries if not (e.get("add") or e.get("remove"))]
    assert no_op == []


def test_every_entry_justifies_itself(entries: list[dict[str, Any]]) -> None:
    """A `why` is what makes this reviewable rather than a pile of assertions. The
    command requires non-empty; require it to be a sentence, not a placeholder."""
    weak = [
        e["slug"] for e in entries if len(str(e.get("why", "")).strip()) < 20
    ]
    assert weak == [], f"entries with no real justification: {weak}"


def test_added_tags_are_active(
    entries: list[dict[str, Any]], vocabulary: dict[str, dict[str, Any]]
) -> None:
    """Adding a deprecated or merged tag puts a case onto a filter being retired."""
    bad = [
        (e["slug"], t)
        for e in entries
        for t in e.get("add") or []
        if vocabulary.get(t, {}).get("status") != "active"
    ]
    assert bad == []


def test_removed_tags_exist(
    entries: list[dict[str, Any]], vocabulary: dict[str, dict[str, Any]]
) -> None:
    """``remove`` may name a DEPRECATED tag — that is its main use — but not one
    that does not exist, which would be a silent no-op."""
    bad = [
        (e["slug"], t)
        for e in entries
        for t in e.get("remove") or []
        if t not in vocabulary
    ]
    assert bad == []


def test_no_entry_adds_and_removes_the_same_tag(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        overlap = set(entry.get("add") or []) & set(entry.get("remove") or [])
        assert overlap == set(), f"{entry['slug']} both adds and removes {overlap}"


def test_the_deprecated_valley_tag_is_cleared_everywhere_it_is_named(
    entries: list[dict[str, Any]],
) -> None:
    """``kathmandu-valley`` is deprecated and sat on nine cases. Any entry that
    mentions it must be removing it, never adding it."""
    for entry in entries:
        assert "kathmandu-valley" not in (entry.get("add") or [])
    removals = sum(
        1 for e in entries if "kathmandu-valley" in (e.get("remove") or [])
    )
    assert removals == 9, (
        f"expected all 9 kathmandu-valley cases to be corrected, found {removals}"
    )


def test_uncurated_cases_are_documented(entries: list[dict[str, Any]]) -> None:
    """The six cases we could NOT call confidently are listed in a trailing comment
    block, so they read as outstanding work rather than as an oversight. If someone
    curates one, they should delete it from that list."""
    text = CURATION.read_text("utf-8")
    assert "NOT curated" in text
    curated = {e["slug"] for e in entries}
    documented = [
        "case-081-cr-0046-240817ce",
        "rabi-lamichhane-cooperative-fr-11ccd7",
        "case-081-cr-0107-patanjali",
        "case-ncell-capital-gains-tax-dispute-part1",
        "case-ncell-capital-gains-tax-dispute-part2",
        "case-ncell-tax-dispute-icsid-arbitration-part3",
    ]
    for slug in documented:
        assert slug in text, f"{slug} should be named in the NOT-curated block"
        assert slug not in curated, (
            f"{slug} is now curated — remove it from the NOT-curated block"
        )
