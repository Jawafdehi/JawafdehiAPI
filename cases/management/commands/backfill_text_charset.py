"""Backfill ``charset=utf-8`` on text objects already stored in the bucket.

Markdown transcripts (and other ``text/*`` uploads) written before the storage
backend started appending an explicit charset carry a bare ``text/markdown``
Content-Type. Browsers opening such a response with no charset fall back to a
legacy encoding (Latin-1), so UTF-8 content (e.g. Devanagari) renders as
mojibake. This command rewrites the Content-Type metadata of those objects
in-place to ``<type>; charset=utf-8`` via an S3 copy with
``MetadataDirective=REPLACE``.

The object bytes are NOT modified — only the Content-Type metadata.

Read-only by default (lists what WOULD change). Pass ``--apply`` to perform the
metadata rewrite.
"""

from __future__ import annotations

import mimetypes

from django.core.files.storage import storages
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Rewrite Content-Type of existing text/* bucket objects to include "
        "charset=utf-8 (dry-run unless --apply)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Perform the metadata rewrite. Without this flag, only lists "
            "objects that would change.",
        )
        parser.add_argument(
            "--prefix",
            default=None,
            help="Limit to keys under this prefix (defaults to the storage's "
            "configured file prefix, e.g. 'case_uploads/').",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        storage = storages["default"]

        # Only meaningful for the S3/R2 backend; the local FileSystemStorage has
        # no Content-Type metadata to rewrite.
        connection = getattr(storage, "connection", None)
        bucket_name = getattr(storage, "bucket_name", None)
        if connection is None or not bucket_name:
            raise CommandError(
                "Default storage is not an S3/R2 backend; nothing to backfill. "
                "(Are AWS credentials configured?)"
            )

        client = connection.meta.client
        prefix = options["prefix"]
        if prefix is None:
            prefix = getattr(storage, "file_prefix", "") or ""

        self.stdout.write(
            f"Scanning bucket '{bucket_name}' prefix '{prefix}' "
            f"({'APPLY' if apply else 'dry-run'})…"
        )

        paginator = client.get_paginator("list_objects_v2")
        scanned = 0
        changed = 0
        skipped = 0

        for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                scanned += 1

                head = client.head_object(Bucket=bucket_name, Key=key)
                current = head.get("ContentType")

                target = self._target_content_type(key, current)
                if target is None:
                    skipped += 1
                    continue

                changed += 1
                self.stdout.write(f"  {key}: '{current}' -> '{target}'")

                if apply:
                    extra = {
                        "ContentType": target,
                        "MetadataDirective": "REPLACE",
                    }
                    # Preserve any user metadata on the object.
                    if head.get("Metadata"):
                        extra["Metadata"] = head["Metadata"]
                    client.copy_object(
                        Bucket=bucket_name,
                        Key=key,
                        CopySource={"Bucket": bucket_name, "Key": key},
                        **extra,
                    )

        verb = "Rewrote" if apply else "Would rewrite"
        self.stdout.write(
            self.style.SUCCESS(
                f"Scanned {scanned}, {verb} {changed}, skipped {skipped}."
            )
        )
        if not apply and changed:
            self.stdout.write("Re-run with --apply to perform the rewrite.")

    @staticmethod
    def _target_content_type(key: str, current: str | None) -> str | None:
        """Return the corrected Content-Type, or None if no change is needed.

        Only text/* objects that lack a charset are rewritten. The base type is
        taken from the current header when present, else guessed from the key.
        """
        content_type = current
        if not content_type:
            content_type, _encoding = mimetypes.guess_type(key)

        if (
            content_type
            and content_type.startswith("text/")
            and "charset=" not in content_type.lower()
        ):
            return f"{content_type}; charset=utf-8"
        return None
