"""``rebuild_case_tags`` — recompute Case.tags from the vocabulary.

The command's whole contract is that it is a pure function of two files plus
``tags_source``. Everything here is a way of pinning that: re-running changes
nothing, the snapshot survives, and a curation file that could mean two things is
rejected rather than resolved by file order.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml
from django.core.management import call_command
from django.core.management.base import CommandError

from case_tags.models import Tag, TagAlias, TagStatus
from cases.models import Case, CaseState, CaseType

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def vocabulary() -> None:
    """A small stand-in for the real file — enough shape to exercise the command."""
    land = Tag.objects.create(
        id="land", label_ne="भूमि", label_en="Land", status=TagStatus.ACTIVE
    )
    Tag.objects.create(
        id="land-grab",
        label_ne="सरकारी जग्गा हडप",
        label_en="Land Grab",
        status=TagStatus.ACTIVE,
        broader=land,
    )
    Tag.objects.create(
        id="local-government",
        label_ne="स्थानीय तह",
        label_en="Local Government",
        status=TagStatus.ACTIVE,
    )
    Tag.objects.create(
        id="lalitpur", label_ne="ललितपुर", label_en="Lalitpur", status=TagStatus.ACTIVE
    )
    for key, tag_id in [
        ("land management", "land"),
        ("land grab", "land-grab"),
        ("land scandel", "land-grab"),
        ("local government", "local-government"),
        ("lalitpur", "lalitpur"),
    ]:
        TagAlias.objects.create(key=key, tag_id=tag_id)
    TagAlias.objects.create(
        key="ciaa", tag=None, retired_reason="duplicates-an-existing-structured-field"
    )


def _case(slug: str, tags: list[str]) -> Case:
    return Case.objects.create(
        title=slug,
        slug=slug,
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        tags=tags,
    )


def _curation(tmp_path: pathlib.Path, cases: list[dict[str, object]]) -> str:
    path = tmp_path / "curation.yml"
    path.write_text(yaml.safe_dump({"cases": cases}, allow_unicode=True), "utf-8")
    return str(path)


class TestRebuild:
    def test_maps_raw_values_and_drops_retired_and_unknown(self) -> None:
        case = _case("c1", ["Land Management", "CIAA", "Some Nonsense"])
        call_command("rebuild_case_tags", apply=True)
        case.refresh_from_db()
        assert case.tags == ["land"]

    def test_snapshots_the_original(self) -> None:
        original = ["Land Management", "CIAA"]
        case = _case("c1", list(original))
        call_command("rebuild_case_tags", apply=True)
        case.refresh_from_db()
        assert case.tags_source == original

    def test_dedupes_preserving_first_seen_order(self) -> None:
        """Two raw values collapsing to one tag must not produce a duplicate, and the
        order has to be deterministic — a caseworker PATCH asserts exact equality."""
        case = _case("c1", ["Land Grab", "Local Government", "Land Scandel"])
        call_command("rebuild_case_tags", apply=True)
        case.refresh_from_db()
        assert case.tags == ["land-grab", "local-government"]

    def test_is_idempotent(self) -> None:
        """Re-running is how it is deployed. The second run reads ``tags_source``,
        not the ids the first run wrote, so the answer cannot drift."""
        case = _case("c1", ["Land Management", "CIAA"])
        call_command("rebuild_case_tags", apply=True)
        case.refresh_from_db()
        first_tags, first_source = case.tags, case.tags_source

        call_command("rebuild_case_tags", apply=True)
        case.refresh_from_db()
        assert case.tags == first_tags
        assert case.tags_source == first_source

    def test_rerun_does_not_overwrite_the_snapshot(self) -> None:
        """If the second run snapshotted the canonical ids over the original free
        text, the rollback path would be gone and the change irreversible."""
        case = _case("c1", ["Land Management"])
        call_command("rebuild_case_tags", apply=True)
        call_command("rebuild_case_tags", apply=True)
        case.refresh_from_db()
        assert case.tags_source == ["Land Management"]
        assert case.tags == ["land"]

    def test_dry_run_writes_nothing(self) -> None:
        case = _case("c1", ["Land Management"])
        call_command("rebuild_case_tags")
        case.refresh_from_db()
        assert case.tags == ["Land Management"]
        assert case.tags_source is None

    def test_stores_specific_tags_not_the_broader_rollup(self) -> None:
        """Roll-up belongs at index time. Writing `land` onto the case record would
        make a land-grab case display a tag nobody chose."""
        case = _case("c1", ["Land Grab"])
        call_command("rebuild_case_tags", apply=True)
        case.refresh_from_db()
        assert case.tags == ["land-grab"]


class TestCuration:
    def test_add_and_remove(self, tmp_path: pathlib.Path) -> None:
        case = _case("c1", ["Land Grab"])
        path = _curation(
            tmp_path,
            [
                {
                    "slug": "c1",
                    "add": ["lalitpur"],
                    "remove": ["land-grab"],
                    "why": "title names ललितपुर; the grab claim is unproven",
                }
            ],
        )
        call_command("rebuild_case_tags", apply=True, curation=path)
        case.refresh_from_db()
        assert case.tags == ["lalitpur"]

    def test_remove_wins_over_an_alias_derived_add(self, tmp_path: pathlib.Path) -> None:
        """An alias is global. Without per-case removal the only way to fix one wrong
        case would be to corrupt the alias for every other case using it."""
        case = _case("c1", ["Land Management", "Local Government"])
        path = _curation(
            tmp_path, [{"slug": "c1", "remove": ["land"], "why": "not a land case"}]
        )
        call_command("rebuild_case_tags", apply=True, curation=path)
        case.refresh_from_db()
        assert case.tags == ["local-government"]

    def test_duplicate_slug_is_an_error(self, tmp_path: pathlib.Path) -> None:
        """Last-wins would make the file mean different things at different orders."""
        _case("c1", [])
        path = _curation(
            tmp_path,
            [
                {"slug": "c1", "add": ["land"], "why": "a"},
                {"slug": "c1", "add": ["lalitpur"], "why": "b"},
            ],
        )
        with pytest.raises(CommandError, match="duplicate slug"):
            call_command("rebuild_case_tags", apply=True, curation=path)

    def test_missing_why_is_an_error(self, tmp_path: pathlib.Path) -> None:
        """An editorial override with no stated reason is unreviewable."""
        _case("c1", [])
        path = _curation(tmp_path, [{"slug": "c1", "add": ["land"], "why": "  "}])
        with pytest.raises(CommandError, match="no `why`"):
            call_command("rebuild_case_tags", apply=True, curation=path)

    def test_unknown_tag_is_an_error(self, tmp_path: pathlib.Path) -> None:
        _case("c1", [])
        path = _curation(tmp_path, [{"slug": "c1", "add": ["not-a-tag"], "why": "x"}])
        with pytest.raises(CommandError, match="not an active tag"):
            call_command("rebuild_case_tags", apply=True, curation=path)

    def test_unresolvable_slug_fails_loudly(self, tmp_path: pathlib.Path) -> None:
        """A silently skipped entry is how curation stops applying without anyone
        noticing. Published cases DO get re-slugged, so this will happen."""
        _case("c1", [])
        path = _curation(tmp_path, [{"slug": "gone", "add": ["land"], "why": "x"}])
        with pytest.raises(CommandError, match="unresolvable slugs"):
            call_command("rebuild_case_tags", apply=True, curation=path)

    def test_renamed_slug_names_its_replacement(self, tmp_path: pathlib.Path) -> None:
        """When a slug moved, say so — "no such case" sends the reader hunting."""
        from cases.models import CaseSlugHistory

        case = _case("c1", [])
        CaseSlugHistory.objects.create(slug="old-slug", case=case)
        path = _curation(tmp_path, [{"slug": "old-slug", "add": ["land"], "why": "x"}])
        with pytest.raises(CommandError, match="renamed to 'c1'"):
            call_command("rebuild_case_tags", apply=True, curation=path)

    def test_absent_curation_file_is_fine(self, tmp_path: pathlib.Path) -> None:
        """The rebuild is correct without curation — it just leaves thin cases thin."""
        case = _case("c1", ["Land Management"])
        call_command(
            "rebuild_case_tags", apply=True, curation=str(tmp_path / "nope.yml")
        )
        case.refresh_from_db()
        assert case.tags == ["land"]
