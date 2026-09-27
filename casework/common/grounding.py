"""Whether an answer the model gave is actually in the text it was given."""

import re
import unicodedata

MIN_EVIDENCE_CHARS = 12
MIN_DEVANAGARI_SHARE = 0.3
TEASER_MIN_CHARS = 400
_STRIP = dict.fromkeys(map(ord, "​‌‍﻿*_"), None)
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LETTER = re.compile(r"[A-Za-zऀ-ॿ]")

#: Quotes carrying any of these words are addresses, CIAA, or the court, never the event place.
LOCATION_REJECT_MARKERS = (
    "स्थायी", "जन्मस्थान", "बस्ने", "वतन", "टंगाल", "टङ्गाल",
    "विशेष अदालत", "बिशेष अदालत", "सर्वोच्च अदालत", "आयोगको कार्यालय", "क्षेत्रीय कार्यालय",
)
#: Phrases that carry a marker word without being an address (a standing committee).
LOCATION_MARKER_EXCEPTIONS = ("स्थायी समिति",)

#: Devanagari letters and signs, i.e. the block minus danda, digits and the abbreviation sign.
_WORD = "ऀ-ॣॱ-ॿ"
#: Signs a mis-decoded transcript leaves after a word (`काठमाडौंैं`); a sign never starts a new word.
_SIGNS = "ऀ-ःऺ-ौॎ-ॏॕ-ॗॢॣ"
#: Case endings written joined to a name (`काठमाडौंमा`, `बाँकेस्थित`, `रामलाई`).
_SUFFIXES = ("अन्तर्गत", "द्वारा", "स्थित", "भित्र", "सम्म", "तर्फ", "बाट", "वाट", "लाई", "संग",
             "मा", "का", "को", "की", "ले", "कै")
_WORD_END = rf"(?=[{_SIGNS}]|(?:{'|'.join(_SUFFIXES)})?(?![{_WORD}]))"


def normalise_for_match(text: str) -> str:
    t = unicodedata.normalize("NFC", text or "").translate(_STRIP).replace("ँ", "ं")
    return re.sub(r"\s+", " ", t).strip()


def has_word(key: str, text: str) -> bool:
    """Whether `key` is in `text` as whole words, allowing a joined case ending (`बाँकेमा`, not `बाँकेपुर`).

    Both are matched as given; normalise them the same way first.
    """
    return bool(key) and re.search(rf"(?<![{_WORD}])" + re.escape(key) + _WORD_END, text) is not None


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
    screened = ev
    for phrase in LOCATION_MARKER_EXCEPTIONS:
        screened = screened.replace(phrase, " ")
    for marker in LOCATION_REJECT_MARKERS:
        if has_word(marker, screened):
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
