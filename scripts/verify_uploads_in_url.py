"""READ-ONLY: verify every uploaded file's URL is already present in its source's
stored `url` column, so dropping the DocumentSourceUpload table / uploaded_file
fields loses nothing referenced by `url`.

Run against the target DB (e.g. prod), read-only — issues only SELECTs:

    python manage.py shell < scripts/verify_uploads_in_url.py

Comparison is by storage PATH (e.g. ``case_uploads/<hash>.pdf``), not full URL,
so it is robust to domain/scheme differences between the FileField's
``.url`` and the absolute link stored in ``url``.
"""

from cases.models import DocumentSource, DocumentSourceUpload


def stored_links(src):
    return [u for u in src.url_links if u]


def file_path(file_field):
    """The storage key/path of a FileField, or None if empty/broken."""
    try:
        return file_field.name or None
    except (ValueError, AttributeError):
        # No file associated / broken descriptor — treat as nothing to record.
        return None


def covered(path, links):
    """True if some stored link contains this storage path (the hashed name)."""
    if not path:
        return False
    # match on the path or just its basename (hash+ext), whichever is present
    base = path.rsplit("/", 1)[-1]
    return any((path in link) or (base in link) for link in links)


missing = []  # (source_id, which, path) where the file URL is NOT in stored url
total_uploads = 0

# 1) DocumentSourceUpload relation rows
for up in DocumentSourceUpload.objects.select_related("source").iterator():
    total_uploads += 1
    path = file_path(up.file)
    links = stored_links(up.source)
    if not covered(path, links):
        missing.append((up.source.source_id, f"upload#{up.pk}", path))

# 2) legacy single uploaded_file field
legacy_qs = DocumentSource.objects.exclude(uploaded_file="").exclude(
    uploaded_file__isnull=True
)
total_legacy = 0
for src in legacy_qs.iterator():
    total_legacy += 1
    path = file_path(src.uploaded_file)
    if not covered(path, stored_links(src)):
        missing.append((src.source_id, "uploaded_file", path))

print("=" * 70)
print("UPLOAD-COVERAGE CHECK (read-only)")
print("=" * 70)
print(f"DocumentSourceUpload rows scanned : {total_uploads}")
print(f"legacy uploaded_file sources      : {total_legacy}")
print(f"file URLs NOT found in stored url  : {len(missing)}")
if missing:
    print("\n-- NOT covered (would be lost if table dropped) --")
    for sid, which, path in missing[:100]:
        print(f"  {sid}  {which}  {path}")
    if len(missing) > 100:
        print(f"  ... and {len(missing) - 100} more")
    print("\n=> DO NOT drop yet: backfill these into `url` first.")
else:
    print("\n=> SAFE: every uploaded file's URL is already in stored `url`.")
