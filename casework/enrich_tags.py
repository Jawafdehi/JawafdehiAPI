#!/usr/bin/env python
"""Classify CIAA Special Court cases with tags (DB-free script). LOCAL WRITES ONLY.

Ported from the deleted `casework/enrich_tags.py` (recovered at donor commit
`0321a85`). Rules-first: `classify_case_rules` runs unconditionally from case
METADATA (title, key_allegations, court_cases, description) -- it touches NO
material/evidence text at all. Its only occurrence of the word "evidence" is
the literal tag string "evidence tamper" inside `CORRUPTION_TYPE_KEYWORDS`
(see donor line 473). Because it reads no material, this script never fetches
the case DETAIL endpoint -- the case dicts `iter_cases()`/`select_cases()`
already return over the LIST endpoint carry every field `classify_case_rules`
needs (`CaseSerializer.Meta.fields` includes title/key_allegations/
court_cases/description/bigo/tags on both LIST and DETAIL; only the nested
`evidence[].material` sub-object differs between the two, and this stage
never reads it).

The LLM only tops up when `use_llm` (i.e. NOT `--no-llm`) and the rule pass
produced fewer than 5 tags, at the CHEAP tier -- the only one of the five
ported enrichers that is not "premium" -- with `max_tokens=256`. Output is
constrained to the controlled tag vocabulary in `validate_tags`; a hard floor
of `["CIAA", "Corruption"]` is guaranteed even if everything else gets
filtered out (see `merge_tags`).

`bigo` is OPTIONAL context, not a gate: `_detect_amount_tier` returns `None`
(and the amount-tier tag is simply omitted) when `bigo` is `None`, and
`build_llm_classification_prompt` only mentions bigo when it is not `None`.
`STAGES["tags"].requires_stages == ("bigo",)` orders this stage after `bigo`
so the amount-tier tag benefits from a populated bigo when one is available,
but it is NOT a `requires_fields`/`requires_materials` gate -- a case with an
unknown bigo is tagged fine, exactly as the donor does.

An LLM failure during the top-up attempt does NOT abort the case: the donor
catches the exception, counts it, and falls through to rule-only tags -- the
case still gets classified and (outside `--dry-run`) PATCHed. This mirrors
`stats["cases_llm_error"]` in the donor, which is incremented independently
of `stats["cases_enriched"]`.

Usage:
    python casework/enrich_tags.py --dry-run
    python casework/enrich_tags.py --slug case-0123
    python casework/enrich_tags.py --limit 10 --verbose
    python casework/enrich_tags.py --no-llm --dry-run
    python casework/enrich_tags.py --apply
"""

import argparse
import json
import logging
import os
import re
import sys
from typing import Optional

from casework.common.api import CaseworkApi
from casework.common.cli import add_common_args, print_summary, setup_logging
from casework.common.llm import bootstrap, tier_for
from casework.common.pipeline import STAGES, RunReport, unmet_prerequisites
from casework.common.select import select_cases

log = logging.getLogger("casework.enrich_tags")

STAGE = STAGES["tags"]

# ── Tag taxonomy (verbatim from the donor's `TagEnricher`-derived lists) ────

SECTOR_TAGS = [
    "Local Government",
    "Health",
    "Education",
    "Infrastructure",
    "Land Management",
    "Finance",
    "Agriculture",
    "Energy",
    "Water Supply",
    "Transportation",
    "Telecommunications",
    "Forestry",
    "Tourism",
    "Revenue",
    "Banking",
    "IT",
    "Housing",
    "Construction",
    "Procurement",
    "Public Works",
    "Social Security",
    "Defense",
    "Foreign Affairs",
    "Home Affairs",
    "Law and Justice",
]

CORRUPTION_TYPE_TAGS = [
    "Bribery",
    "Illegal Property Acquisition",
    "Procurement Irregularities",
    "Public Office Abuse",
    "Embezzlement",
    "Forged Documents",
    "Revenue Leakage",
    "Nepotism",
    "Witness Tampering",
    "Bid Rigging",
    "Tax Evasion",
    "Money Laundering",
    "Assets Beyond Known Income",
    "Conflict of Interest",
    "Kickbacks",
]

REGION_TAGS = [
    "Province 1",
    "Madhesh",
    "Bagmati",
    "Gandaki",
    "Lumbini",
    "Karnali",
    "Sudurpashchim",
    "Kathmandu Valley",
    "Kathmandu",
    "Lalitpur",
    "Bhaktapur",
]

CONTEXT_TAGS = [
    "CIAA",
    "Special Court",
    "Supreme Court",
    "Corruption",
]

# Precomputed valid tag set
_VALID_TAGS = frozenset(SECTOR_TAGS + CORRUPTION_TYPE_TAGS + REGION_TAGS + CONTEXT_TAGS)


# ── Rule-based classifiers (verbatim from the donor) ─────────────────────────

SECTOR_KEYWORDS = {
    "Local Government": [
        "municipality",
        "नगरपालिका",
        "local government",
        "स्थानीय",
        "vdc",
        "ward office",
        "वडा",
        "गाउँपालिका",
        "rural municipality",
        "metropolitan",
        "महानगर",
        "उपमहानगर",
        "sub-metropolitan",
    ],
    "Health": [
        "health",
        "स्वास्थ्य",
        "hospital",
        "अस्पताल",
        "medical",
        "चिकित्सा",
        "medicine",
        "औषधि",
        "pharmacy",
        "doctor",
    ],
    "Education": [
        "education",
        "शिक्षा",
        "school",
        "विद्यालय",
        "college",
        "क्याम्पस",
        "university",
        "विश्वविद्यालय",
        "teacher",
        "शिक्षक",
        "campus",
        "student",
        "professor",
        "प्राध्यापक",
    ],
    "Infrastructure": [
        "infrastructure",
        "पूर्वाधार",
        "road",
        "सडक",
        "bridge",
        "पुल",
        "airport",
        "विमानस्थल",
        "highway",
        "राजमार्ग",
    ],
    "Land Management": [
        "land",
        "जग्गा",
        "जमिन",
        "lalita niwas",
        "ललिता निवास",
        "land grab",
        "कित्ता",
        "plot",
        "ropani",
        "रोपनी",
        "land revenue",
        "मालपोत",
        "land registration",
        "survey",
        "नापी",
        "baluwatar",
        "बालुवाटार",
    ],
    "Finance": [
        "finance",
        "वित्त",
        "budget",
        "बजेट",
        "treasury",
        "कोष",
        "financial",
        "financial management",
        "fiscal",
        "audit",
        "लेखापरीक्षण",
    ],
    "Agriculture": [
        "agriculture",
        "कृषि",
        "farming",
        "खेती",
        "irrigation",
        "सिंचाइ",
        "fertilizer",
        "मल",
        "livestock",
        "पशुपालन",
    ],
    "Energy": [
        "energy",
        "ऊर्जा",
        "electricity",
        "विद्युत",
        "hydropower",
        "जलविद्युत",
        "सौर्य",
        "solar",
        "petroleum",
        "तेल",
    ],
    "Water Supply": [
        "water supply",
        "खानेपानी",
        "melamchi",
        "मेलम्ची",
        "drinking water",
        "sewerage",
        "ढल",
        "sanitation",
        "सरसफाइ",
        "water resource",
    ],
    "Transportation": [
        "transport",
        "यातायात",
        "vehicle",
        "सवारी",
        "railway",
        "रेल",
        "truck",
        "ट्रक",
        "airline",
    ],
    "Telecommunications": [
        "telecom",
        "दूरसञ्चार",
        "phone",
        "फोन",
        "internet",
        "इन्टरनेट",
        "ntc",
        "ncell",
        "frequency",
        "spectrum",
    ],
    "Forestry": [
        "forest",
        "वन",
        "timber",
        "काठ",
        "wildlife",
        "वन्यजन्तु",
        "national park",
        "निकुञ्ज",
        "deforestation",
        "logging",
    ],
    "Tourism": [
        "tourism",
        "पर्यटन",
        "hotel",
        "होटल",
        "casino",
        "क्यासिनो",
        "travel",
        "mountaineering",
        "trekking",
    ],
    "Revenue": [
        "revenue",
        "राजस्व",
        "tax",
        "कर",
        "customs",
        "भन्सार",
        "excise",
        "अन्तःशुल्क",
        "value added tax",
        "मूल्य अभिवृद्धि कर",
    ],
    "Banking": [
        "bank",
        "बैंक",
        "cooperative",
        "सहकारी",
        "loan",
        "ऋण",
        "credit",
        "कर्जा",
        "deposit",
        "निक्षेप",
        "microfinance",
        "लघुवित्त",
        "savings",
        "बचत",
    ],
    "IT": [
        "information technology",
        "सूचना प्रविधि",
        "software",
        "computer",
        "कम्प्युटर",
        "digital",
        "डिजिटल",
        "database",
        "server",
    ],
    "Housing": [
        "housing",
        "आवास",
        "apartment",
        "अपार्टमेन्ट",
        "building",
        "भवन",
        "colony",
        "real estate",
        "घरजग्गा",
        "settlement",
    ],
    "Construction": [
        "construction",
        "निर्माण",
        "building",
        "भवन",
        "contractor",
        "ठेकेदार",
        "civil works",
        "structure",
        "bridge construction",
    ],
    "Procurement": [
        "procurement",
        "खरिद",
        "tender",
        "टेन्डर",
        "supply",
        "आपूर्ति",
        "contract",
        "सम्झौता",
        "purchase",
        "bid",
    ],
    "Public Works": [
        "public works",
        "सार्वजनिक निर्माण",
        "public building",
        "government building",
    ],
}

CORRUPTION_TYPE_KEYWORDS = {
    "Bribery": [
        "bribe",
        "bribery",
        "घुस",
        "रिश्वत",
        "pay off",
        "illegal payment",
        "गैरकानूनी भुक्तानी",
        "undue advantage",
    ],
    "Illegal Property Acquisition": [
        "illegal property",
        "अवैध सम्पत्ति",
        "property acquisition",
        "disproportionate assets",
        "source unknown",
        "स्रोत नखुलेको",
        "illegal wealth",
        "अवैध धन",
        "property amassed",
        "benami",
        "बेनामी",
        "land grab",
        "जग्गा कब्जा",
    ],
    "Procurement Irregularities": [
        "procurement irregular",
        "खरिद अनियमितता",
        "tender manipulation",
        "contract violation",
        "supply fraud",
        "fake bill",
        "नक्कली बिल",
        "quality compromise",
        "गुणस्तरहीन",
        "over invoicing",
    ],
    "Public Office Abuse": [
        "abuse of authority",
        "abuse of office",
        "पद दुरुपयोग",
        "अख्तियार दुरुपयोग",
        "misuse of power",
        "misuse of position",
        "power abuse",
        "authority misuse",
        "official misconduct",
        "dereliction of duty",
        "कर्तव्य पालनामा लापरवाही",
    ],
    "Embezzlement": [
        "embezzl",
        "हिनामिना",
        "misappropriat",
        "अपचलन",
        "siphon",
        "fund diversion",
        "रकम अपचलन",
        "defalcation",
        "theft",
        "चोरी",
        "fraudulent transfer",
        "financial irregularity",
    ],
    "Forged Documents": [
        "forged",
        "forgery",
        "किर्ते",
        "fake document",
        "नक्कली",
        "false document",
        "fabricated",
        "बनावटी",
        "counterfeit",
        "tampered document",
        "fake certificate",
        "नक्कली प्रमाणपत्र",
        "forged signature",
    ],
    "Revenue Leakage": [
        "revenue leak",
        "राजस्व चुहावट",
        "tax avoidance",
        "customs fraud",
        "under invoicing",
        "smuggling",
        "तस्करी",
        "duty evasion",
    ],
    "Nepotism": [
        "nepotism",
        "crony",
        "nepot",
        "भाई-भतिजा",
        "कृपा",
        "favouritism",
        "favoritism",
        "relative appointment",
        "आफन्त",
        "nephew",
    ],
    "Witness Tampering": [
        "witness tamper",
        "witness intimidat",
        "साक्षी",
        "गवाह",
        "witness influenc",
        "evidence tamper",
        "प्रमाण",
        "threaten witness",
    ],
    "Bid Rigging": [
        "bid rig",
        "मिलेमतो",
        "collusion",
        "cartel",
        "price fixing",
        "rigged tender",
        "सेटिङ",
    ],
    "Tax Evasion": [
        "tax evasion",
        "tax fraud",
        "कर छली",
        "tax dodge",
        "false tax",
        "undeclared income",
        "hidden income",
    ],
    "Money Laundering": [
        "money launder",
        "सम्पत्ति शुद्धीकरण",
        "black money",
        "कालो धन",
        "hawala",
        "हुन्डी",
        "illicit finance",
        "shell company",
        "offshore",
        "round tripping",
    ],
    "Assets Beyond Known Income": [
        "assets beyond",
        "income source",
        "आयस्रोत",
        "wealth beyond",
        "property disproportionate",
        "सम्पत्ति विवरण",
        "lifestyle audit",
    ],
    "Conflict of Interest": [
        "conflict of interest",
        "स्वार्थ बाझिनु",
        "vested interest",
        "personal interest",
        "निजी स्वार्थ",
        "self-dealing",
    ],
    "Kickbacks": [
        "kickback",
        "commission",
        "कमिशन",
        "percentage cut",
        "दलाली",
        "brokerage",
        "middleman fee",
        "बिचौलिया",
    ],
}

REGION_KEYWORDS = {
    "Province 1": [
        "province 1",
        "कोशी",
        "koshi",
        "birtamod",
        "bhadrapur",
        "dharan",
        "धरान",
        "biratnagar",
        "विराटनगर",
        "illam",
        "इलाम",
        "jhapa",
        "झापा",
        "sunsari",
        "सुनसरी",
        "morang",
        "मोरङ",
        "sankhuwasabha",
        "taplejung",
        "terhathum",
        "bhojpur",
        "भोजपुर",
        "dhankuta",
        "धनकुटा",
        "khotang",
        "खोटाङ",
        "okhaldhunga",
        "solukhumbu",
        "udayapur",
        "उदयपुर",
    ],
    "Madhesh": [
        "madhesh",
        "मधेश",
        "janakpur",
        "जनकपुर",
        "birgunj",
        "वीरगन्ज",
        "saptari",
        "सप्तरी",
        "siraha",
        "सिराहा",
        "dhanusa",
        "धनुषा",
        "mahottari",
        "महोत्तरी",
        "sarlahi",
        "सर्लाही",
        "rautahat",
        "रौतहट",
        "bara",
        "बारा",
        "parsa",
        "पर्सा",
        "rajbiraj",
        "राजविराज",
    ],
    "Bagmati": [
        "bagmati",
        "बागमती",
        "hetauda",
        "हेटौंडा",
        "chitwan",
        "चितवन",
        "makwanpur",
        "मकवानपुर",
        "dhading",
        "धादिङ",
        "nuwakot",
        "नुवाकोट",
        "rasuwa",
        "रसुवा",
        "sindhupalchok",
        "सिन्धुपाल्चोक",
        "dolakha",
        "दोलखा",
        "kavre",
        "काभ्रे",
        "ramechhap",
        "रामेछाप",
        "sindhuli",
        "सिन्धुली",
    ],
    "Gandaki": [
        "gandaki",
        "गण्डकी",
        "pokhara",
        "पोखरा",
        "kaski",
        "कास्की",
        "lamjung",
        "लमजुङ",
        "tanahun",
        "तनहुँ",
        "gorakha",
        "गोरखा",
        "manang",
        "मनाङ",
        "mustang",
        "मुस्ताङ",
        "myagdi",
        "म्याग्दी",
        "parbat",
        "पर्वत",
        "syanja",
        "स्याङ्जा",
        "nawalparasi east",
        "baglung",
        "बागलुङ",
    ],
    "Lumbini": [
        "lumbini",
        "लुम्बिनी",
        "butwal",
        "बुटवल",
        "bhairahawa",
        "rupandehi",
        "रुपन्देही",
        "kapilvastu",
        "कपिलवस्तु",
        "nawalparasi west",
        "palpa",
        "पाल्पा",
        "arghakhanchi",
        "gulmi",
        "गुल्मी",
        "dang",
        "दाङ",
        "pyuthan",
        "प्युठान",
        "rolpa",
        "रोल्पा",
        "banke",
        "बाँके",
        "bardiya",
        "बर्दिया",
    ],
    "Karnali": [
        "karnali",
        "कर्णाली",
        "birendranagar",
        "surkhet",
        "सुर्खेत",
        "dailekh",
        "दैलेख",
        "jajarkot",
        "जाजरकोट",
        "rukum west",
        "salyan",
        "सल्यान",
        "kalikot",
        "कालिकोट",
        "jumla",
        "जुम्ला",
        "humla",
        "हुम्ला",
        "dolpa",
        "डोल्पा",
        "mugu",
        "मुगु",
    ],
    "Sudurpashchim": [
        "sudurpashchim",
        "सुदूरपश्चिम",
        "dhangadhi",
        "धनगढी",
        "kailali",
        "कैलाली",
        "kanchanpur",
        "कञ्चनपुर",
        "doti",
        "डोटी",
        "achham",
        "अछाम",
        "bajhang",
        "बझाङ",
        "bajura",
        "बाजुरा",
        "baitadi",
        "बैतडी",
        "darchula",
        "दार्चुला",
        "dadeldhura",
        "डडेल्धुरा",
    ],
    "Kathmandu": [
        "kathmandu",
        "काठमाडौं",
        "ktm",
        "kathmandu metropolitan",
        "kathmandu city",
        "kathmandu district",
    ],
    "Lalitpur": [
        "lalitpur",
        "ललितपुर",
        "patan",
        "पाटन",
        "godawari",
        "jawalakhel",
        "pulchowk",
    ],
    "Bhaktapur": [
        "bhaktapur",
        "भक्तपुर",
        "bhaktapur municipality",
    ],
    "Kathmandu Valley": [
        "kathmandu valley",
        "काठमाडौं उपत्यका",
    ],
}


def _precompile_keyword_map(keyword_map):
    """Precompile regex patterns for ASCII keywords, non-ASCII kept as literals."""
    compiled = {}
    for tag, keywords in keyword_map.items():
        regexes = []
        literals = []
        for kw in keywords:
            if kw.isascii() and all(c.isascii() for c in kw):
                regexes.append(re.compile(r"\b" + re.escape(kw) + r"\w*\b"))
            else:
                literals.append(kw)
        compiled[tag] = (regexes, literals)
    return compiled


# Precompile keyword maps once
_COMPILED_SECTORS = _precompile_keyword_map(SECTOR_KEYWORDS)
_COMPILED_CORRUPTION = _precompile_keyword_map(CORRUPTION_TYPE_KEYWORDS)
_COMPILED_REGIONS = _precompile_keyword_map(REGION_KEYWORDS)


def _match_keywords(text: str, compiled_map) -> list:
    """Match text against precompiled keyword maps. Returns matched tag names."""
    matched = []
    for tag, (regexes, literals) in compiled_map.items():
        for pat in regexes:
            if pat.search(text):
                matched.append(tag)
                break
        else:
            for kw in literals:
                if kw in text:
                    matched.append(tag)
                    break
    return matched


def _detect_amount_tier(bigo) -> Optional[str]:
    """Return human-readable Nepali amount string, or None if bigo is unknown.

    `bigo` is OPTIONAL context here, not a gate -- this returns `None` (no
    amount-tier tag at all) when `bigo` is `None`, it never raises and never
    invents a placeholder tier.
    """
    if bigo is None:
        return None

    arab = int(bigo // 1_000_000_000)
    remainder = bigo % 1_000_000_000
    crore = int(remainder // 10_000_000)
    remainder = remainder % 10_000_000
    lakh = int(remainder // 100_000)
    remainder = remainder % 100_000
    hazar = int(remainder // 1_000)

    parts = []
    if arab:
        parts.append(f"{arab} Arab")
    if crore:
        parts.append(f"{crore} Crore")
    if lakh and not arab:
        parts.append(f"{lakh} Lakh")
    if hazar and not arab and not crore:
        parts.append(f"{hazar} Hazar")

    if not parts:
        return "Under 1 Hazar"

    return "~" + " ".join(parts)


def _detect_court_context(case: dict) -> list:
    """Detect court context tags (CIAA, Special Court, Supreme Court, Corruption).

    CONCERN (see task-14a-report.md): the donor recognizes "Special Court" /
    "Supreme Court" only when a `court_cases` entry starts with the literal
    `"special:"` / `"supreme:"` prefix. Every court-case fixture in this
    ported project's OWN test suite (e.g. `test_enrich_missing_bigo.py`'s
    `PRESS_CASE_READY`) instead uses the
    `https://jawafdehi.org/courtcase/special/<number>` IRI shape that
    `casework.common.select` parses -- never the donor's colon-prefixed
    form. Ported byte-for-byte from the donor per this task's mandate
    (donor is source of truth); "CIAA" and "Corruption" are unconditional
    and unaffected, but "Special Court"/"Supreme Court" likely never fire
    against real data under this check. Flagged for the dispatcher's
    review rather than silently "fixed" to a guessed format.
    """
    tags = []
    tags.append("CIAA")
    tags.append("Corruption")
    if case.get("court_cases"):
        for cc in case.get("court_cases") or []:
            if isinstance(cc, str):
                if cc.startswith("special:"):
                    tags.append("Special Court")
                elif cc.startswith("supreme:"):
                    tags.append("Supreme Court")
    return tags


def _collect_case_text(case: dict) -> str:
    """Build a searchable text blob from case metadata for keyword matching."""
    parts = [case.get("title") or ""]
    if case.get("key_allegations"):
        parts.extend(case.get("key_allegations") or [])
    if case.get("court_cases"):
        for cc in case.get("court_cases") or []:
            if isinstance(cc, str):
                parts.append(cc)
    if case.get("description"):
        parts.append(case.get("description"))
    return " ".join(parts).lower()


def classify_case_rules(case: dict) -> list:
    """Rule-based tag classification for a CIAA case.

    Reads ONLY case fields (title/key_allegations/court_cases/description/
    bigo) -- no material, no evidence, no network I/O. `_detect_court_context`
    unconditionally appends "CIAA" and "Corruption", so in practice this list
    is never empty for a real case dict.
    """
    text = _collect_case_text(case)
    tags = []

    sectors = _match_keywords(text, _COMPILED_SECTORS)
    tags.extend(sectors[:3])

    corruption_types = _match_keywords(text, _COMPILED_CORRUPTION)
    tags.extend(corruption_types[:3])

    regions = _match_keywords(text, _COMPILED_REGIONS)
    tags.extend(regions[:2])

    amount_tier = _detect_amount_tier(case.get("bigo"))
    if amount_tier is not None:
        tags.append(amount_tier)

    context_tags = _detect_court_context(case)
    tags.extend(context_tags)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    return unique


def validate_tags(tags: list) -> list:
    """Filter tags to only include valid controlled vocabulary and deduplicate.

    Amount tier tags (~X Crore Y Lakh etc.) are dynamic and always pass through.
    """
    valid = []
    seen = set()
    for t in tags:
        is_amount = t.startswith("~") or t == "Under 1 Hazar"
        if (t in _VALID_TAGS or is_amount) and t not in seen:
            seen.add(t)
            valid.append(t)
    return valid


def merge_tags(rule_tags: list, llm_tags: list) -> list:
    """Merge rule-based and LLM-topped-up tags into the final tag list.

    Extracted from the donor's inline `_process_case` merge logic:
    `dict.fromkeys(rule_tags + llm_tags)` (dedup, rule_tags first) ->
    `validate_tags` (controlled-vocabulary filter, amount tiers always pass)
    -> the hard `["CIAA", "Corruption"]` floor when nothing survives.
    `classify_case_rules` already includes CIAA/Corruption unconditionally
    (via `_detect_court_context`), so in real runs the floor here is a
    defensive backstop, exactly as it is in the donor -- it exists to catch
    `validate_tags` somehow stripping everything, not to paper over a rule
    pass that forgot the floor.
    """
    merged = list(dict.fromkeys(list(rule_tags) + list(llm_tags)))
    validated = validate_tags(merged)
    return validated if validated else ["CIAA", "Corruption"]


def _build_tag_selection_instructions() -> str:
    """Build the common tag selection instructions for LLM prompts."""
    lines = []
    lines.append("Select the most appropriate tags from each category:")
    lines.append("")
    lines.append(f"Sector (choose 1-3): {', '.join(SECTOR_TAGS)}")
    lines.append(f"Corruption Type (choose 1-3): {', '.join(CORRUPTION_TYPE_TAGS)}")
    lines.append(f"Region (choose 1-2): {', '.join(REGION_TAGS)}")
    lines.append("")
    lines.append("Always include: CIAA, Corruption")
    lines.append("")
    lines.append("Return ONLY a JSON array of tag strings, nothing else.")
    lines.append(
        'Example: ["CIAA", "Corruption", "Local Government", "Bribery", "Kathmandu Valley"]'
    )
    return "\n".join(lines)


def build_llm_classification_prompt(case: dict) -> str:
    """Build a prompt for LLM-based tag classification from case metadata."""
    lines = []
    lines.append(
        "Classify the following Nepal corruption case with tags "
        "from the controlled vocabulary below."
    )
    lines.append("")
    lines.append(f"Case Title: {case.get('title', '')}")
    if case.get("key_allegations"):
        lines.append("Key Allegations:")
        for a in case.get("key_allegations") or []:
            lines.append(f"  - {a}")
    if case.get("court_cases"):
        lines.append(
            "Court Cases: "
            + ", ".join(c for c in case.get("court_cases") or [] if isinstance(c, str))
        )
    if case.get("bigo") is not None:
        lines.append(f"Bigo (Disputed Amount): NPR {case.get('bigo'):,}")
    lines.append("")
    lines.append(_build_tag_selection_instructions())
    return "\n".join(lines)


def parse_llm_response(response: str) -> list:
    """Parse LLM response to extract a JSON list of tags."""
    response = response.strip()
    if response.startswith("```"):
        lines = response.split("\n")
        response = "\n".join(line for line in lines if not line.startswith("```"))
        response = response.strip()

    for match in re.finditer(r"\[[^\]]*\]", response):
        try:
            tags = json.loads(match.group())
            if isinstance(tags, list) and all(isinstance(t, str) for t in tags):
                return tags
        except json.JSONDecodeError:
            continue
    return []


def build_api(args):
    """Construct the client. Basic (local DEV_AUTH) unless a token is given."""
    if args.api_token:
        return CaseworkApi(args.api_base_url, token=args.api_token)
    return CaseworkApi(
        args.api_base_url,
        basic=(os.getenv("CASEWORK_API_USER", "abgen"),
               os.getenv("CASEWORK_API_PASSWORD", "local-dev-only")),
    )


def main(argv=None):
    """Main entry point."""
    ap = argparse.ArgumentParser(
        description="Classify CIAA cases with tags via rule-based + LLM classification (DB-free).",
        epilog="Reads/writes cases entirely over the Jawafdehi HTTP API.",
    )
    add_common_args(ap)
    ap.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip LLM classification, use rules only",
    )
    args = ap.parse_args(argv)

    setup_logging(args.verbose)

    # Bootstrap Django + LLM (MUST come before importing llm.invoke)
    try:
        bootstrap(args.provider, args.model)
    except Exception as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        sys.exit(1)

    from llm.invoke import invoke_text
    from llm.usage import UsageAccumulator, render_usage_table

    api = build_api(args)
    usage = UsageAccumulator()
    report = RunReport()

    all_cases = list(api.iter_cases())
    cases = select_cases(
        all_cases,
        fiscal_year=args.fiscal_year,
        slugs=args.slug,
        court_cases=args.court_case,
    )
    if args.limit:
        cases = cases[: args.limit]

    total = len(cases)
    if total == 0:
        print("No matching CIAA case(s) to process.", file=sys.stderr)
        print_summary(report.summary(), args.dry_run, "Tag classification")
        return report

    print(f"Found {total} matching case(s).")
    if args.force:
        print("  --force: re-tagging even for populated cases")
    if args.no_llm:
        print("  --no-llm: using rule-based classification only")
    if args.dry_run:
        print("  [DRY RUN] No changes will be saved.")

    for idx, case in enumerate(cases, 1):
        slug = case.get("slug") or "?"
        print(f"\n[{idx}/{total}] {slug} — {(case.get('title') or '')[:80]}")

        if case.get("tags") and not args.force:
            report.record(slug, "tags", "already", f"tags already {case['tags']}")
            print("  tags already populated — skipping (use --force to re-tag)")
            continue

        # tags reads no material (see module docstring) -- STAGE's
        # requires_materials/requires_fields are both empty, so this is
        # always []. Called anyway for structural parity with the other
        # ported stages and in case a future STAGES["tags"] edit adds a
        # real prerequisite.
        unmet = unmet_prerequisites(STAGE, case)
        if unmet:
            for reason in unmet:
                report.record(slug, "tags", "unmet", reason)
            print(f"  Unmet prerequisite(s): {'; '.join(unmet)}")
            continue

        rule_tags = classify_case_rules(case)
        log.debug("  Rule-based tags: %s", rule_tags)

        llm_tags: list = []
        tier = "rule_based"

        if not args.no_llm and len(rule_tags) < 5:
            log.info("  Attempting metadata_llm classification...")
            try:
                prompt = build_llm_classification_prompt(case)
                response = invoke_text(
                    system="You are a Nepali corruption case tag classifier.",
                    content=prompt,
                    max_tokens=256,
                    tier=tier_for("tags"),
                    usage=usage,
                )
                parsed = parse_llm_response(response)
                if parsed:
                    new_tags = list(dict.fromkeys(rule_tags + parsed))
                    if len(new_tags) > len(rule_tags):
                        llm_tags = parsed
                        tier = "metadata_llm"
                        log.info("  + metadata_llm succeeded: %d tags", len(new_tags))
                    else:
                        log.debug("  - metadata_llm returned no new tags")
                else:
                    log.debug("  - metadata_llm returned no tags")
            except Exception as exc:
                # An LLM failure does NOT abort this case -- it falls
                # through to rule-only tags, exactly as the donor's
                # `stats["cases_llm_error"]` is tracked independently of
                # `stats["cases_enriched"]`.
                report.record(slug, "tags", "llm-error", f"metadata_llm failed: {exc}")
                print(f"  - metadata_llm failed: {str(exc)[:120]}")
                if args.verbose:
                    import traceback

                    traceback.print_exc()

        all_tags = merge_tags(rule_tags, llm_tags)

        print(f"  Classified {len(all_tags)} tag(s) ({tier})")
        for i, tag in enumerate(all_tags[:5], 1):
            print(f"    {i}. {tag}")
        if len(all_tags) > 5:
            print(f"    ... and {len(all_tags) - 5} more")

        if args.dry_run:
            report.record(slug, "tags", "would-enrich", f"tags={all_tags} tier={tier}")
            print("  [DRY RUN] Would PATCH but --dry-run is set")
            continue

        try:
            api.patch_field(slug, "tags", all_tags)
            report.record(slug, "tags", "enriched", f"tags={all_tags} tier={tier}")
            print(f"  [UPDATED] {slug}")
        except Exception as exc:
            report.record(slug, "tags", "error", f"PATCH failed: {exc}")
            print(f"  Failed to PATCH tags: {exc}")

    stats = report.summary()
    print_summary(stats, args.dry_run, "Tag classification")
    unmet_reasons = report.unmet_reasons()
    if unmet_reasons:
        print("  unmet reasons:")
        for reason, count in unmet_reasons.most_common():
            print(f"    {count} x {reason}")

    if usage.calls > 0:
        print()
        print(render_usage_table(usage.as_dict()["by_provider"], title="tags usage"))

    return report


if __name__ == "__main__":
    main()
