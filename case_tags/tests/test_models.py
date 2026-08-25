"""Vocabulary invariants and raw-value resolution."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from case_tags.models import Resolution, Tag, TagAlias, TagStatus, resolve

pytestmark = pytest.mark.django_db


def _tag(tag_id: str, **kwargs: object) -> Tag:
    defaults: dict[str, object] = {
        "label_ne": tag_id,
        "label_en": tag_id,
        "status": TagStatus.ACTIVE,
    }
    defaults.update(kwargs)
    return Tag.objects.create(id=tag_id, **defaults)


class TestBroader:
    """Roll-up is applied at index time by walking ``broader``, so the shape of that
    walk has to be bounded and predictable."""

    def test_with_broader_returns_self_then_parent(self) -> None:
        land = _tag("land")
        grab = _tag("land-grab", broader=land)
        assert grab.with_broader() == ["land-grab", "land"]
        assert land.with_broader() == ["land"]

    def test_chaining_beyond_one_level_is_rejected(self) -> None:
        """Two levels would make the index-time walk unbounded and let selecting a
        tag silently pull in a grandparent nobody chose."""
        land = _tag("land")
        admin = _tag("land-administration", broader=land)
        deeper = Tag(
            id="land-pooling",
            label_ne="x",
            label_en="x",
            status=TagStatus.ACTIVE,
            broader=admin,
        )
        with pytest.raises(ValidationError) as exc:
            deeper.full_clean()
        assert "one level only" in str(exc.value)

    def test_self_reference_rejected(self) -> None:
        tag = Tag(id="land", label_ne="भूमि", label_en="Land", status=TagStatus.ACTIVE)
        tag.broader_id = "land"
        with pytest.raises(ValidationError):
            tag.full_clean()


class TestLifecycle:
    def test_merged_without_target_is_rejected_by_the_database(self) -> None:
        """Not just by clean() — a merged tag pointing nowhere strands every case
        carrying it, so the constraint has to survive a bulk write too."""
        with pytest.raises(IntegrityError), transaction.atomic():
            Tag.objects.create(
                id="orphan",
                label_ne="x",
                label_en="x",
                status=TagStatus.MERGED,
            )

    def test_merged_with_target_is_allowed(self) -> None:
        keeper = _tag("illicit-enrichment")
        merged = _tag("illegal-wealth", status=TagStatus.MERGED, merged_into=keeper)
        assert merged.merged_into_id == "illicit-enrichment"


class TestAliasIntegrity:
    def test_key_must_already_be_normalized(self) -> None:
        """Lookups normalize the incoming value and compare directly. A row stored
        un-normalized is unreachable — worse than absent, because it looks present."""
        alias = TagAlias(key="Land Management", tag=_tag("land-administration"))
        with pytest.raises(ValidationError) as exc:
            alias.full_clean()
        assert "normalized" in str(exc.value)

    def test_alias_must_map_or_explain(self) -> None:
        """Neither a tag nor a reason is a silent hole in the vocabulary."""
        with pytest.raises(IntegrityError), transaction.atomic():
            TagAlias.objects.create(key="mystery", tag=None, retired_reason="")

    def test_keys_are_unique(self) -> None:
        a = _tag("bribery")
        b = _tag("kickbacks-tag")
        TagAlias.objects.create(key="kickbacks", tag=a)
        with pytest.raises(IntegrityError), transaction.atomic():
            TagAlias.objects.create(key="kickbacks", tag=b)


class TestResolve:
    def test_resolves_an_alias_to_its_tag(self) -> None:
        tag = _tag("land-administration")
        TagAlias.objects.create(key="land management", tag=tag)
        # Raw value, not the key — resolve() does the folding.
        result = resolve("Land Management")
        assert result.resolution is Resolution.CANONICAL
        assert result.tag_id == "land-administration"
        assert result.is_canonical

    def test_retired_is_distinct_from_unknown(self) -> None:
        """`?tags=CIAA` is a live URL today. After the cleanup it must be able to say
        "that filter was removed", not "unknown tag" — the second reads as a bug to
        anyone holding a bookmark."""
        TagAlias.objects.create(
            key="ciaa",
            tag=None,
            retired_reason="duplicates-an-existing-structured-field",
        )
        retired = resolve("CIAA")
        assert retired.resolution is Resolution.RETIRED
        assert retired.reason == "duplicates-an-existing-structured-field"
        assert retired.tag_id is None

        assert resolve("never-seen-before").resolution is Resolution.UNKNOWN

    def test_follows_a_merge_to_the_replacement(self) -> None:
        """Recording a merge rather than deleting the row is what keeps the old id
        working; resolve() is where that promise is kept."""
        keeper = _tag("illicit-enrichment")
        old = _tag("illegal-wealth", status=TagStatus.MERGED, merged_into=keeper)
        TagAlias.objects.create(key="illegal wealth", tag=old)
        assert resolve("Illegal Wealth").tag_id == "illicit-enrichment"

    def test_survives_a_merge_cycle(self) -> None:
        """A cycle is a vocabulary bug, but it must not hang the request path.

        Note the setup has to go through valid states to exist at all — the
        merged-needs-target constraint refuses to create a dangling merge — so a
        cycle can only arrive by editing two rows that were each individually fine.
        Exactly how it would happen in production, and why resolve() guards for it.
        """
        a = _tag("a-tag")
        b = _tag("b-tag", merged_into=a)
        Tag.objects.filter(pk=b.pk).update(status=TagStatus.MERGED)
        Tag.objects.filter(pk=a.pk).update(status=TagStatus.MERGED, merged_into=b)

        TagAlias.objects.create(key="a tag", tag=a)
        result = resolve("a tag")  # must terminate
        assert result.resolution is Resolution.CANONICAL
        assert result.tag_id in {"a-tag", "b-tag"}

    def test_devanagari_artefact_resolves(self) -> None:
        """End to end: the broken Preeti spelling in the corpus reaches its tag."""
        tag = _tag("public-asset-damage")
        TagAlias.objects.create(key="सार्वजनिक सम्पत्ति हानी नोकसानी", tag=tag)
        assert resolve("सार्वजनिक सम्पत्ति हानी नाेकसानी").tag_id == "public-asset-damage"
