"""Attach converted Markdown to a DocumentSource.

When a source is converted to Markdown (e.g. via likhit during a casework
review), we persist that Markdown to storage (S3) and record a ``MARKDOWN``-role
link in the source's ``url`` list, so the rendered markdown is a first-class,
durable URL on the source rather than something recomputed every review.

This is idempotent: a source that already has a MARKDOWN url is left untouched
unless ``overwrite=True``.
"""

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone

from cases.models import (
    DocumentSource,
    SourceLinkRole,
    validate_url_list,
)
from cases.services.storage_links import absolute_media_url


def source_has_markdown(source: DocumentSource) -> bool:
    """True if the source already carries a MARKDOWN-role url."""
    for item in source.url or []:
        if isinstance(item, dict) and item.get("role") == SourceLinkRole.MARKDOWN.value:
            return True
    return False


def attach_markdown(source: DocumentSource, markdown: str, *, overwrite: bool = False):
    """Persist `markdown` as an upload on `source` and add a MARKDOWN url.

    Returns a dict: {created: bool, link: <url or None>, skipped: bool}.
    No-op (skipped) when the source already has a MARKDOWN url and not overwrite.
    """
    if not (markdown or "").strip():
        return {"created": False, "link": None, "skipped": True}

    if source_has_markdown(source) and not overwrite:
        existing = next(
            (
                i["link"]
                for i in source.url
                if isinstance(i, dict)
                and i.get("role") == SourceLinkRole.MARKDOWN.value
            ),
            None,
        )
        return {"created": False, "link": existing, "skipped": True}

    # Save the markdown to storage (S3) and record its link. A source's links
    # live solely in `url`, so we do not persist a separate uploaded-file record.
    filename = f"{source.source_id}.md"
    stored_name = default_storage.save(filename, ContentFile(markdown.encode("utf-8")))
    link = absolute_media_url(default_storage.url(stored_name))

    # Append (or replace) the MARKDOWN-role url on the source.
    urls = [
        i
        for i in (source.url or [])
        if not (isinstance(i, dict) and i.get("role") == SourceLinkRole.MARKDOWN.value)
    ]
    urls.append({"link": link, "role": SourceLinkRole.MARKDOWN.value})

    # Validate just the url list we're writing, then persist ONLY that column
    # via an UPDATE. Going through source.save() would run full_clean() over the
    # whole row and reject the write for an unrelated invalid field (e.g. a
    # MEDIA_NEWS source missing publication_date), failing the maintenance fix
    # on otherwise-valid sources.
    validate_url_list(urls)
    DocumentSource.objects.filter(pk=source.pk).update(
        url=urls, updated_at=timezone.now()
    )
    source.url = urls

    return {"created": True, "link": link, "skipped": False}
