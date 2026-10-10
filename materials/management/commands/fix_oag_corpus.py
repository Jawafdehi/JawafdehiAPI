"""One-off corrections to the Office of the Auditor General corpus.

The 228 ``official_report`` materials were ingested from a HuggingFace dataset in
October 2026. Splitting that one shelf into five by document kind surfaced a set
of upstream data defects that the shelving work deliberately left alone — they
are listed, with the evidence for each, in
``docs/2026-10-02-oag-series-split/PLAN.md`` of the meta-repo.

This command applies them. It is a one-off, but it is written to be re-runnable:
every action states the "before" it expects and **skips rather than writes** when
the live row no longer matches. That matters more than it usually would, because
the findings were made a week before this ran and nothing stops a caseworker
correcting a title by hand in the meantime — a blind overwrite would silently
undo their work.

Nothing here is a raw SQL write: each correction loads the ``Material``, edits
``data``, and calls ``.save()``, so ``materials.signals`` re-indexes (or, for a
soft-delete, evicts) that row the same way any other write would.

Dry-run by default. ``--apply`` is the only thing that writes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from django.core.management.base import BaseCommand
from django.db import transaction

from jawafdehi_shared.entities.ids import build_material_iri
from materials.models import Material

#: Every row this command touches lives under this source token. The token is
#: also what shelves a document as an Auditor General publication on the site,
#: which is why MOVING a document off it (see ``move_source``) is the way to
#: unshelve one without unpublishing it.
OAG_SOURCE = "official_report"


class Skip(Exception):
    """A precondition did not hold, so this correction does nothing.

    Raised rather than returned so an action body can bail from anywhere without
    every caller re-checking. The message is reported verbatim to the operator.
    """


@dataclass
class Result:
    ident: str
    what: str
    before: str
    after: str


def _name(material: Material) -> dict[str, Any]:
    name = material.data.get("name")
    if not isinstance(name, dict):
        raise Skip(f"name is {type(name).__name__}, not a bilingual dict")
    return name


def _text(material: Material) -> str:
    """The flat transcript, as the duplicate check's fingerprint.

    ``Material`` carries no checksum column, so byte-identity of the underlying
    PDF cannot be re-verified here. The transcript is the next best thing: two
    rows mirroring one upstream file have character-identical text, and two rows
    that merely look alike (a re-issue, a translation) do not.
    """
    text = material.data.get("text")
    if isinstance(text, dict):
        return "\n".join(v for v in text.values() if isinstance(v, str))
    return text if isinstance(text, str) else ""


# ── Actions ──────────────────────────────────────────────────────────────────


def retitle_en(new_title: str, *, expect: str) -> Callable[[Material], Result]:
    """Replace ``name.en``, refusing to touch a title that has already moved."""

    def action(material: Material) -> Result:
        name = _name(material)
        current = name.get("en")
        if current == new_title:
            raise Skip("already correct")
        if current != expect:
            raise Skip(f"expected {expect!r}, found {current!r}")
        name["en"] = new_title
        return Result(material.ident, "retitle", expect, new_title)

    return action


def set_property(key: str, value: str) -> Callable[[Material], Result]:
    """Set a JSON-LD property that is currently absent.

    Deliberately refuses to overwrite an existing value: every use here is
    filling in a field the ingest never populated, so a value already being
    present means someone else got there first and knows more than this table.
    """

    def action(material: Material) -> Result:
        current = material.data.get(key)
        if current == value:
            raise Skip("already correct")
        if current not in (None, ""):
            raise Skip(f"{key} already set to {current!r}")
        material.data[key] = value
        return Result(material.ident, f"set {key}", repr(current), value)

    return action


#: " - X - X" (or " - X - X - X") collapsed to a single " - X". The province
#: reports were ingested with their province name repeated two or three times.
_REPEATED_SUFFIX = re.compile(r"(\s-\s(?P<seg>[^-]+?))(?:\s-\s(?P=seg))+\s*$")


def collapse_repeated_suffix() -> Callable[[Material], Result]:
    def action(material: Material) -> Result:
        name = _name(material)
        current = name.get("en")
        if not isinstance(current, str):
            raise Skip("no English title")
        collapsed = _REPEATED_SUFFIX.sub(r"\1", current).strip()
        if collapsed == current:
            raise Skip("no repeated suffix")
        name["en"] = collapsed
        return Result(material.ident, "collapse repeat", current, collapsed)

    return action


def soft_delete_duplicate(survivor_ident: str) -> Callable[[Material], Result]:
    """Flag a row deleted, but only once its survivor is proven to exist.

    The three checks below are the whole point of doing this in a command rather
    than by hand: a soft-delete whose survivor is missing (or is itself deleted,
    or holds different text) removes a document from the public archive and
    leaves nothing in its place.
    """

    def action(material: Material) -> Result:
        if material.is_deleted:
            raise Skip("already deleted")
        survivor = Material.objects.filter(source=OAG_SOURCE, ident=survivor_ident).first()
        if survivor is None:
            raise Skip(f"survivor {survivor_ident} not found")
        if survivor.is_deleted:
            raise Skip(f"survivor {survivor_ident} is itself deleted")
        mine, theirs = _text(material), _text(survivor)
        if not mine or not theirs:
            raise Skip("one of the pair has no transcript to compare")
        if mine != theirs:
            raise Skip(f"transcript differs from {survivor_ident} — not a duplicate")
        material.is_deleted = True
        return Result(material.ident, "soft-delete", "live", f"deleted (dup of {survivor_ident})")

    return action


def move_source(new_source: str, *, publisher: dict[str, Any]) -> Callable[[Material], Result]:
    """Republish a document under a different source token, correcting publisher.

    A material's ``source`` is derived from its ``@id`` and validated against it,
    so there is no in-place edit: this creates a new row at the new IRI and
    soft-deletes the old one.

    **The old public URL 404s afterwards.** There is no redirect table for
    materials. That is an accepted cost here — the document is reachable at its
    new IRI and through search — but it is the reason this action is used once,
    for a document that is not what its shelf claims, rather than offered as a
    general tidying tool.
    """

    def action(material: Material) -> Result:
        new_iri = build_material_iri(new_source, material.ident)
        if Material.objects.filter(iri=new_iri, is_deleted=False).exists():
            raise Skip(f"{new_iri} already exists")
        data = dict(material.data)
        data["@id"] = new_iri
        data["publisher"] = publisher
        moved = Material.from_jsonld(data, material_type=material.material_type)
        moved.full_clean()
        moved.save()
        material.is_deleted = True
        return Result(material.ident, "move source", material.iri, new_iri)

    return action


# ── The corrections ──────────────────────────────────────────────────────────
#
# Each entry is (ident, why, action). The "why" is printed, so an operator
# reading the dry-run sees the justification next to the change rather than
# having to hold the plan open beside it.

CORRECTIONS: list[tuple[str, str, Callable[[Material], Result]]] = [
    (
        "oag-11320",
        "upstream title ends in a stray editing artefact; currently ranks #1 for q=test",
        retitle_en(
            "Audit Bulletin Issue 1, June/July 2017",
            expect="Audit Bulletin Issue 1, June/July 2017 test",
        ),
    ),
    (
        "oag-11101",
        "the 2nd report of BS 2021 (1964 CE), presented to King Mahendra — not a 2021 CE report",
        retitle_en(
            "Second Annual Report of the Auditor General, BS 2021 (1964)",
            expect="Annual Report, 2021",
        ),
    ),
    (
        "oag-11101",
        "without a fiscal year the 1964 report sorts into the 2021 slot on the annual-report shelf",
        set_property("jawafdehi:fiscalYearBS", "2021"),
    ),
    (
        "oag-11146",
        "an unofficial partial English translation of the 58th report, not a report of its own",
        retitle_en(
            "Fifty-Eighth Annual Report of the Auditor General, FY 2078 "
            "(unofficial partial English translation)",
            expect="Annual report 2021 English version",
        ),
    ),
    (
        "oag-11146",
        "ditto: it belongs in the 2078 slot, not an unplaced one",
        set_property("jawafdehi:fiscalYearBS", "2078"),
    ),
    (
        "oag-11141",
        "the surviving copy of four; it has zero Devanagari, so it IS the English version",
        retitle_en(
            "Special Audit Report on Management of COVID-19, 2021 (English Version)",
            expect="Special Audit Report on Management of COVID-19, 2021",
        ),
    ),
    ("oag-11143", "ingested four times across two buckets", soft_delete_duplicate("oag-11141")),
    ("oag-11147", "ingested four times across two buckets", soft_delete_duplicate("oag-11141")),
    ("oag-11148", "ingested four times across two buckets", soft_delete_duplicate("oag-11141")),
    (
        "oag-11657",
        "duplicate of the canonical-URL copy of the 62nd Annual Report Summary",
        soft_delete_duplicate("oag-11716"),
    ),
    (
        "20260321.a19b89d0",
        "hand-added row duplicating the Madhesh 5th province report; no publisher, date, "
        "transcript or R2 mirror",
        soft_delete_duplicate("oag-11537"),
    ),
    *[
        (ident, "province name repeated by the ingest", collapse_repeated_suffix())
        for ident in (
            "oag-12484",
            "oag-12485",
            "oag-12486",
            "oag-12487",
            "oag-12488",
            "oag-12489",
            "oag-12490",
            "oag-13462",
        )
    ],
    (
        "oag-13477",
        "the English title says 61st where the cover says त्रिसट्ठिऔं (63rd)",
        retitle_en(
            "Institutions designated by organized and federal laws "
            "(related to the 63rd Annual Report of the Auditor General) 2083",
            expect="Institutions designated by organized and federal laws "
            "(related to the 61st Annual Report of the Auditor General) 2083",
        ),
    ),
    (
        "oag-11353",
        "a Government of Nepal AML/CFT risk assessment, not an OAG document: the ingest "
        "shaper hardcodes the OAG as publisher for every row",
        move_source(
            "document",
            publisher={
                "@type": "GovernmentOrganization",
                "name": {"en": "Government of Nepal", "ne": "नेपाल सरकार"},
            },
        ),
    ),
]


class Command(BaseCommand):
    help = (
        "Apply the one-off Auditor General corpus corrections (titles, fiscal years, "
        "duplicate soft-deletes, one mis-attributed document). Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the corrections. Without it, report what would change and exit.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        applied: list[Result] = []
        skipped: list[tuple[str, str]] = []
        missing: list[str] = []

        # One transaction for the whole run, so a crash midway leaves nothing
        # half-applied. It is NOT an all-or-nothing gate on the corrections
        # themselves: each one independently verifies its own "before" state, and
        # a single stale entry in the table should not block the other nineteen.
        with transaction.atomic(using="ngm"):
            for ident, why, action in CORRECTIONS:
                material = Material.objects.filter(source=OAG_SOURCE, ident=ident).first()
                if material is None:
                    missing.append(ident)
                    self.stdout.write(self.style.ERROR(f"  MISSING  {ident}"))
                    continue
                try:
                    result = action(material)
                except Skip as exc:
                    skipped.append((ident, str(exc)))
                    self.stdout.write(f"  skip     {ident}: {exc}")
                    continue
                applied.append(result)
                self.stdout.write(self.style.SUCCESS(f"  {result.what:<16} {ident}"))
                self.stdout.write(f"      why:    {why}")
                self.stdout.write(f"      before: {result.before}")
                self.stdout.write(f"      after:  {result.after}")
                if apply:
                    material.save()

            if not apply:
                # Nothing was written, but the actions mutated in-memory copies and
                # move_source() really did INSERT. Unwind it all.
                transaction.set_rollback(True, using="ngm")

        verb = "applied" if apply else "would apply"
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {len(applied)} correction(s); "
                f"skipped {len(skipped)}; missing {len(missing)}"
            )
        )
        if not apply:
            self.stdout.write("Dry run — nothing was written. Re-run with --apply.")
        if missing:
            # The corrections that passed their preconditions have been applied
            # and are kept — but a missing ident means the corpus is no longer
            # what this table was written against, so exit non-zero rather than
            # let a green run imply the whole table was satisfied.
            self.stderr.write(
                self.style.ERROR(f"{len(missing)} ident(s) not found: {', '.join(missing)}")
            )
            raise SystemExit(1)
