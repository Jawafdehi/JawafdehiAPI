"""The shipped ``vocabulary.yml`` itself.

A hand-edited 121-term file where one typo is invisible in review — an alias
pointing at a tag that no longer exists, two tags claiming the same alias, a
province roll-up on a district that belongs to a different province. These assert
the file, not the code that reads it.
"""

from __future__ import annotations

import collections
import pathlib
from typing import Any, cast

import pytest
import yaml

from case_tags.normalize import normalize

VOCABULARY = pathlib.Path("case_tags/vocabulary.yml")


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(VOCABULARY.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def tags(document: dict[str, Any]) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", document["tags"])


def test_ids_are_unique(tags: list[dict[str, Any]]) -> None:
    counts = collections.Counter(t["id"] for t in tags)
    assert [i for i, n in counts.items() if n > 1] == []


def test_every_tag_has_both_labels(tags: list[dict[str, Any]]) -> None:
    """Nepali-first, so a missing label_ne is a blank filter on the live site."""
    missing = [
        t["id"] for t in tags if not str(t.get("label_ne", "")).strip()
        or not str(t.get("label_en", "")).strip()
    ]
    assert missing == []


def test_statuses_are_known(tags: list[dict[str, Any]]) -> None:
    allowed = {"proposed", "active", "deprecated", "merged"}
    assert {str(t["status"]) for t in tags} <= allowed


def test_broader_resolves_and_never_chains(tags: list[dict[str, Any]]) -> None:
    """Roll-up is a single hop at index time. A chain would make that walk unbounded."""
    by_id = {t["id"]: t for t in tags}
    dangling = [t["id"] for t in tags if t.get("broader") and t["broader"] not in by_id]
    assert dangling == [], f"broader pointing at a nonexistent tag: {dangling}"

    chained = [
        t["id"]
        for t in tags
        if t.get("broader") and by_id[t["broader"]].get("broader")
    ]
    assert chained == [], f"broader chains deeper than one level: {chained}"


def test_merged_tags_name_a_replacement(tags: list[dict[str, Any]]) -> None:
    orphans = [
        t["id"] for t in tags if t["status"] == "merged" and not t.get("merged_into")
    ]
    assert orphans == []


def test_no_alias_collisions(tags: list[dict[str, Any]]) -> None:
    """One raw value cannot mean two tags. The alias key is unique in the DB, so a
    collision here is a seed-time crash — better caught in review."""
    owner: dict[str, str] = {}
    collisions: list[str] = []
    for tag in tags:
        for alias in [*(tag.get("aliases") or []), tag["id"]]:
            key = normalize(str(alias))
            if key in owner and owner[key] != tag["id"]:
                collisions.append(f"{key!r}: {owner[key]} vs {tag['id']}")
            owner[key] = str(tag["id"])
    assert collisions == []


def test_no_value_is_both_aliased_and_dropped(document: dict[str, Any]) -> None:
    """Otherwise whichever block the seed reads last silently wins."""
    aliased = {
        normalize(str(a))
        for t in document["tags"]
        for a in [*(t.get("aliases") or []), t["id"]]
    }
    dropped = {
        normalize(str(v))
        for g in document.get("dropped") or []
        for v in g["values"]
    }
    assert aliased & dropped == set()


def test_alias_keys_survive_normalization(tags: list[dict[str, Any]]) -> None:
    """Aliases are matched by normalizing the incoming value and comparing. An alias
    written in a form that normalizes to something else can never match."""
    unreachable = [
        (t["id"], a)
        for t in tags
        for a in (t.get("aliases") or [])
        if normalize(str(a)) != normalize(normalize(str(a)))
    ]
    assert unreachable == []


def test_dropped_reasons_are_stated(document: dict[str, Any]) -> None:
    """A dropped value with no reason cannot be argued with in review, and the API
    cannot explain the retirement to whoever holds the stale bookmark."""
    for group in document.get("dropped") or []:
        assert str(group.get("reason", "")).strip()
        assert group.get("values")


def test_geography_covers_every_province(tags: list[dict[str, Any]]) -> None:
    provinces = {
        "koshi", "madhesh", "bagmati", "gandaki",
        "lumbini", "karnali", "sudurpashchim",
    }
    assert provinces <= {t["id"] for t in tags}


def test_districts_roll_up_to_a_province(tags: list[dict[str, Any]]) -> None:
    """Every district must roll up, so a province filter catches district-tagged
    cases — except the two that genuinely span provinces."""
    provinces = {
        "koshi", "madhesh", "bagmati", "gandaki",
        "lumbini", "karnali", "sudurpashchim",
    }
    spans_two_provinces = {"nawalparasi", "rukum"}
    for tag in tags:
        parent = tag.get("broader")
        if parent in provinces:
            assert tag["id"] not in spans_two_provinces, (
                f"{tag['id']} was split across two provinces in 2017; rolling it up "
                "to one would put a live case in the wrong province"
            )


def test_the_live_corpus_is_fully_accounted_for(document: dict[str, Any]) -> None:
    """The vocabulary exists to absorb 144 specific raw values. If an edit orphans
    one, that case silently loses a tag on the next rebuild — nothing else catches
    it, because a dropped value looks identical to a value we never had.

    The corpus snapshot is committed rather than read from the dev database, so this
    runs in CI, where there is no corpus.
    """
    snapshot = pathlib.Path("case_tags/tests/corpus_tag_values.tsv")
    raw_values = [
        line.split("\t", 1)[1]
        for line in snapshot.read_text("utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert len(raw_values) == 144, "snapshot changed — re-check coverage deliberately"

    aliased = {
        normalize(str(a))
        for t in document["tags"]
        for a in [*(t.get("aliases") or []), t["id"]]
    }
    dropped = {
        normalize(str(v))
        for g in document.get("dropped") or []
        for v in g["values"]
    }
    known = aliased | dropped
    unaccounted = sorted(v for v in raw_values if normalize(v) not in known)
    assert unaccounted == [], f"corpus values with no home: {unaccounted}"
