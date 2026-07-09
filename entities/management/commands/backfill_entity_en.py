"""Generate corrected English names (``name.en`` + ``alternateName``) for stored
entities and write them to a mutations CSV.

WHY. Historic bulk loads populated ``name.en`` with raw character-level
transliterations of the Devanagari — academic IAST ("Nārāyaṇī") or Harvard-Kyoto
("nArAyaNI aspatAla") — or left it null. A correct English name TRANSLATES
generic/type nouns (अस्पताल→Hospital, मन्त्रालय→Ministry, आयोग→Commission) and
romanizes proper nouns in clean, conventional, plain-ASCII spelling
(नारायणी→Narayani). Where romanization is genuinely ambiguous (रवि = Rabi/Ravi)
the primary spelling is ``name.en`` and the alternates go to ``alternateName``.

This command GENERATES the mutation set only — it does not write to entities.
Applying the CSV is a separate, auditable step (an operator runs the apply tool
against the entity PATCH API with a write-scoped token). Keeping generate and
apply separate mirrors how the ``cases`` enrichers stage their output and keeps
a bulk LLM rewrite out of the request/write path.

NO IAST library is used: names come from the in-tree Bedrock LLM client
(``llm.invoke``); a diacritic/Harvard-Kyoto scrub-net drops any residual
transliteration the model emits.

Usage::

    python manage.py backfill_entity_en --out mutations.csv --dry-run
    python manage.py backfill_entity_en --out mutations.csv --prefix organization/hospital
    python manage.py backfill_entity_en --out mutations.csv --limit 200 --tier cheap
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sys

from django.core.management.base import BaseCommand, CommandError

from entities.persistence import EntityRepository
from llm.invoke import invoke_json

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

# Fields the model returns per entity; scrubbed against this before CSV write.
_DIACRITIC = re.compile(r"[āīūṛṝḷḹṅñṭḍṇśṣṃḥĀĪŪṚṜḶḸṄÑṬḌṆŚṢṂḤ]")
_HK_TOKEN = re.compile(r"^[a-z]+[AIUEMR]([a-z]|$)")
_HK_TILDE = re.compile(r"[a-zA-Z]~[a-zA-Z]")
_TOKEN_SPLIT = re.compile(r"[\s,()]+")

SYSTEM = (
    "You produce the correct English name for a Nepali (Nepal) entity, given its "
    "Nepali (Devanagari) name and its schema.org type. This is for a public civic "
    "archive; names must read naturally to an English reader.\n\n"
    "RULES:\n"
    "1. TRANSLATE common/generic and administrative nouns to standard English: "
    "अस्पताल=Hospital, कार्यालय=Office, मन्त्रालय=Ministry, विभाग=Department, "
    "आयोग=Commission, नगरपालिका=Municipality, महानगरपालिका=Metropolitan City, "
    "गाउँपालिका=Rural Municipality, वडा=Ward, नं.=No., जिल्ला=District, "
    "प्रदेश=Province, समिति=Committee, बैंक=Bank, विद्यालय=School, "
    "विश्वविद्यालय=University, सहकारी=Cooperative, कम्पनी=Company, प्रहरी=Police, "
    "नेपाल सरकार=Government of Nepal, प्रा.लि.=Pvt. Ltd., लि.=Ltd., "
    "अख्तियार दुरुपयोग अनुसन्धान आयोग=Commission for the Investigation of Abuse of "
    "Authority (CIAA), नेपाल राष्ट्र बैंक=Nepal Rastra Bank.\n"
    "2. TRANSLITERATE proper nouns (people, places, org-specific names) with clean, "
    "conventional romanization as commonly written in Nepal, using ONLY plain "
    "ASCII A-Z/a-z. It is FORBIDDEN to output diacritics/accents or academic "
    "IAST/Harvard-Kyoto capitals inside a word (write 'Narayani', NEVER 'nArAyaNI'; "
    "'Sindhupalchok', never with an accented a). Capitalize only the first letter "
    "of each word. Use widely-used spellings (पौडेल=Poudel, श्रेष्ठ=Shrestha, "
    "काठमाडौँ=Kathmandu).\n"
    "3. Preserve numerals as Arabic digits (वडा नं. ३ -> Ward No. 3).\n"
    "4. Keep parenthetical/location qualifiers, translating their nouns too.\n"
    "5. If the name is ALREADY good English, return it unchanged apart from "
    "obvious fixes.\n"
    "6. VARIANTS: Nepali romanization is often ambiguous (व = b/v: रवि = Rabi/Ravi; "
    "inherent vowel: पर्वत = Parbat/Parvat; ी = i/ee; स/श = s/sh; final schwa). "
    "Give the single most common spelling as 'canonical' and list OTHER "
    "equally-valid ASCII spellings in 'alternates' (0-3; [] when unambiguous, e.g. "
    "an official institution name). Every alternate MUST also obey rule 2."
)

USER_TMPL = (
    "Produce the correct English name for each item. Each item has an id, a "
    "Nepali name (ne), and a schema.org type. Return ONLY a JSON object "
    '{{"names": [{{"id": <id>, "canonical": "<English name>", '
    '"alternates": ["<other spelling>", ...]}}, ...]}} with one entry per input '
    'id. "alternates" is [] when there is only one correct spelling. No prose.\n\n'
    "ITEMS:\n{items}"
)


def _name_map(doc):
    """Return (ne, en) from a schema.org ``name`` (language map or bare string)."""
    name = doc.get("name")
    if isinstance(name, dict):
        return name.get("ne"), name.get("en")
    if isinstance(name, str):
        return None, None  # a bare string can't tell us which script it is
    return None, None


def _looks_iast_ascii(s):
    """IAST/HK signature check for a candidate ASCII spelling (scrub-net)."""
    if not isinstance(s, str) or not s:
        return False
    if _DIACRITIC.search(s) or _HK_TILDE.search(s):
        return True
    return any(_HK_TOKEN.search(tok) for tok in _TOKEN_SPLIT.split(s))


class Command(BaseCommand):
    help = "Generate corrected English names for entities into a mutations CSV."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="mutations.csv",
                            help="Output CSV path (default: mutations.csv).")
        parser.add_argument("--prefix", default=None,
                            help="Only entities under this IRI prefix "
                                 "(e.g. organization/hospital).")
        parser.add_argument("--type", dest="entity_type", default=None,
                            help="Only entities of this schema.org @type.")
        parser.add_argument("--limit", type=int, default=None,
                            help="Process at most N entities.")
        parser.add_argument("--batch-size", type=int, default=25,
                            help="Entities per LLM call (default 25).")
        parser.add_argument("--tier", choices=("premium", "cheap"), default="cheap",
                            help="LLM tier (default cheap — Haiku-class is enough).")
        parser.add_argument("--changed-only", action="store_true",
                            help="Write only rows whose en actually changes.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Log a preview of the first batch; write nothing.")

    def handle(self, *args, **opts):
        repo = EntityRepository()
        batch_size = max(1, opts["batch_size"])
        entities = self._load(repo, opts["prefix"], opts["entity_type"], opts["limit"])
        if not entities:
            raise CommandError("No entities matched the given filters.")
        logger.info("Loaded %d entities for en generation.", len(entities))

        if opts["dry_run"]:
            preview = self._translate_batch(entities[:batch_size], opts["tier"])
            for e in entities[:batch_size]:
                r = preview.get(e["iri"], {})
                logger.info("  %s\n     ne=%r canonical=%r alternates=%s",
                            e["iri"], e["ne"], r.get("canonical"),
                            r.get("alternates"))
            logger.info("[DRY RUN] previewed %d; wrote nothing.",
                        min(batch_size, len(entities)))
            return

        self._write_csv(entities, opts["out"], batch_size, opts["tier"],
                        opts["changed_only"])

    def _load(self, repo, prefix, entity_type, limit):
        """Collect {iri, type, ne, en} for the matching stored entities."""
        out = []
        offset = 0
        page = 1000
        while True:
            docs = repo.search_entities(
                prefix=prefix, entity_type=entity_type, limit=page, offset=offset
            )
            if not docs:
                break
            for doc in docs:
                ne, en = _name_map(doc)
                if not ne:
                    continue  # nothing to translate without a Devanagari source
                atype = doc.get("@type")
                out.append({
                    "iri": doc.get("@id"),
                    "type": ",".join(atype) if isinstance(atype, list) else atype,
                    "ne": ne,
                    "en": en,
                })
                if limit and len(out) >= limit:
                    return out
            offset += page
        return out

    def _translate_batch(self, batch, tier):
        """Return {iri: {"canonical": str, "alternates": [str]}} for a batch."""
        items = [{"id": i, "ne": e["ne"], "type": e["type"]}
                 for i, e in enumerate(batch)]
        prompt = USER_TMPL.format(
            items=json.dumps(items, ensure_ascii=False)
        )
        try:
            data = invoke_json(SYSTEM, prompt, max_tokens=8000, tier=tier)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM batch failed (%d items): %s", len(batch), exc)
            return {}
        rows = data.get("names") if isinstance(data, dict) else data
        out = {}
        for obj in rows or []:
            idx = obj.get("id")
            if not (isinstance(idx, int) and 0 <= idx < len(batch)):
                continue
            canonical = (obj.get("canonical") or "").strip()
            if _looks_iast_ascii(canonical):
                canonical = ""  # scrub-net: never emit a transliteration
            alts = []
            for a in obj.get("alternates") or []:
                a = (a or "").strip() if isinstance(a, str) else ""
                if a and not _looks_iast_ascii(a) and a != canonical and a not in alts:
                    alts.append(a)
            out[batch[idx]["iri"]] = {"canonical": canonical, "alternates": alts}
        return out

    def _write_csv(self, entities, out_path, batch_size, tier, changed_only):
        n = changed = 0
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["iri", "type", "ne", "old_en", "new_en", "alternates",
                        "changed"])
            for start in range(0, len(entities), batch_size):
                chunk = entities[start:start + batch_size]
                res = self._translate_batch(chunk, tier)
                for e in chunk:
                    r = res.get(e["iri"]) or {"canonical": "", "alternates": []}
                    new_en, alts = r["canonical"], r["alternates"]
                    old_en = e["en"]
                    is_changed = bool(new_en) and (
                        (new_en or None) != (old_en or None) or bool(alts)
                    )
                    n += 1
                    changed += int(is_changed)
                    if is_changed or not changed_only:
                        w.writerow([e["iri"], e["type"], e["ne"], old_en or "",
                                    new_en, json.dumps(alts, ensure_ascii=False),
                                    int(is_changed)])
                if n % 500 < batch_size:
                    logger.info("  %d/%d processed, %d changed", n, len(entities),
                                changed)
        logger.info("DONE: %d processed, %d mutations -> %s", n, changed, out_path)
