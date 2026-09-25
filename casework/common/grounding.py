"""Whether an answer the model gave is actually in the text it was given."""

import re
import unicodedata

MIN_EVIDENCE_CHARS = 12
MIN_DEVANAGARI_SHARE = 0.3
TEASER_MIN_CHARS = 400
_STRIP = dict.fromkeys(map(ord, "​‌‍﻿*_"), None)
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LETTER = re.compile(r"[A-Za-zऀ-ॿ]")

#: Quotes carrying any of these are addresses, CIAA, or the court, never the event place.
LOCATION_REJECT_MARKERS = (
    "स्थायी", "जन्मस्थान", "बस्ने", "वतन", "टंगाल", "टङ्गाल",
    "विशेष अदालत", "बिशेष अदालत", "सर्वोच्च अदालत", "आयोगको कार्यालय", "क्षेत्रीय कार्यालय",
)


def normalise_for_match(text: str) -> str:
    t = unicodedata.normalize("NFC", text or "").translate(_STRIP).replace("ँ", "ं")
    return re.sub(r"\s+", " ", t).strip()


def evidence_found(evidence: str, text: str) -> bool:
    ev = normalise_for_match(evidence)
    return len(ev) >= MIN_EVIDENCE_CHARS and ev in normalise_for_match(text)


def location_quote_problem(evidence: str, place: str, text: str, caption_end: int = 0) -> str:
    """Why this quote cannot ground a location, or "" when it can."""
    ev, body = normalise_for_match(evidence), normalise_for_match(text)
    if len(ev) < MIN_EVIDENCE_CHARS:
        return "evidence too short"
    if ev not in body:
        return "evidence not found in the source"
    if body.find(ev, len(normalise_for_match(text[:caption_end]))) == -1:
        return "evidence is only in the caption"
    if normalise_for_match(place) not in ev:
        return "the place is not in its own evidence"
    for marker in LOCATION_REJECT_MARKERS:
        if marker in ev:
            return f"evidence carries {marker!r}: an address, CIAA or the court"
    return ""


def is_readable_devanagari(text: str) -> bool:
    """False for a Preeti-encoded transcript, which extracts as ASCII."""
    letters = len(_LETTER.findall(text or ""))
    return bool(letters) and len(_DEVANAGARI.findall(text)) / letters >= MIN_DEVANAGARI_SHARE


def is_teaser(text: str) -> bool:
    """A press release stored as its cut-off web teaser ("क्रमश... डाउनलोडमा थिच्नुहोला")."""
    t = text or ""
    return len(t.strip()) < TEASER_MIN_CHARS or ("क्रमश" in t and "डाउनलोड" in t)
