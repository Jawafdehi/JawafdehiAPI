"""Seed the case tag controlled vocabulary.

GENERATED from ``jawafdehi-meta/work/2026-08-21-tag-crowding/vocabulary.yml``, which is
itself a verified transcription of ``management/policies/case-tagging/policy.md`` at
commit ``67ad845`` — every slug and both labels copied from §4.1 (status ladder), §5.1
and §5.3 (verdict), §6.1 (nature), §6.2 (pre-prosecution states), §6.3 (arbitration),
§8.1 (offence), §8.2 (sector) and §8.3 (governance level). The transcription was checked
term by term against the source: 56 terms, 31 from markdown tables and 25 from inline
prose, **0 mismatches**.

Nothing here is authored. That distinction matters: transcribing a table that exists is
safe, whereas inventing a Nepali label is not — policy §7.4 records three labels chosen
against attested usage in our own published case texts (स्रोत नखुलेको सम्पत्ति आर्जन ×40 vs
अवैध ×2; घुस ×67 vs घूस ×0), which is measured editorial judgement and not something to
infer.

**ALIASES ARE DELIBERATELY EMPTY.** ``policy.md`` contains none, and no mechanical rule
produces them — nothing derives ``एनसेल`` → ``ncell``. They arrive as reviewable
``TagProposal`` rows and become ``TagAlias`` rows only when a human ticks them. Until
then the resolver canonicalises nothing and returns ``None``, which is correct: an
unresolved value is better than a silently wrong mapping.

TWO DIVERGENCES FROM policy.md AS WRITTEN, both decided 2026-08-23 (Ashwini, who holds
this — there is no separate editorial board to wait on). Recorded here because a reader
comparing this migration against the policy document will otherwise think it is a
transcription bug:

1. ``offence.min_per_case`` is **0**, where §3 writes 1. §3 required at least one offence
   tag per case while §8.1's 18 terms may not cover every case, so as written a case
   fitting none could not legally be saved — a contradiction, not an ambiguity. Resolved
   by dropping the floor rather than adding an ``unclassified`` term, which would have
   become an attractor: both the model and a hurried caseworker would reach for it
   instead of choosing, and it would end up the largest bucket.

2. There is **no nickname axis**, where §8.7 defines one (0–2, free text). Every live
   nickname value matches one or two cases, so it filtered nothing — a chip that matches
   a single case is a search result, not a filter — and as the only free-text axis it was
   the path of least resistance for anything that would not fit a controlled axis, which
   is precisely how ``case-template.md:106`` produced 144 tags. Nicknames move to a
   ``case_aliases`` field indexed into the search document's ``aliases`` (design.md §5,
   ranked above body text by §9), so recall survives. This answers §13 open question 6.

One further caveat: ``nature``'s 0–1 bound is **inferred**, not transcribed. §6.1 calls
nature "an ordinary (non-highlighted) tag" but §3's count table omits it entirely; 0–1
follows from a case running on one track. It is the only bound in this file not taken
from policy.
"""

from typing import Any

from django.db import migrations

AXES = [
    {
        "id": "status",
        "label_ne": "स्थिति",
        "label_en": "Status",
        "min_per_case": 1,
        "max_per_case": 1,
        "highlighted": True,
        "members": "enumerated",
        "set_by": "court-data",
        "sort_order": 10,
        "note": "§6.2 pre-prosecution and §6.3 arbitration states sit on this same axis, so one status field serves every case. Those two groups cannot be derived from court data and must be caseworker-set.",
    },
    {
        "id": "verdict",
        "label_ne": "फैसला",
        "label_en": "Verdict",
        "min_per_case": 0,
        "max_per_case": 1,
        "highlighted": True,
        "members": "enumerated",
        "set_by": "entity-outcomes",
        "sort_order": 20,
        "note": "§5.2 — never on a विचाराधीन case. A blank verdict is more honest than a defaulted सफाइ.",
    },
    {
        "id": "offence",
        "label_ne": "कसुरको प्रकृति",
        "label_en": "Offence",
        "min_per_case": 0,
        "max_per_case": 3,
        "highlighted": False,
        "members": "enumerated",
        "set_by": "",
        "sort_order": 30,
        "note": "",
    },
    {
        "id": "sector",
        "label_ne": "क्षेत्र",
        "label_en": "Sector",
        "min_per_case": 0,
        "max_per_case": 2,
        "highlighted": False,
        "members": "enumerated",
        "set_by": "",
        "sort_order": 40,
        "note": "",
    },
    {
        "id": "governance_level",
        "label_ne": "शासन तह",
        "label_en": "Governance level",
        "min_per_case": 0,
        "max_per_case": 1,
        "highlighted": False,
        "members": "enumerated",
        "set_by": "",
        "sort_order": 50,
        "note": "",
    },
    {
        "id": "nature",
        "label_ne": "प्रकृति",
        "label_en": "Nature",
        "min_per_case": 0,
        "max_per_case": 1,
        "highlighted": False,
        "members": "enumerated",
        "set_by": "",
        "sort_order": 60,
        "note": '§6.1 calls nature "an ordinary (non-highlighted) tag" but §3\'s count table omits it. 0-1 is INFERRED (a case runs on one track) and needs confirming — the only bound in this file not taken from §3.',
    },
    {
        "id": "institution",
        "label_ne": "निकाय",
        "label_en": "Institution",
        "min_per_case": 0,
        "max_per_case": 3,
        "highlighted": False,
        "members": "entities",
        "set_by": "",
        "sort_order": 70,
        "note": "§8.4 — named public bodies. `CIAA` and `Special Court` are explicitly NOT institution tags (§9): the CIAA is the filer in nearly every case and the Special Court the venue, so neither discriminates.",
    },
    {
        "id": "geography",
        "label_ne": "भौगोलिक क्षेत्र",
        "label_en": "Geography",
        "min_per_case": 0,
        "max_per_case": 3,
        "highlighted": False,
        "members": "external",
        "set_by": "",
        "sort_order": 80,
        "note": "§8.5 — provinces and districts from a fixed official list (7 + 77), not typed free-hand. §13 open question 4 asks whether these should instead be generated from the case's location `entities`; unresolved, so `external` for now. `Kathmandu Valley` (9 live cases) is neither a province nor a district and fits no member of this axis — open item.",
    },
    {
        "id": "person",
        "label_ne": "व्यक्ति / संस्था",
        "label_en": "Named person / entity",
        "min_per_case": 0,
        "max_per_case": 5,
        "highlighted": False,
        "members": "entities",
        "set_by": "",
        "sort_order": 90,
        "note": "§8.6 — naming an individual NEVER implies a finding of guilt; guilt is expressed only by the verdict axis.",
    },
]

TERMS = [
    {
        "id": "sub-judice",
        "axis": "status",
        "label_ne": "विचाराधीन",
        "label_ne_composed": None,
        "label_en": "Sub judice",
        "status": "active",
        "note": "",
    },
    {
        "id": "first-instance-decided",
        "axis": "status",
        "label_ne": None,
        "label_ne_composed": {
            "special": "विशेष अदालतको फैसला",
            "district": "जिल्ला अदालतको फैसला",
            "high": "उच्च अदालतको फैसला",
        },
        "label_en": "Decided at first instance",
        "status": "active",
        "note": "",
    },
    {
        "id": "under-appeal",
        "axis": "status",
        "label_ne": "पुनरावेदन",
        "label_ne_composed": None,
        "label_en": "Under Appeal",
        "status": "active",
        "note": "",
    },
    {
        "id": "concluded",
        "axis": "status",
        "label_ne": "टुङ्गिएको",
        "label_ne_composed": None,
        "label_en": "Concluded",
        "status": "active",
        "note": "",
    },
    {
        "id": "under-investigation",
        "axis": "status",
        "label_ne": "अनुसन्धानमा",
        "label_ne_composed": None,
        "label_en": "Under Investigation",
        "status": "active",
        "note": "",
    },
    {
        "id": "investigation-stalled",
        "axis": "status",
        "label_ne": "अनुसन्धान रोकिएको",
        "label_ne_composed": None,
        "label_en": "Investigation Stalled",
        "status": "active",
        "note": "replaces the banned editorial tag `Stalled Investigation` (§9)",
    },
    {
        "id": "no-action-taken",
        "axis": "status",
        "label_ne": "कारबाही नभएको",
        "label_ne_composed": None,
        "label_en": "No Action Taken",
        "status": "active",
        "note": "",
    },
    {
        "id": "arbitration-pending",
        "axis": "status",
        "label_ne": "मध्यस्थता विचाराधीन",
        "label_ne_composed": None,
        "label_en": "Arbitration Pending",
        "status": "active",
        "note": "",
    },
    {
        "id": "arbitration-concluded",
        "axis": "status",
        "label_ne": "मध्यस्थता टुङ्गिएको",
        "label_ne_composed": None,
        "label_en": "Arbitration Concluded",
        "status": "active",
        "note": "",
    },
    {
        "id": "convicted",
        "axis": "verdict",
        "label_ne": "ठहर",
        "label_ne_composed": None,
        "label_en": "Convicted",
        "status": "active",
        "note": "",
    },
    {
        "id": "partially-convicted",
        "axis": "verdict",
        "label_ne": "अंशिक ठहर",
        "label_ne_composed": None,
        "label_en": "Partially Convicted",
        "status": "active",
        "note": "§5.1 — EITHER mixed across defendants (some convicted, some acquitted) OR partial on charges. Defined explicitly so no fourth term gets invented.",
    },
    {
        "id": "acquitted",
        "axis": "verdict",
        "label_ne": "सफाइ",
        "label_ne_composed": None,
        "label_en": "Acquitted",
        "status": "active",
        "note": "",
    },
    {
        "id": "claim-upheld",
        "axis": "verdict",
        "label_ne": "रिट जारी",
        "label_ne_composed": None,
        "label_en": "Claim Upheld",
        "status": "active",
        "note": "writ petitions — a writ convicts nobody, so ठहर/सफाइ are category errors",
    },
    {
        "id": "claim-denied",
        "axis": "verdict",
        "label_ne": "रिट खारेज",
        "label_ne_composed": None,
        "label_en": "Claim Denied",
        "status": "active",
        "note": "",
    },
    {
        "id": "abated",
        "axis": "verdict",
        "label_ne": "मुद्दा तामेली",
        "label_ne_composed": None,
        "label_en": "Abated",
        "status": "active",
        "note": "ended without a merits decision, typically on death of the accused. Not सफाइ.",
    },
    {
        "id": "procurement-irregularity",
        "axis": "offence",
        "label_ne": "सार्वजनिक खरिद अनियमितता",
        "label_ne_composed": None,
        "label_en": "Procurement Irregularity",
        "status": "active",
        "note": "",
    },
    {
        "id": "illicit-enrichment",
        "axis": "offence",
        "label_ne": "स्रोत नखुलेको सम्पत्ति आर्जन",
        "label_ne_composed": None,
        "label_en": "Illicit Enrichment",
        "status": "active",
        "note": "",
    },
    {
        "id": "abuse-of-public-office",
        "axis": "offence",
        "label_ne": "पदको दुरुपयोग",
        "label_ne_composed": None,
        "label_en": "Abuse of Public Office",
        "status": "active",
        "note": "",
    },
    {
        "id": "embezzlement",
        "axis": "offence",
        "label_ne": "हिनामिना/अपचलन",
        "label_ne_composed": None,
        "label_en": "Embezzlement",
        "status": "active",
        "note": "",
    },
    {
        "id": "revenue-leakage",
        "axis": "offence",
        "label_ne": "राजस्व चुहावट",
        "label_ne_composed": None,
        "label_en": "Revenue Leakage",
        "status": "active",
        "note": "",
    },
    {
        "id": "forged-documents",
        "axis": "offence",
        "label_ne": "कीर्ते कागजात",
        "label_ne_composed": None,
        "label_en": "Forged Documents",
        "status": "active",
        "note": "",
    },
    {
        "id": "tax-evasion",
        "axis": "offence",
        "label_ne": "कर छली",
        "label_ne_composed": None,
        "label_en": "Tax Evasion",
        "status": "active",
        "note": "",
    },
    {
        "id": "bid-rigging",
        "axis": "offence",
        "label_ne": "बोलपत्रमा मिलेमतो",
        "label_ne_composed": None,
        "label_en": "Bid Rigging",
        "status": "active",
        "note": "§8.1 — use where specifically alleged, else `procurement-irregularity`",
    },
    {
        "id": "land-grab",
        "axis": "offence",
        "label_ne": "सरकारी जग्गा हडप",
        "label_ne_composed": None,
        "label_en": "Land Grab",
        "status": "active",
        "note": "",
    },
    {
        "id": "money-laundering",
        "axis": "offence",
        "label_ne": "सम्पत्ति शुद्धीकरण",
        "label_ne_composed": None,
        "label_en": "Money Laundering",
        "status": "active",
        "note": "",
    },
    {
        "id": "bribery",
        "axis": "offence",
        "label_ne": "घुस रिसवत",
        "label_ne_composed": None,
        "label_en": "Bribery",
        "status": "active",
        "note": "",
    },
    {
        "id": "conflict-of-interest",
        "axis": "offence",
        "label_ne": "स्वार्थ बाझ्ने अवस्था",
        "label_ne_composed": None,
        "label_en": "Conflict of Interest",
        "status": "active",
        "note": "",
    },
    {
        "id": "public-asset-damage",
        "axis": "offence",
        "label_ne": "सार्वजनिक सम्पत्ति हानि नोक्सानी",
        "label_ne_composed": None,
        "label_en": "Public Asset Damage",
        "status": "active",
        "note": "",
    },
    {
        "id": "budget-misuse",
        "axis": "offence",
        "label_ne": "बजेट दुरुपयोग",
        "label_ne_composed": None,
        "label_en": "Budget Misuse",
        "status": "active",
        "note": "",
    },
    {
        "id": "fake-employees",
        "axis": "offence",
        "label_ne": "नक्कली कर्मचारी",
        "label_ne_composed": None,
        "label_en": "Fake Employees",
        "status": "active",
        "note": "",
    },
    {
        "id": "witness-tampering",
        "axis": "offence",
        "label_ne": "साक्षी प्रभावित पार्ने",
        "label_ne_composed": None,
        "label_en": "Witness Tampering",
        "status": "active",
        "note": "",
    },
    {
        "id": "cooperative-fraud",
        "axis": "offence",
        "label_ne": "सहकारी ठगी",
        "label_ne_composed": None,
        "label_en": "Cooperative Fraud",
        "status": "active",
        "note": "",
    },
    {
        "id": "policy-corruption",
        "axis": "offence",
        "label_ne": "नीतिगत भ्रष्टाचार",
        "label_ne_composed": None,
        "label_en": "Policy Corruption",
        "status": "active",
        "note": "",
    },
    {
        "id": "land-administration",
        "axis": "sector",
        "label_ne": "भूमि प्रशासन",
        "label_ne_composed": None,
        "label_en": "Land Administration",
        "status": "active",
        "note": "§13 Q1 — chosen over मालपोत (x126) because मालपोत names an office, not a sector",
    },
    {
        "id": "infrastructure",
        "axis": "sector",
        "label_ne": "पूर्वाधार",
        "label_ne_composed": None,
        "label_en": "Infrastructure",
        "status": "active",
        "note": "",
    },
    {
        "id": "finance",
        "axis": "sector",
        "label_ne": "वित्त",
        "label_ne_composed": None,
        "label_en": "Finance",
        "status": "active",
        "note": "",
    },
    {
        "id": "forestry",
        "axis": "sector",
        "label_ne": "वन",
        "label_ne_composed": None,
        "label_en": "Forestry",
        "status": "active",
        "note": "",
    },
    {
        "id": "water-supply",
        "axis": "sector",
        "label_ne": "खानेपानी",
        "label_ne_composed": None,
        "label_en": "Water Supply",
        "status": "active",
        "note": "",
    },
    {
        "id": "health",
        "axis": "sector",
        "label_ne": "स्वास्थ्य",
        "label_ne_composed": None,
        "label_en": "Health",
        "status": "active",
        "note": "",
    },
    {
        "id": "education",
        "axis": "sector",
        "label_ne": "शिक्षा",
        "label_ne_composed": None,
        "label_en": "Education",
        "status": "active",
        "note": "",
    },
    {
        "id": "information-technology",
        "axis": "sector",
        "label_ne": "सूचना प्रविधि",
        "label_ne_composed": None,
        "label_en": "Information Technology",
        "status": "active",
        "note": "",
    },
    {
        "id": "agriculture",
        "axis": "sector",
        "label_ne": "कृषि",
        "label_ne_composed": None,
        "label_en": "Agriculture",
        "status": "active",
        "note": "",
    },
    {
        "id": "transport",
        "axis": "sector",
        "label_ne": "यातायात",
        "label_ne_composed": None,
        "label_en": "Transport",
        "status": "active",
        "note": "",
    },
    {
        "id": "energy",
        "axis": "sector",
        "label_ne": "ऊर्जा",
        "label_ne_composed": None,
        "label_en": "Energy",
        "status": "active",
        "note": "",
    },
    {
        "id": "telecommunications",
        "axis": "sector",
        "label_ne": "दूरसञ्चार",
        "label_ne_composed": None,
        "label_en": "Telecommunications",
        "status": "active",
        "note": "",
    },
    {
        "id": "cooperatives",
        "axis": "sector",
        "label_ne": "सहकारी",
        "label_ne_composed": None,
        "label_en": "Cooperatives",
        "status": "active",
        "note": "",
    },
    {
        "id": "postal-services",
        "axis": "sector",
        "label_ne": "हुलाक",
        "label_ne_composed": None,
        "label_en": "Postal Services",
        "status": "active",
        "note": "",
    },
    {
        "id": "local-government",
        "axis": "governance_level",
        "label_ne": "स्थानीय तह",
        "label_ne_composed": None,
        "label_en": "Local Government",
        "status": "active",
        "note": "the only governance value in live use (26 cases)",
    },
    {
        "id": "province-government",
        "axis": "governance_level",
        "label_ne": "प्रदेश सरकार",
        "label_ne_composed": None,
        "label_en": "Province Government",
        "status": "active",
        "note": "",
    },
    {
        "id": "federal-government",
        "axis": "governance_level",
        "label_ne": "संघीय सरकार",
        "label_ne_composed": None,
        "label_en": "Federal Government",
        "status": "active",
        "note": "",
    },
    {
        "id": "state-owned-entity",
        "axis": "governance_level",
        "label_ne": "सरकारी संस्थान",
        "label_ne_composed": None,
        "label_en": "State-Owned Entity",
        "status": "active",
        "note": "",
    },
    {
        "id": "corruption-prosecution",
        "axis": "nature",
        "label_ne": "भ्रष्टाचार अभियोजन",
        "label_ne_composed": None,
        "label_en": "Corruption Prosecution",
        "status": "active",
        "note": "the standard track — 74 of 82 live cases",
    },
    {
        "id": "writ",
        "axis": "nature",
        "label_ne": "रिट",
        "label_ne_composed": None,
        "label_en": "Writ",
        "status": "active",
        "note": '§6.1 — derived from the court identifier PLUS the case-number prefix. A Supreme Court record with a wo/wf/wc number is original jurisdiction, not an appeal; keying on "has a Supreme Court record" mislabels all four live writ cases as पुनरावेदन.',
    },
    {
        "id": "ordinary-criminal",
        "axis": "nature",
        "label_ne": "साधारण फौजदारी",
        "label_ne_composed": None,
        "label_en": "Ordinary Criminal",
        "status": "active",
        "note": "",
    },
    {
        "id": "arbitration",
        "axis": "nature",
        "label_ne": "मध्यस्थता",
        "label_ne_composed": None,
        "label_en": "Arbitration",
        "status": "active",
        "note": "",
    },
    {
        "id": "pre-prosecution",
        "axis": "nature",
        "label_ne": "अदालत बाहिर",
        "label_ne_composed": None,
        "label_en": "Pre-prosecution",
        "status": "active",
        "note": "",
    },
]


def seed(apps: Any, schema_editor: Any) -> None:
    TagAxis = apps.get_model("case_tags", "TagAxis")
    Tag = apps.get_model("case_tags", "Tag")
    # ``update_or_create`` rather than ``create``: this migration is re-run against
    # databases that already have the rows (a rebuilt test DB, a squashed history, a
    # replay after a rollback), and a duplicate-key crash there is a pointless outage.
    for row in AXES:
        TagAxis.objects.update_or_create(id=row["id"], defaults=row)
    for row in TERMS:
        payload = dict(row)
        payload["axis_id"] = payload.pop("axis")
        Tag.objects.update_or_create(id=payload["id"], defaults=payload)


def unseed(apps: Any, schema_editor: Any) -> None:
    # Reverse by ID, not ``.all().delete()``: an operator may have added terms through
    # the admin since, and a reverse migration has no business deleting those.
    TagAxis = apps.get_model("case_tags", "TagAxis")
    Tag = apps.get_model("case_tags", "Tag")
    Tag.objects.filter(id__in=[r["id"] for r in TERMS]).delete()
    # Only axes with no remaining tags. ``Tag.axis`` is PROTECT, so an operator-added
    # term under a seeded axis — exactly what the review queue produces — would make
    # this delete raise and the whole rollback fail partway through.
    TagAxis.objects.filter(id__in=[r["id"] for r in AXES], tags__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("case_tags", "0001_initial")]
    operations = [migrations.RunPython(seed, unseed)]
