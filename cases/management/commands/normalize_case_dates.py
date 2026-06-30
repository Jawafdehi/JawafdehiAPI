"""Normalize authored Bikram-Sambat dates in a case `description` to the
`वि सं YYYY-MM-DD` Devanagari standard (the `case_overview_date_format` rule).

Conservative + idempotent: only rewrites unmarked 4-digit `YYYY[sep]MM[sep]DD`
tokens whose year is in the BS range (2000–2099), in Devanagari or Latin numerals,
with `/`, `।` or `-` separators. It adds the `वि सं ` era marker, converts the
numerals to Devanagari, and normalises separators to `-`. Dates already carrying
an era marker (`...सं`/`सन्`), out-of-range years, and impossible month/day values
are left untouched (so source-quoted dates and re-runs are safe).

Usage: `manage.py normalize_case_dates <court_ref|slug> [...]`
  e.g. `manage.py normalize_case_dates special:080-CR-0007 special:080-CR-0014`
"""

import re

from django.core.management.base import BaseCommand

from cases.models import Case

_DEVA = "०१२३४५६७८९"
_TO_DEVA = str.maketrans("0123456789", _DEVA)
_TO_ASC = str.maketrans(_DEVA, "0123456789")
_DATE = re.compile(
    r"([०-९0-9]{4})\s*[/।\-]\s*([०-९0-9]{1,2})\s*[/।\-]\s*([०-९0-9]{1,2})"
)


def normalize_dates(text):
    """Return (normalized_text, n_changes)."""
    if not text:
        return text, 0
    count = [0]

    def repl(m):
        y, mo, d = m.groups()
        year = int(y.translate(_TO_ASC))
        if not (2000 <= year <= 2099):  # BS range for these cases (no AD overlap)
            return m.group(0)
        month, day = int(mo.translate(_TO_ASC)), int(d.translate(_TO_ASC))
        if month > 12 or day > 32:
            return m.group(0)
        pre = text[max(0, m.start() - 10) : m.start()]
        if "सं" in pre or "सन" in pre:  # already era-marked (idempotent)
            return m.group(0)
        count[0] += 1
        ymd = f"{year}-{month:02d}-{day:02d}".translate(_TO_DEVA)
        return f"वि सं {ymd}"

    return _DATE.sub(repl, text), count[0]


class Command(BaseCommand):
    help = (
        "Normalize authored BS dates in case description(s) to वि सं Devanagari form."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "targets", nargs="+", help="court refs (special:...) or slugs"
        )

    def handle(self, *args, **options):
        total = 0
        for target in options["targets"]:
            if ":" in target:
                qs = Case.objects.filter(court_cases__contains=[target])
            else:
                qs = Case.objects.filter(slug=target)
            for case in qs:
                new, n = normalize_dates(case.description or "")
                if n and new != case.description:
                    Case.objects.filter(pk=case.pk).update(description=new)
                    total += n
                    self.stdout.write(
                        self.style.SUCCESS(f"{case.slug}: normalized {n} dates")
                    )
                else:
                    self.stdout.write(f"{case.slug}: no change")
        self.stdout.write(f"done — {total} dates normalized")
