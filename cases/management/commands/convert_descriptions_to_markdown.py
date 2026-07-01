"""Convert existing TinyMCE-generated HTML descriptions and notes to Markdown."""

import logging
import re
import sys

from django.core.management.base import BaseCommand
from markdownify import markdownify as md

from cases.models import Case

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

FIELD_LABELS = {
    "description": "Case.description",
    "notes": "Case.notes",
}


def _preprocess_html(html: str) -> str:
    if not html or not html.strip():
        return html
    text = html
    # Replace &nbsp; with regular space
    text = text.replace("&nbsp;", " ")
    # Remove empty paragraph tags (including those with only whitespace/&nbsp;)
    text = re.sub(r"<p>\s*</p>", "", text, flags=re.IGNORECASE)
    # Remove empty span tags
    text = re.sub(r"<span[^>]*>\s*</span>", "", text, flags=re.IGNORECASE)
    # Strip style attributes from all tags
    text = re.sub(r'\s+style="[^"]*"', "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+style='[^']*'", "", text, flags=re.IGNORECASE)
    # Strip class attributes
    text = re.sub(r'\s+class="[^"]*"', "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+class='[^']*'", "", text, flags=re.IGNORECASE)
    return text.strip()


def _convert_html(html: str) -> str:
    if not html or not html.strip():
        return html
    preprocessed = _preprocess_html(html)
    if not preprocessed.strip():
        return ""
    # Convert to markdown with sensible defaults
    return md(
        preprocessed,
        heading_style="ATX",
        bullets="-",
        strip=["span"],
    ).strip()


def _format_diff(field_label: str, case_id: int, old_html: str, new_md: str) -> str:
    parts = [
        f"--- {field_label} (Case id={case_id})",
        f"+++ {field_label} (Case id={case_id})",
        f"-HTML ({len(old_html)} chars):",
    ]
    # Truncate HTML preview to first 500 chars
    html_preview = old_html[:500]
    if len(old_html) > 500:
        html_preview += "..."
    parts.append(html_preview)

    parts.append(f"+MARKDOWN ({len(new_md)} chars):")
    md_preview = new_md[:500]
    if len(new_md) > 500:
        md_preview += "..."
    parts.append(md_preview)

    return "\n".join(parts)


class Command(BaseCommand):
    help = "Convert existing TinyMCE-generated HTML descriptions/notes to Markdown"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without saving to database",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of cases to process",
        )
        parser.add_argument(
            "--case-id",
            type=int,
            default=None,
            help="Process only a specific case ID",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]
        case_id = options["case_id"]

        qs = Case.objects.all()
        if case_id is not None:
            qs = qs.filter(id=case_id)
        if limit is not None:
            qs = qs[:limit]

        total = qs.count()
        if total == 0:
            logger.info("No cases found to process.")
            return

        if dry_run:
            logger.info("DRY RUN — no changes will be saved.")
        else:
            logger.info("LIVE RUN — changes WILL be saved to database.")

        converted_count = 0
        skipped_count = 0

        for case in qs:
            changed = False
            for field in ("description", "notes"):
                original = getattr(case, field, None) or ""
                if not original.strip():
                    continue
                converted = _convert_html(original)
                if converted == original.strip():
                    continue
                if dry_run:
                    logger.info(
                        _format_diff(FIELD_LABELS[field], case.id, original, converted)
                    )
                else:
                    setattr(case, field, converted)
                    changed = True
                    logger.info(
                        "Converted %s for Case id=%s (%d chars -> %d chars)",
                        FIELD_LABELS[field],
                        case.id,
                        len(original),
                        len(converted),
                    )

            if changed:
                case.save(update_fields=["description", "notes", "updated_at"])
                converted_count += 1
            else:
                skipped_count += 1

        summary = (
            f"\n{'DRY RUN' if dry_run else 'LIVE RUN'} complete. "
            f"Processed {total} case(s). "
            f"Converted: {converted_count}, Skipped (no change): {skipped_count}."
        )
        logger.info(summary)
