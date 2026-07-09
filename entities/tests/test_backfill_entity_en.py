"""Tests for the backfill_entity_en management command (generate-to-CSV).

The LLM call is mocked; these pin the command's plumbing: it loads entities that
have a Devanagari name, produces a mutations CSV with the right columns, marks
`changed` correctly, carries alternates, and the scrub-net drops any residual
IAST the model returns. No network / no Bedrock.
"""

import csv
import json
from unittest import mock

import pytest
from django.core.management import call_command

from entities.persistence import EntityRepository

pytestmark = pytest.mark.django_db


def _put(repo, slug, ne, en, atype="Hospital", prefix="organization/hospital"):
    doc = {
        "@context": "https://schema.org",
        "@id": f"https://jawafdehi.org/entity/{prefix}/{slug}",
        "@type": atype,
        "name": {"ne": ne, **({"en": en} if en is not None else {})},
    }
    from datetime import datetime, timezone
    repo.put_entity(doc, version=1, created_at=datetime.now(timezone.utc))
    return doc["@id"]


def _fake_llm(system, prompt, max_tokens=8000, tier="cheap", usage=None):
    """Echo a canonical/alternates per item, injecting one IAST value to prove
    the scrub-net drops it."""
    items = json.loads(prompt.split("ITEMS:\n", 1)[1])
    names = []
    for it in items:
        ne = it["ne"]
        if ne == "नारायणी अस्पताल":
            names.append({"id": it["id"], "canonical": "Narayani Hospital",
                          "alternates": []})
        elif ne == "रवि":
            names.append({"id": it["id"], "canonical": "Ravi",
                          "alternates": ["Rabi"]})
        elif ne == "पद्मा":
            # model misbehaves and returns Harvard-Kyoto — scrub-net must drop it
            names.append({"id": it["id"], "canonical": "padmA", "alternates": []})
        else:
            names.append({"id": it["id"], "canonical": "X", "alternates": []})
    return {"names": names}


def _read(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


@mock.patch("entities.management.commands.backfill_entity_en.invoke_json",
            side_effect=_fake_llm)
def test_generates_csv_with_expected_columns(_m, tmp_path):
    repo = EntityRepository()
    iri = _put(repo, "narayani-hospital-x1", "नारायणी अस्पताल", "nArAyaNI aspatAla")
    out = tmp_path / "mutations.csv"
    call_command("backfill_entity_en", "--out", str(out), "--batch-size", "10")
    rows = _read(out)
    assert rows[0].keys() >= {"iri", "type", "ne", "old_en", "new_en",
                              "alternates", "changed"}
    row = next(r for r in rows if r["iri"] == iri)
    assert row["old_en"] == "nArAyaNI aspatAla"
    assert row["new_en"] == "Narayani Hospital"
    assert row["changed"] == "1"


@mock.patch("entities.management.commands.backfill_entity_en.invoke_json",
            side_effect=_fake_llm)
def test_alternates_carried(_m, tmp_path):
    repo = EntityRepository()
    iri = _put(repo, "ravi-p1", "रवि", None, atype="Person", prefix="person")
    out = tmp_path / "m.csv"
    call_command("backfill_entity_en", "--out", str(out))
    row = next(r for r in _read(out) if r["iri"] == iri)
    assert row["new_en"] == "Ravi"
    assert json.loads(row["alternates"]) == ["Rabi"]
    assert row["changed"] == "1"


@mock.patch("entities.management.commands.backfill_entity_en.invoke_json",
            side_effect=_fake_llm)
def test_scrub_net_drops_iast_from_model(_m, tmp_path):
    repo = EntityRepository()
    iri = _put(repo, "padma-x2", "पद्मा", None, atype="Organization",
               prefix="organization")
    out = tmp_path / "m.csv"
    call_command("backfill_entity_en", "--out", str(out))
    row = next(r for r in _read(out) if r["iri"] == iri)
    # 'padmA' was scrubbed -> empty new_en -> not a mutation
    assert row["new_en"] == ""
    assert row["changed"] == "0"


@mock.patch("entities.management.commands.backfill_entity_en.invoke_json",
            side_effect=_fake_llm)
def test_changed_only_filters(_m, tmp_path):
    repo = EntityRepository()
    # already-correct en that the model returns unchanged with no alternates
    _put(repo, "good-x3", "क", "X", atype="Organization", prefix="organization")
    out = tmp_path / "m.csv"
    call_command("backfill_entity_en", "--out", str(out), "--changed-only")
    rows = _read(out)
    assert all(r["changed"] == "1" for r in rows)


@mock.patch("entities.management.commands.backfill_entity_en.invoke_json",
            side_effect=_fake_llm)
def test_skips_entities_without_devanagari(_m, tmp_path):
    repo = EntityRepository()
    # name is a bare English string (no ne) -> not eligible
    from datetime import datetime, timezone
    repo.put_entity({
        "@context": "https://schema.org",
        "@id": "https://jawafdehi.org/entity/organization/eng-only-x4",
        "@type": "Organization",
        "name": "AsiaInfo Limited",
    }, version=1, created_at=datetime.now(timezone.utc))
    out = tmp_path / "m.csv"
    with pytest.raises(Exception):
        # no eligible entities -> CommandError
        call_command("backfill_entity_en", "--out", str(out))
