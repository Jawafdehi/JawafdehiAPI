"""Redact the text of documents stored before redaction existed.

    uv run python manage.py backfill_redaction                           # dry run, whole archive
    uv run python manage.py backfill_redaction --material-type charge_sheet
    uv run python manage.py backfill_redaction --material-type charge_sheet --apply
    uv run python manage.py backfill_redaction --iri <iri> --report work/gate.json

Redaction became unconditional at ``upsert_single_source_material``, so every
write lands redacted from that point on. Everything written BEFORE it did not,
and nothing re-reads a document it already holds — so the archive stays exactly
as it was until something walks it. This is that walk.

**Additive, and deliberately so.** ``data["text"]`` becomes the redacted text and
the text as it stood moves to ``Material.source_text``, which is served only to a
caller holding ``materials.view_unredacted``. Nothing is deleted and no stored
object is touched, so a wrong tier or a bad detector costs a re-run rather than a
corpus. That is the whole reason this is safe to run before the PDF half exists.

**Both halves, because one of them alone is not a redaction.** The row's text is
one of two things a reader can reach: the other is the ``MARKDOWN`` artifact, our
own transcript, linked from the public payload. ``artifacts.is_withheld``
withholds that link only once a ``REDACTED_MARKDOWN`` exists to publish instead —
so rewriting ``data["text"]`` and stopping there cleans the search index and
leaves the full transcript one click away, which looks closed from every surface
except the one a reader actually clicks. This command therefore fetches the
stored transcript, redacts *that*, and publishes it beside the redacted text.

**Three things it does NOT do**, stated because the gaps are the point:

* **It does not make an original unreachable.** Publishing the redacted
  transcript stops us *linking* the original, but our object storage serves any
  caller holding the URL and the URL does not change — so a charge sheet someone
  already has a link to stays exactly as fetchable as it was. Closing that means
  moving or deleting stored objects, which breaks every link already in the wild,
  and it is a separate and genuinely irreversible decision.
* **It does not redact a PDF.** ``REDACTED_PDF`` has no producer in this repo, so
  the original PDF stays published whatever this command does. For a scanned
  document that is the whole exposure: the text pass removes the value from
  search and leaves it legible in the file.
* **It does not touch the page-level content plane.** ``DocumentPage`` and
  friends have their own writer and their own redactor
  (``redaction.redact_bundle``); sweeping them belongs with the extraction
  pipeline that writes them, not here.

**The gate itself is not here** — it is ``redaction.redact_jsonld``, so the write
path is held to it too. What this command chooses is only what to do when a
document fails it: ``on_residue="raise"``, which leaves an already-public row
exactly as it was and reports the IRI. The write path withholds the text instead,
because nobody is watching an ingest, while removing a transcript the archive has
been serving is a decision for a human reading a list.

**Exit codes mirror likhit's**, because a partial run must not read as a complete
one: ``0`` every document clean, ``1`` ran with skips, ``2`` at least one document
refused. A refusal is a document whose redacted text still scans positive for a
class the tier is supposed to remove — it is reported and left exactly as it was,
never written half-redacted. On the first corpus run that was **67 of 883**
charge sheets, almost all of them bank accounts.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from materials import artifacts, provenance, transcripts
from materials.models import Material
from materials.redaction import (
    TEXT_KEY,
    RedactionIncomplete,
    RedactionUnavailable,
    gated_classes,
    redact_jsonld,
    redact_text,
    redaction_summary,
    residual_classes,
    withheld_by_the_write_path,
)

#: A transcript is markdown. 32 MiB is far above the largest one in the archive
#: and far below anything that would be a PDF fetched by mistake.
MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024


#: Sensitivity order for chunking, highest first. The September corpus scan put
#: the personal data in charge sheets and court orders by a wide margin, and the
#: plan asks for the run to be chunked so an abort leaves a coherent state: the
#: types that matter most should be done first, not alphabetically somewhere in
#: the middle. Anything not named here follows, alphabetically.
SENSITIVITY_ORDER = ("charge_sheet", "court_order")


class Command(BaseCommand):
    help = (
        "Redact the stored text of materials written before redaction existed. "
        "Reports only unless --apply is given."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Write the redacted text. Without this the command reports what "
                "it would do and changes nothing."
            ),
        )
        parser.add_argument(
            "--material-type",
            dest="material_type",
            help="Only this material_type (e.g. charge_sheet).",
        )
        parser.add_argument("--source", help="Only this source.")
        parser.add_argument("--iri", help="Only this one document.")
        parser.add_argument(
            "--limit",
            type=int,
            help="Stop after this many documents (counts every document looked at).",
        )
        parser.add_argument(
            "--report",
            help=(
                "Write the run's counts to this path as JSON. Counts and class "
                "names only — never a value, never a location."
            ),
        )
        parser.add_argument(
            "--no-transcript",
            action="store_true",
            help=(
                "Skip the transcript half. Redacting data['text'] alone leaves "
                "the MARKDOWN artifact published, so this is for measuring the "
                "text pass quickly, not for a real run."
            ),
        )

    def handle(self, *args, **options):
        apply_writes = options["apply"]
        limit = options.get("limit")
        if limit is not None and limit < 1:
            raise CommandError("--limit must be at least 1.")

        mode = self._check_redactor()
        gated = gated_classes()
        self.stdout.write(
            f"tier={mode.value}  gate={','.join(sorted(gated)) or '(none)'}  "
            f"{'APPLYING' if apply_writes else 'dry run — nothing will be written'}"
        )

        totals: Counter = Counter()
        #: What happened to each document's transcript half — see `_transcript`.
        self._transcript_notes: Counter = Counter()
        per_type: dict[str, Counter] = {}
        refused: list[str] = []
        looked_at = 0

        for material_type in self._types_in_order(options):
            counts = per_type.setdefault(material_type, Counter())
            for material in self._materials(material_type, options):
                if limit is not None and looked_at >= limit:
                    break
                looked_at += 1
                outcome, removed = self._one(
                    material, apply_writes, skip_transcript=options["no_transcript"]
                )
                counts[outcome] += 1
                totals.update(removed)
                if outcome == "refused":
                    refused.append(material.iri)
            if limit is not None and looked_at >= limit:
                break

        self._report(per_type, totals, refused, apply_writes, looked_at)
        if options.get("report"):
            self._write_report(
                options["report"], per_type, totals, refused, mode, apply_writes
            )

        if refused:
            raise CommandError(
                f"{len(refused)} document(s) refused: the redacted text still "
                "scans positive for a class this tier removes. Nothing was "
                "written for them.",
                returncode=2,
            )
        skipped = sum(c.get("unreadable", 0) for c in per_type.values())
        # A transcript left unpublished is a skip, not a success: the row's text
        # is redacted while its MARKDOWN artifact still serves the original.
        skipped += self._transcript_notes["residue"]
        skipped += self._transcript_notes["unreadable"]
        skipped += sum(c.get("changed_underfoot", 0) for c in per_type.values())
        if skipped:
            raise CommandError(
                f"{skipped} document(s) were skipped or left half-published; "
                "see the transcript counts above.",
                returncode=1,
            )

    # -- the work ---------------------------------------------------------

    def _one(self, material: Material, apply_writes: bool, *, skip_transcript: bool = False):
        """Redact one document. Returns ``(outcome, removed counts)``.

        The order here is the whole safety argument: redact into memory, GATE the
        result, and only then write — and write both fields in one transaction,
        because ``data["text"]`` is overwritten in place and the original
        survives only because ``source_text`` was written beside it. A crash
        between those two writes loses a transcript that cost an OCR run and is
        not reproducible byte-for-byte.

        ``on_residue="raise"`` is what makes this a *sweep* rather than the write
        path: the write path withholds the text and carries on, because nobody is
        watching an ingest. Here a row is already public, so removing its
        transcript is a decision for a human reading a list of IRIs.
        """
        try:
            redacted_data, original_text, removed = redact_jsonld(
                material.data, on_residue="raise"
            )
        except RedactionIncomplete as exc:
            self.stderr.write(
                f"  REFUSED {material.iri}: residual "
                f"{dict(sorted(exc.residual.items()))}"
            )
            return "refused", Counter()
        except RedactionUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad document must not end the run
            self.stderr.write(f"  unreadable {material.iri}: {exc}")
            return "unreadable", Counter()

        # 🛑 **The transcript half runs even when the text needs nothing**, and that
        # ordering is the whole point rather than a detail. `data["text"]` and the
        # MARKDOWN artifact are not guaranteed to be the same bytes — which is the
        # reason this command reads the artifact rather than the indexed text — so
        # a row that is clean in one can carry values in the other. Returning on
        # `clean` first also made the documented remedy a permanent no-op: a row an
        # earlier text-only sweep already redacted is clean on every later run, and
        # would never get the REDACTED_MARKDOWN that stops `is_withheld` publishing
        # its original.
        #
        # It runs BEFORE the lock: it fetches and uploads, and holding a row lock
        # across two network round trips would block every other writer of this
        # document for as long as they take.
        transcript = None
        transcript_source = None
        if not skip_transcript:
            transcript, note, transcript_source = self._transcript(
                material, apply_writes=apply_writes
            )
            self._transcript_notes[note] += 1

        if not original_text:
            # redact_jsonld returns the original text ONLY when redaction changed
            # something, so an empty one means there was nothing to remove from the
            # indexed text. The row still needs its transcript published, so that is
            # all this writes.
            if transcript is not None and apply_writes:
                # 🛑 The note is corrected from the WRITE's result, not left at what
                # the upload reported. `_transcript` says "published" as soon as the
                # object is stored, so a skipped attach used to leave the report
                # claiming a transcript nobody can reach — and the run exited 0,
                # because `stranded` counts only residue and unreadable.
                outcome = self._attach(
                    material,
                    transcript,
                    transcript_source,
                    self._redacted_transcript_url(material.data),
                )
                if outcome != "published":
                    self._note_not_attached(outcome)
                    return outcome, Counter()
            return "clean", removed

        if not apply_writes:
            return "would_redact", removed

        with transaction.atomic(using="ngm"):
            # Re-read inside the transaction, because this run is not the only
            # writer: a scraper upserting the same document mid-sweep would
            # already have redacted it, and writing our copy over theirs would
            # put stale text back.
            fresh = (
                Material.objects.using("ngm")
                .select_for_update()
                .filter(pk=material.iri)
                .first()
            )
            if fresh is None:
                if transcript is not None:
                    self._note_not_attached("vanished")
                return "vanished", Counter()
            if (fresh.data or {}) != (material.data or {}):
                # Somebody wrote this document between the read and the lock. The
                # assignment below is a whole-blob overwrite from the PRE-lock
                # snapshot, so continuing would discard their write — and comparing
                # only `text` would miss a writer that touched associatedMedia or
                # provenance and clobber that instead. Theirs went through the same
                # gate ours did; leave it alone and report.
                if transcript is not None:
                    self._note_not_attached("changed_underfoot")
                return "changed_underfoot", Counter()
            fresh.data = redacted_data
            # ⚠️ No `_still_paired` call here, and that is deliberate rather than an
            # omission: the blob comparison above already returns on ANY change to
            # this row, `associatedMedia` included, so by this line the locked row
            # and the snapshot are identical. A pairing check here would re-read the
            # very list `transcript_source` came from and be unconditionally true —
            # which is what it was, until a review pointed out that this branch's
            # own StalePairingTests passes on the blob check and not on the guard.
            if transcript is not None:
                # Publish the redacted transcript beside the redacted text. Until
                # one exists, materials.artifacts.is_withheld keeps publishing the
                # ORIGINAL MARKDOWN — so a text-only sweep redacts the search index
                # and leaves the unredacted transcript one link away.
                fresh.data["associatedMedia"] = transcripts.replace(
                    artifacts.media_list(fresh.data), [transcript]
                )
            outcome = "redacted"
            if fresh.source_text:
                # Redact the text, but KEEP the stored original. A non-empty
                # source_text means this row's data["text"] is already a redacted
                # derivative — redacted at a narrower tier, or by a pass that has
                # since improved — so what we just redacted is not the original.
                # Writing it here would replace the pristine copy with a partly
                # redacted one and lose the only thing that cannot be re-derived.
                outcome = "redacted_kept_original"
            else:
                fresh.source_text = original_text
            # save(), not queryset.update(): post_save is what re-indexes the row,
            # and an index still holding the unredacted body is the exposure this
            # command exists to close.
            # `updated_at` is load-bearing in update_fields: materials/views.py
            # derives the material's ETag from it, so leaving it out lets a client
            # holding a pre-sweep token pass its If-Match precondition and clobber
            # what was just published. `auto_now` only fires for listed fields.
            fresh.save(
                using="ngm", update_fields=["data", "source_text", "updated_at"]
            )
        return outcome, removed

    def _transcript(self, material, *, apply_writes: bool):
        """``(MediaObject or None, note, the MARKDOWN url it read)`` for a row.

        🛑 **Redacted from the stored MARKDOWN, never from ``data["text"]``.** The
        two are usually the same bytes and are not guaranteed to be: the indexed
        field is language-mapped and has the page markers stripped. A
        ``REDACTED_MARKDOWN`` built from the indexed text would therefore be a
        redaction of a *different document* from the ``MARKDOWN`` it replaces —
        and ``is_withheld`` would start withholding the original on the strength
        of it, so the mismatch would be invisible and permanent.

        The notes are the reason this returns a string rather than raising:

        ``published``
            Stored and ready to publish.
        ``no_transcript``
            No MARKDOWN artifact. Nothing is being published, so nothing to do.
        ``unreadable``
            The artifact would not fetch or would not decode. ~7% of the AG shelf
            answers 200 with zero bytes, and an empty object publishes nothing —
            so this is not a failure to fix here.
        ``residue``
            The transcript still scans positive after redaction. The text half
            still goes ahead: it shrinks the public surface from "anyone
            searching" to "anyone holding the URL", which is strictly better than
            leaving both. Reported so it is not mistaken for done.
        """
        from materials.extract import DocumentFetchError, fetch_document

        url = None
        source_url = None
        published = None
        for mo in artifacts.media_list(material.data or {}):
            role = artifacts.role_of(mo)
            if role == artifacts.ORIGINAL_TRANSCRIPT_ROLE:
                url = mo.get("contentUrl")
            elif role == artifacts.REDACTED_TRANSCRIPT_ROLE:
                published = mo
            elif role in artifacts.ORIGINAL_PDF_ROLES and source_url is None:
                # The publisher's file, for `source_url` — the ORIGINAL's
                # provenance. What this redaction was made OF rides separately as
                # `derived_from`, because provenance is published verbatim.
                source_url = mo.get("contentUrl")

        # 🛑 **Asked FIRST, before any network call, and it asks about the
        # PAIRING rather than mere existence.** Without any check the sweep is
        # not idempotent: a finished row re-fetches its MARKDOWN, re-uploads a
        # derivative and re-saves every run, and `build_provenance` stamps
        # `captured_at = now()`, so the write always sticks and `post_save`
        # re-indexes — a second sweep becomes a corpus-wide reindex.
        #
        # ⚠️ But an EXISTENCE check is not enough. If a writer replaces the
        # MARKDOWN alone — which `transcripts.replace` supports by design, since
        # its drop set comes from what is being written — the old redaction
        # survives beside the new original, `is_withheld` withholds the new one
        # on the strength of it, and an existence check short-circuits every
        # later run before noticing. The documented remedy, re-running, could
        # never repair the one state it most needs to.
        if published is not None and self._pairs_with(published, url):
            return None, "already_published", None

        if not isinstance(url, str) or not url:
            return None, "no_transcript", None

        try:
            raw = fetch_document(url, max_bytes=MAX_TRANSCRIPT_BYTES)
            text = raw.decode("utf-8")
        except (DocumentFetchError, UnicodeDecodeError, OSError) as exc:
            self.stderr.write(f"  transcript unreadable {material.iri}: {exc}")
            return None, "unreadable", None
        if not text.strip():
            return None, "unreadable", None

        redacted, _removed = redact_text(text)
        residual = residual_classes(redacted)
        if residual:
            self.stderr.write(
                f"  transcript residue {material.iri}: "
                f"{dict(sorted(residual.items()))}"
            )
            return None, "residue", None
        if not apply_writes:
            return None, "would_publish", None

        try:
            link = transcripts.store(
                redacted, artifacts.REDACTED_TRANSCRIPT_ROLE, "material.redacted.md"
            )
        except Exception as exc:  # noqa: BLE001 - one bad upload must not end the run
            # Outside the handler this was the one call in `_one` that could abort a
            # corpus sweep on a single object-store hiccup, after an unknown number
            # of documents had already been written.
            self.stderr.write(f"  transcript upload failed {material.iri}: {exc}")
            return None, "unreadable", None
        return (
            transcripts.media(
                link,
                artifacts.REDACTED_TRANSCRIPT_ROLE,
                redacted,
                source_url,
                derived=True,
                # What it was made OF, as a digest a payload can carry safely.
                derived_from=url,
            ),
            "published",
            url,
        )

    def _pairs_with(self, published: dict, markdown_url: Any) -> bool:
        """Whether ``published`` is a redaction OF the row's current MARKDOWN.

        The pairing is read from the derived artifact's own provenance, where
        :func:`materials.transcripts.media` records a DIGEST of the url it was
        made from — never the url, which provenance would publish to anonymous
        callers and so undo the withholding entirely.

        ⚠️ **A redaction written before that was recorded does not pair**, and
        that is the safe direction: it re-derives once, on the first sweep after
        this lands, and is stable from then on. The alternative — treating a
        missing record as "paired" — would make every pre-existing mispairing
        permanently invisible, which is the defect this exists to close.
        """
        prov = published.get(provenance.PROVENANCE_KEY) or {}
        recorded = prov.get("derived_from") if isinstance(prov, dict) else None
        if not recorded or not isinstance(markdown_url, str):
            return False
        return recorded == hashlib.sha256(markdown_url.encode("utf-8")).hexdigest()

    def _note_not_attached(self, outcome: str) -> None:
        """Correct the note `_transcript` recorded when the object was stored.

        🛑 Both write paths need this, not just the clean one. `_transcript` says
        "published" as soon as the upload succeeds, so a redacting-path row that
        then returns `vanished` or `changed_underfoot` left the report — and the
        `--report` JSON — claiming a transcript nobody can reach, with the run
        exiting 0 because `stranded` counts only residue and unreadable.
        """
        self._transcript_notes["published"] -= 1
        self._transcript_notes[f"not_attached_{outcome}"] += 1

    def _redacted_transcript_url(self, data: dict) -> Any:
        """The row's current redacted transcript link, or None."""
        for mo in artifacts.media_list(data or {}):
            if artifacts.role_of(mo) == artifacts.REDACTED_TRANSCRIPT_ROLE:
                return mo.get("contentUrl")
        return None

    def _still_paired(self, data: dict, source_url: str | None) -> bool:
        """Whether the row still carries the MARKDOWN this redaction was made from.

        🛑 The transcript is fetched and redacted BEFORE the lock, by design —
        two network round trips are not something to hold a row lock across. If a
        scraper re-converts the document in that window, both the MARKDOWN object
        and its link change, and publishing our derivative anyway would have
        `artifacts.is_withheld` withhold the NEW original on the strength of a
        redaction of the OLD one. ``materials.transcripts`` names that outcome
        exactly: "a stale pairing publishes a redaction of a document we are no
        longer serving".
        """
        if source_url is None:
            return False
        return any(
            artifacts.role_of(mo) == artifacts.ORIGINAL_TRANSCRIPT_ROLE
            and mo.get("contentUrl") == source_url
            for mo in artifacts.media_list(data or {})
        )

    def _attach(
        self,
        material: Material,
        transcript: dict,
        source_url: str | None,
        expected_existing: Any = None,
    ) -> str:
        """Publish a transcript on a row whose indexed text needed no change.

        Returns the outcome so the caller can correct the note `_transcript`
        recorded at upload time — `published`, `vanished`, or
        `changed_underfoot`.

        Builds from the row read UNDER the lock rather than from a pre-lock
        snapshot, so unlike the redacting path there is nothing here that can
        discard a concurrent writer: the only field it sets is the one it owns.
        """
        with transaction.atomic(using="ngm"):
            fresh = (
                Material.objects.using("ngm")
                .select_for_update()
                .filter(pk=material.iri)
                .first()
            )
            if fresh is None:
                # A deletion is not a competing write. The redacting path calls the
                # same event `vanished` and exits 0 over it; reporting
                # `changed_underfoot` here sent an operator looking for a writer
                # that does not exist, and failed the run.
                return "vanished"
            if self._redacted_transcript_url(fresh.data) != expected_existing:
                # 🛑 **Not "is one present" — "is it the one we came to replace".**
                # A row being REPAIRED carries a stale redaction by definition, so
                # an existence check refuses the repair it was asked to perform,
                # leaks the object uploaded before the lock, and does it again on
                # every run. What this has to catch is a DIFFERENT artifact
                # appearing: another run publishing in the two network round trips
                # since the pre-lock read, whose link `transcripts.replace` would
                # otherwise drop by role without a word — possibly one made at a
                # different tier, since the tier is resolved per process.
                return "changed_underfoot"
            if not self._still_paired(fresh.data, source_url):
                return "changed_underfoot"
            data = dict(fresh.data or {})
            data["associatedMedia"] = transcripts.replace(
                artifacts.media_list(data), [transcript]
            )
            fresh.data = data
            fresh.save(using="ngm", update_fields=["data", "updated_at"])
        return "published"

    # -- selection --------------------------------------------------------

    def _types_in_order(self, options) -> list[str]:
        if options.get("iri") or options.get("material_type"):
            return [options.get("material_type") or ""]
        present = set(
            Material.objects.using("ngm")
            .filter(is_deleted=False)
            .values_list("material_type", flat=True)
            .distinct()
        )
        ranked = [t for t in SENSITIVITY_ORDER if t in present]
        return ranked + sorted(present - set(ranked))

    def _materials(self, material_type: str, options):
        # Soft-deleted rows are skipped: they are off the read plane and out of
        # the index, and a restore goes back through the upsert primitive, which
        # redacts. They are the one population this command leaves alone.
        qs = Material.objects.using("ngm").filter(
            is_deleted=False, data__has_key=TEXT_KEY
        )
        if options.get("iri"):
            qs = qs.filter(pk=options["iri"])
        elif material_type:
            qs = qs.filter(material_type=material_type)
        if options.get("source"):
            qs = qs.filter(source=options["source"])
        return qs.order_by("iri").iterator(chunk_size=200)

    # -- plumbing ---------------------------------------------------------

    def _check_redactor(self):
        """Fail before touching anything if the redactor is not the one we expect.

        A backfill that discovers mid-corpus that it cannot redact has already
        walked half the archive deciding nothing.
        """
        try:
            from likhit.privacy import RedactionMode, scan_text  # noqa: F401

            from materials.redaction import _redactor

            _redact, mode = _redactor()
        except RedactionUnavailable as exc:
            raise CommandError(str(exc), returncode=2) from exc
        except ImportError as exc:  # pragma: no cover - older pin
            raise CommandError(f"likhit.privacy is unavailable: {exc}", returncode=2)
        if mode is RedactionMode.NONE:  # pragma: no cover - _redactor refuses first
            raise CommandError("MATERIALS_REDACTION_MODE=none redacts nothing.", 2)
        return mode

    def _report(self, per_type, totals, refused, apply_writes, looked_at) -> None:
        self.stdout.write("")
        for material_type, counts in per_type.items():
            if not counts:
                continue
            line = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            self.stdout.write(f"{material_type or '(all)'}: {line}")
        self.stdout.write("")
        self.stdout.write(f"looked at {looked_at} document(s)")
        if totals:
            verb = "removed" if apply_writes else "would remove"
            self.stdout.write(f"{verb}: {redaction_summary(totals)}")
        if self._transcript_notes:
            line = "  ".join(
                f"{k}={v}" for k, v in sorted(self._transcript_notes.items())
            )
            self.stdout.write(f"transcripts: {line}")
            stranded = sum(
                n
                for k, n in self._transcript_notes.items()
                if k in ("residue", "unreadable") or k.startswith("not_attached_")
            )
            if stranded:
                self.stdout.write(
                    self.style.WARNING(
                        f"{stranded} document(s) got redacted text but NO redacted "
                        "transcript — their MARKDOWN artifact still publishes the "
                        "original"
                    )
                )
        if refused:
            self.stdout.write(
                self.style.ERROR(f"refused: {len(refused)} document(s), listed above")
            )
        elif apply_writes:
            self.stdout.write(self.style.SUCCESS("every document passed the gate"))
        withheld = withheld_by_the_write_path().count()
        if withheld:
            self.stdout.write(
                self.style.WARNING(
                    f"{withheld} document(s) are carrying a withheld transcript "
                    "(the write path gated them); none of them are in the counts above"
                )
            )

    def _write_report(
        self, path, per_type, totals, refused, mode, apply_writes
    ) -> None:
        payload = {
            "tier": mode.value,
            "applied": apply_writes,
            "per_material_type": {k: dict(v) for k, v in per_type.items() if v},
            "removed": redaction_summary(totals),
            # IRIs, not text: an IRI is already public and names the document a
            # human has to look at, while anything from the text would put back
            # exactly what the run took out.
            "refused": sorted(refused),
            "transcripts": dict(sorted(self._transcript_notes.items())),
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        self.stdout.write(f"report written to {path}")
