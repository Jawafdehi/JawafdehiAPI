import hashlib
import re

from django.db import migrations, models
from django.utils.text import slugify

import cases.validators


def _generate_slug(case_id, title, court_cases):
    """Generate a unique slug from case data (mirrors Case._generate_unique_slug)."""
    parts = []

    if court_cases and isinstance(court_cases, list):
        for cc in court_cases:
            if ":" in cc:
                _, case_no = cc.split(":", 1)
                if case_no:
                    parts.append(slugify(case_no))
                    break

    if not parts and title:
        cr_match = re.search(r"(\d{3}-CR-\d{4})", title)
        if cr_match:
            parts.append(slugify(cr_match.group(1)))

    if title:
        parts.append(slugify(title)[:30])

    base = "-".join(p for p in parts if p)
    if not base:
        base = slugify(case_id) or "case"

    if base and not base[0].isalpha():
        base = f"case-{base}"

    stable_suffix = hashlib.md5(case_id.encode()).hexdigest()[:6]
    slug = f"{base}-{stable_suffix}"
    return slug[:50]


def backfill_null_slugs(apps, schema_editor):
    """Generate slugs for all cases that have a NULL slug."""
    Case = apps.get_model("cases", "Case")
    null_slug_cases = Case.objects.filter(slug__isnull=True)
    count = 0
    for case in null_slug_cases:
        case.slug = _generate_slug(case.case_id, case.title, case.court_cases)
        case.save(update_fields=["slug"])
        count += 1
    if count:
        print(f"  Generated slugs for {count} case(s) with NULL slug.")


def reverse_backfill(apps, schema_editor):
    """No reverse — slugs may have been auto-generated and are now required."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0020_remove_promises_case_type"),
    ]

    operations = [
        migrations.RunPython(
            backfill_null_slugs,
            reverse_code=reverse_backfill,
        ),
        migrations.AlterField(
            model_name="case",
            name="slug",
            field=models.SlugField(
                blank=True,
                null=False,
                unique=True,
                max_length=50,
                validators=[cases.validators.validate_slug],
                help_text="A slug will go in the URL (e.g., jawafdehi.org/case/YOUR-SLUG). For CIAA corruption cases, you can prepend the special court case number (e.g., case-078-WC-0123-sunil-poudel). Must start with a letter and contain only letters, numbers, and hyphens (max 50 characters). Immutable once set, auto-generated on save if not provided.",
            ),
        ),
    ]
