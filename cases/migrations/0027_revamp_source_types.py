"""Revamp DocumentSource.source_type taxonomy + fix markdown link roles.

Two data operations, both deterministic and idempotent:

1. **Re-classify source_type** for every non-deleted source using the shared
   ``cases.services.source_classifier``. The old taxonomy (OFFICIAL_GOVERNMENT,
   LEGAL_PROCEDURAL, MEDIA_NEWS, ...) conflated several document kinds; the new
   one (CIAA_PRESS_RELEASE, AG_ABHIYOG_PATRA, COURT_ORDER, NEWS, MISC, ...)
   splits them. The classifier reads the title/description/urls and uses the
   prior label only as a last-resort hint, so no information is lost.

2. **Fix markdown link roles**: any source-link whose URL path ends in ``.md``
   is a converted-markdown artifact and is re-roled to MARKDOWN. Several
   pre-date ``source_markdown.attach_markdown`` and were stored as RAW/PERMALINK,
   which makes the review poller think the source has no markdown and append a
   duplicate. Re-roling them is safe and stops the duplication.

The classifier returns current ``SourceType`` *values* (plain strings), which is
exactly what is written to the column, so this migration stays correct even as
the enum class evolves.
"""

from __future__ import annotations

import urllib.parse

from django.db import migrations

# Role value mirrored from cases.models.SourceLinkRole.MARKDOWN. Hardcoded
# (not imported) so this historical migration is insulated from later edits to
# the enum.
_MARKDOWN_ROLE = "MARKDOWN"


def _links(url_field):
    """Yield (index, link_str) for each source-link dict in a url JSON list."""
    if not isinstance(url_field, list):
        return
    for idx, item in enumerate(url_field):
        if isinstance(item, dict):
            link = item.get("link")
            if isinstance(link, str) and link:
                yield idx, link


def _is_markdown_link(link: str) -> bool:
    return urllib.parse.urlparse(link).path.lower().endswith(".md")


def reclassify_and_fix_roles(apps, schema_editor):
    from cases.services.source_classifier import classify_source_type

    DocumentSource = apps.get_model("cases", "DocumentSource")

    type_updates: dict[str, list[int]] = {}
    role_fixed = 0

    sources = DocumentSource.objects.filter(is_deleted=False).iterator()
    for source in sources:
        links = [link for _, link in _links(source.url)]

        # 1. Re-classify source_type.
        new_type = classify_source_type(
            source.title,
            source.description,
            links,
            prior_type=source.source_type,
        )
        if str(new_type) != (source.source_type or ""):
            type_updates.setdefault(str(new_type), []).append(source.pk)

        # 2. Fix markdown link roles in-place. Persist via .update() (not
        # .save()) so the write never triggers full row validation — a NEWS
        # source missing publication_date would otherwise reject this
        # url-only maintenance write.
        changed = False
        if isinstance(source.url, list):
            for idx, link in _links(source.url):
                if (
                    _is_markdown_link(link)
                    and source.url[idx].get("role") != _MARKDOWN_ROLE
                ):
                    source.url[idx]["role"] = _MARKDOWN_ROLE
                    changed = True
        if changed:
            DocumentSource.objects.filter(pk=source.pk).update(url=source.url)
            role_fixed += 1

    for new_type, pks in type_updates.items():
        DocumentSource.objects.filter(pk__in=pks).update(source_type=new_type)

    retyped = sum(len(pks) for pks in type_updates.values())
    print(
        f"  revamp_source_types: re-typed {retyped} source(s), "
        f"fixed markdown role on {role_fixed} source(s)."
    )


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0026_merge_20260611_0704"),
    ]

    operations = [
        # Irreversible: the old taxonomy can't be reconstructed from the new
        # labels. Re-running the forward op is safe (idempotent), so reverse is
        # a no-op rather than an error.
        migrations.RunPython(
            reclassify_and_fix_roles,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
