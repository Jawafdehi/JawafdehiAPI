"""Normalize the five court identifiers in Case.court_cases to NGM's spelling.

cases.validators.COURT_CHOICES previously used romanizations (e.g. ``birgunjhc``)
that differ from the spellings the NGM scraper stores in
``court_cases.court_identifier`` (``birganjhc``). COURT_CHOICES now uses NGM's
spelling, so rewrite any existing case references that still use the old spelling
instead of translating at query time. This is a no-op on data that already matches
(no production case used the old spellings at the time of writing).
"""

from django.db import migrations

# old cases-app spelling -> NGM spelling (now the COURT_CHOICES value)
_RENAMES = {
    "arghakhanchidc": "argakhanchidc",
    "birgunjhc": "birganjhc",
    "ilamhc": "illamhc",
    "ramechhapdc": "ramechapdc",
    "tehrathumdc": "therathumdc",
}


def _rename_entry(entry):
    if not isinstance(entry, str) or ":" not in entry:
        return entry
    identifier, rest = entry.split(":", 1)
    new = _RENAMES.get(identifier)
    return f"{new}:{rest}" if new else entry


def normalize_court_identifiers(apps, schema_editor):
    Case = apps.get_model("cases", "Case")
    changed = 0
    for case in Case.objects.exclude(court_cases=[]).only("id", "court_cases"):
        original = case.court_cases or []
        updated = [_rename_entry(e) for e in original]
        if updated != original:
            case.court_cases = updated
            case.save(update_fields=["court_cases"])
            changed += 1
    if changed:
        print(f"  Normalized court identifiers on {changed} case(s).")


def reverse(apps, schema_editor):
    """No reverse: after the rename the new spellings are indistinguishable from
    cases always stored with NGM's spelling, so a blind reversal would corrupt
    those legitimate entries."""


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0037_case_internal_notes"),
    ]

    operations = [
        migrations.RunPython(normalize_court_identifiers, reverse_code=reverse),
    ]
