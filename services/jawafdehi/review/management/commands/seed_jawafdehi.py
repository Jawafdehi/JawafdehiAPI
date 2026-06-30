"""Seed the LOCAL database from a remote Jawafdehi (JDS) server.

Per the VOL-3 directive: "add in a CLI to seed local database from a remote
jawafdehi server (include both sources, cases, jawaf entities), after which
we'll work with everything locally."

This command pulls from the remote JDS public API (configured via
settings.JAWAFDEHI_API_BASE + JAWAFDEHI_API_TOKEN, overridable with flags) and
upserts three kinds of records into THIS database:

  * cases.DocumentSource           (sources)
  * cases.Case (+ CaseEntityRelationship)  (cases and their NES-entity binds)

After seeding, the review system's local case provider
(review.case_provider, REVIEW_CASE_SOURCE="local") can score cases entirely
offline. Source *artifacts* (PDFs) are still fetched from their public URLs
lazily at conversion time — they are large and not stored in the DB.

Examples:
  # Seed everything (all cases + every source they reference + entities)
  python manage.py seed_jawafdehi

  # Seed only PUBLISHED cases (the quality bar), limited to 25
  python manage.py seed_jawafdehi --state PUBLISHED --limit 25

  # Seed one specific case by slug (and just the sources/entities it needs)
  python manage.py seed_jawafdehi --slug case-080-cr-0111-080-cr-0111-c65a35

  # Point at a different server / token
  python manage.py seed_jawafdehi --api-base https://portal.jawafdehi.org/api \
      --token $JAWAFDEHI_API_TOKEN
"""

import datetime as _dt

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from jawafdehi_shared.entities.ids import is_valid_entity_iri

from cases.models import (
    Case,
    CaseEntityRelationship,
    DocumentSource,
    RelationshipType,
)
from review import jds_client

# Map the JDS entity "type" string onto a CaseEntityRelationship.relationship_type.
_REL_BY_TYPE = {c.value: c.value for c in RelationshipType}


def _parse_date(value):
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class Command(BaseCommand):
    help = "Seed the local DB with cases, sources and jawaf entities from a remote Jawafdehi server."

    def add_arguments(self, parser):
        parser.add_argument(
            "--slug",
            action="append",
            default=[],
            help="Seed only these case slug(s). Repeatable. Default: all cases.",
        )
        parser.add_argument(
            "--state",
            default=None,
            help="Only seed cases in this state (e.g. PUBLISHED).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Max number of cases to seed.",
        )
        parser.add_argument(
            "--api-base",
            default=None,
            help="Remote JDS API base (overrides settings.JAWAFDEHI_API_BASE).",
        )
        parser.add_argument(
            "--token",
            default=None,
            help=(
                "DEPRECATED: the JDS API is OIDC-only; jds_client now "
                "authenticates with a Zitadel client-credentials bearer "
                "(CASEWORK_OIDC_CLIENT_ID/SECRET). This static token is ignored."
            ),
        )
        parser.add_argument(
            "--sources-only",
            action="store_true",
            help="Only seed the standalone sources catalog (no cases/entities).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch and report counts without writing to the database.",
        )

    def handle(self, *args, **opts):
        # Allow per-invocation overrides of the remote without editing settings.
        if opts.get("api_base"):
            settings.JAWAFDEHI_API_BASE = opts["api_base"].rstrip("/")
        if opts.get("token"):
            # The JDS API is OIDC-only now; jds_client authenticates with a
            # Zitadel client-credentials bearer. Keep the setting assignment for
            # any other reader, but warn that it no longer drives auth.
            settings.JAWAFDEHI_API_TOKEN = opts["token"]
            self.stdout.write(
                self.style.WARNING(
                    "--token is deprecated and no longer used for JDS auth "
                    "(OIDC-only); set CASEWORK_OIDC_CLIENT_ID/SECRET instead."
                )
            )

        base = getattr(settings, "JAWAFDEHI_API_BASE", "")
        self.stdout.write(self.style.MIGRATE_HEADING(f"Seeding from {base}"))
        self.dry = opts["dry_run"]
        if self.dry:
            self.stdout.write(self.style.WARNING("DRY RUN — no DB writes."))

        self._counts = {
            "entities": 0,
            "sources": 0,
            "cases": 0,
            "relationships": 0,
        }

        try:
            if opts["sources_only"]:
                self._seed_sources_catalog()
            else:
                self._seed_cases(opts)
        except jds_client.JdsError as e:
            raise CommandError(str(e))

        c = self._counts
        self.stdout.write(
            self.style.SUCCESS(
                "Done. Upserted "
                f"{c['cases']} cases, {c['sources']} sources, "
                f"{c['entities']} entities, {c['relationships']} relationships."
            )
        )

    # ---------------- cases (+ their sources + entities) ----------------

    def _seed_cases(self, opts):
        slugs = opts["slug"]
        if slugs:
            cases = []
            for slug in slugs:
                self.stdout.write(f"  fetching case {slug} ...")
                cases.append(jds_client.get_case(slug))
        else:
            params = {}
            if opts["state"]:
                params["state"] = opts["state"]
            # The LIST endpoint (cases/) returns lightweight rows whose
            # `evidence[]` has NO nested source.url. We MUST fetch each case's
            # DETAIL (cases/{slug}/) to get full evidence + source URLs, or
            # every seeded source ends up with an empty url and conversion is
            # skipped. So collect slugs from the list, then fetch detail.
            list_rows = []
            for c in jds_client.iter_paginated("cases/", params=params):
                list_rows.append(c)
                if opts["limit"] and len(list_rows) >= opts["limit"]:
                    break
            cases = []
            for idx, row in enumerate(list_rows, 1):
                slug = row.get("slug")
                if not slug:
                    cases.append(row)
                    continue
                try:
                    cases.append(jds_client.get_case(slug))
                except jds_client.JdsError as e:
                    self.stdout.write(
                        self.style.WARNING(
                            f"  [{idx}/{len(list_rows)}] detail fetch failed for {slug}: {e}; using list row."
                        )
                    )
                    cases.append(row)
                if idx % 25 == 0:
                    self.stdout.write(f"  fetched detail {idx}/{len(list_rows)} ...")

        self.stdout.write(f"  {len(cases)} case(s) to seed.")

        # Collect the source_ids these cases reference so we can pull source
        # detail (we already have nested `source` blocks in evidence, but we
        # also upsert the standalone DocumentSource rows for completeness).
        for case in cases:
            self._upsert_case(case)

    @transaction.atomic
    def _upsert_case(self, case):
        slug = case.get("slug")
        if self.dry:
            self._counts["cases"] += 1
            # still walk sources/entities to report counts
            self._upsert_sources_from_evidence(case)
            self._note_entities(case)
            return

        defaults = {
            "case_type": case.get("case_type") or "CORRUPTION",
            "state": case.get("state") or "DRAFT",
            "title": case.get("title") or slug,
            "short_description": case.get("short_description") or "",
            "thumbnail_url": case.get("thumbnail_url") or "",
            "banner_url": case.get("banner_url") or "",
            "case_start_date": _parse_date(case.get("case_start_date")),
            "case_end_date": _parse_date(case.get("case_end_date")),
            "tags": case.get("tags") or [],
            "description": case.get("description") or "",
            "key_allegations": case.get("key_allegations") or [],
            "timeline": case.get("timeline") or [],
            "evidence": case.get("evidence") or [],
            "notes": case.get("notes") or "",
            "court_cases": case.get("court_cases") or [],
            "missing_details": case.get("missing_details") or "",
            "bigo": case.get("bigo"),
            "versionInfo": case.get("versionInfo") or {},
        }

        # Upsert by slug (the stable identifier). Bypass the model's
        # slug-immutability and full_clean by updating fields directly via
        # queryset when it exists.
        existing = Case.objects.filter(slug=slug).first() if slug else None

        if existing is None:
            obj = Case(slug=slug or None, **defaults)
            # Keep the remote slug; only auto-generate if remote had none.
            obj.save()
        else:
            for k, v in defaults.items():
                setattr(existing, k, v)
            # allow keeping the remote slug if present
            if slug:
                existing._original_slug = slug
                existing.slug = slug
            existing.save()
            obj = existing

        self._counts["cases"] += 1

        # Upsert the standalone source rows this case references.
        self._upsert_sources_from_evidence(case)

        # Upsert entities + relationships.
        self._upsert_entities_for_case(obj, case)

    def _upsert_sources_from_evidence(self, case):
        for ev in case.get("evidence") or []:
            src = ev.get("source") or {}
            sid = ev.get("source_id")
            if not sid:
                continue
            if self.dry:
                self._counts["sources"] += 1
                continue
            source_type = src.get("source_type") or None
            pub_date = _parse_date(src.get("publication_date"))
            # The DocumentSource model REQUIRES publication_date for MEDIA_NEWS
            # sources. Some production rows have a null date; fall back to the
            # case start date (else today) so the seed doesn't blow up.
            if source_type == "MEDIA_NEWS" and pub_date is None:
                pub_date = _parse_date(case.get("case_start_date")) or _dt.date.today()
            try:
                DocumentSource.objects.update_or_create(
                    source_id=sid,
                    defaults={
                        "title": (src.get("title") or sid)[:300],
                        "description": src.get("description") or "",
                        "source_type": source_type,
                        "url": src.get("url") or [],
                        "publication_date": pub_date,
                        "is_deleted": False,
                    },
                )
                self._counts["sources"] += 1
            except (
                Exception
            ) as e:  # noqa: BLE001 - never let one bad source kill the seed
                self.stdout.write(self.style.WARNING(f"    skip source {sid}: {e}"))

    def _note_entities(self, case):
        self._counts["entities"] += len(case.get("entities") or [])

    def _upsert_entities_for_case(self, case_obj, case):
        for ent in case.get("entities") or []:
            nes_id = (ent.get("nes_id") or "").strip()
            # NES owns entities; a bind requires a valid canonical NES id. There
            # is no display-name fallback, so entries without a valid nes_id are
            # skipped (display details are resolved from NES at read time).
            if not is_valid_entity_iri(nes_id):
                self._counts["entities_skipped"] = (
                    self._counts.get("entities_skipped", 0) + 1
                )
                continue
            self._counts["entities"] += 1

            rel_type = _REL_BY_TYPE.get(
                (ent.get("type") or "").lower(), RelationshipType.RELATED.value
            )
            _, created = CaseEntityRelationship.objects.get_or_create(
                case=case_obj,
                nes_id=nes_id,
                relationship_type=rel_type,
                defaults={"notes": (ent.get("notes") or "")[:500]},
            )
            if created:
                self._counts["relationships"] += 1

    # ---------------- standalone sources catalog ----------------

    def _seed_sources_catalog(self):
        for src in jds_client.iter_paginated("sources/"):
            sid = src.get("source_id")
            if not sid:
                continue
            if self.dry:
                self._counts["sources"] += 1
                continue
            DocumentSource.objects.update_or_create(
                source_id=sid,
                defaults={
                    "title": (src.get("title") or sid)[:300],
                    "description": src.get("description") or "",
                    "source_type": src.get("source_type") or None,
                    "url": src.get("url") or [],
                    "publication_date": _parse_date(src.get("publication_date")),
                    "is_deleted": False,
                },
            )
            self._counts["sources"] += 1
