"""Identity for NES entities this package creates: the slug and the prefix rule.

Two pure functions, deliberately apart from `enrich_related_entities`: they take
strings and return strings, touch no API, and are the only place that decides
what a created entity is *called* and where it is *filed*.

Both answer questions the case API does not. `validate_jsonld_entity` checks the
IRI's shape and nothing else, and the prefix list at `/api/entity_prefixes` is
`SELECT DISTINCT prefix` over live entities (`entities/persistence.py:313`) --
a report of what exists, not a whitelist. So a POST with an invented prefix
succeeds, and the prefix then appears in that endpoint's own output. Every
guard here is ours.
"""

import re

from jawafdehi_shared.entities.ids import MAX_IRI_LENGTH

# `_colloquial_fold` is private to that module, and imported anyway: it owns the
# sound-spelling map (ś→sh, ṣ→sh, ṛ→ri) that makes `बिष्ट` read as `bishta`
# rather than `bista`. Folding the diacritics here instead -- plain NFKD, which
# is what the rest of that module's public surface would give us -- drops the
# retroflex dot and yields `bista`, a spelling no NES entity uses. The
# alternative is copying a phonetic table that must never drift from the one the
# search index uses. `test_slug_transliterates_a_devanagari_person_name` pins
# the mapping, so a change on that side fails here rather than silently
# re-spelling every entity we create.
from jawafdehi_shared.search.transliterate import _colloquial_fold, to_roman

#: Bound on a generated slug. Far below `MAX_IRI_LENGTH` (300) on purpose: the
#: prefix, the host and `/entity/` all share that budget, and the deepest live
#: prefix is already 44 characters. A slug this long is also unreadable, which
#: matters because a caseworker reads these IRIs in `created.jsonl`.
MAX_SLUG_LENGTH = 80

#: The prefix grammar, mirroring `_PREFIX` in `jawafdehi_shared.entities.ids`:
#: 1 to 4 slash-joined segments of lowercase letters, digits and underscores.
#: Duplicated rather than imported because that module keeps it private, and a
#: silent drift here would let a malformed prefix reach a POST.
_PREFIX_RE = re.compile(r"^[a-z0-9_]+(?:/[a-z0-9_]+){0,3}$")


def _slugify(text) -> str:
    """Lowercase, hyphenate, trim to `MAX_SLUG_LENGTH`. "" if nothing survives."""
    if not text or not isinstance(text, str):
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > MAX_SLUG_LENGTH:
        # Cut at a hyphen so the slug ends on a whole word rather than mid-name.
        slug = slug[:MAX_SLUG_LENGTH].rsplit("-", 1)[0].strip("-")
    # The grammar is `[a-z0-9][a-z0-9-]*`, so a slug starting with a digit is
    # fine but one starting with a hyphen is not. `strip("-")` covers it.
    return slug if len(slug) <= MAX_IRI_LENGTH else slug[:MAX_SLUG_LENGTH]


def entity_slug(name: str, name_en=None) -> str:
    """A stable, IRI-legal slug for `name`, or "" if nothing usable survives.

    Prefers `name_en`, the English name the extraction supplies. Most company
    names in these court orders are English written in Devanagari, and sounding
    them back out is unusable: `फरेष्ट डेभलपमेन्ट एण्ड इण्डष्ट्रिज` transliterates to
    `phareshta-debhalapamenta-enda-indashtrija`, where the English name gives
    `forest-development-and-industries`. The IRI is permanent and a caseworker
    authoring that firm by hand would write the second, so preferring the
    English name is what keeps NES from holding the company twice.

    Falls back to transliteration whenever `name_en` yields no slug -- absent,
    blank, punctuation, or not a string. Falling back beats returning "": ""
    makes the caller skip an entity it could have created.

    Returns "" rather than raising: a name of pure punctuation has no slug, and
    the caller's job is to skip and record it, not to build an invalid IRI.

    Uses `to_roman` and folds the diacritics here, NOT `to_roman_colloquial`.
    That function returns two spellings joined by a space -- a strict
    transliteration and a schwa-dropped one, e.g. "hema raja bishta hem raj
    bisht" -- because it feeds a search index that should match either. Slugging
    it produces `hema-raja-bishta-hem-raj-bisht`.

    The schwa-dropped spelling alone is closer to how Nepali is written in Latin
    ("hem raj bisht"), and is still not used: it deletes a trailing vowel it
    cannot distinguish from an inherent one, turning जिल्ला into `jill`.

    Stability matters more than beauty. The same name must slug identically on
    every run, or a re-run creates a second entity instead of finding the first.
    """
    english = _slugify(name_en)
    if english:
        return english

    if not name or not isinstance(name, str):
        return ""

    # `biṣṭa` -> `bishta`, `dhanagaḍhī` -> `dhanagadhi`: the sound map first, then
    # the remaining combining marks stripped.
    return _slugify(_colloquial_fold(to_roman(name)))


def prefix_is_creatable(prefix, live_prefixes) -> bool:
    """True when we may file a new entity under `prefix`.

    Two ways to qualify: the prefix is already in use, or its IMMEDIATE parent
    is. So `organization/government/forest` is allowed because
    `organization/government` exists, while `organizaton/government/police` is
    not -- its parent is nowhere, which is what a typo in the trunk looks like.

    A brand-new root (`ministry`, `ministry/forest`) never qualifies. A root has
    no parent to vouch for it, so accepting one would let any single misspelled
    word become a permanent top-level category.

    The parent must be the immediate one, not any ancestor. Accepting a
    grandparent would wave through a typo in a middle segment:
    `organization/government/revnue/customs` would borrow
    `organization/government`'s legitimacy.

    Known limit: a typo in the LAST segment still passes, because only the
    parent is checked. `organization/government/polices` has a valid parent.
    Trunk typos are the ones worth catching -- they strand the entity where no
    search filter reaches it, and they compound, because the bad prefix then
    reports as live.
    """
    if not prefix or not isinstance(prefix, str):
        return False
    if not _PREFIX_RE.match(prefix):
        return False

    live = set(live_prefixes or ())
    if prefix in live:
        return True
    parent, sep, _leaf = prefix.rpartition("/")
    if not sep:
        return False        # a root nobody uses yet
    return parent in live
