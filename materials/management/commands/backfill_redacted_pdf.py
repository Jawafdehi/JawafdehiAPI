"""Produce the redacted PDF a material is supposed to publish.

    uv run python manage.py backfill_redacted_pdf --limit 20          # dry run
    uv run python manage.py backfill_redacted_pdf --iri <iri> --apply
    uv run python manage.py backfill_redacted_pdf --material-type charge_sheet --apply

``materials.artifacts`` has described four artifacts since #542 — original PDF,
redacted PDF, original text, redacted text — and publishes the redacted two.
**Nothing in this repository has ever produced the redacted PDF.** ``git grep
REDACTED_PDF_ROLE`` outside ``artifacts.py`` finds no writer, so every material
in the archive publishes its original, and ``is_withheld`` goes on publishing it
because the counterpart that would replace it does not exist.

That is the whole exposure for a scanned document: redacting the text removes
the value from search and from the payload, and leaves it legible in the file a
reader opens.

**likhit does the work and decides what it cannot do.**
``likhit.privacy.pdf.redact_pdf`` classifies every page and **refuses the
document** if any page is one it cannot locate values on — a legacy pre-Unicode
font, or a scan with no text layer at all. That refusal is the feature: the
alternative is a file named as a redacted derivative that is byte-identical to
its source, published in place of an original that is then withheld. Measured
over 28 charge sheets held locally: 10 redactable, 11 scanned, 7 carrying a
handful of legacy pages inside an otherwise Unicode document.

So this command is **phase one of three**, and the two it does not cover are
named in ``docs/2026-10-13-likhit-pii-redaction`` §3.4: the legacy glyph map
(specified upstream, unbuilt) and bounding-box OCR (a new dependency).

🛑 **Nothing is deleted and no original is touched.** The redacted PDF is a new
object beside the original; what changes for a reader is which of the two the
payload links. The original keeps its URL and stays fetchable by anyone holding
it — closing *that* means moving stored objects, which is a separate and
irreversible decision.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from jawafdehi_shared.storage import store_file_as_link
from materials import artifacts, jsonld, provenance
from materials.models import Material

#: A ceiling on what is worth pulling into memory to redact. The largest document
#: measured upstream — the Auditor General's 63rd annual report — is 79.7 MiB.
MAX_PDF_BYTES = 192 * 1024 * 1024


class Command(BaseCommand):
    help = (
        "Produce and publish the redacted PDF for materials that have none. "
        "Reports only unless --apply is given."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--material-type", dest="material_type")
        parser.add_argument("--source")
        parser.add_argument("--iri")
        parser.add_argument("--limit", type=int)
        parser.add_argument("--report", help="Write the run's counts here as JSON.")

    def handle(self, *args, **options):
        apply_writes = options["apply"]
        limit = options.get("limit")
        if limit is not None and limit < 1:
            raise CommandError("--limit must be at least 1.")
        mode = self._mode()
        self.stdout.write(
            f"tier={mode.value}  "
            f"{'APPLYING' if apply_writes else 'dry run — nothing will be written'}"
        )

        outcomes: Counter = Counter()
        blocked: Counter = Counter()
        acted_on = 0

        for material in self._materials(options):
            outcome, detail = self._one(material, mode, apply_writes)
            outcomes[outcome] += 1
            # 🛑 The limit counts documents this run CHANGED, and nothing else.
            # Slicing the queryset took the same first N IRIs every time, so a
            # batched run never advanced past the first batch. Counting failures
            # stalls it from the other side: 18 of 28 charge sheets are blocked
            # for good (11 scanned, 7 legacy), nothing skips them on a later run,
            # so the budget goes on the same permanent failures forever — and
            # each one is re-downloaded and re-redacted to get there.
            if outcome in ("published", "would_publish"):
                acted_on += 1
            if outcome == "blocked":
                blocked.update(detail)
                self.stderr.write(f"  blocked {material.iri}: {dict(detail)}")
            elif outcome in ("unreadable", "refused"):
                self.stderr.write(f"  {outcome} {material.iri}: {detail}")
            if limit is not None and acted_on >= limit:
                break

        self._report(outcomes, blocked, acted_on, apply_writes)
        if options.get("report"):
            with open(options["report"], "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "tier": mode.value,
                        "applied": apply_writes,
                        "outcomes": dict(outcomes),
                        "blocked_by_page_class": dict(blocked),
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
        if outcomes["blocked"] or outcomes["refused"] or outcomes["unreadable"]:
            raise CommandError(
                f"{outcomes['blocked']} document(s) carry a page this build cannot "
                f"redact, {outcomes['refused']} were refused by verification and "
                f"{outcomes['unreadable']} could not be read.",
                returncode=1,
            )

    # -- the work ---------------------------------------------------------

    def _mode(self):
        """The configured tier, asked of the same place every other writer asks.

        Raised rather than defaulted: a PDF redacted at a tier nobody chose is
        worse than no redacted PDF, because it is published as the thing a
        reader gets INSTEAD of the original.
        """
        from materials.redaction import configured_mode

        return configured_mode()

    def _one(self, material: Material, mode, apply_writes: bool):
        """Redact one document's PDF. Returns ``(outcome, detail)``."""
        from likhit.privacy.pdf import redact_pdf

        from materials.extract import DocumentFetchError, fetch_document

        if self._has_redacted(material):
            # Distinct from "no PDF": an operator reading the report needs to
            # know whether a document was already done or never had a file.
            return "already_published", ""
        url = self._original_pdf(material)
        if url is None:
            return "no_pdf", ""
        try:
            raw = fetch_document(url, max_bytes=MAX_PDF_BYTES)
        except (DocumentFetchError, OSError) as exc:
            return "unreadable", str(exc)
        if not raw:
            # The empty-object case audit_stored_artifacts exists for: there is
            # nothing to redact and nothing being published either.
            return "unreadable", "the stored object is empty"

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.pdf"
            destination = Path(tmp) / "redacted.pdf"
            source.write_bytes(raw)
            try:
                report = redact_pdf(source, destination, mode=mode)
            except ValueError as exc:
                # likhit refuses two different things through one ValueError: a
                # page class it cannot locate values on, and residue in its own
                # output. Both mean no publishable derivative, and the message is
                # the only thing that distinguishes them.
                text = str(exc)
                if "verification failed" in text:
                    return "refused", text
                return "blocked", self._blocked_classes(text)
            except Exception as exc:  # noqa: BLE001 - one bad file must not end the run
                # 🛑 **Not every failure is a ValueError.** PyMuPDF raises
                # `FileDataError`, which subclasses RuntimeError, for a body that
                # is not a PDF at all — and one of those would otherwise abort an
                # --apply run partway through, with the --report never written.
                return "unreadable", f"{type(exc).__name__}: {exc}"
            redacted = destination.read_bytes()

        if not apply_writes:
            return "would_publish", report.values_removed
        self._publish(material, redacted, url)
        return "published", report.values_removed

    def _blocked_classes(self, message: str) -> Counter:
        """Every page class named in the refusal, not just the first.

        A document mixing legacy and scanned pages is blocked by both, and
        reporting one hides half of what the remaining work has to cover.
        `blank` is deliberately absent: `PageClass.BLANK.needs_work` is False, so
        it can never be the reason.
        """
        found = Counter(
            {name: 1 for name in ("legacy", "scanned") if name in message}
        )
        # A ValueError that names no class is something else entirely — a
        # password, a zero-page file, a destination that already exists.
        return found or Counter({"other": 1})

    def _publish(self, material: Material, redacted: bytes, source_url: str) -> None:
        """Store the derivative and point the row at it, under a row lock."""
        link = store_file_as_link(
            ContentFile(redacted, name="material.redacted.pdf"),
            role=artifacts.REDACTED_PDF_ROLE,
            content_hash=hashlib.sha256(redacted).hexdigest(),
        )
        with transaction.atomic(using="ngm"):
            fresh = (
                Material.objects.using("ngm")
                .select_for_update()
                .filter(pk=material.iri)
                .first()
            )
            if fresh is None:
                return
            if self._has_redacted(fresh):
                # `_has_redacted` was consulted before the fetch and the redaction,
                # which is two network round trips ago. Another run — or a re-run
                # started while this one was still walking — may have published in
                # that window, and the replace below would discard its link without
                # a word, possibly one produced at a different tier since `_mode()`
                # is read per process.
                self.stderr.write(f"  changed_underfoot {material.iri}")
                return
            data = dict(fresh.data or {})
            # Replace-or-append by role, the same rule materials.transcripts states
            # for the transcript pair. Worth converging the two once both have
            # landed; kept local here so this command does not depend on an
            # unmerged branch.
            media = [
                mo
                for mo in artifacts.media_list(data)
                if artifacts.role_of(mo) != artifacts.REDACTED_PDF_ROLE
            ]
            mo = jsonld._media_object(
                {"link": link["link"], "role": artifacts.REDACTED_PDF_ROLE}
            )
            if mo is None:
                return
            # `fetch_method="redaction"`, never "ocr": this file was produced by
            # redacting a PDF we already held, not by reading the publisher's.
            # materials.transcripts makes the same distinction for the same
            # reason — two artifacts of the same source differing only in a
            # digest would describe themselves as the same kind of claim.
            mo[provenance.PROVENANCE_KEY] = provenance.build_provenance(
                sha256=hashlib.sha256(redacted).hexdigest(),
                fetch_method="redaction",
                source_url=source_url,
                content_length=len(redacted),
            )
            media.append(mo)
            data["associatedMedia"] = media
            fresh.data = data
            # `updated_at` is load-bearing in update_fields, not decoration: it is
            # what materials/views.py derives the ETag from, so omitting it leaves
            # a stale If-Match passing its precondition and clobbering the link
            # this just published. Every sibling writer includes it.
            fresh.save(using="ngm", update_fields=["data", "updated_at"])

    # -- selection --------------------------------------------------------

    def _original_pdf(self, material: Material) -> str | None:
        """Our own copy of the original, or None.

        A publisher's URL is deliberately not redacted: we did not store that
        file, hosting a derivative of it would be a new publication of theirs,
        and the citation chain a reader follows is the point of the link.
        """
        for media in artifacts.media_list(material.data or {}):
            url = media.get("contentUrl")
            # ⚠️ No extension test. An object key's suffix comes from the
            # client-supplied filename — the court scraper defaults to `.doc` —
            # so filtering on `.pdf` silently skipped stored PDFs and reported
            # them as `no_pdf`. Whether the bytes are a PDF is likhit's question,
            # and it now answers it as `unreadable` rather than aborting.
            if (
                artifacts.role_of(media) in artifacts.ORIGINAL_PDF_ROLES
                and isinstance(url, str)
                and artifacts.is_our_copy(url)
            ):
                return url
        return None

    def _has_redacted(self, material: Material) -> bool:
        return any(
            artifacts.role_of(media) == artifacts.REDACTED_PDF_ROLE
            for media in artifacts.media_list(material.data or {})
        )

    def _materials(self, options):
        rows = Material.objects.using("ngm").filter(is_deleted=False)
        if options.get("iri"):
            rows = rows.filter(pk=options["iri"])
        if options.get("material_type"):
            rows = rows.filter(material_type=options["material_type"])
        if options.get("source"):
            rows = rows.filter(source=options["source"])
        return rows.order_by("iri").iterator(chunk_size=50)

    def _report(self, outcomes, blocked, acted_on, apply_writes) -> None:
        self.stdout.write("")
        # Not "looked at": the limit counts documents acted on, so this has to
        # name the same thing the limit does or the two read as a contradiction.
        self.stdout.write(f"acted on {acted_on} document(s)")
        if outcomes:
            self.stdout.write(
                "  ".join(f"{k}={v}" for k, v in sorted(outcomes.items()))
            )
        if blocked:
            self.stdout.write(
                self.style.WARNING(
                    "pages this build cannot redact: "
                    + "  ".join(f"{k}={v}" for k, v in sorted(blocked.items()))
                    + " — see docs/2026-10-13-likhit-pii-redaction §3.4"
                )
            )
        if apply_writes and outcomes["published"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"{outcomes['published']} document(s) now publish a redacted PDF; "
                    "their originals are withheld from the public payload"
                )
            )
