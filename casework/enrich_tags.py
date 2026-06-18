#!/usr/bin/env python
"""Enrich CIAA Special Court cases with tags (DB-free script using the llm package).

Standalone script to classify CIAA cases with tag categories (sector, corruption type,
region, amount tier, context) via rule-based detection and optional LLM classification.

Phase A.3 of the CIAA Case Enrichment pipeline. Populates ``Case.tags`` with sector,
corruption type, region, amount tier, and context tags using the controlled vocabulary.

Usage:
    python casework/enrich_tags.py --dry-run
    python casework/enrich_tags.py --slug case-0123
    python casework/enrich_tags.py --limit 10 --verbose
    python casework/enrich_tags.py --fiscal-year 080 --no-llm --dry-run
    python casework/enrich_tags.py --force
"""

import argparse
import logging
import os
import re
import sys
from typing import Optional

# Ensure the api dir is in sys.path so imports work when run as a file
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from casework.common import (
    CaseworkApi,
    add_common_args,
    bootstrap,
    get_target_cases,
    setup_logging,
)

logger = logging.getLogger(__name__)

# ── Tag taxonomy (from TagEnricher) ──────────────────────────────────────────

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


# ── Rule-based classifiers (from TagEnricher) ────────────────────────────────

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
    """Return human-readable Nepali amount string, or None if bigo is unknown."""
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
    """Detect court context tags (CIAA, Special Court, Supreme Court, Corruption)."""
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
    """Rule-based tag classification for a CIAA case."""
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

    import json

    for match in re.finditer(r"\[[^\]]*\]", response):
        try:
            tags = json.loads(match.group())
            if isinstance(tags, list) and all(isinstance(t, str) for t in tags):
                return tags
        except json.JSONDecodeError:
            continue
    return []


def main():
    """Main entry point."""
    ap = argparse.ArgumentParser(
        description="Enrich CIAA cases with tags via rule-based + LLM classification (DB-free).",
        epilog="Reads cases and writes results entirely over HTTP via JAWAFDEHI_API_TOKEN.",
    )
    add_common_args(ap)
    ap.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip LLM classification, use rules only",
    )

    args = ap.parse_args()

    # Set up logging
    setup_logging(args.verbose)

    # Bootstrap Django + LLM (MUST come before importing llm)
    try:
        bootstrap(args.provider, args.model)
    except Exception as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # Import after bootstrap
    from llm.invoke import invoke_text
    from llm.usage import UsageAccumulator, render_usage_table

    # Validate config
    try:
        api = CaseworkApi(base_url=args.api_base_url, token=args.api_token)
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Accumulate token usage
    usage = UsageAccumulator()

    # Collect target cases
    cases = list(get_target_cases(api, args, skip_field="tags"))

    total = len(cases)
    if total == 0:
        print("No CIAA draft cases to process.", file=sys.stderr)
        sys.exit(0)

    print(f"Found {total} CIAA draft case(s) to process.")
    if args.force:
        print("  --force: re-tagging even for populated cases")
    if args.no_llm:
        print("  --no-llm: using rule-based classification only")
    if args.dry_run:
        print("  [DRY RUN] No changes will be saved.")

    stats = {
        "cases_processed": 0,
        "cases_enriched": 0,
        "cases_skipped": 0,
        "rule_based": 0,
        "metadata_llm": 0,
        "cases_llm_error": 0,
    }

    # Process each case
    for idx, case in enumerate(cases, 1):
        try:
            _process_case(
                case=case,
                idx=idx,
                total=total,
                dry_run=args.dry_run,
                use_llm=not args.no_llm,
                api=api,
                invoke_text=invoke_text,
                usage=usage,
                stats=stats,
            )
        except Exception as exc:
            stats["cases_llm_error"] += 1
            print(f"Unhandled error processing case: {exc}", file=sys.stderr)
            if args.verbose:
                import traceback

                traceback.print_exc()

    # Print summary
    from casework.common import print_summary

    print_summary(stats, args.dry_run, "Tag enrichment")

    # Print usage table
    if usage.calls > 0:
        print()
        print(render_usage_table(usage.as_dict()["by_provider"], title="tags usage"))


def _process_case(
    case: dict,
    idx: int,
    total: int,
    dry_run: bool,
    use_llm: bool,
    api: CaseworkApi,
    invoke_text,
    usage,
    stats: dict,
):
    """Process a single case: classify tags, PATCH or preview."""
    stats["cases_processed"] += 1
    case_id = case.get("case_id", "?")
    slug = case.get("slug", case_id)
    title = case.get("title", "")
    print(f"\n[{idx}/{total}] {case_id} — {title[:80]}")

    # Rule-based classification
    rule_tags = classify_case_rules(case)
    logger.debug(f"  Rule-based tags: {rule_tags}")

    all_tags = rule_tags
    tier = "rule_based"

    # Optional LLM classification
    if use_llm and len(rule_tags) < 5:
        logger.info("  Attempting metadata_llm classification...")
        try:
            prompt = build_llm_classification_prompt(case)
            response = invoke_text(
                system="You are a Nepali corruption case tag classifier.",
                content=prompt,
                max_tokens=256,
                tier="cheap",
                usage=usage,
            )
            llm_tags = parse_llm_response(response)
            if llm_tags:
                new_tags = list(dict.fromkeys(rule_tags + llm_tags))
                if len(new_tags) > len(rule_tags):
                    all_tags = new_tags
                    tier = "metadata_llm"
                    logger.info(f"  + metadata_llm succeeded: {len(all_tags)} tags")
                else:
                    logger.debug("  - metadata_llm returned no new tags")
            else:
                logger.debug("  - metadata_llm returned no tags")
        except Exception as exc:
            stats["cases_llm_error"] += 1
            logger.warning(f"  - metadata_llm failed: {str(exc)[:120]}")

    # Validate and finalize
    all_tags = validate_tags(all_tags)
    if not all_tags:
        all_tags = ["CIAA", "Corruption"]

    print(f"  Classified {len(all_tags)} tag(s) ({tier})")
    for i, tag in enumerate(all_tags[:5], 1):
        print(f"    {i}. {tag}")
    if len(all_tags) > 5:
        print(f"    ... and {len(all_tags) - 5} more")

    if dry_run:
        print("  [DRY RUN] Would PATCH but --dry-run is set")
        return

    # PATCH the tags
    try:
        api.patch_field(slug, "tags", all_tags)
        stats["cases_enriched"] += 1
        stats[tier] += 1
        print(f"  [UPDATED] {case_id}")
    except Exception as exc:
        stats["cases_llm_error"] += 1
        logger.error(f"  Failed to PATCH tags: {exc}")


if __name__ == "__main__":
    main()
