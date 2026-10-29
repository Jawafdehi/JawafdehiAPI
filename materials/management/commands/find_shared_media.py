"""Find media objects shared by more than one Material, and say which are wrong.

Two materials pointing at one ``contentUrl`` is frequently CORRECT, and telling
the legitimate cases from the damaged ones is this command's whole job — a
report that mixes them trains the reader to skim past it.

Three classes, reported apart:

* **An EXTERNAL url shared is normal provenance.** One Koshi red-book PDF backs
  ten budget-line materials; one CourtListener docket backs five US filings; a
  news article and its ``web.archive.org`` snapshot are cited by two materials.
  The shared thing is somebody else's document that genuinely covers several of
  ours. Hidden unless ``--all``.

* **A shared TRANSCRIPT whose parents hold the same text is also correct.**
  Transcripts are stored content-addressed (see ``materials/conversion.py``), so
  two documents whose text is byte-identical legitimately share one object.

* **A shared transcript whose parents hold DIFFERENT text is damage.** That is
  the filename-keyed collision: the second upload won, and a material now links
  to another document's transcript. ``conversion.py`` calls this out as
  forward-only — rows damaged before the switch still carry the wrong link.

The verdict therefore comes from each parent's own ``data["text"]``, which is
per-row and correct even when the stored artifact is not. That is also what
makes repair cheap: a backfill can re-store the transcript from the database
without re-running OCR.

**READ-ONLY.** It reports; it never edits.

    manage.py find_shared_media                      # our artifacts only
    manage.py find_shared_media --all                # include external urls
    manage.py find_shared_media --role MARKDOWN      # transcripts only
    manage.py find_shared_media --source charge_sheet
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from materials.artifacts import is_our_copy, media_list, role_of
from materials.models import Material

TEXT_KEY = "text"


def _text_fingerprint(data: Any) -> str:
    """A stable digest of the material's own transcript, or ``""`` if it has none.

    ``data["text"]`` is a language-mapped container, so it is hashed as a whole
    rather than by picking a language — two rows differing only in which
    language they carry are still different text.
    """
    if not isinstance(data, dict):
        return ""
    text = data.get(TEXT_KEY)
    if not text:
        return ""
    return hashlib.sha256(repr(sorted(text.items()) if isinstance(text, dict) else text)
                          .encode("utf-8")).hexdigest()[:16]


class Command(BaseCommand):
    help = "Report media objects referenced by more than one Material."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Also report external urls.")
        parser.add_argument("--role", action="append", default=[], help="Filter by linkRole.")
        parser.add_argument("--source", action="append", default=[], help="Limit to sources.")

    def handle(self, *args, **options):
        roles = {r.strip().upper() for r in options["role"]}
        sources = options["source"]

        if sources:
            # A mistyped token used to produce a confident empty report, which
            # reads exactly like "nothing is wrong".
            known = set(
                Material.objects.filter(source__in=sources)
                .values_list("source", flat=True)
                .distinct()
            )
            missing = sorted(set(sources) - known)
            if missing:
                raise CommandError(f"no materials for --source {missing}")

        rows = Material.objects.filter(is_deleted=False).only("iri", "data")
        if sources:
            rows = rows.filter(source__in=sources)

        # Keyed by url → {iri: role}. A DICT, not a list: one material holding
        # the same url under two roles (RAW and ALTERNATE, say) is one
        # material, and counting links reported it as "shared by 2" — a false
        # positive on a perfectly ordinary document.
        refs: dict[str, dict[str, str]] = defaultdict(dict)
        texts: dict[str, str] = {}
        scanned = 0
        for material in rows.iterator(chunk_size=2000):
            scanned += 1
            texts[material.iri] = _text_fingerprint(material.data)
            for link in media_list(material.data):
                url = link.get("contentUrl")
                # Not a string ⇒ not a url. `.strip()` on a list or an int out
                # of free-form JSONB would crash the sweep, and a sweep whose
                # purpose is finding bad data must survive bad data.
                if not isinstance(url, str) or not url.strip():
                    continue
                role = role_of(link)
                if roles and role not in roles:
                    continue
                refs[url.strip()].setdefault(material.iri, role)

        shared = {u: r for u, r in refs.items() if len(r) > 1}
        ours = {u: r for u, r in shared.items() if is_our_copy(u)}
        external = {u: r for u, r in shared.items() if not is_our_copy(u)}

        damaged, benign = {}, {}
        for url, parents in ours.items():
            fingerprints = {texts.get(iri, "") for iri in parents}
            (damaged if len(fingerprints) > 1 else benign)[url] = parents

        self.stdout.write(
            f"scanned {scanned} materials, {len(refs)} distinct media urls, "
            f"{len(shared)} shared ({len(ours)} ours, {len(external)} external)"
        )
        self._report(
            "DAMAGED — parents hold DIFFERENT text, so at least one links to "
            "another document's transcript",
            damaged,
            texts,
            warn=True,
        )
        self._report(
            "shared but consistent — parents hold the same text, which "
            "content-addressed storage makes correct",
            benign,
            texts,
            warn=False,
        )
        if options["all"]:
            self._report("EXTERNAL urls — normal provenance", external, texts, warn=False)
        elif external:
            self.stdout.write(f"\n{len(external)} shared external url(s) hidden; pass --all.")

        if damaged:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{len(damaged)} damaged artifact(s). Each parent's own data['text'] "
                    "is correct, so a backfill can re-store from the DB without OCR."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("\nno damaged artifacts"))

    def _report(self, title, groups, texts, *, warn):
        if not groups:
            return
        self.stdout.write(f"\n=== {title}: {len(groups)} ===")
        # Sorted by size then url: a report that reorders between runs cannot
        # be diffed against the previous one.
        for url, parents in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            line = f"\n  {url}\n    shared by {len(parents)} materials"
            self.stdout.write(self.style.WARNING(line) if warn else line)
            for iri, role in sorted(parents.items()):
                self.stdout.write(f"      {role:10} text={texts.get(iri) or '-':16} {iri}")
