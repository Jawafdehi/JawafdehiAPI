"""``seed_case_tags`` — YAML vocabulary into the database.

The command is the only writer of ``Tag``/``TagAlias``, so its guarantees are the
vocabulary's guarantees: idempotent, never silently destructive, and it refuses a
file that contradicts itself.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml
from django.core.management import call_command
from django.core.management.base import CommandError

from case_tags.models import Tag, TagAlias, TagStatus

pytestmark = pytest.mark.django_db

VOCAB: dict[str, object] = {
    "version": 1,
    "tags": [
        {
            "id": "land",
            "label_ne": "भूमि",
            "label_en": "Land",
            "status": "active",
            "aliases": ["land", "land deal"],
        },
        {
            "id": "land-grab",
            "label_ne": "सरकारी जग्गा हडप",
            "label_en": "Land Grab",
            "status": "active",
            "broader": "land",
            "aliases": ["Land Grab", "Land Scandel"],
        },
        {
            "id": "province-government",
            "label_ne": "प्रदेश सरकार",
            "label_en": "Province Government",
            "status": "proposed",
            "aliases": [],
        },
    ],
    "dropped": [
        {
            "reason": "duplicates-an-existing-structured-field",
            "values": ["CIAA", "Corruption"],
        },
        {"reason": "not-a-tag-money-amount", "values": ["~45 Hazar"]},
    ],
}


def _write(tmp_path: pathlib.Path, document: dict[str, object]) -> pathlib.Path:
    path = tmp_path / "vocabulary.yml"
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return path


def test_seeds_tags_aliases_and_retired_values(tmp_path: pathlib.Path) -> None:
    call_command("seed_case_tags", path=str(_write(tmp_path, VOCAB)))

    assert Tag.objects.count() == 3
    assert Tag.objects.get(pk="land-grab").broader_id == "land"
    assert Tag.objects.get(pk="province-government").status == TagStatus.PROPOSED

    # Aliases are stored NORMALIZED, and the id is always its own alias.
    assert TagAlias.objects.get(key="land scandel").tag_id == "land-grab"
    assert TagAlias.objects.get(key="land grab").tag_id == "land-grab"

    # Dropped values become rows with a null tag, so a retired filter can explain
    # itself instead of reading as "unknown tag".
    ciaa = TagAlias.objects.get(key="ciaa")
    assert ciaa.tag_id is None
    assert ciaa.retired_reason == "duplicates-an-existing-structured-field"


def test_is_idempotent(tmp_path: pathlib.Path) -> None:
    """Re-running is how the vocabulary is deployed, so a second run must be a
    no-op rather than a duplicate-key error or a pile of orphaned aliases."""
    path = _write(tmp_path, VOCAB)
    call_command("seed_case_tags", path=str(path))
    before = (Tag.objects.count(), TagAlias.objects.count())
    call_command("seed_case_tags", path=str(path))
    assert (Tag.objects.count(), TagAlias.objects.count()) == before


def test_dry_run_writes_nothing(tmp_path: pathlib.Path) -> None:
    call_command("seed_case_tags", path=str(_write(tmp_path, VOCAB)), dry_run=True)
    assert Tag.objects.count() == 0
    assert TagAlias.objects.count() == 0


def test_edits_are_applied_on_reseed(tmp_path: pathlib.Path) -> None:
    path = _write(tmp_path, VOCAB)
    call_command("seed_case_tags", path=str(path))

    edited = yaml.safe_load(yaml.safe_dump(VOCAB))
    edited["tags"][0]["label_ne"] = "जग्गा"
    edited["tags"][2]["status"] = "active"
    call_command("seed_case_tags", path=str(_write(tmp_path, edited)))

    assert Tag.objects.get(pk="land").label_ne == "जग्गा"
    assert Tag.objects.get(pk="province-government").status == TagStatus.ACTIVE


def test_a_tag_missing_from_the_file_is_reported_not_deleted(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cases may still carry it. Deleting the row would strand them, and the seed
    command has no way to know whether the omission was deliberate."""
    call_command("seed_case_tags", path=str(_write(tmp_path, VOCAB)))

    trimmed = yaml.safe_load(yaml.safe_dump(VOCAB))
    trimmed["tags"] = [t for t in trimmed["tags"] if t["id"] != "land-grab"]
    call_command("seed_case_tags", path=str(_write(tmp_path, trimmed)))

    assert Tag.objects.filter(pk="land-grab").exists()
    assert "land-grab" in capsys.readouterr().out


def test_rejects_a_value_that_is_both_aliased_and_dropped(
    tmp_path: pathlib.Path,
) -> None:
    """Otherwise whichever block loads last silently wins, and the vocabulary
    means something different depending on file order."""
    contradictory = yaml.safe_load(yaml.safe_dump(VOCAB))
    contradictory["dropped"].append({"reason": "oops", "values": ["Land Grab"]})
    with pytest.raises(CommandError, match="dropped"):
        call_command("seed_case_tags", path=str(_write(tmp_path, contradictory)))


def test_rejects_a_broader_chain(tmp_path: pathlib.Path) -> None:
    """The one-level rule is enforced at seed time too — a file that violates it
    must fail loudly rather than produce a vocabulary the indexer cannot walk."""
    chained = yaml.safe_load(yaml.safe_dump(VOCAB))
    chained["tags"].append(
        {
            "id": "land-pooling",
            "label_ne": "x",
            "label_en": "x",
            "status": "active",
            "broader": "land-grab",
        }
    )
    with pytest.raises(Exception, match="one level"):
        call_command("seed_case_tags", path=str(_write(tmp_path, chained)))


def test_missing_file_is_an_error(tmp_path: pathlib.Path) -> None:
    with pytest.raises(CommandError, match="No vocabulary"):
        call_command("seed_case_tags", path=str(tmp_path / "nope.yml"))


def test_empty_vocabulary_is_an_error(tmp_path: pathlib.Path) -> None:
    """An empty file would otherwise wipe every alias — the destructive no-op."""
    with pytest.raises(CommandError, match="no tags"):
        call_command("seed_case_tags", path=str(_write(tmp_path, {"version": 1})))
