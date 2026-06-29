"""Idempotent one-off prod data fixes for priority cases (review follow-ups).

- BIGO_CONVENTION markers in ``internal_notes`` for cases where the recorded
  ``bigo`` deliberately follows a documented convention (court-upheld figure vs
  the higher CIAA-alleged loss the court rejected; lead-defendant figure vs the
  scheme total). The amended ``review.bigo_matches_press_release`` rule reads
  this marker so it does not flag the figure as a mismatch.
- A descriptive, neutral public ``slug`` for the National Payment Gateway
  procurement case, re-slugged off a defendant who was acquitted.

Safe to re-run: each fix is applied only when not already present.
"""

from django.core.management.base import BaseCommand

from cases.models import Case

# court_case ref (drift-proof) -> BIGO_CONVENTION line to ensure in internal_notes
BIGO_CONVENTION = {
    "special:080-CR-0173": (
        "BIGO_CONVENTION: court-upheld figure (Rs 75 lakh); "
        "CIAA-claimed Rs 24 cr was not sustained."
    ),
    "special:080-CR-0061": (
        "BIGO_CONVENTION: lead-defendant figure per press release (Rs 20,931,647)."
    ),
}

# The reviewer-proposed full slug `case-national-payment-gateway-procurement-allegations`
# is 53 chars; Case.slug is varchar(50), so it is trimmed.
SLUGS = {
    "special:080-CR-0048": "case-national-payment-gateway-procurement",
}


class Command(BaseCommand):
    help = "Idempotently apply priority-case BIGO_CONVENTION notes + slug fixes."

    def _case(self, ref):
        return Case.objects.get(court_cases__contains=[ref])

    def handle(self, *args, **options):
        for ref, line in BIGO_CONVENTION.items():
            case = self._case(ref)
            notes = case.internal_notes or ""
            if "BIGO_CONVENTION" in notes:
                self.stdout.write(f"{ref}: BIGO_CONVENTION already present — skip")
                continue
            new = (notes.rstrip() + "\n" + line).strip() if notes.strip() else line
            Case.objects.filter(pk=case.pk).update(internal_notes=new)
            self.stdout.write(self.style.SUCCESS(f"{ref}: appended BIGO_CONVENTION"))

        for ref, slug in SLUGS.items():
            case = self._case(ref)
            if case.slug == slug:
                self.stdout.write(f"{ref}: slug already {slug} — skip")
                continue
            Case.objects.filter(pk=case.pk).update(slug=slug)
            self.stdout.write(self.style.SUCCESS(f"{ref}: slug -> {slug}"))
