"""Object keys for uploads must derive from the BYTES, not the filename.

``HashedFilenameS3Boto3Storage`` keys an object on ``sha256(salt + filename
stem)``, so two different files sharing a base name land on one R2 key. Nothing
sets ``file_overwrite`` (django-storages defaults it True) and
``get_available_name`` is overridden to never suffix, so the second write
silently destroys the first — and R2 has no object versioning to recover from.

Two live consequences motivated these tests:

* ``materials.conversion`` named every transcript ``material.md``, so every
  converted material's MARKDOWN link resolved to ONE object. Six materials were
  observed sharing a single ``.md`` URL in production on 2026-09-30.
* A bulk ingest (the Auditor General corpus) carries upstream filenames that
  repeat: 2 of its 227 national-level documents collide, and 453 of the full
  6,234 do.

``store_file_as_link(..., content_hash=...)`` is the fix: the caller passes the
SHA-256 it already computed and the key becomes a function of the content.
"""

import hashlib
from unittest.mock import patch

import pytest
from django.core.files.base import ContentFile

from jawafdehi_shared.storage import (
    HashedFilenameS3Boto3Storage,
    store_file_as_link,
)


class RecordingStorage:
    """A ``default_storage`` stand-in that records the name it is asked to save.

    The unit under test is which NAME ``store_file_as_link`` hands to storage;
    the backend's own hashing is exercised separately below against the real
    ``HashedFilenameS3Boto3Storage``.
    """

    def __init__(self):
        self.saved = []

    def save(self, name, content, max_length=None):
        self.saved.append(name)
        return name

    def url(self, name):
        return f"https://s3.example.org/{name}"


@pytest.fixture
def storage():
    recording = RecordingStorage()
    with patch("jawafdehi_shared.storage.default_storage", recording):
        yield recording


def _upload(data: bytes, name: str) -> ContentFile:
    return ContentFile(data, name=name)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- the name store_file_as_link chooses -------------------------------------


def test_without_a_content_hash_the_client_filename_is_still_used(storage):
    """Unchanged default: existing callers keep their current object keys."""
    store_file_as_link(_upload(b"%PDF-1.4", "ujuri.pdf"))

    assert storage.saved == ["ujuri.pdf"]


def test_a_content_hash_replaces_the_filename(storage):
    data = b"%PDF-1.4 charge sheet"
    store_file_as_link(_upload(data, "अभियोगपत्र.pdf"), content_hash=_sha(data))

    assert storage.saved == [f"{_sha(data)}.pdf"]


def test_two_different_files_sharing_a_name_get_different_keys(storage):
    """The regression. ``अन्नपूर्ण गाउँपालिका.pdf`` names four distinct audit
    reports in the Auditor General corpus; before this they were one object."""
    first, second = b"2078 audit", b"2079 audit"
    name = "अन्नपूर्ण गाउँपालिका.pdf"

    store_file_as_link(_upload(first, name), content_hash=_sha(first))
    store_file_as_link(_upload(second, name), content_hash=_sha(second))

    assert storage.saved[0] != storage.saved[1]


def test_the_same_bytes_under_different_names_get_one_key(storage):
    """Content addressing cuts the other way too: re-running a bulk ingest must
    be a no-op rather than a second copy."""
    data = b"identical bytes"

    store_file_as_link(_upload(data, "first-download.pdf"), content_hash=_sha(data))
    store_file_as_link(_upload(data, "renamed-copy.pdf"), content_hash=_sha(data))

    assert storage.saved[0] == storage.saved[1]


def test_the_extension_is_preserved_and_normalized(storage):
    """Extension survives (the Content-Type map keys on it), lowercased so the
    ``.PDF``/``.pdf`` mix in the upstream corpus cannot split one object in two."""
    data = b"%PDF-1.4"
    store_file_as_link(_upload(data, "REPORT.PDF"), content_hash=_sha(data))

    assert storage.saved == [f"{_sha(data)}.pdf"]


def test_a_file_with_no_extension_keys_on_the_bare_hash(storage):
    data = b"no extension here"
    store_file_as_link(_upload(data, "README"), content_hash=_sha(data))

    assert storage.saved == [_sha(data)]


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "not-a-hash",
        "",
        "ABC123",  # upper-case hex: not what hexdigest() emits
        "a" * 63,  # too short
        "a" * 65,  # too long
    ],
)
def test_a_content_hash_that_is_not_a_sha256_is_rejected(storage, bad):
    """The value lands in a filename. In production the backend re-hashes it, but
    under FileSystemStorage (dev, tests) it is used as-is — so the contract is
    enforced here rather than trusted."""
    with pytest.raises(ValueError, match="content_hash"):
        store_file_as_link(_upload(b"x", "x.pdf"), content_hash=bad)

    assert storage.saved == []


def test_the_returned_link_and_role_are_unchanged(storage):
    data = b"%PDF-1.4"
    result = store_file_as_link(
        _upload(data, "x.pdf"), role="MARKDOWN", content_hash=_sha(data)
    )

    assert result["role"] == "MARKDOWN"
    assert result["link"] == f"https://s3.example.org/{_sha(data)}.pdf"


# --- the real backend, which hashes the name it is given ---------------------


def test_the_production_backend_keeps_content_keys_distinct():
    """``HashedFilenameS3Boto3Storage`` salts and re-hashes whatever stem it gets.
    That is fine — a hash of a content hash is still a function of the content —
    but it has to stay injective in practice, which is what this pins."""
    backend = HashedFilenameS3Boto3Storage(
        bucket_name="test-bucket", access_key="key", secret_key="secret"
    )
    first, second = _sha(b"2078 audit"), _sha(b"2079 audit")

    key_a = backend._get_hashed_filename(f"{first}.pdf")
    key_b = backend._get_hashed_filename(f"{second}.pdf")

    assert key_a != key_b
    assert key_a == backend._get_hashed_filename(f"{first}.pdf")  # deterministic
    assert key_a.endswith(".pdf")
