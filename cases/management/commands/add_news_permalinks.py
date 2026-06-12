"""Add a PERMALINK (web-archive) link to NEWS DocumentSources.

NEWS sources usually carry a single RAW link to the live article, which is
prone to link rot. This command attaches a stable archival copy as a
``PERMALINK``-role entry alongside the existing links (the RAW link is never
removed or altered).

For each in-scope source it resolves a permalink from the Wayback Machine:

1. **Use existing** — query the availability API for the closest existing
   snapshot of the RAW link.
2. **Else save** — if no snapshot exists (and ``--no-save`` was not passed),
   request a fresh capture via the Save Page Now endpoint, throttled by
   ``--save-delay`` because SPN is rate-limited.

Scope: ``source_type=NEWS``, not soft-deleted, with at least one non-archival
RAW link and no existing PERMALINK (idempotent — a re-run skips done sources).

NEWS sources with no ``publication_date`` are **skipped and reported**, not
modified: ``DocumentSource.save()`` runs ``full_clean()``, which requires a
publication date for NEWS (raising ValidationError), so they cannot be saved
here and are listed for separate remediation.

Read-only by default (lists what WOULD change). Pass ``--apply`` to persist.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand

from cases.models import DocumentSource, SourceLinkRole, SourceType

# The public archive hosts whose links already count as a permalink — a source
# whose only RAW link is itself an archive URL needs no further archiving.
ARCHIVE_HOSTS = ("web.archive.org", "archive.org")
AVAILABILITY_API = "https://archive.org/wayback/available"
SAVE_BASE = "https://web.archive.org/save/"
# A browser-like UA: archive.org throttles/blocks some default library UAs.
UA = "Mozilla/5.0 (jawafdehi-news-permalinks)"


def _host(link: str) -> str:
    try:
        return (urllib.parse.urlparse(link).netloc or "").lower()
    except ValueError:
        return ""


def _is_archive(link: str) -> bool:
    host = _host(link)
    return any(host == h or host.endswith("." + h) for h in ARCHIVE_HOSTS)


class Command(BaseCommand):
    help = (
        "Attach a Wayback PERMALINK to NEWS sources' RAW links "
        "(dry-run unless --apply)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist the new PERMALINK links. Without this flag, only "
            "lists what would change.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of sources to process (for safe batching).",
        )
        parser.add_argument(
            "--source-id",
            default=None,
            help="Process only this single source_id.",
        )
        parser.add_argument(
            "--no-save",
            action="store_true",
            help="Only use existing Wayback snapshots; do not request new "
            "captures for RAW links that have none.",
        )
        parser.add_argument(
            "--save-delay",
            type=float,
            default=6.0,
            help="Seconds to wait between Save Page Now requests (SPN is "
            "rate-limited). Default: 6.0",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=30.0,
            help="HTTP timeout in seconds for archive.org calls. Default: 30",
        )

    def handle(self, *args, **options):
        self.apply = options["apply"]
        self.timeout = options["timeout"]
        self.save_missing = not options["no_save"]
        self.save_delay = options["save_delay"]
        self._last_save = 0.0

        qs = DocumentSource.objects.filter(
            source_type=SourceType.NEWS, is_deleted=False
        ).order_by("created_at")
        if options["source_id"]:
            qs = qs.filter(source_id=options["source_id"])
        if options["limit"] is not None:
            qs = qs[: options["limit"]]

        self.stdout.write(
            f"Scanning {qs.count()} NEWS source(s) "
            f"({'APPLY' if self.apply else 'dry-run'})…"
        )

        # Report buckets.
        patched = 0
        skipped_has_permalink = 0
        skipped_no_raw = 0
        skipped_null_pubdate = []
        save_failed = []
        errors = []

        for src in qs:
            url_list = src.url if isinstance(src.url, list) else []

            # Already archived — nothing to do. We check for ANY archive-host
            # link (any role), not just a PERMALINK-role one: legacy data
            # sometimes stored the archive URL as RAW, and appending it again as
            # PERMALINK would duplicate the link under two roles.
            if any(
                isinstance(u, dict) and _is_archive(u.get("link") or "")
                for u in url_list
            ):
                skipped_has_permalink += 1
                continue

            # NEWS without a publication date cannot be saved (full_clean would
            # reject it). Report, don't touch.
            if not src.publication_date:
                skipped_null_pubdate.append(src.source_id)
                continue

            target = self._pick_raw(url_list)
            if not target:
                skipped_no_raw += 1
                continue

            permalink = self._resolve_permalink(target)
            if not permalink:
                save_failed.append((src.source_id, target))
                continue

            self.stdout.write(
                f"  {src.source_id}\n    RAW: {target}\n    + PERMALINK: {permalink}"
            )

            if not self.apply:
                patched += 1
                continue

            src.url = list(url_list) + [
                {"link": permalink, "role": SourceLinkRole.PERMALINK.value}
            ]
            try:
                src.save()
                patched += 1
            except ValidationError as exc:
                errors.append((src.source_id, exc.message_dict))
                self.stderr.write(f"    FAILED {src.source_id}: {exc.message_dict}")

        self._report(
            patched=patched,
            skipped_has_permalink=skipped_has_permalink,
            skipped_no_raw=skipped_no_raw,
            skipped_null_pubdate=skipped_null_pubdate,
            save_failed=save_failed,
            errors=errors,
        )

    # --- link selection -------------------------------------------------

    @staticmethod
    def _pick_raw(url_list):
        """Return the first non-archival RAW link, or None.

        A source whose only RAW link is itself an archive URL is treated as
        already-archival and skipped (returns None).
        """
        for u in url_list:
            if not isinstance(u, dict):
                continue
            if u.get("role") != SourceLinkRole.RAW.value:
                continue
            link = (u.get("link") or "").strip()
            if link and not _is_archive(link):
                return link
        return None

    # --- wayback resolution ---------------------------------------------

    def _resolve_permalink(self, raw_link):
        """Closest existing snapshot, else (optionally) a fresh save. None on miss."""
        snapshot = self._closest_snapshot(raw_link)
        if snapshot:
            return snapshot
        if self.save_missing:
            return self._save_page(raw_link)
        return None

    def _closest_snapshot(self, raw_link):
        params = urllib.parse.urlencode({"url": raw_link})
        status, data = self._get(f"{AVAILABILITY_API}?{params}")
        if status != 200 or not isinstance(data, dict):
            return None
        closest = (data.get("archived_snapshots") or {}).get("closest") or {}
        if closest.get("available") and closest.get("url"):
            # Normalize the scheme to https; the API sometimes returns http.
            return closest["url"].replace(
                "http://web.archive.org", "https://web.archive.org", 1
            )
        return None

    def _save_page(self, raw_link):
        """Request a fresh capture via SPN; return its snapshot URL or None.

        Throttled by --save-delay. On any failure the source is reported as
        save-failed rather than aborting the batch.
        """
        if not self.apply:
            # Don't hit SPN during a dry-run; report it as a would-save.
            return f"[would save: {raw_link}]"

        wait = self.save_delay - (time.monotonic() - self._last_save)
        if wait > 0:
            time.sleep(wait)
        self._last_save = time.monotonic()

        req = urllib.request.Request(
            SAVE_BASE + raw_link, method="GET", headers={"User-Agent": UA}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                # SPN returns the capture location in Content-Location:
                # /web/<timestamp>/<original-url>
                loc = resp.headers.get("Content-Location")
                if loc and loc.startswith("/web/"):
                    return "https://web.archive.org" + loc
        except (urllib.error.URLError, OSError, ValueError):
            pass

        # Fall back to re-querying availability — the capture may have landed.
        return self._closest_snapshot(raw_link)

    def _get(self, url):
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except (urllib.error.URLError, OSError, ValueError):
            return 0, None

    # --- reporting ------------------------------------------------------

    def _report(
        self,
        *,
        patched,
        skipped_has_permalink,
        skipped_no_raw,
        skipped_null_pubdate,
        save_failed,
        errors,
    ):
        verb = "Added" if self.apply else "Would add"
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} PERMALINK to {patched} source(s). "
                f"Skipped: {skipped_has_permalink} already-archived, "
                f"{skipped_no_raw} no usable RAW link."
            )
        )
        if skipped_null_pubdate:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{len(skipped_null_pubdate)} NEWS source(s) skipped — "
                    f"no publication_date (cannot save; fix separately):"
                )
            )
            for sid in skipped_null_pubdate:
                self.stdout.write(f"  {sid}")
        if save_failed:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{len(save_failed)} source(s) had no snapshot and could "
                    f"not be archived:"
                )
            )
            for sid, link in save_failed:
                self.stdout.write(f"  {sid}  {link}")
        if errors:
            self.stdout.write(
                self.style.ERROR(
                    f"\n{len(errors)} source(s) failed validation on save:"
                )
            )
            for sid, detail in errors:
                self.stdout.write(f"  {sid}: {detail}")
        if not self.apply and patched:
            self.stdout.write("\nRe-run with --apply to persist.")
