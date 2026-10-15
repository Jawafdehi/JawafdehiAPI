"""Which of a case's accused were parties to each of its appeals.

The appeal's verdict is one value for the whole Supreme Court docket, and the CIAA
often appeals only some of the accused. The docket's party rows name who was on
it, so each case's accused are paired with those names when the case is served:
nothing is stored, and a new appeal or a newly bound accused shows at once.

Pairing only ever looks inside one case: the appeal is already tied to the case,
so a docket name can only be one of that case's own accused. A row someone has
linked by hand (``CaseEntity.nes_id``) is trusted before any name.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass

EXACT = "exact"
NEAR = "near"

#: Below this a near pair is not made at all.
MIN_NEAR_SCORE = 0.85
#: A near pair is dropped when a rival comes within this of it.
RIVAL_MARGIN = 0.05
#: Shortest key that may match by containment (an extra surname on one side).
MIN_CONTAINED_LENGTH = 6

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u093c"))
#: Chandrabindu is dropped, not folded to anusvara: चापागाईँ and चापागाई, फुयाँल and
#: फुयाल are one surname in the court rows.
_CHANDRABINDU = dict.fromkeys(map(ord, "\u0901"))
#: A docket row that stands for a group ("... समेत ६४", "... १७ जना"), and a doctor's
#: title, are not part of the name.
_GROUP_SUFFIX = re.compile(r"(समेत\s*[०-९0-9]*|[०-९0-9]+\s*जना)\s*$")
_HONORIFIC = re.compile(r"^\s*डा\s*\.\s*")
#: गुरुङ and गुरुङ्ग, तामाङ and तामाङ्ग are one surname.
_FINAL_NGA = re.compile(r"ङ्ग(?![\u0900-\u097f])")
_NASAL_CONJUNCT = re.compile(r"[ङञणनम]्(?=[क-ह])")
_STRAY_HALANT = re.compile(r"(?<![क-ह])्")
_NOT_A_LETTER = re.compile(r"[^ऀ-ॿA-Za-z0-9]")
_PARENTHESISED = re.compile(r"\(.*?\)")
#: Vowel length, and ब/व -- the court rows write वहादुर, वस्नेत, बिना for बहादुर,
#: बस्नेत, विना throughout.
_LENGTH_FOLD = str.maketrans({"ी": "ि", "ू": "ु", "ई": "इ", "ऊ": "उ", "व": "ब"})
_GOVERNMENT = "नेपाल सरकार"


def name_key(name: str) -> str:
    """A name with only its spelling FORM folded away.

    Folds spaces, punctuation, zero-width joiners, a doctor's title, a group
    suffix, vowel length, ब/व, the nasal forms and chandrabindu. No other
    consonant: साह and शाह stay apart, as do कमल and कमला -- those pair
    as near pairs.
    """
    text = unicodedata.normalize("NFC", name or "").translate(_ZERO_WIDTH)
    text = _GROUP_SUFFIX.sub("", _HONORIFIC.sub("", text))
    text = _FINAL_NGA.sub("ङ", text)
    text = _NASAL_CONJUNCT.sub("ं", text).translate(_LENGTH_FOLD).translate(_CHANDRABINDU)
    text = _STRAY_HALANT.sub("", text)
    return _NOT_A_LETTER.sub("", text)


#: ``<alias> भन्ने <legal name>``; same split as ``casework.court_record.split_alias``,
#: which the API does not import.
_ALIAS_MARKER = "भन्ने"


def _split_alias(name: str) -> tuple[str, list[str]]:
    head, marker, legal = name.rpartition(_ALIAS_MARKER)
    if not marker or not head.strip() or not legal.strip():
        return name, []
    return legal.strip(), [part.strip() for part in head.split(_ALIAS_MARKER) if part.strip()]


def _party_keys(party: dict) -> set[str]:
    raw = unicodedata.normalize("NFC", party.get("name") or "").translate(_ZERO_WIDTH)
    legal, aliases = _split_alias(raw)
    forms = [legal, *aliases, _PARENTHESISED.sub(" ", legal)]
    return {key for key in map(name_key, forms) if key}


def _accused_keys(bind: dict) -> set[str]:
    forms = [bind.get("display_name") or "", (bind.get("name") or {}).get("ne") or ""]
    return {key for key in map(name_key, forms) if key}


def _similarity(left: set[str], right: set[str]) -> float:
    best = 0.0
    for a in left:
        for b in right:
            score = difflib.SequenceMatcher(None, a, b).ratio()
            if min(len(a), len(b)) >= MIN_CONTAINED_LENGTH and (a in b or b in a):
                score = max(score, 0.9)
            best = max(best, score)
    return best


@dataclass
class Pairing:
    nes_id: str
    display_name: str
    party: dict | None = None
    tier: str | None = None
    score: float = 0.0


def pair_accused(accused: list[dict], parties: list[dict]) -> list[Pairing]:
    """One `Pairing` per accused, in roster order; each party is used at most once.

    Exact first: an accused and a party pair exactly only when each is the
    other's sole key match, so two namesakes on one roster pair with nobody.
    Then near, best score first, dropping any pair with a rival within
    `RIVAL_MARGIN` on either side.
    """
    parties = [p for p in parties if _GOVERNMENT not in (p.get("name") or "")]
    a_keys = [_accused_keys(b) for b in accused]
    p_keys = [_party_keys(p) for p in parties]
    pairs = [Pairing(b.get("nes_id") or "", b.get("display_name") or "") for b in accused]
    used: set[int] = set()

    hits = {ai: [pi for pi, pk in enumerate(p_keys) if ak & pk] for ai, ak in enumerate(a_keys)}
    for ai, candidates in hits.items():
        if len(candidates) != 1:
            continue
        pi = candidates[0]
        if sum(pi in other for other in hits.values()) == 1:
            pairs[ai].party, pairs[ai].tier, pairs[ai].score = parties[pi], EXACT, 1.0
            used.add(pi)

    open_a = [ai for ai, p in enumerate(pairs) if p.tier is None]
    open_p = [pi for pi in range(len(parties)) if pi not in used]
    scores = {(ai, pi): _similarity(a_keys[ai], p_keys[pi]) for ai in open_a for pi in open_p}
    for (ai, pi), score in sorted(scores.items(), key=lambda kv: -kv[1]):
        if score < MIN_NEAR_SCORE or pairs[ai].tier is not None or pi in used:
            continue
        rivals = [s for (a2, p2), s in scores.items()
                  if (a2, p2) != (ai, pi) and (a2 == ai or p2 == pi)
                  and pairs[a2].tier is None and p2 not in used
                  and s >= score - RIVAL_MARGIN]
        if rivals:
            continue
        pairs[ai].party, pairs[ai].tier, pairs[ai].score = parties[pi], NEAR, round(score, 2)
        used.add(pi)
    return pairs


def parties_on_appeal(accused: list[dict], rows: list[dict]) -> list[str]:
    """The nes_ids on one appeal docket, sorted: every row linked by hand, plus
    each accused whose name pairs (exactly or near) with an unlinked row.

    ``accused`` are the case's accused binds (``nes_id``, ``display_name`` and
    ``name``: ``{"en", "ne"}``); ``rows`` are the docket's party rows
    (``name``, ``side``, ``nes_id``).
    """
    linked = {row["nes_id"] for row in rows if row.get("nes_id")}
    open_accused = [bind for bind in accused if bind.get("nes_id") not in linked]
    open_rows = [row for row in rows if not row.get("nes_id")]
    paired = {pair.nes_id for pair in pair_accused(open_accused, open_rows) if pair.party}
    return sorted(linked | paired)
