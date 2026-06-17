"""LOCAL-DEV ONLY: copy a production case into the local DB for testing.

Pulls a published/draft case (and its DocumentSources + entities) from the prod
Jawafdehi API and upserts it into the LOCAL database so the local API serves a
faithful copy. Used to benchmark ``enrich_ciaa_description`` against real source
documents without touching production.

The case is imported as DRAFT with its ``description`` STRIPPED (so we can
regenerate it from scratch), but the original prod description is saved to a
sidecar file ``ground_truth/<court_number>.md`` as the diff benchmark.

This command never writes to prod — it only GETs from the prod API and writes to
the local DB. Refuses to run unless the local DB is sqlite (a guard against
accidentally importing into a Postgres prod connection).

Usage::

    python manage.py import_prod_case_local 080-CR-0018 080-CR-0047 080-CR-0007
    python manage.py import_prod_case_local 080-CR-0018 --keep-description
"""

import os
import re
import urllib.parse
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    DocumentSource,
    JawafEntity,
)

# Map the public API's lowercase entity "type" to the RelationshipType value.
_REL_TYPES = {"accused", "related", "location", "alleged"}

# A court case number like 080-CR-0047 / 081-WO-1234, matched as a whole token.
_COURT_RE = re.compile(r"(?<![\dA-Za-z])\d{2,3}-[A-Za-z]{1,3}-\d{3,4}(?![\dA-Za-z])")


class Command(BaseCommand):
    help = "Copy a prod case (sources + entities) into the local DB for testing."

    def add_arguments(self, parser):
        parser.add_argument(
            "court_numbers",
            nargs="+",
            help="Court case numbers to import, e.g. 080-CR-0018.",
        )
        parser.add_argument(
            "--api-base-url",
            default=os.environ.get(
                "JAWAFDEHI_API_BASE", "https://portal.jawafdehi.org/api"
            ),
            help="Prod API base URL (defaults to JAWAFDEHI_API_BASE).",
        )
        parser.add_argument(
            "--api-token",
            default=os.environ.get("JAWAFDEHI_API_TOKEN"),
            help="Prod API token (defaults to JAWAFDEHI_API_TOKEN).",
        )
        parser.add_argument(
            "--keep-description",
            action="store_true",
            help="Import the prod description as-is instead of stripping it.",
        )
        parser.add_argument(
            "--ground-truth-dir",
            default=str(Path(settings.BASE_DIR) / "ground_truth"),
            help="Directory to save prod descriptions for benchmarking.",
        )

    def handle(self, *args, **options):
        if "sqlite" not in settings.DATABASES["default"]["ENGINE"]:
            raise CommandError(
                "Refusing to run: local DB is not sqlite. Source localdev.env.sh "
                "first so imports never land in a prod Postgres connection."
            )
        token = options["api_token"]
        if not token:
            raise CommandError("Set --api-token or JAWAFDEHI_API_TOKEN.")

        base = options["api_base_url"].rstrip("/")
        if not base.endswith("/api"):
            base = f"{base}/api"
        session = requests.Session()
        session.headers.update(
            {"Authorization": f"Token {token}", "Accept": "application/json"}
        )

        gt_dir = Path(options["ground_truth_dir"])
        gt_dir.mkdir(parents=True, exist_ok=True)

        failures = []
        for number in options["court_numbers"]:
            try:
                self._import_one(
                    number, base, session, gt_dir, options["keep_description"]
                )
            except CommandError as exc:
                self.stderr.write(self.style.ERROR(f"{number}: {exc}"))
                failures.append(number)

        if failures:
            # Exit non-zero so a batch with any failed import doesn't look
            # successful to automation.
            raise CommandError(
                f"{len(failures)} import(s) failed: {', '.join(failures)}"
            )

    def _import_one(self, number, base, session, gt_dir, keep_description):
        detail = self._fetch_prod_case(number, base, session)
        slug = detail["slug"]

        prod_desc = detail.get("description") or ""
        gt_path = gt_dir / f"{number}.md"

        with transaction.atomic():
            sources_by_id = self._import_sources(detail)
            case = self._upsert_case(detail, keep_description)
            self._import_entities(case, detail.get("entities") or [])
            self._link_evidence(case, detail.get("evidence") or [], sources_by_id)
            # Only write the benchmark ground-truth file once the DB import
            # actually commits, so a failed/rolled-back import leaves no stale .md.
            transaction.on_commit(
                lambda: gt_path.write_text(prod_desc, encoding="utf-8")
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"{number}: imported {slug} "
                f"(sources={len(sources_by_id)}, "
                f"prod_desc={len(prod_desc)} chars -> {gt_path.name}, "
                f"local_desc={'kept' if keep_description else 'stripped'})"
            )
        )

    def _fetch_prod_case(self, number, base, session):
        """Find the prod case for a court number and return its detail document."""
        resp = session.get(f"{base}/cases/", params={"search": number}, timeout=60)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        target = str(number).strip().casefold()

        def _exact_number_in(text) -> bool:
            # Match the court number as a whole token, not a substring, so
            # "080-CR-0047" does NOT match "080-CR-00478" / "1080-CR-0047".
            return target in {m.casefold() for m in _COURT_RE.findall(str(text or ""))}

        match = None
        for c in results:
            refs = c.get("court_cases") or []
            if any(_exact_number_in(r) for r in refs) or _exact_number_in(
                c.get("title")
            ):
                match = c
                break
        # No fuzzy fallback: importing an arbitrary results[0] (or a partial/
        # prefix collision) would save the WRONG case's description to
        # ground_truth/<number>.md and corrupt the benchmark. Require an exact
        # court-number match in court_cases or the title.
        if match is None:
            raise CommandError(
                f"no case on prod with an exact court-number match for '{number}' "
                f"({len(results)} fuzzy search hit(s), none matched)"
            )

        slug = match["slug"]
        quoted = urllib.parse.quote(str(slug), safe="")
        detail = session.get(
            f"{base}/cases/{quoted}/", params={"fetch_sources": "true"}, timeout=60
        )
        detail.raise_for_status()
        return detail.json()

    def _import_sources(self, detail):
        """Upsert DocumentSources from the case's resolved sources. Returns map."""
        resolved = detail.get("_resolved_sources") or []
        # Fall back to the nested evidence[].source shape if _resolved_sources
        # was not returned. The source_id lives on the evidence entry, not the
        # nested source object, so copy it down.
        if not resolved:
            resolved = []
            for e in detail.get("evidence") or []:
                src = e.get("source")
                if isinstance(src, dict):
                    src = {
                        **src,
                        "source_id": src.get("source_id") or e.get("source_id"),
                    }
                    resolved.append(src)

        out = {}
        for s in resolved:
            source_id = s.get("source_id")
            if not source_id:
                continue
            source_type = s.get("source_type") or "MISC"
            pub_date = s.get("publication_date") or None
            # NEWS/media sources require a publication_date (model validation).
            # Prod sometimes lacks it; use a placeholder so the local copy saves.
            if source_type == "NEWS" and not pub_date:
                pub_date = "2024-01-01"
            obj, _ = DocumentSource.objects.update_or_create(
                source_id=source_id,
                defaults={
                    "title": s.get("title") or source_id,
                    "description": s.get("description") or "",
                    "source_type": source_type,
                    "url": s.get("urls") or s.get("url") or [],
                    "publication_date": pub_date,
                },
            )
            out[source_id] = obj
        return out

    def _upsert_case(self, detail, keep_description):
        defaults = {
            "case_type": detail.get("case_type") or "CORRUPTION",
            "state": CaseState.DRAFT,
            "title": detail.get("title") or detail["slug"],
            "slug": detail.get("slug"),
            "short_description": detail.get("short_description") or "",
            "description": (
                (detail.get("description") or "") if keep_description else ""
            ),
            "key_allegations": detail.get("key_allegations") or [],
            "timeline": detail.get("timeline") or [],
            "evidence": [],  # linked after sources exist
            "court_cases": detail.get("court_cases") or [],
            "bigo": detail.get("bigo"),
            "notes": detail.get("notes") or "",
            "missing_details": detail.get("missing_details") or None,
        }
        case, _ = Case.objects.update_or_create(
            case_id=detail["case_id"], defaults=defaults
        )
        return case

    def _import_entities(self, case, entities):
        case.entity_relationships.all().delete()
        for e in entities:
            name = (e.get("display_name") or "").strip()
            rel = (e.get("type") or "").strip()
            if not name or rel not in _REL_TYPES:
                continue
            entity, _ = JawafEntity.objects.get_or_create(display_name=name)
            CaseEntityRelationship.objects.create(
                case=case,
                entity=entity,
                relationship_type=rel,
                notes=(e.get("notes") or "")[:500],
            )

    def _link_evidence(self, case, evidence, sources_by_id):
        """Rebuild Case.evidence as [{source_id, description}] for known sources."""
        rebuilt = []
        for e in evidence:
            source_id = e.get("source_id")
            if source_id and source_id in sources_by_id:
                rebuilt.append(
                    {"source_id": source_id, "description": e.get("description") or ""}
                )
        case.evidence = rebuilt
        case.save(update_fields=["evidence"])
