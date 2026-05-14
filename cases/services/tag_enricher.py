"""Service for rule-based and LLM-based tag classification of CIAA cases."""

import logging
import re

from django.conf import settings
from cases.models import Case, DocumentSource, DocumentSourceUpload

logger = logging.getLogger(__name__)

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

AMOUNT_TIER_TAGS = [
    "Under 1M NPR",
    "1M-10M NPR",
    "10M-100M NPR",
    "100M-1B NPR",
    "Over 1B NPR",
    "Unknown Amount",
]

CONTEXT_TAGS = [
    "CIAA",
    "Special Court",
    "Supreme Court",
    "Corruption",
]

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
        "power",
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
        "bus",
        "बस",
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
        "vat",
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
        "interest",
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
        "kickback",
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
        "forg",
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
        "tax evasion",
        "tax avoidance",
        "customs fraud",
        "under invoicing",
        "smuggling",
        "तस्करी",
        "duty evasion",
        "tax fraud",
    ],
    "Nepotism": [
        "nepotism",
        "crony",
        "nepot",
        "भाई-भतिजा",
        "कृपा",
        "favouritism",
        "favoritism",
        "nepot",
        "relative appointment",
        "आफन्त",
        "nephew",
        "nati",
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
        "mil",
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
        "birtanagar",
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
        "tapurjung",
        "terhathum",
        "bhojpur",
        "भोजपुर",
        "dhankuta",
        "धनकुटा",
        "khotang",
        "खोटाङ",
        "okhaldhunga",
        "solukhumbu",
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
        "dailekha",
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
        "valley",
    ],
}


def _collect_case_text(case: Case) -> str:
    """Build a searchable text blob from a case for keyword matching."""
    parts = [case.title or ""]
    if case.key_allegations:
        parts.extend(case.key_allegations)
    if case.court_cases:
        for cc in case.court_cases:
            if isinstance(cc, str):
                parts.append(cc)
    if case.description:
        parts.append(case.description)
    return " ".join(parts).lower()


def _collect_evidence_text(case: Case) -> str:
    """Build a text blob from evidence entries, using actual file content when available.

    Priority:
    1. DocumentSource uploaded_file content (converted via MarkItDown)
    2. DocumentSourceUpload file content (converted via MarkItDown)
    3. DocumentSource title + description (metadata)
    4. Evidence entry description
    """
    parts = []
    if not case.evidence:
        return ""

    source_ids = []
    for entry in case.evidence:
        if isinstance(entry, dict):
            desc = entry.get("description", "")
            if desc:
                parts.append(desc)
            sid = entry.get("source_id", "")
            if sid:
                source_ids.append(sid)

    if not source_ids:
        return " ".join(parts)

    try:
        sources = DocumentSource.objects.filter(source_id__in=source_ids)
    except Exception as e:
        logger.warning(f"Failed to fetch DocumentSource records for {case.case_id}: {e}")
        return " ".join(parts)

    for src in sources:
        file_text = _convert_source_file(src)
        if file_text:
            parts.append(file_text)
        else:
            if src.title:
                parts.append(src.title)
            if src.description:
                parts.append(src.description)

    return " ".join(parts)


def _convert_source_file(src: DocumentSource) -> str:
    """Convert a DocumentSource's uploaded file(s) to text using MarkItDown.

    Returns file content as markdown text, or empty string if conversion fails.
    """
    try:
        from markitdown import MarkItDown
    except ImportError:
        logger.warning("markitdown not installed, skipping file conversion")
        return ""

    md_converter = MarkItDown()

    files_to_try = []
    if src.uploaded_file and hasattr(src.uploaded_file, "path"):
        files_to_try.append(src.uploaded_file.path)

    try:
        uploads = DocumentSourceUpload.objects.filter(source_id=src.id)
        for upload in uploads:
            if upload.file and hasattr(upload.file, "path"):
                files_to_try.append(upload.file.path)
    except Exception:
        pass

    for filepath in files_to_try:
        try:
            result = md_converter.convert(filepath)
            if result and result.text_content:
                return result.text_content[:16000]
        except Exception as e:
            logger.debug(f"MarkItDown conversion failed for {filepath}: {e}")
            continue

    return ""


def _match_keywords(text: str, keyword_map: dict[str, list[str]]) -> list[str]:
    """Match text against keyword maps. Returns matched tag names."""
    matched = []
    for tag, keywords in keyword_map.items():
        for kw in keywords:
            if kw in text:
                matched.append(tag)
                break
    return matched


def _detect_amount_tier(bigo) -> str:
    if bigo is None:
        return "Unknown Amount"
    if bigo < 1_000_000:
        return "Under 1M NPR"
    if bigo < 10_000_000:
        return "1M-10M NPR"
    if bigo < 100_000_000:
        return "10M-100M NPR"
    if bigo < 1_000_000_000:
        return "100M-1B NPR"
    return "Over 1B NPR"


def _detect_court_context(case: Case) -> list[str]:
    tags = []
    tags.append("CIAA")
    tags.append("Corruption")
    if case.court_cases:
        for cc in case.court_cases:
            if isinstance(cc, str):
                if cc.startswith("special:"):
                    tags.append("Special Court")
                elif cc.startswith("supreme:"):
                    tags.append("Supreme Court")
    return tags


def classify_case_rules(case: Case) -> list[str]:
    """Rule-based tag classification for a CIAA case."""
    text = _collect_case_text(case)
    tags = []

    sectors = _match_keywords(text, SECTOR_KEYWORDS)
    tags.extend(sectors[:3])

    corruption_types = _match_keywords(text, CORRUPTION_TYPE_KEYWORDS)
    tags.extend(corruption_types[:3])

    regions = _match_keywords(text, REGION_KEYWORDS)
    tags.extend(regions[:2])

    amount_tier = _detect_amount_tier(case.bigo)
    tags.append(amount_tier)

    context_tags = _detect_court_context(case)
    tags.extend(context_tags)

    seen = set()
    unique = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    return unique


def build_llm_classification_prompt(case: Case) -> str:
    """Build a prompt for LLM-based tag classification from case metadata."""
    lines = []
    lines.append(
        "Classify the following Nepal corruption case with tags from the controlled vocabulary below."
    )
    lines.append("")
    lines.append(f"Case Title: {case.title}")
    if case.key_allegations:
        lines.append("Key Allegations:")
        for a in case.key_allegations:
            lines.append(f"  - {a}")
    if case.court_cases:
        lines.append(
            "Court Cases: "
            + ", ".join(c for c in case.court_cases if isinstance(c, str))
        )
    if case.bigo is not None:
        lines.append(f"Bigo (Disputed Amount): NPR {case.bigo:,}")
    lines.append("")
    lines.append("Select the most appropriate tags from each category:")
    lines.append("")
    lines.append(f"Sector (choose 1-3): {', '.join(SECTOR_TAGS)}")
    lines.append(f"Corruption Type (choose 1-3): {', '.join(CORRUPTION_TYPE_TAGS)}")
    lines.append(f"Region (choose 1-2): {', '.join(REGION_TAGS)}")
    lines.append(f"Amount Tier (choose 1): {', '.join(AMOUNT_TIER_TAGS)}")
    lines.append("")
    lines.append("Always include: CIAA, Corruption, Special Court")
    lines.append("")
    lines.append("Return ONLY a JSON array of tag strings, nothing else.")
    lines.append(
        'Example: ["CIAA", "Corruption", "Special Court", "Local Government", "Bribery", "Kathmandu Valley", "10M-100M NPR"]'
    )
    return "\n".join(lines)


def build_llm_classification_prompt_from_sources(
    case: Case, evidence_text: str
) -> str:
    """Build a prompt for LLM-based tag classification using source documents."""
    lines = []
    lines.append(
        "Classify the following Nepal corruption case with tags from the controlled vocabulary below."
    )
    lines.append("Use the source documents (press releases, court orders) as the primary evidence.")
    lines.append("")
    lines.append(f"Case Title: {case.title}")
    lines.append("")
    lines.append("Source Documents (press releases, court orders, evidence):")
    truncated = evidence_text[:8000]
    if len(evidence_text) > 8000:
        truncated += " [truncated]"
    lines.append(truncated)
    lines.append("")
    if case.court_cases:
        lines.append(
            "Court Cases: "
            + ", ".join(c for c in case.court_cases if isinstance(c, str))
        )
    if case.bigo is not None:
        lines.append(f"Bigo (Disputed Amount): NPR {case.bigo:,}")
    lines.append("")
    lines.append("Select the most appropriate tags from each category:")
    lines.append("")
    lines.append(f"Sector (choose 1-3): {', '.join(SECTOR_TAGS)}")
    lines.append(f"Corruption Type (choose 1-3): {', '.join(CORRUPTION_TYPE_TAGS)}")
    lines.append(f"Region (choose 1-2): {', '.join(REGION_TAGS)}")
    lines.append(f"Amount Tier (choose 1): {', '.join(AMOUNT_TIER_TAGS)}")
    lines.append("")
    lines.append("Always include: CIAA, Corruption, Special Court")
    lines.append("")
    lines.append("Return ONLY a JSON array of tag strings, nothing else.")
    lines.append(
        'Example: ["CIAA", "Corruption", "Special Court", "Local Government", "Bribery", "Kathmandu Valley", "10M-100M NPR"]'
    )
    return "\n".join(lines)


def parse_llm_response(response: str) -> list[str]:
    """Parse LLM response to extract a JSON list of tags."""
    response = response.strip()
    if response.startswith("```"):
        lines = response.split("\n")
        response = "\n".join(line for line in lines if not line.startswith("```"))
        response = response.strip()

    import json

    json_match = re.search(r"\[.*?\]", response, re.DOTALL)
    if json_match:
        try:
            tags = json.loads(json_match.group())
            if isinstance(tags, list) and all(isinstance(t, str) for t in tags):
                return tags
        except json.JSONDecodeError:
            pass
    return []


def validate_tags(tags: list[str]) -> list[str]:
    """Filter tags to only include valid controlled vocabulary and deduplicate."""
    all_valid = set()
    for tag_list in [
        SECTOR_TAGS,
        CORRUPTION_TYPE_TAGS,
        REGION_TAGS,
        AMOUNT_TIER_TAGS,
        CONTEXT_TAGS,
    ]:
        all_valid.update(tag_list)

    valid = []
    seen = set()
    for t in tags:
        if t in all_valid and t not in seen:
            seen.add(t)
            valid.append(t)
    return valid


class TagEnricher:
    """Service for enriching CIAA cases with tags via source documents + rule-based + LLM classification.

    Three-tier pipeline per case:
    1. Primary: LLM classification from source documents (evidence + DocumentSource)
    2. Fallback: Rule-based keyword matching on case metadata
    3. Default: Core tags only (CIAA, Corruption, Special Court)
    """

    def __init__(self, use_llm: bool = True):
        self.use_llm = use_llm
        self._llm_service = None

    def enrich_case(self, case: Case, force: bool = False) -> dict:
        """Enrich a single case with tags. Returns dict with status, tags, and tier."""
        if case.tags and len(case.tags) > 0 and not force:
            return {
                "status": "skipped",
                "tags": case.tags,
                "tier": "already_tagged",
                "reason": "already has tags",
            }

        evidence_text = _collect_evidence_text(case)
        has_evidence = bool(evidence_text.strip())

        if self.use_llm and has_evidence:
            try:
                llm_tags = self._classify_with_llm_from_sources(case, evidence_text)
                if llm_tags and len(llm_tags) >= 3:
                    all_tags = list(dict.fromkeys(llm_tags))
                    all_tags = validate_tags(all_tags)
                    return {"status": "enriched", "tags": all_tags, "tier": "source_llm", "reason": ""}
            except Exception as e:
                logger.warning(
                    f"Source-based LLM classification failed for {case.case_id}: {e}"
                )

        tags = classify_case_rules(case)

        if self.use_llm and len(tags) < 5:
            try:
                llm_tags = self._classify_with_llm(case)
                if llm_tags:
                    all_tags = list(dict.fromkeys(tags + llm_tags))
                else:
                    all_tags = tags
            except Exception as e:
                logger.warning(f"LLM classification failed for {case.case_id}: {e}")
                all_tags = tags
        else:
            all_tags = tags

        all_tags = validate_tags(all_tags)

        if not all_tags:
            all_tags = ["CIAA", "Corruption", "Special Court"]

        tier = "rule_based" if not has_evidence else "metadata_llm"
        return {"status": "enriched", "tags": all_tags, "tier": tier, "reason": ""}

    def _classify_with_llm(self, case: Case) -> list[str]:
        """Use LLM to classify a case from metadata. Returns list of tag strings."""
        if self._llm_service is None:
            from caseworker.services import LLMService

            self._llm_service = LLMService()

        prompt = build_llm_classification_prompt(case)
        llm = self._llm_service.get_llm()
        response = self._llm_service._call_llm(llm, prompt)
        return parse_llm_response(response)

    def _classify_with_llm_from_sources(
        self, case: Case, evidence_text: str
    ) -> list[str]:
        """Use LLM to classify a case from source documents. Returns list of tag strings."""
        if self._llm_service is None:
            from caseworker.services import LLMService

            self._llm_service = LLMService()

        prompt = build_llm_classification_prompt_from_sources(case, evidence_text)
        llm = self._llm_service.get_llm()
        response = self._llm_service._call_llm(llm, prompt)
        return parse_llm_response(response)

    def enrich_cases(self, cases, force: bool = False, dry_run: bool = False) -> dict:
        """Enrich multiple cases. Returns stats dict."""
        stats = {"total": 0, "enriched": 0, "skipped": 0, "failed": 0,
                 "source_llm": 0, "metadata_llm": 0, "rule_based": 0}
        for case in cases:
            stats["total"] += 1
            try:
                result = self.enrich_case(case, force=force)
                if result["status"] == "skipped":
                    stats["skipped"] += 1
                    logger.debug(f"Skipped {case.case_id}: {result['reason']}")
                else:
                    stats["enriched"] += 1
                    tier = result.get("tier", "unknown")
                    if tier in stats:
                        stats[tier] += 1
                    if not dry_run:
                        case.tags = result["tags"]
                        case.save(update_fields=["tags", "updated_at"])
                    logger.info(
                        f"Enriched {case.case_id} ({tier}): {result['tags']}"
                    )
            except Exception as e:
                stats["failed"] += 1
                logger.error(f"Failed to enrich {case.case_id}: {e}")
        return stats
