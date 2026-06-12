"""Backfill uploaded-file URLs into each source's ``url`` list.

A DocumentSource's links live solely in its ``url`` JSON list. Historically some
files were attached via the ``uploaded_file`` FileField or ``DocumentSourceUpload``
rows and surfaced only at read time by the serializer. Before that machinery is
removed (a later PR), this command ensures every such file's URL is recorded in
``url`` so nothing is lost.

Idempotent: a file whose URL is already present in ``url`` (matched by storage
path / hashed basename) is skipped. ``--dry-run`` (default) reports without
writing; pass ``--apply`` to persist.

    python manage.py backfill_upload_links_into_url            # dry run
    python manage.py backfill_upload_links_into_url --apply    # write
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from cases.models import (
    DocumentSource,
    DocumentSourceUpload,
    SourceLinkRole,
    validate_url_list,
)
from cases.services.storage_links import absolute_media_url


def _covered(path, links):
    """True if some stored link already points at this file.

    Match on the file's basename (a sha256 hash + extension under the storage
    prefix, e.g. ``case_uploads/<hash>.pdf``) as a complete final path segment of
    a stored link, rather than a loose substring, so e.g. ``<hash>.pdf`` does not
    spuriously match ``<hash>_copy.pdf``.
    """
    if not path:
        return True  # nothing to record
    base = path.rsplit("/", 1)[-1]
    return any(link.rsplit("/", 1)[-1] == base for link in links)


def _role_for(name):
    return (
        SourceLinkRole.MARKDOWN.value
        if (name or "").lower().endswith(".md")
        else SourceLinkRole.RAW.value
    )


class Command(BaseCommand):
    help = "Backfill uploaded-file URLs into DocumentSource.url (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist changes (default is a dry run).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        prefix = "" if apply else "[DRY RUN] "

        # source_id -> list of {link, role} to append
        to_append = {}

        def consider(source, file_field):
            if not file_field:
                return
            try:
                name = file_field.name
                url = absolute_media_url(file_field.url)
            except (ValueError, AttributeError):
                return
            links = [u for u in source.url_links if u]
            pending = [d["link"] for d in to_append.get(source.source_id, [])]
            if _covered(name, links + pending):
                return
            to_append.setdefault(source.source_id, []).append(
                {"link": url, "role": _role_for(name)}
            )

        # 1) DocumentSourceUpload relation rows
        for up in DocumentSourceUpload.objects.select_related("source").iterator():
            consider(up.source, up.file)

        # 2) legacy single uploaded_file field
        legacy = DocumentSource.objects.exclude(uploaded_file="").exclude(
            uploaded_file__isnull=True
        )
        for src in legacy.iterator():
            consider(src, src.uploaded_file)

        self.stdout.write(
            self.style.WARNING(
                f"{prefix}{len(to_append)} source(s) need upload links backfilled."
            )
        )
        for sid, links in list(to_append.items())[:50]:
            for d in links:
                self.stdout.write(f"  {sid}  +{d['role']}  {d['link']}")
        if len(to_append) > 50:
            self.stdout.write(f"  ... and {len(to_append) - 50} more")

        if not apply or not to_append:
            self.stdout.write(self.style.SUCCESS(f"{prefix}done (no writes)."))
            return

        updated = 0
        for sid, new_links in to_append.items():
            with transaction.atomic():
                src = DocumentSource.objects.select_for_update().get(source_id=sid)
                existing = [u for u in src.url_links if u]
                # Normalize first: legacy rows may hold plain string URLs, which
                # validate_url_list rejects. normalize_url_list coerces them to
                # {link, role: RAW} so the validate below passes.
                merged = DocumentSource.normalize_url_list(list(src.url or []))
                for d in new_links:
                    if not _covered(d["link"], existing):
                        merged.append(d)
                # persist only the url column (avoid full_clean on unrelated fields)
                validate_url_list(merged)
                DocumentSource.objects.filter(pk=src.pk).update(
                    url=merged, updated_at=timezone.now()
                )
                updated += 1

        self.stdout.write(self.style.SUCCESS(f"Backfilled {updated} source(s)."))
