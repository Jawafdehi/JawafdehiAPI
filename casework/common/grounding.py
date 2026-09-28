"""Whether an answer the model gave is actually in the text it was given."""

import re
import unicodedata

MIN_EVIDENCE_CHARS = 12
#: The shortest word-for-word stretch that grounds a quote the model only partly copied.
MIN_VERBATIM_CHARS = 40
MIN_DEVANAGARI_SHARE = 0.3
TEASER_MIN_CHARS = 400
_STRIP = dict.fromkeys(map(ord, "​‌‍﻿*_"), None)
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LETTER = re.compile(r"[A-Za-zऀ-ॿ]")

#: Any of these words beside the place makes it an address, CIAA, or the court, never the event place.
LOCATION_REJECT_MARKERS = (
    "स्थायी", "जन्मस्थान", "बस्ने", "वतन", "घर भई", "टंगाल", "टङ्गाल",
    "विशेष अदालत", "बिशेष अदालत", "सर्वोच्च अदालत", "आयोगको कार्यालय", "क्षेत्रीय कार्यालय",
)
#: Phrases that carry a marker word without being an address (a standing committee, a permanent post).
LOCATION_MARKER_EXCEPTIONS = ("स्थायी समिति", "स्थायी नियुक्ति")
#: How far from the place a marker counts. A court "sentence" is often a whole paragraph,
#: and one naming the event place can also give other people's addresses 200 chars on
#: (077-CR-0004); an address phrase sits right against its place (`X वडा नं. ५ बस्ने`).
MARKER_BEFORE_CHARS = 40
MARKER_AFTER_CHARS = 120
_PUNCTUATION = re.compile(r"[,;:।॥\-–—()\"'/]+")

#: Devanagari letters and signs, i.e. the block minus danda, digits and the abbreviation sign.
_WORD = "ऀ-ॣॱ-ॿ"
#: Signs a mis-decoded transcript leaves after a word (`काठमाडौंैं`); a sign never starts a new word.
_SIGNS = "ऀ-ःऺ-ौॎ-ॏॕ-ॗॢॣ"
#: Case endings written joined to a name (`काठमाडौंमा`, `बाँकेस्थित`, `रामलाई`).
_SUFFIXES = ("अन्तर्गत", "द्वारा", "जिल्ला", "स्थित", "भित्र", "सम्म", "तर्फ", "बाट", "वाट", "लाई", "संग",
             "मा", "का", "को", "की", "ले", "कै")
_WORD_END = rf"(?=[{_SIGNS}]|(?:{'|'.join(_SUFFIXES)})?(?![{_WORD}]))"


def normalise_for_match(text: str) -> str:
    t = unicodedata.normalize("NFC", text or "").translate(_STRIP).replace("ँ", "ं")
    return re.sub(r"\s+", " ", t).strip()


def has_word(key: str, text: str) -> bool:
    """Whether `key` is in `text` as whole words, allowing a joined case ending (`बाँकेमा`, not `बाँकेपुर`).

    Both are matched as given; normalise them the same way first.
    """
    return next(iter(_word_matches(key, text)), None) is not None


def evidence_found(evidence: str, text: str) -> bool:
    ev = normalise_for_match(evidence)
    return len(ev) >= MIN_EVIDENCE_CHARS and ev in normalise_for_match(text)


def _longest_run(evidence: str, body: str) -> str:
    """The longest run of `evidence`'s whole words that is in `body` (already normalised), as written."""
    words, best = (evidence or "").split(), ""
    for i in range(len(words)):
        lo, hi = i, len(words)
        while lo < hi:  # the longest words[i:j] in body; any shorter prefix of it is in body too
            mid = (lo + hi + 1) // 2
            if normalise_for_match(" ".join(words[i:mid])) in body:
                lo = mid
            else:
                hi = mid - 1
        run = " ".join(words[i:lo])
        if len(normalise_for_match(run)) > len(normalise_for_match(best)):
            best = run
    return best


def verbatim_quote(evidence: str, text: str, min_share: float = 0.0) -> str:
    """The part of `evidence` that is word for word in `text`, as the quote wrote it; "" when too little is.

    All of it when it is all there. Otherwise its longest run of whole words, which must be at
    least `MIN_VERBATIM_CHARS` and `min_share` of the quote: the model copies a sentence and
    rewords its last few words (077-CR-0004), and the stretch it did copy still grounds it.
    """
    ev, body = normalise_for_match(evidence), normalise_for_match(text)
    if len(ev) < MIN_EVIDENCE_CHARS:
        return ""
    if ev in body:
        return (evidence or "").strip()
    best = _longest_run(evidence, body)
    return best if len(normalise_for_match(best)) >= max(MIN_VERBATIM_CHARS, min_share * len(ev)) else ""


#: Where the model skipped part of a sentence ("उजूरी निवेदक... नेत्र प्रसाद रेग्मी").
_ELLIPSIS = re.compile(r"\.{2,}|…")
#: The shortest piece of an elided quote that counts: a scrap like "र" is in every order.
MIN_PIECE_CHARS = 4


def entity_quote(evidence: str, text: str, name: str = "") -> str:
    """The part of an entity's or accused note's quote that `text` backs, or "" when none does.

    The whole quote when it is word for word: under `MIN_EVIDENCE_CHARS` only if it holds `name`
    (the prompt asks for the shortest phrase, often the name alone). Every piece of a quote the
    model elided with "...", each at least `MIN_PIECE_CHARS`. Otherwise its longest word-for-word
    run, if that is `MIN_VERBATIM_CHARS` long. Failing all of those, the name alone when both the
    quote and the order hold it ("उजुरीकर्ता X" where the order spells उजूरीकर्ता; a bank named in
    a table between `|`s).
    """
    ev, body, key = normalise_for_match(evidence), normalise_for_match(text), normalise_for_match(name)
    if not ev:
        return ""
    if ev in body and (len(ev) >= MIN_EVIDENCE_CHARS or has_word(key, ev)):
        return evidence.strip()
    pieces = [normalise_for_match(p) for p in _ELLIPSIS.split(evidence) if p.strip()]
    if (len(pieces) > 1 and all(len(p) >= MIN_PIECE_CHARS and p in body for p in pieces)
            and sum(map(len, pieces)) >= MIN_EVIDENCE_CHARS):
        return evidence.strip()
    run = _longest_run(evidence, body)
    if len(normalise_for_match(run)) >= MIN_VERBATIM_CHARS:
        return run
    if has_word(key, ev) and has_word(key, body):
        return name.strip()
    return ""


def location_quote_problem(evidence: str, place: str, text: str, caption_end: int = 0,
                           names=(), fold=None) -> str:
    """Why this quote cannot ground a location, or "" when it can.

    Only the verbatim part of the quote counts. The place is in it when the place as written
    is, or when one of `names` is -- the gazetteer's own keys for what the place resolved to,
    matched as words in `fold(quote)` -- so a place the model assembled from pieces of the
    sentence still grounds. One mention with no address marker beside it is enough.
    """
    if len(normalise_for_match(evidence)) < MIN_EVIDENCE_CHARS:
        return "evidence too short"
    ev = normalise_for_match(verbatim_quote(evidence, text))
    if not ev:
        return "evidence not found in the source"
    if normalise_for_match(text).find(ev, len(normalise_for_match(text[:caption_end]))) == -1:
        return "evidence is only in the caption"
    loose_ev, loose_place = _loosen(ev), _loosen(place)
    mentions = [(loose_ev, m.start(), m.end())
                for m in re.finditer(re.escape(loose_place), loose_ev)] if loose_place else []
    folded = fold(ev) if fold else ev
    for key in names:
        mentions += [(folded, m.start(), m.end()) for m in _word_matches(key, folded)]
    if not mentions:
        return "the place is not in its own evidence"
    problems = [_marker_beside(within, start, end) for within, start, end in mentions]
    return "" if "" in problems else problems[0]


def _word_matches(key: str, text: str):
    return re.finditer(rf"(?<![{_WORD}])" + re.escape(key) + _WORD_END, text) if key else ()


def _marker_beside(text: str, start: int, end: int) -> str:
    near = text[max(0, start - MARKER_BEFORE_CHARS):end + MARKER_AFTER_CHARS]
    for phrase in LOCATION_MARKER_EXCEPTIONS:
        near = near.replace(phrase, " ")
    for marker in LOCATION_REJECT_MARKERS:
        if has_word(marker, near):
            return f"evidence carries {marker!r} beside the place: an address, CIAA or the court"
    return ""


def _loosen(text: str) -> str:
    """`normalise_for_match` with punctuation dropped: `जिल्ला खोटाङ, दिक्तेल` is `जिल्ला खोटाङ दिक्तेल`."""
    return re.sub(r"\s+", " ", _PUNCTUATION.sub(" ", normalise_for_match(text))).strip()


def is_readable_devanagari(text: str) -> bool:
    """False for a Preeti-encoded transcript, which extracts as ASCII."""
    letters = len(_LETTER.findall(text or ""))
    return bool(letters) and len(_DEVANAGARI.findall(text)) / letters >= MIN_DEVANAGARI_SHARE


def is_teaser(text: str) -> bool:
    """A press release stored as its cut-off web teaser ("क्रमश... डाउनलोडमा थिच्नुहोला")."""
    t = text or ""
    return len(t.strip()) < TEASER_MIN_CHARS or ("क्रमश" in t and "डाउनलोड" in t)
