"""Record which accused were parties to each appeal docket.

The appeal's verdict is one value for the whole Supreme Court docket, and the CIAA
often appeals only some of the accused. This pairs each case's accused with the
party rows of its appeal docket, so the verdict can be shown on the cards of the
accused who were on that appeal and nobody else.

Two steps, never one:

1. Dry run (the default) writes a review CSV. Exact pairs are pre-marked
   ``bind``; near pairs and leftover parties are left blank for a human.
2. ``--apply --from-csv`` writes ONLY the rows marked ``bind``, as ``nes_id`` on
   the court party row (``POST /ingestion/entities/resolve/``). It re-matches
   nothing.

Usage:
    python -m casework.appeal_parties --out work/appeal-parties.csv
    python -m casework.appeal_parties --apply --allow-remote-writes \\
        --from-csv work/appeal-parties.csv
"""

from __future__ import annotations

import argparse
import csv
import difflib
import os
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass

from casework.common.api import CaseworkApi
from casework.court_record import court_ref, split_alias

EXACT = "exact"
NEAR = "near"

#: Below this a near pair is not offered for review at all.
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
    consonant: साह and शाह stay apart, as do कमल and कमला -- those go to review
    as near pairs.
    """
    text = unicodedata.normalize("NFC", name or "").translate(_ZERO_WIDTH)
    text = _GROUP_SUFFIX.sub("", _HONORIFIC.sub("", text))
    text = _FINAL_NGA.sub("ङ", text)
    text = _NASAL_CONJUNCT.sub("ं", text).translate(_LENGTH_FOLD).translate(_CHANDRABINDU)
    text = _STRAY_HALANT.sub("", text)
    return _NOT_A_LETTER.sub("", text)


def _party_keys(party: dict) -> set[str]:
    raw = unicodedata.normalize("NFC", party.get("name") or "").translate(_ZERO_WIDTH)
    legal, aliases = split_alias(raw)
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


FIELDS = [
    "slug", "state", "appeal_iri", "court", "case_number", "verdict",
    "accused_nes_id", "accused_name", "party_name", "party_side", "party_nes_id",
    "tier", "score", "decision",
]


def _decision(pair: Pairing) -> str:
    party_id = (pair.party or {}).get("nes_id") or ""
    if pair.party is None:
        return ""
    if party_id == pair.nes_id:
        return "already"
    if party_id:
        return "conflict"
    return "bind" if pair.tier == EXACT else ""


def review_rows(case: dict, appeal_iri: str, verdict: str | None,
                parties: list[dict]) -> list[dict]:
    """The review-file rows for one case's appeal: one per accused, then one per
    party no accused paired with."""
    court, number = court_ref(appeal_iri) or ("", "")
    base = {"slug": case.get("slug") or "", "state": case.get("state") or "",
            "appeal_iri": appeal_iri, "court": court.lower(),
            "case_number": number.upper(), "verdict": verdict or ""}
    accused = [e for e in case.get("entities") or [] if e.get("type") == "accused"]
    pairs = pair_accused(accused, parties)
    rows = []
    for pair in pairs:
        party = pair.party or {}
        rows.append({**base, "accused_nes_id": pair.nes_id, "accused_name": pair.display_name,
                     "party_name": party.get("name") or "", "party_side": party.get("side") or "",
                     "party_nes_id": party.get("nes_id") or "", "tier": pair.tier or "",
                     "score": f"{pair.score:.2f}" if pair.tier else "",
                     "decision": _decision(pair)})
    paired = {id(p.party) for p in pairs if p.party is not None}
    for party in parties:
        if id(party) in paired or _GOVERNMENT in (party.get("name") or ""):
            continue
        rows.append({**base, "accused_nes_id": "", "accused_name": "",
                     "party_name": party.get("name") or "", "party_side": party.get("side") or "",
                     "party_nes_id": party.get("nes_id") or "", "tier": "", "score": "",
                     "decision": ""})
    return rows


def write_rows(path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def read_rows(path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def apply_rows(api, rows: list[dict], batch_size: int = 100) -> dict:
    """Write the rows marked ``bind`` and nothing else. Returns the tallies."""
    marked, seen = [], set()
    for row in rows:
        if (row.get("decision") or "").strip().lower() != "bind":
            continue
        key = (row["court"], row["case_number"], row["party_side"], row["party_name"],
               row["accused_nes_id"])
        if key not in seen:  # two cases citing one appeal docket
            seen.add(key)
            marked.append(row)
    stats: dict = {"marked": len(marked), "resolved": 0, "unmatched": 0, "failed": 0,
                   "unmatched_rows": [], "failed_rows": []}
    for start in range(0, len(marked), batch_size):
        chunk = marked[start:start + batch_size]
        items = [{"court": r["court"], "case_number": r["case_number"],
                  "side": r["party_side"], "name": r["party_name"],
                  "nes_id": r["accused_nes_id"]} for r in chunk]
        response = api.resolve_court_parties(items)
        for result in response.get("results") or []:
            row = chunk[result["index"]]
            if result.get("status") == "resolved":
                stats["resolved"] += 1
            else:
                key = "failed" if result.get("status") == "failed" else "unmatched"
                stats[key] += 1
                stats[f"{key}_rows"].append((row["slug"], row["party_name"]))
    return stats


def _appeal_stages(case: dict) -> list[str]:
    stages = (case.get("dates") or {}).get("stages") or []
    return [s["courtcase_iri"] for s in stages
            if s.get("stage") == "appeal" and s.get("courtcase_iri")]


def _mark_cross_case_conflicts(rows: list[dict]) -> None:
    """A party row holds one nes_id. Two cases citing one appeal docket that would
    bind the same row to DIFFERENT entities (duplicate NES records) overwrite each
    other, so every such row becomes a conflict."""
    ids: dict[tuple, set[str]] = {}
    for row in rows:
        if row["decision"] == "bind":
            key = (row["court"], row["case_number"], row["party_side"], row["party_name"])
            ids.setdefault(key, set()).add(row["accused_nes_id"])
    for row in rows:
        key = (row["court"], row["case_number"], row["party_side"], row["party_name"])
        if row["decision"] == "bind" and len(ids[key]) > 1:
            row["decision"] = "conflict"


def build_review(api, slugs: list[str]) -> list[dict]:
    """Every case's appeal dockets, paired. Read-only.

    Each slug is reviewed once even when the case list returns it twice. An
    appealed case is re-read in detail: the list sends `name: {}`, and only the
    detail carries the Nepali name of an accused shown in English.
    """
    cases = [api.get_case(slug) for slug in slugs] if slugs else api.iter_cases()
    rows: list[dict] = []
    seen: set[str] = set()
    for case in cases:
        if case.get("slug") in seen:
            continue
        seen.add(case.get("slug") or "")
        if not any(e.get("type") == "accused" for e in case.get("entities") or []):
            continue
        if not _appeal_stages(case):
            continue
        if not slugs:
            case = api.get_case(case["slug"])
        verdicts = case.get("appeal_verdicts") or {}
        for iri in _appeal_stages(case):
            ref = court_ref(iri)
            if ref is None:
                continue
            parties = api.get_court_case_entities(ref[0], ref[1].upper())
            rows.extend(review_rows(case, iri, verdicts.get(iri), parties))
    _mark_cross_case_conflicts(rows)
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api-base-url", default=os.environ.get("JAWAFDEHI_API_BASE"))
    parser.add_argument("--api-token", default=os.environ.get("JAWAFDEHI_API_TOKEN", ""),
                        help="Defaults to $JAWAFDEHI_API_TOKEN; prefer the env var.")
    parser.add_argument("--slug", action="append", default=[])
    parser.add_argument("--out", help="Review CSV to write (dry run).")
    parser.add_argument("--from-csv", help="Reviewed CSV to apply.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-remote-writes", action="store_true")
    args = parser.parse_args(argv)
    if not args.api_token:
        parser.error("an API token is required: set JAWAFDEHI_API_TOKEN")
    api = CaseworkApi(args.api_base_url, token=args.api_token,
                      allow_remote_writes=args.allow_remote_writes)

    if args.apply:
        if not args.from_csv:
            parser.error("--apply writes a reviewed file: pass --from-csv")
        stats = apply_rows(api, read_rows(args.from_csv))
        for key in ("marked", "resolved", "unmatched", "failed"):
            print(f"{key}: {stats[key]}")
        for slug, name in stats["unmatched_rows"] + stats["failed_rows"]:
            print(f"  not written: {slug} {name}")
        return 1 if stats["unmatched"] or stats["failed"] else 0

    if not args.out:
        parser.error("a dry run writes a review file: pass --out")
    rows = build_review(api, args.slug)
    write_rows(args.out, rows)
    tally = Counter((r["tier"] or ("no party" if r["accused_nes_id"] else "party only"),
                     r["decision"] or "blank") for r in rows)
    print(f"wrote {len(rows)} rows to {args.out}")
    for (tier, decision), count in sorted(tally.items()):
        print(f"  {tier:10} {decision:9} {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
