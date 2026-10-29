"""Find materials whose own stored file is not there.

    uv run python manage.py audit_stored_artifacts --limit 500
    uv run python manage.py audit_stored_artifacts --material-type charge_sheet --report work/empty.json

A material's public payload advertises what we hold — ``jawafdehi:artifacts``
says ``pdf.original: true`` — on the strength of a ``RAW`` link being present in
``associatedMedia``. Nothing checks that the object behind it has any bytes.

**It sometimes has none.** The stored object answers HTTP **200 with
Content-Length 0**, so a reader following the link gets an empty file and no
error, and every count we publish of "documents held" includes it. Sampling 30
materials at random on 2026-10-28 found 2 in that state.

🛑 **This reads and reports; it writes nothing.** Repair is not symmetric and so
is not automatic: a material carrying ``jawafdehi:sourceUrl`` can be re-fetched
from the publisher, while one without it has lost the file for good and what
needs correcting is the *claim* rather than the object. Two of the three empty
objects found by hand had no upstream URL at all. Which of those a document gets
is a decision about the archive, not a sweep's side effect.

Exit codes mirror the redaction commands: ``0`` every object present, ``1`` at
least one empty or unreachable.
"""

from __future__ import annotations

import json
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from materials import artifacts
from materials.models import Material

#: Only our own copies. A publisher's link that 404s is their outage and their
#: fix; ``is_our_copy`` is where that line already lives.
#: 🛑 The REDACTED roles are in here too, and they are the ones that matter most:
#: a redacted artifact is what `is_withheld` publishes INSTEAD of the original, so
#: an empty one leaves a reader with nothing while the original is withheld on the
#: strength of its existence.
CHECKED_ROLES = artifacts.ORIGINAL_PDF_ROLES | {
    artifacts.ORIGINAL_TRANSCRIPT_ROLE,
    artifacts.REDACTED_TRANSCRIPT_ROLE,
    artifacts.REDACTED_PDF_ROLE,
}


#: Roles we produce ourselves. An empty one is repaired by re-running the command
#: that makes it, never by asking the publisher for a file they never had.
#: ⚠️ MARKDOWN is in here too. It is written by the convert job from OCR, so the
#: publisher never had that file either — the same argument that puts the two
#: REDACTED roles here. Only the ORIGINAL PDF roles are genuinely re-fetchable.
DERIVED_ROLES = frozenset(
    {
        artifacts.REDACTED_PDF_ROLE,
        artifacts.REDACTED_TRANSCRIPT_ROLE,
        artifacts.ORIGINAL_TRANSCRIPT_ROLE,
    }
)


class Command(BaseCommand):
    help = "Report materials whose stored artifact is empty or unreachable."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--material-type", dest="material_type")
        parser.add_argument("--source")
        parser.add_argument("--limit", type=int)
        parser.add_argument(
            "--report", help="Write the findings to this path as JSON."
        )

    def handle(self, *args, **options):
        limit = options.get("limit")
        if limit is not None and limit < 1:
            raise CommandError("--limit must be at least 1.")

        import requests

        from review.jds_client import UA

        rows = Material.objects.using("ngm").filter(is_deleted=False)
        if options.get("material_type"):
            rows = rows.filter(material_type=options["material_type"])
        if options.get("source"):
            rows = rows.filter(source=options["source"])
        rows = rows.order_by("iri")
        if limit:
            rows = rows[:limit]

        counts: Counter = Counter()
        findings: list[dict] = []
        session = requests.Session()

        for material in rows.iterator(chunk_size=200):
            for media in artifacts.media_list(material.data or {}):
                role = artifacts.role_of(media)
                url = media.get("contentUrl")
                # `is_our_copy` already rejects a non-string — contentUrl comes
                # out of free-form JSONB and can be an int, a list or absent —
                # but it rejects it behind a function call, which is a narrowing
                # no type checker can follow. Saying it here states the invariant
                # at the point that depends on it.
                if not isinstance(url, str) or role not in CHECKED_ROLES:
                    continue
                if not artifacts.is_our_copy(url):
                    continue
                counts["checked"] += 1
                verdict, detail = self._probe(session, url, UA)
                counts[verdict] += 1
                if verdict == "present":
                    continue
                findings.append(
                    {
                        "iri": material.iri,
                        "role": role,
                        "verdict": verdict,
                        "detail": detail,
                        # The repair route, decided per document rather than by a sweep.
                        "upstream": (material.data or {}).get("jawafdehi:sourceUrl"),
                    }
                )
                self.stderr.write(f"  {verdict} {material.iri} [{role}]: {detail}")

        self._report(counts, findings)
        if options.get("report"):
            with open(options["report"], "w", encoding="utf-8") as handle:
                json.dump(
                    {"counts": dict(counts), "findings": findings},
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
        broken = len(findings)
        if broken:
            raise CommandError(
                f"{broken} stored artifact(s) are empty or unreachable.", returncode=1
            )

    def _probe(self, session, url: str, ua: str):
        """``(verdict, detail)`` for one stored object.

        ⚠️ **GET, not HEAD**, and streamed so the body is never downloaded. Some
        object stores answer HEAD without a ``Content-Length`` at all, which is
        indistinguishable here from the zero this command exists to find.
        """
        try:
            with session.get(
                url, headers={"User-Agent": ua}, timeout=30, stream=True
            ) as response:
                if response.status_code != 200:
                    return "unreachable", f"HTTP {response.status_code}"
                for chunk in response.iter_content(chunk_size=1):
                    if chunk:
                        return "present", ""
                return "empty", "HTTP 200, no bytes"
        except Exception as exc:  # noqa: BLE001 - one bad URL must not end the sweep
            return "unreachable", f"{type(exc).__name__}: {exc}"

    def _report(self, counts: Counter, findings: list[dict]) -> None:
        self.stdout.write("")
        self.stdout.write(
            f"checked {counts['checked']} stored artifact(s): "
            f"present={counts['present']} empty={counts['empty']} "
            f"unreachable={counts['unreachable']}"
        )
        # 🛑 Three remedies, not two, and the role decides which. A REDACTED
        # artifact is one WE derived — the publisher never had it, so "re-fetch
        # from upstream" is never the answer for the class this command calls the
        # most important. Routing it there sent an operator triaging the report to
        # the wrong remedy for the highest-priority finding.
        # One pass each, and on the role rather than on list membership: `f not
        # in rederive` compared whole dicts, which is O(n^2) over a corpus-wide
        # run and treats two value-equal findings as one.
        rederive = [f for f in findings if f["role"] in DERIVED_ROLES]
        refetch = [
            f
            for f in findings
            if f["role"] not in DERIVED_ROLES and f["upstream"]
        ]
        reclaim = len(findings) - len(rederive) - len(refetch)
        if findings:
            self.stdout.write(
                self.style.WARNING(
                    f"{len(rederive)} need re-deriving (run the backfill that "
                    f"produces them); {len(refetch)} can be re-fetched from the "
                    f"publisher; {reclaim} have no upstream URL and need the CLAIM "
                    "corrected instead"
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("every stored artifact has bytes"))
