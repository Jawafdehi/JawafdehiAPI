"""Find media objects referenced by more than one Material.

A ``Material`` carries its files in ``data["associatedMedia"]`` as roled
``MediaObject`` entries (``RAW`` the original, ``MARKDOWN`` our transcript, and
so on). Two materials pointing at the SAME ``contentUrl`` is not automatically
wrong, and the distinction is the whole point of this command:

* **An EXTERNAL url shared is usually correct.** One Koshi red-book PDF backs
  ten budget-line materials; one CourtListener docket backs five US filings; a
  news article and its ``web.archive.org`` snapshot are cited by two materials.
  Nothing is wrong there — the shared thing is somebody else's document that
  genuinely covers several of ours.

* **One of OUR OWN stored artifacts shared is a smell**, and a shared
  ``MARKDOWN`` is close to a defect by construction. Uploads are keyed on
  filename rather than content, so two files uploaded under one name collide on
  a single object and the second silently wins. The material then serves text
  that belongs to a different document, with nothing in the payload saying so.

So the command splits the two and reports them apart. ``--ours-only`` (the
default) suppresses the external class, because reporting it trains the reader
to skim past the output.

**This is READ-ONLY.** It reports; it never edits. The fix for a real collision
is to re-upload the correct artifact for the losing material, which is a
judgement call per row — a transcript cannot be reattached mechanically because
the right one may no longer exist anywhere.

Why a command rather than a script against the public API: the API can only
enumerate ``LISTED`` rows, which silently omits every private and case-gated
material, and paging 366k rows over HTTP is rude and slow. The duplicate check
is a group-by; it belongs next to the data.

    manage.py find_shared_media                      # our artifacts only
    manage.py find_shared_media --all                # include external urls
    manage.py find_shared_media --role MARKDOWN      # the likeliest defects
    manage.py find_shared_media --source charge_sheet --source ag
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.core.management.base import BaseCommand

from materials.models import Material

#: Host of our own object store. A ``contentUrl`` here is an artifact WE wrote,
#: so two materials sharing one is a collision rather than shared provenance.
OURS = "s3.jawafdehi.org"


def _media(data: Any) -> list[dict[str, Any]]:
    """The material's ``associatedMedia`` entries, tolerating a malformed doc.

    ``data`` is a free-form ``JSONField``; a row written before the current
    shape, or by hand, may carry a string or null here. A sweep that exists to
    find data defects must not itself fall over on one.
    """
    media = data.get("associatedMedia") if isinstance(data, dict) else None
    if not isinstance(media, list):
        return []
    return [m for m in media if isinstance(m, dict)]


class Command(BaseCommand):
    help = "Report media objects referenced by more than one Material."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Also report shared EXTERNAL urls, which are usually legitimate.",
        )
        parser.add_argument(
            "--role",
            action="append",
            default=[],
            help="Only consider links with this jawafdehi:linkRole (repeatable).",
        )
        parser.add_argument(
            "--source",
            action="append",
            default=[],
            help="Limit to these source tokens (repeatable). Default: every source.",
        )

    def handle(self, *args, **options):
        roles = set(options["role"])
        sources = options["source"]

        rows = Material.objects.filter(is_deleted=False).only("iri", "data")
        if sources:
            rows = rows.filter(source__in=sources)

        by_url: dict[str, list[tuple[str, str]]] = defaultdict(list)
        scanned = 0
        for material in rows.iterator(chunk_size=2000):
            scanned += 1
            for link in _media(material.data):
                url = (link.get("contentUrl") or "").strip()
                if not url:
                    # An empty contentUrl is its own (separate) data defect, and
                    # bucketing every one of them together under "" would
                    # report a single enormous fake collision.
                    continue
                role = link.get("jawafdehi:linkRole") or "?"
                if roles and role not in roles:
                    continue
                by_url[url].append((material.iri, role))

        shared = {u: refs for u, refs in by_url.items() if len(refs) > 1}
        ours = {u: r for u, r in shared.items() if OURS in u}
        external = {u: r for u, r in shared.items() if OURS not in u}

        self.stdout.write(
            f"scanned {scanned} materials, {len(by_url)} distinct media urls, "
            f"{len(shared)} shared ({len(ours)} ours, {len(external)} external)"
        )

        self._report("OUR OWN ARTIFACTS — investigate each", ours, warn=True)
        if options["all"]:
            self._report("EXTERNAL urls — usually legitimate", external, warn=False)
        elif external:
            self.stdout.write(
                f"\n{len(external)} shared external url(s) hidden; pass --all to see them."
            )

        if ours:
            # Non-zero-ish signal for a human reading a cron log, without
            # failing the command — this is a report, not a gate.
            self.stdout.write(
                self.style.WARNING(
                    f"\n{len(ours)} shared artifact(s) need a decision. A shared MARKDOWN "
                    "means a material may be serving another document's text."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("\nno shared artifacts of our own"))

    def _report(self, title: str, groups: dict[str, list[tuple[str, str]]], *, warn: bool) -> None:
        if not groups:
            return
        self.stdout.write(f"\n=== {title}: {len(groups)} ===")
        for url, refs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            roles = sorted({role for _, role in refs})
            line = f"\n  {url}\n    shared by {len(refs)} materials, roles={roles}"
            self.stdout.write(self.style.WARNING(line) if warn else line)
            for iri, role in sorted(refs):
                self.stdout.write(f"      {role:10} {iri}")
