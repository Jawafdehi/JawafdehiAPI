"""Object keys for uploads must derive from the BYTES, not the filename.

``HashedFilenameS3Boto3Storage`` keys an object on ``sha256(salt + filename
stem)``, so two different files sharing a base name land on one R2 key.
``get_available_name`` is overridden to never suffix, so the second write
silently destroys the first — and R2 has no object versioning to recover from.
(``file_overwrite`` is not a second cause: it is read only by the
``S3Storage.get_available_name`` that the subclass replaces.)

Two consequences motivated these tests:

* ``materials.conversion`` named every transcript ``material.md``, so every
  converted material's MARKDOWN link resolved to ONE object. Seventeen materials
  — every material ever converted — were sharing a single ``.md`` URL in
  production when this was measured on 2026-09-30. Each material's own
  ``data["text"]`` was unaffected, so the damage is confined to the archive
  plane.
* A bulk ingest (the Auditor General corpus) can carry repeating upstream
  filenames. How many collide depends on which name the ingest passes: on the
  corpus's derived display name, 453 of 6,234 share a stem (2 of the 227
  national-level v1 documents); on the URL basename — what this repo's existing
  bulk ingest passes — none do. Content-addressing removes the question.

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
    """The regression. ``अन्नपूर्ण गाउँपालिका`` names four distinct audit reports
    in the Auditor General corpus; under a name-keyed store they are one object."""
    first, second = b"2078 audit", b"2079 audit"
    name = "अन्नपूर्ण गाउँपालिका.pdf"

    store_file_as_link(_upload(first, name), content_hash=_sha(first))
    store_file_as_link(_upload(second, name), content_hash=_sha(second))

    assert storage.saved[0] != storage.saved[1]


def test_the_same_bytes_under_different_names_get_one_key(storage):
    """Content addressing cuts the other way too: re-running a bulk ingest asks
    storage for the SAME key rather than a second one.

    What that key does on arrival is the backend's business and is not asserted
    here: under the production backend the write is an idempotent overwrite,
    while FileSystemStorage suffixes and does make a second file. This pins the
    name ``store_file_as_link`` chooses, which is the half it owns.
    """
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
    """The value lands in a filename, so the contract is enforced rather than
    trusted. Django's ``Storage.save`` would itself reject the traversal case via
    ``validate_file_name``; what this adds is everything Django permits — path
    separators and non-hex junk — and a clear error at the call site."""
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
    but it has to stay injective in practice, which is what this pins.

    Note this asserts on ONE hash pass. The real key is hashed twice: ``save()``
    hashes the name, then Django's ``Storage.save`` calls the overridden
    ``get_available_name``, which hashes the result again. Injectivity composes,
    so the property holds either way, but do not read the value below as the key
    that lands in R2.
    """
    backend = HashedFilenameS3Boto3Storage(
        bucket_name="test-bucket", access_key="key", secret_key="secret"
    )
    first, second = _sha(b"2078 audit"), _sha(b"2079 audit")

    key_a = backend._get_hashed_filename(f"{first}.pdf")
    key_b = backend._get_hashed_filename(f"{second}.pdf")

    assert key_a != key_b
    assert key_a == backend._get_hashed_filename(f"{first}.pdf")  # deterministic
    assert key_a.endswith(".pdf")
