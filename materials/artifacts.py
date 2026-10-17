"""The four artifacts a document can have, and who may see each.

A material used to carry two things a reader could open: the PDF, and the
transcript we made from it. Both were published as-is, which meant every
identifier the document printed was published too. There are now four:

================  ====================  ==========================  ==========
Artifact          ``jawafdehi:linkRole``  Produced by                 Published
================  ====================  ==========================  ==========
original PDF      ``RAW`` / ``ALTERNATE`` / ``PERMALINK``  the publisher  staff only, when the copy is OURS
redacted PDF      ``REDACTED_PDF``      us, from the original       yes
original text     ``MARKDOWN``          us (OCR / conversion)       staff only
redacted text     ``REDACTED_MARKDOWN`` us, from the original       yes
================  ====================  ==========================  ==========

Plus ``SOURCE_PAGE`` — the publisher's own web page, which is a citation, not a
copy, and is always published.

**A link to the publisher is never withheld.** The distinction this module draws
is not redacted-vs-original but *whose copy it is*: when ``RAW`` points at
``ciaa.gov.np`` we never hosted that file and hiding the URL would only break the
citation chain a reader needs to check a quote. When it points at our own
storage, we are the distributor, and that is the copy a publication decision
applies to. :func:`is_our_copy` is where that line is drawn.

🛑 **This is a publication decision, not an access control, and the difference
matters.** Our storage domain serves objects to anyone who has the URL — the key
is a hash, so the URL is unguessable, but it is not secret and nothing checks a
caller. Withholding a link from the public payload stops us *publishing* the
original; it does not stop someone who already has the URL (from a sitemap, a
cached page, a prior response) from fetching it. Making that a real gate means
moving the bucket behind signed URLs, which breaks every link already in the
wild — separate work, and it should not be mistaken for done because this
module exists.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from django.conf import settings

#: Roles carrying an ORIGINAL (unredacted) copy of the document itself.
ORIGINAL_PDF_ROLES = frozenset({"RAW", "ALTERNATE", "PERMALINK"})

#: Our redacted rendering of that document, which is what the public gets.
REDACTED_PDF_ROLE = "REDACTED_PDF"

#: The transcript we produced. MARKDOWN is the original; the other is redacted.
ORIGINAL_TRANSCRIPT_ROLE = "MARKDOWN"
REDACTED_TRANSCRIPT_ROLE = "REDACTED_MARKDOWN"

#: Roles whose content is ours and unredacted — the ones a public payload omits
#: when the copy is hosted by us.
_WITHHELD_ROLES = ORIGINAL_PDF_ROLES | {ORIGINAL_TRANSCRIPT_ROLE}

#: Every role the upload endpoint and the JSON-LD renderer know about.
ALL_ROLES = frozenset(
    ORIGINAL_PDF_ROLES
    | {
        REDACTED_PDF_ROLE,
        ORIGINAL_TRANSCRIPT_ROLE,
        REDACTED_TRANSCRIPT_ROLE,
        "SOURCE_PAGE",
    }
)


def media_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    """``associatedMedia`` as a list — it may legitimately be a single dict."""
    media = (data or {}).get("associatedMedia")
    if isinstance(media, dict):
        return [media]
    if isinstance(media, list):
        return [m for m in media if isinstance(m, dict)]
    return []


def role_of(media: dict[str, Any]) -> str:
    """The link's role, defaulting to RAW. Tolerates a non-string in JSONB.

    A role that is not a string cannot be matched against the vocabulary, and
    falling back to RAW is the SAFE default here rather than the lenient one: an
    unrecognisable role on a link of ours is withheld, not published.
    """
    role = media.get("jawafdehi:linkRole")
    if not isinstance(role, str) or not role.strip():
        return "RAW"
    return role.strip().upper()


def _our_hosts() -> set[str]:
    """Hostnames that serve files WE host.

    Read from settings rather than hardcoded, because a deployment that moves its
    object storage must not silently start publishing originals again — a wrong
    answer here is not an error anywhere, it is a quieter payload or a louder one.
    """
    hosts = set()
    for value in (
        getattr(settings, "JAWAFDEHI_S3_BASE", "") or "",
        getattr(settings, "AWS_S3_CUSTOM_DOMAIN", "") or "",
        getattr(settings, "MEDIA_URL", "") or "",
    ):
        candidate = value if "//" in value else f"//{value}"
        host = urlparse(candidate).hostname
        if host:
            hosts.add(host.lower())
    return hosts


def is_our_copy(url: Any) -> bool:
    """Whether this URL is a file we host, as opposed to the publisher's own.

    ``contentUrl`` comes out of free-form JSONB and is not guaranteed to be a
    string — a malformed document can carry an int, a list or a dict there. This
    runs on the material detail payload, the hottest public read in the archive,
    so a type error here is a 500 on a document a reader can otherwise see.
    Anything that is not a string is not a URL of ours.
    """
    if not isinstance(url, str):
        return False
    host = urlparse(url.strip()).hostname
    return bool(host and host.lower() in _our_hosts())


def is_withheld(media: dict[str, Any]) -> bool:
    """Whether this link is an unredacted copy of ours, withheld from the public.

    Both halves are required. A redacted rendering is published whoever hosts it,
    and an original hosted by the publisher is their publication, not ours.
    """
    if role_of(media) not in _WITHHELD_ROLES:
        return False
    return is_our_copy(media.get("contentUrl"))


def public_media(data: dict[str, Any]) -> list[dict[str, Any]]:
    """``associatedMedia`` as a signed-out reader sees it."""
    return [m for m in media_list(data) if not is_withheld(m)]


def artifact_summary(data: dict[str, Any]) -> dict[str, Any]:
    """Which of the four artifacts exist, and which this payload is not showing.

    Reported even when a link is withheld, because "we hold an original and are
    not publishing it" and "no original exists" are different facts about a
    document, and a reader comparing our redacted copy against the record needs
    to know which one they are looking at. Saying *that* we hold it is not the
    same as handing it over.
    """
    roles = [role_of(m) for m in media_list(data)]
    withheld = sum(1 for m in media_list(data) if is_withheld(m))
    return {
        "pdf": {
            "original": any(role in ORIGINAL_PDF_ROLES for role in roles),
            "redacted": REDACTED_PDF_ROLE in roles,
        },
        "transcript": {
            "original": ORIGINAL_TRANSCRIPT_ROLE in roles,
            "redacted": REDACTED_TRANSCRIPT_ROLE in roles,
        },
        # Named rather than counted-and-forgotten: a reader who sees 0 here and a
        # reader who sees 2 are looking at different documents, and only the
        # second has a reason to ask a caseworker.
        "withheld_from_this_payload": withheld,
    }


def redact_payload(data: dict[str, Any]) -> dict[str, Any]:
    """The stored document as a signed-out reader sees it.

    Copies rather than mutating: ``data`` is the row's own JSONB, and the public
    and staff representations are served from the same object in one process.
    """
    public = dict(data or {})
    public["associatedMedia"] = public_media(data)
    public["jawafdehi:artifacts"] = artifact_summary(data)
    return public
