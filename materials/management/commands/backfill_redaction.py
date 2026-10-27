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

**Three things it does NOT do**, stated because the gaps are the point:

* **It does not make an original unreachable.** Our object storage serves any
  caller holding the URL and the URL does not change, so a charge sheet someone
  already has a link to stays exactly as fetchable as it was. Closing that means
  moving or deleting stored objects — which breaks every link already in the
  wild — and it is a separate and genuinely irreversible decision.
* **It does not redact a PDF.** ``REDACTED_PDF`` has no producer in this repo.
* **It does not touch the page-level content plane.** ``DocumentPage`` and
  friends have their own writer and their own redactor
  (``redaction.redact_bundle``); sweeping them belongs with the extraction
  pipeline that writes them, not here.

**Exit codes mirror likhit's**, because a partial run must not read as a complete
one: ``0`` every document clean, ``1`` ran with skips, ``2`` at least one document
refused. A refusal is a document whose redacted text still scans positive for a
class the tier is supposed to remove — it is reported and left exactly as it was,
never written half-redacted.
"""

from __future__ import annotations

import json
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from materials.models import Material
from materials.redaction import (
    TEXT_KEY,
    RedactionUnavailable,
    redact_jsonld,
    redaction_summary,
)


#: Which tier first removes each class the scanner reports. Only the tier
#: BOUNDARY is recorded here; whether the configured tier reaches it is asked of
#: likhit (``mode.removes(minimum)``) rather than re-decided here, so widening a
#: tier upstream does not need an edit in this file.
#:
#: ``test_gate_covers_every_scanned_class`` fails if likhit starts reporting a
#: class this table does not know, because an unknown class is silently ungated —
#: which is the one failure mode a gate must not have.
def _introduced_at() -> dict:
    from likhit.privacy import RedactionMode

    return {
        "citizenship": RedactionMode.IDENTIFIERS,
        "date_of_birth": RedactionMode.IDENTIFIERS,
        "mobile": RedactionMode.CONTACT,
        "landline": RedactionMode.CONTACT,
        "email": RedactionMode.CONTACT,
        "bank_account": RedactionMode.STRICT,
        "passport": RedactionMode.STRICT,
    }


#: Scanner keys that no tier removes, and that therefore must never gate a write.
#:
#: ``pan`` is policy: a permanent account number identifies a *business*, is
#: public by design, and is frequently the only handle tying a procurement
#: finding to a registered contractor. ``phone_labelled`` is a shape diagnostic —
#: it counts labels, not values, and a label is what we deliberately leave behind
#: so a reader can see that something was removed.
NOT_REMOVED = frozenset({"pan", "phone_labelled"})

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

    def handle(self, *args, **options):
        apply_writes = options["apply"]
        limit = options.get("limit")
        if limit is not None and limit < 1:
            raise CommandError("--limit must be at least 1.")

        mode = self._check_redactor()
        gated = self._gated_classes(mode)
        self.stdout.write(
            f"tier={mode.value}  gate={','.join(sorted(gated)) or '(none)'}  "
            f"{'APPLYING' if apply_writes else 'dry run — nothing will be written'}"
        )

        totals: Counter = Counter()
        per_type: dict[str, Counter] = {}
        refused: list[str] = []
        looked_at = 0

        for material_type in self._types_in_order(options):
            counts = per_type.setdefault(material_type, Counter())
            for material in self._materials(material_type, options):
                if limit is not None and looked_at >= limit:
                    break
                looked_at += 1
                outcome, removed = self._one(material, gated, apply_writes)
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
        if skipped:
            raise CommandError(
                f"{skipped} document(s) could not be read and were skipped.",
                returncode=1,
            )

    # -- the work ---------------------------------------------------------

    def _one(self, material: Material, gated: set, apply_writes: bool):
        """Redact one document. Returns ``(outcome, removed counts)``.

        The order here is the whole safety argument: redact into memory, GATE the
        result, and only then write — and write both fields in one transaction,
        because ``data["text"]`` is overwritten in place and the original
        survives only because ``source_text`` was written beside it. A crash
        between those two writes loses a transcript that cost an OCR run and is
        not reproducible byte-for-byte.
        """
        try:
            redacted_data, original_text, removed = redact_jsonld(material.data)
        except RedactionUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad document must not end the run
            self.stderr.write(f"  unreadable {material.iri}: {exc}")
            return "unreadable", Counter()

        if not original_text:
            # redact_jsonld returns the original text ONLY when redaction changed
            # something, so an empty one means there was nothing to remove. That
            # covers the already-redacted rows too, which is what makes re-running
            # this command a no-op rather than a second pass over its own output.
            return "clean", removed

        residual = self._residual(redacted_data.get(TEXT_KEY), gated)
        if residual:
            self.stderr.write(
                f"  REFUSED {material.iri}: residual {dict(sorted(residual.items()))}"
            )
            return "refused", Counter()

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
                return "vanished", Counter()
            fresh.data = redacted_data
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
            fresh.save(using="ngm", update_fields=["data", "source_text"])
        return outcome, removed

    def _residual(self, text_value, gated: set) -> Counter:
        """Classes the tier should have removed that the scanner still sees.

        This is the plan's staged gate. It runs on the redacted text BEFORE
        anything is written, so a document whose redaction came out incomplete is
        left exactly as it was rather than published as redacted.
        """
        from likhit.privacy import scan_text

        found: Counter = Counter()
        for chunk in _strings(text_value):
            detections, _name_signals = scan_text(chunk)
            for kind, count in detections.items():
                if count and kind in gated:
                    found[kind] += count
        return found

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

    def _gated_classes(self, mode) -> set:
        """Scanner keys this tier is expected to zero out."""
        return {
            kind
            for kind, minimum in _introduced_at().items()
            if kind not in NOT_REMOVED and mode.removes(minimum)
        }

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
        if refused:
            self.stdout.write(
                self.style.ERROR(f"refused: {len(refused)} document(s), listed above")
            )
        elif apply_writes:
            self.stdout.write(self.style.SUCCESS("every document passed the gate"))

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
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        self.stdout.write(f"report written to {path}")


def _strings(value):
    """Every string in a ``text`` value, whatever shape it arrived in."""
    if isinstance(value, str):
        if value.strip():
            yield value
    elif isinstance(value, dict):
        for item in value.values():
            if isinstance(item, str) and item.strip():
                yield item
