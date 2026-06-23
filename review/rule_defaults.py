"""Default rule set for the Casework Review System (rule-centered model).

Per VOL-3 operator guidance (latest comment): the system is now a set of
*rules*. Each rule has a short title, a markdown description, good/bad
examples, a condition ("it is only active when ..."), a score (weight) and a
category. While grading, every rule whose condition matches produces a score
and a confidence (mean + variance).

`detector` names a function in rules_engine.DETECTORS for deterministic rules.
`kind="llm"` rules are scored by the Bedrock judge (sampled N times for variance).
`applies_to` lists the case types the rule is active for (["ALL"] = always).

Case types: CIAA_BASIC, CIAA_EXTENDED, NON_CIAA (see casetype.py).
"""

ALL = ["ALL"]
CIAA = ["CIAA_BASIC", "CIAA_EXTENDED", "CIAA_HAS_VERDICT"]

DEFAULT_RULES = [
    # ---------------- Completeness ----------------
    {
        "key": "court_case_number",
        "title": "Court case number present",
        "category": "Completeness",
        "kind": "deterministic",
        "detector": "court_case_number",
        "condition_text": (
            "Active for charge-sheet / special-court cases, which must carry a "
            "formal court case number."
        ),
        "applies_to": ["CIAA_EXTENDED", "CIAA_HAS_VERDICT"],
        "description": (
            "Charge-sheet and special-court cases **must** record a well-formed "
            "court case number (e.g. `081-CR-0136`) in the `court_cases` field. "
            "This is the anchor that lets a reader pull the official record."
        ),
        "weight": 1.4,
        "is_gate": True,
        "gate_min": 60,
        "enabled": True,
        "order": 10,
    },
    {
        "key": "additional_description_present",
        "title": "Substantial additional description",
        "category": "Accuracy",
        "kind": "deterministic",
        "detector": "additional_description",
        "condition_text": "Always active.",
        "applies_to": ALL,
        "description": (
            "Beyond a one-line summary, a case needs a **substantial additional "
            "description** (≥600 chars) that explains the scheme, the parties and "
            "the status. A stub description fails this rule."
        ),
        "weight": 1.2,
        "is_gate": True,
        "gate_min": 40,
        "enabled": True,
        "order": 20,
    },
    {
        "key": "structural_completeness",
        "title": "Core fields populated",
        "category": "Completeness",
        "kind": "deterministic",
        "detector": "structural_completeness",
        "condition_text": "Always active.",
        "applies_to": ALL,
        "description": (
            "All core structured fields are populated: key allegations, timeline, "
            "evidence, entities and tags. Measures how fully the case record is "
            "filled out relative to the gold-standard bar."
        ),
        "weight": 1.0,
        "enabled": True,
        "order": 30,
    },
    # ---------------- Description quality (LLM) ----------------
    {
        "key": "description_summarises_case",
        "title": "Description summarises the case",
        "category": "Accuracy",
        "kind": "llm",
        "detector": "",
        "condition_text": "Always active.",
        "applies_to": ALL,
        "description": (
            "The `description` must accurately and completely **summarise the "
            "case**: who is accused, what they allegedly did, the amount/scheme, "
            "the agency (CIAA etc.), and the current status — faithful to the "
            "source documents.\n\n"
            "Score on substance. Cosmetic matters — overall length, section "
            "ordering, a malformed markdown table, minor wording — belong in "
            "`notes`, not `issues`, and must not lower the score; only a "
            "substantive failure (missing the core allegation/amount/status, or "
            "contradicting the sources) is a scored issue."
        ),
        "good_examples": "A reader who only reads the description understands the whole case and it matches the sources.",
        "bad_examples": "Description omits the core allegation, the amount, or contradicts the sources; or is a stub.",
        "weight": 1.4,
        "enabled": True,
        "order": 40,
    },
    {
        "key": "bigo_matches_press_release",
        "title": "Bigo amount matches the press release",
        "category": "Accuracy",
        "kind": "llm",
        "detector": "",
        "condition_text": "Active for all CIAA cases.",
        "applies_to": CIAA,
        "description": (
            "The **bigo (बिगो)** amount recorded on the case (provided as `bigo`, "
            "in NPR, in the case data) must match the disputed / embezzled "
            "amount stated in the CIAA **press release** (and, where present, the "
            "charge sheet).\n\n"
            "Find the headline amount in the source documents and compare it to "
            "the `bigo` figure. Allow for unit phrasing (e.g. arba/karod/lakh vs. "
            "plain rupees) and rounding, but flag a genuine mismatch in the "
            "figure. If the press release states the amount in a foreign currency "
            "with an NPR conversion, compare against the NPR total.\n\n"
            "Treat paisa-level rounding (e.g. dropping a trailing `.85`) and a "
            "single divergent news-outlet figure — when the authoritative "
            "press-release / charge-sheet amount still matches `bigo` — as a "
            "`notes` observation, NOT a scored issue: keep the score at 100. Only "
            "an actual contradiction with the authoritative figure is an `issue`.\n\n"
            "If the case is a certified no-bigo case (empty `bigo` with a "
            "`NO_BIGO:` marker in `internal_notes`), there is no figure to match — "
            "keep the score at 100."
        ),
        "good_examples": (
            "Press release states रु. ३.२१ अर्ब and `bigo` is 3,218,377,182 — the "
            "same amount, just different formatting."
        ),
        "bad_examples": (
            "`bigo` is set to a figure that does not appear in, or contradicts, "
            "the amount stated in the press release / charge sheet."
        ),
        "weight": 1.2,
        "enabled": True,
        "order": 41,
    },
    {
        "key": "slug_public_view_quality",
        "title": "Good public-view slug",
        "category": "Completeness",
        "kind": "llm",
        "detector": "",
        "condition_text": "Always active.",
        "applies_to": ALL,
        "description": (
            "The case `slug` is the public-facing URL identifier, so it must "
            "**accurately portray the case** — readable, human-meaningful, and "
            "faithful to the case title / summary / key allegations. A good slug "
            "names the central party and/or the substance of the case (e.g. the "
            "accused, institution, scheme, or place) in lowercase, hyphen-"
            "separated words. "
            "It is **acceptable and encouraged** for the slug to ALSO include the "
            "official court / case number (e.g. `081-cr-0095`) and/or a short "
            "random suffix (e.g. `32c84510`) as additional segments — these aid "
            "citation and disambiguate related or otherwise similar cases. Do NOT "
            "penalise a slug merely for embedding a court number or a random "
            "suffix, **so long as a human-meaningful descriptive part is also "
            "present**. Only penalise when the slug is opaque overall — i.e. it is "
            "ONLY a bare code, number, or hash with no descriptive words — or when "
            "it is misleading relative to what the case is actually about. Judge "
            "whether a member of the public reading the slug would correctly "
            "anticipate the case's title, summary and key allegations."
        ),
        "good_examples": (
            "`baluwatar-land-grab-singha-durbar` for a case about a land-grab "
            "scheme in Singha Durbar; `ncell-capital-gains-tax-evasion` — the "
            "slug names the party and the substance and matches the title. "
            "`baluwatar-land-grab-081-cr-0095` or "
            "`ncell-capital-gains-tax-evasion-32c84510` — descriptive AND carrying "
            "the court number / a disambiguating suffix; both are fine."
        ),
        "bad_examples": (
            "`case-081-cr-0107` or `a3f9c21` — opaque: ONLY a code / hash with no "
            "descriptive words, so the public learns nothing (note: the same court "
            "number is fine when ATTACHED to a descriptive slug); `road-project` "
            "for a detailed embezzlement case (too vague); a slug naming the wrong "
            "party or contradicting the title."
        ),
        "weight": 1.0,
        "enabled": True,
        "order": 45,
    },
    # ---------------- Tonal neutrality (LLM) ----------------
    {
        "key": "tonal_neutrality",
        "title": "Tonal neutrality",
        "category": "Tone",
        "kind": "llm",
        "detector": "",
        "condition_text": "Always active.",
        "applies_to": ALL,
        "description": (
            "Language is neutral and non-speculative. Unproven claims are hedged "
            "as **alleged** (आरोप). No editorialising, loaded adjectives, or "
            "conclusions stated as established fact before a verdict."
        ),
        "good_examples": '"X is accused of (आरोप) misappropriating Rs. Y" — hedged, factual.',
        "bad_examples": '"X is a corrupt official who stole Rs. Y" — verdict asserted, loaded.',
        "weight": 1.5,
        "enabled": True,
        "order": 50,
    },
    # ---------------- Sourcing ----------------
    {
        "key": "source_evidence_completeness",
        "title": "Source / evidence completeness",
        "category": "Sourcing",
        "kind": "deterministic",
        "detector": "sourcing",
        "condition_text": "Always active.",
        "applies_to": ALL,
        "description": (
            "Enough evidence sources, each resolving to its primary (RAW) "
            "document, and a spread of official/legal source types. The case "
            "should be verifiable from its attached evidence."
        ),
        "weight": 1.5,
        "is_gate": True,
        "gate_min": 35,
        "enabled": True,
        "order": 60,
    },
    {
        "key": "source_link_roles_valid",
        "title": "Source links are well-formed (one RAW each)",
        "category": "Sourcing",
        "kind": "deterministic",
        "detector": "source_link_roles_valid",
        "condition_text": "Always active (hard gate).",
        "applies_to": ALL,
        "description": (
            "Every document source **must** have exactly **one** canonical "
            "`RAW` link, and every link must carry a recognised role "
            "(`RAW`, `MARKDOWN`, `PERMALINK`, `SOURCE_PAGE`, `ALTERNATE`). "
            "A source with no links, no RAW link, more than one RAW link, or an "
            "unrecognised role **fails the review** — additional copies of the "
            "same document (mirrors, alternate-format exports, archives, markdown "
            "renderings) belong under `ALTERNATE` / `PERMALINK` / `MARKDOWN`, not "
            "as a second RAW. This is the review-side enforcement of the stored "
            "source-link convention — stricter than the model's link validator, "
            "which only requires each link to carry a recognised role."
        ),
        "weight": 1.3,
        "is_gate": True,
        "gate_min": 50,
        "enabled": True,
        "order": 60.5,
    },
    {
        "key": "ciaa_press_release_attached",
        "title": "CIAA press release attached",
        "category": "Sourcing",
        "kind": "deterministic",
        "detector": "ciaa_press_release",
        "condition_text": "Active only for CIAA cases (must carry the अख्तियार press release).",
        "applies_to": ["CIAA_BASIC", "CIAA_EXTENDED", "CIAA_HAS_VERDICT"],
        "description": (
            "A CIAA case is defined by an attached CIAA (अख्तियार) press release. "
            "This rule confirms that defining source is actually present among the "
            "evidence."
        ),
        "weight": 1.1,
        "enabled": True,
        "order": 65,
    },
    {
        "key": "court_record_attached",
        "title": "Charge sheet + special-court verdict attached",
        "category": "Sourcing",
        "kind": "deterministic",
        "detector": "court_record",
        "condition_text": "Active for CIAA cases with the special-court verdict available.",
        "applies_to": ["CIAA_HAS_VERDICT"],
        "description": (
            "Cases that have reached a special-court verdict must attach the "
            "charge sheet (अभियोग पत्र) AND the court verdict (फैसला) or order "
            "(आदेश). These are the highest-detail primary documents."
        ),
        "weight": 1.2,
        "enabled": True,
        "order": 66,
    },
    {
        "key": "charge_sheet_attached",
        "title": "Charge sheet attached",
        "category": "Sourcing",
        "kind": "deterministic",
        "detector": "charge_sheet",
        "condition_text": "Active for CIAA cases that have reached the charge-sheet stage.",
        "applies_to": ["CIAA_EXTENDED", "CIAA_HAS_VERDICT"],
        "description": (
            "Once a CIAA case is filed in special court, the charge sheet "
            "(अभियोग पत्र) is the defining primary document and must be among "
            "the sources."
        ),
        "weight": 1.1,
        "enabled": True,
        "order": 67,
    },
    {
        "key": "no_duplicate_document_sources",
        "title": "No duplicate document sources",
        "category": "Sourcing",
        "kind": "llm",
        "detector": "",
        "condition_text": "Always active.",
        "applies_to": ALL,
        "description": (
            "Each distinct underlying document should appear as **exactly ONE** "
            "source entry. A single source may legitimately carry **multiple "
            "URLs** (e.g. the original link plus a web-archive mirror, or two "
            "hosts serving the same file) — that is correct and expected. What is "
            "NOT allowed is the SAME document being entered as **two or more "
            "separate source entries** (e.g. two sources both titled and both "
            "being 'Supreme Court verdict on case …', or the same charge sheet / "
            "press release / news article duplicated across entries). When the "
            "same document has several locations, consolidate them into one "
            "source with several URLs rather than creating duplicate sources. "
            "Judge by whether two source entries refer to the **same underlying "
            "document** (same court order / verdict / article / file) — compare "
            "titles, source types and converted content. Genuinely different "
            "documents (e.g. a 2072 verdict vs. a separate 2076 verdict, or a "
            "charge sheet vs. a news report) are NOT duplicates and must each be "
            "their own source. Score 100 when there are no duplicate document "
            "entries; lower the score the more the same document is split across "
            "multiple separate source entries."
        ),
        "good_examples": (
            "One source 'Supreme Court verdict 081-WO-1235' carrying both the "
            "S3 URL and a web.archive.org mirror; a 2072 verdict and a 2076 "
            "verdict kept as two sources because they are different documents."
        ),
        "bad_examples": (
            "Two separate source entries that are both the same Supreme Court "
            "verdict on the case (should be one source with two URLs); the same "
            "charge sheet or news article appearing as two distinct sources."
        ),
        "weight": 1.0,
        "enabled": True,
        "order": 68,
    },
    {
        "key": "source_raw_link_quality",
        "title": "RAW link is the right kind of document",
        "category": "Sourcing",
        "kind": "llm",
        "detector": "",
        "condition_text": "Always active.",
        "applies_to": ALL,
        "description": (
            "Each source's single canonical **RAW** link should point at the "
            "actual primary **document** — an uploaded / stored file or a direct "
            "document link (e.g. a PDF or scan of the press release, charge sheet, "
            "court order, or verdict) — rather than a generic web page.\n\n"
            "**Exception — NEWS sources:** for a news source there is no document "
            "to upload, so the news article's own URL is the legitimate RAW and "
            "must NOT be penalised.\n\n"
            "Also check that each source has **exactly one** RAW link and that any "
            "*additional* links are filed under the correct supporting role rather "
            "than as a second RAW: a markdown rendering as **MARKDOWN**, an "
            "archive / web-capture as **PERMALINK**, and any other supplementary "
            "copy (an alternate-format export, a mirror, another hosting of the "
            "same file) as **ALTERNATE**.\n\n"
            "Score 100 when every non-news source's RAW is a document file (news "
            "RAW being a URL is fine), each source has a single RAW, and extra "
            "links use the right roles. Lower the score the more sources put a "
            "bare web page as a non-news RAW, carry multiple RAW links, or "
            "mis-file supporting links as RAW."
        ),
        "good_examples": (
            "A CIAA press-release source whose RAW is the uploaded PDF/scan, with "
            "the ciaa.gov.np page kept as ALTERNATE and a web.archive.org capture "
            "as PERMALINK. A NEWS source whose RAW is the article URL itself."
        ),
        "bad_examples": (
            "A press-release source whose only RAW is a bare ciaa.gov.np web page "
            "with the uploaded PDF mis-tagged (or absent); a source carrying the "
            "ciaa.gov.np page, a .doc export and a .pdf export all three tagged "
            "RAW (only one should be RAW, the others ALTERNATE)."
        ),
        "weight": 0.8,
        "enabled": True,
        "order": 68.5,
    },
    {
        "key": "ephemeral_sources_archived",
        "title": "Ephemeral web sources are archived",
        "category": "Sourcing",
        "kind": "llm",
        "detector": "",
        "condition_text": "Always active.",
        "applies_to": ALL,
        "description": (
            "When a source's **RAW** link is an **ephemeral** external web page — "
            "a news outlet or general website that can change or disappear — the "
            "source should also carry a **PERMALINK** archive copy (e.g. a "
            "`web.archive.org` capture) to guard against link rot.\n\n"
            "This expectation applies **only** to ephemeral news/web pages. It "
            "does **not** apply to:\n"
            "1. Files in our own storage (`*.jawafdehi.org`), which are durable.\n"
            "2. Official government / court / institutional records (e.g. "
            "`*.gov.np`, UN, World Bank), which are treated as durable primary "
            "records.\n"
            "3. A link that is itself already a stable permalink.\n\n"
            "Score 100 when every ephemeral news/web source has a PERMALINK "
            "archive companion; lower the score the more such sources lack one. "
            "Do not penalise official-record or own-storage sources for missing "
            "an archive."
        ),
        "good_examples": (
            "A NEWS source whose article URL (RAW) is paired with a "
            "web.archive.org capture (PERMALINK). An official ciaa.gov.np / "
            "*.gov.np source with no archive — fine, it is a durable record."
        ),
        "bad_examples": (
            "A NEWS source citing only a news-outlet article URL as RAW with no "
            "web.archive.org (or other) PERMALINK companion, leaving the citation "
            "exposed to link rot."
        ),
        "weight": 0.6,
        "enabled": True,
        "order": 68.7,
    },
    # ---------------- Timeline ----------------
    {
        "key": "timeline_completeness",
        "title": "Timeline completeness",
        "category": "Timeline",
        "kind": "deterministic",
        "detector": "timeline",
        "condition_text": "Always active.",
        "applies_to": ALL,
        "description": (
            "Enough dated, detailed, chronologically-ordered timeline events, with "
            "both Bikram Sambat (date_bs) and Gregorian (date) dates."
        ),
        "weight": 1.0,
        "is_gate": True,
        "gate_min": 30,
        "enabled": True,
        "order": 70,
    },
    # ---------------- Entities ----------------
    {
        "key": "accused_present",
        "title": "At least one accused entity",
        "category": "Entities",
        "kind": "deterministic",
        "detector": "accused_present",
        "condition_text": "Active for case types that name an accused (hard gate).",
        "applies_to": ALL,
        "description": (
            "For case types that name an accused (e.g. corruption), the case "
            "**must** tag at least one **accused** entity — the person(s) or "
            "organisation(s) the allegations are against — and cannot be "
            "published without one. Case types that do not name an accused "
            "(e.g. tax evasion) are exempt and pass automatically."
        ),
        "weight": 1.2,
        "is_gate": True,
        "gate_min": 100,
        "enabled": True,
        "order": 80,
    },
    {
        "key": "location_entity_count",
        "title": "Location entities (where applicable)",
        "category": "Entities",
        "kind": "llm",
        "detector": "",
        # Gate stays, but judged on the cheap tier (see _tier_for_rule): the
        # judgment is simple enough not to need the premium model.
        "tier": "cheap",
        "condition_text": "Always active.",
        "applies_to": ALL,
        "description": (
            "Judge whether the case is tagged with the place(s) where the case "
            "events actually happened or where the assets/funds at issue are "
            "located — the district (जिल्ला), municipality (नगरपालिका), or "
            "province. Look at the `entities` list (type `location`) and weigh it "
            "against the description and sources.\n\n"
            "**Locations are NOT always required.** Many legitimate cases have no "
            "district-level scene: central-government procurement, ministry / "
            "authority / board / corporation HQ-level matters, and policy-level "
            "cases are administered centrally and may correctly carry **zero** "
            "location entities. When the case is clearly central / national in "
            "scope (the related and accused entities are national bodies, the "
            "events are a head-office procurement or policy decision), zero "
            "locations is CORRECT — score it high.\n\n"
            "Score LOW only when the sources or description clearly establish a "
            "specific district/municipality where the events occurred (a local "
            "project, a district office, a municipality scheme) and that location "
            "was NOT tagged. One good location is plenty for a single-district "
            "case; a few are fine for a case that genuinely spans districts.\n\n"
            "Also penalise WRONG or over-tagged locations: an accused person's "
            "home/birthplace/permanent address, the seat of the court or the "
            "CIAA inquiry office tagged as if it were the event location, or a "
            "long list of marginal places (more than ~5) that dilutes the signal."
        ),
        "good_examples": (
            "A district-level scheme tagged with its district, e.g. "
            "'साझा भण्डार सहकारी - सुर्खेत जिल्ला'. A central NTA telecom "
            "procurement or Nepal Airlines aircraft-purchase case with zero "
            "location entities, because there is no district-level scene — only "
            "the head office in Kathmandu, which is correctly left untagged."
        ),
        "bad_examples": (
            "A local municipality embezzlement case whose sources name the "
            "municipality but carries no location entity. A case tagged only with "
            "the accused's home district, or with 'काठमाडौं' solely because that "
            "is where the court / CIAA office sits. A case over-tagged with seven "
            "loosely-related places."
        ),
        "weight": 1.0,
        "is_gate": True,
        "gate_min": 50,
        "enabled": True,
        "order": 81,
    },
    {
        "key": "related_entity_present",
        "title": "At least one related entity",
        "category": "Entities",
        "kind": "deterministic",
        "detector": "related_entity_present",
        "condition_text": "Always active.",
        "applies_to": ALL,
        "description": (
            "The case **should** tag at least one **related** entity — anything "
            "that is neither the accused nor a location (e.g. a related "
            "organisation, official body, or other involved party)."
        ),
        "weight": 1.0,
        "enabled": True,
        "order": 82,
    },
    {
        "key": "court_case_number_required",
        "title": "CIAA case has a court case number",
        "category": "Completeness",
        "kind": "deterministic",
        "detector": "court_case_number",
        "condition_text": "Active for all CIAA cases (hard gate).",
        "applies_to": CIAA,
        "description": (
            "Every CIAA case — at any stage (press-release-only, charge sheet, "
            "or verdict) — **must** carry a court case number in the "
            "`court_cases` field, formatted as `<court_identifier>:<case_number>` "
            "(e.g. `special:081-CR-0079`). This is the anchor used to pull the "
            "official court record from the NGM database, so a missing court "
            "number **fails the review**."
        ),
        "weight": 1.4,
        "is_gate": True,
        "gate_min": 50,
        "enabled": True,
        "order": 83,
    },
    {
        "key": "court_number_in_title",
        "title": "CIAA case title includes the court case number",
        "category": "Completeness",
        "kind": "deterministic",
        "detector": "court_number_in_title",
        "condition_text": "Active for all CIAA cases (hard gate).",
        "applies_to": CIAA,
        "description": (
            "Every CIAA case **must** include its special-court case number in "
            "the **title** (e.g. `081-CR-0095`), typically in parentheses — e.g. "
            "`टेरामक्स (TERAMOCS) खरिदमा भ्रष्टाचार मुद्दा (081-CR-0095)`. A title "
            "with no court case number **fails the review**, and the number in "
            "the title **must match** the recorded `court_cases` reference — a "
            "missing or mismatched number fails the review."
        ),
        "weight": 1.2,
        "is_gate": True,
        "gate_min": 50,
        "enabled": True,
        "order": 83.5,
    },
    {
        "key": "bigo_amount_present",
        "title": "Bigo amount is set",
        "category": "Completeness",
        "kind": "deterministic",
        "detector": "bigo_amount_present",
        "condition_text": "Active for all CIAA cases (hard gate).",
        "applies_to": CIAA,
        "description": (
            "Every CIAA case **must** record the **bigo (बिगो)** — the total "
            "disputed / embezzled amount claimed in the case, in NPR. This figure "
            "anchors the allegation, so a missing or non-positive bigo **fails "
            "the review**.\n\n"
            "**Exception — legitimate no-bigo cases.** A few cases genuinely have "
            "no quantified bigo: record/process offences (दफा ११ / ८ — forgery, "
            "land-record or citizenship tampering where harm is not monetized), "
            "non-CIAA jurisdictions where the bigo concept does not map, or "
            "pre-charge allegations with no charge sheet. These pass **only** "
            "when an explicit `NO_BIGO:` marker line is recorded in the case's "
            "internal `internal_notes` field (e.g. `NO_BIGO: record_offence — "
            "आरोपपत्रमा बिगो रकम उल्लेख छैन`). A bare empty bigo with no marker "
            "still fails — that is the common data-extraction gap we want to catch."
        ),
        "weight": 1.2,
        "is_gate": True,
        "gate_min": 50,
        "enabled": True,
        "order": 83.7,
    },
    {
        "key": "accused_list_matches_court_record",
        "title": "Accused list matches court record",
        "category": "Entities",
        "kind": "llm",
        "detector": "",
        "condition_text": "Active for all CIAA cases.",
        "applies_to": CIAA,
        "description": (
            "The case's **accused** entities must match the defendants on the "
            "official court record. The case summary includes an "
            "`ngm_court_record` block with the defendant names pulled from the "
            "NGM judicial database for this case's court reference(s).\n\n"
            "Compare the two lists (Nepali names; allow for minor spelling / "
            "spacing differences):\n"
            "1. Every accused entity on the case should correspond to a "
            "defendant in the NGM record.\n"
            "2. Flag accused that do not appear among the NGM defendants, and "
            "NGM defendants that were not captured as accused.\n\n"
            "If `ngm_court_record.court_refs` is empty (no court number) or a "
            "lookup error is present, say the match could not be verified rather "
            "than penalising heavily."
        ),
        "good_examples": (
            "All accused entities appear among the NGM defendants for the "
            "case's court reference, with none missing on either side."
        ),
        "bad_examples": (
            "An accused entity is tagged that is not a defendant on the court "
            "record, or a court-record defendant is missing from the accused list."
        ),
        "weight": 1.2,
        "is_gate": False,
        "gate_min": 50,
        "enabled": True,
        "order": 84,
    },
    {
        "key": "non_accused_entities_in_sources",
        "title": "Non-accused entities appear in the primary sources",
        "category": "Entities",
        "kind": "llm",
        "detector": "",
        "condition_text": "Always active.",
        "applies_to": ALL,
        "description": (
            "Every **non-accused** entity on the case (i.e. the `related`, "
            "`location` and `alleged` entities) must be traceable to the "
            "primary source documents — the **press release**, the **charge "
            "sheet**, or the **court order / verdict**.\n\n"
            "Check both directions:\n"
            "1. Each non-accused entity tagged on the case actually appears in "
            "(or is clearly implied by) one of those source documents.\n"
            "2. Prominent non-accused parties / places named in those documents "
            "are not missing from the case's entities.\n\n"
            "Flag any non-accused entity that cannot be traced to a source "
            "(possible mis-tag), and any clearly-relevant party/place in the "
            "sources that was not captured."
        ),
        "good_examples": (
            "Every related organisation and location tagged on the case is named "
            "in the press release / charge sheet / court order, with nothing "
            "invented."
        ),
        "bad_examples": (
            "A location 'Pokhara' is tagged but appears in none of the source "
            "documents, or the charge sheet names a key related body that was "
            "never tagged."
        ),
        "weight": 1.0,
        "is_gate": False,
        "gate_min": 50,
        "enabled": True,
        "order": 85,
    },
    # ---------------- Privacy / ethics (LLM) ----------------
    {
        "key": "privacy_ethics",
        "title": "Privacy & ethics",
        "category": "Ethics",
        "kind": "llm",
        "detector": "",
        "condition_text": (
            "Active, EXCEPT for content quoted/reproduced from court orders, "
            "charge sheets, verdicts, and other official government materials."
        ),
        "applies_to": ALL,
        "description": (
            "Sticks to public-record information about public officials / "
            "institutions. No gratuitous personal details about private "
            "individuals, and no unverified accusations presented as fact.\n\n"
            "IMPORTANT EXEMPTION: This rule does NOT apply to information that "
            "appears in court orders, charge sheets, verdicts, CIAA filings, or "
            "other official government documents. Jawafdehi presents such "
            "primary government materials as-is, including any personal details "
            "they contain. Do NOT penalise the case for personal details, names, "
            "or accusations that originate from these official sources — they are "
            "public record by virtue of being in a government document. Only flag "
            "privacy/ethics concerns for content the editors added themselves "
            "beyond what the official materials state."
        ),
        "good_examples": (
            "Public-record facts about an official's conduct in office. "
            "Personal details (e.g. name, role) reproduced from a court verdict "
            "or charge sheet that we present as-is."
        ),
        "bad_examples": (
            "Editor-added home address / family details of a private individual "
            "that are NOT in any official source, or rumour stated as fact."
        ),
        "weight": 0.9,
        "enabled": True,
        "order": 90,
    },
    # ---------------- Gap honesty ----------------
    {
        "key": "gap_honesty",
        "title": "Gap honesty",
        "category": "Integrity",
        "kind": "llm",
        "detector": "",
        "condition_text": "Always active.",
        "applies_to": ALL,
        "description": (
            "`missing_details` is for honest disclosure of the **sources and "
            "documents the caseworker could not obtain** while researching the "
            "case — research/sourcing limitations — NOT a restatement of every "
            "uncertain case fact.\n\n"
            "Judge whether the case is honest about what **evidence it is "
            "missing**: name primary documents that were sought but not found or "
            "not yet available (e.g. the full charge sheet, the signed "
            "special-court verdict text, an audit report), and flag any claim that "
            "rests on secondary reporting because the primary record could not be "
            "retrieved.\n\n"
            "Do **NOT** penalise the case for failing to declare an uncertain "
            "*case fact* as 'missing' — e.g. whether an appeal was filed, the "
            "appellate outcome, or a still-pending status. Those belong in the "
            "narrative / timeline, not in `missing_details`, and their absence "
            "from `missing_details` is **not** a gap-honesty problem.\n\n"
            "Still flag genuine **dishonesty**: a contested or unconfirmed figure "
            "presented as settled fact, or the case leaning on a source while "
            "hiding that the primary document was never obtained."
        ),
        "good_examples": (
            "`missing_details` states that the full charge sheet and the signed "
            "special-court verdict could not be obtained, so some figures rely on "
            "the CIAA press release and news reporting — an honest disclosure of "
            "which sources are missing."
        ),
        "bad_examples": (
            "The case relies on a news figure as if it were the official "
            "charge-sheet amount without noting the primary document was never "
            "retrieved; or it presents a contested bigo as final fact. NOT bad: "
            "simply not listing 'appeal status unknown' in `missing_details` — "
            "that is a case fact, not a source gap, and must not be penalised."
        ),
        "weight": 0.6,
        "enabled": True,
        "order": 100,
    },
    # ---------------- Title quality (LLM) ----------------
    # NOTE: appended at the END of the list on purpose. CodeRule.id is the
    # definition index (+1), so inserting mid-list would renumber every later
    # rule's id. `order: 46` keeps it in its intended DISPLAY slot (right after
    # the slug rule) without shifting any existing ids.
    {
        "key": "title_quality",
        "title": "Title is a clear, strong headline",
        "category": "Completeness",
        "kind": "llm",
        "detector": "",
        "condition_text": "Always active (judges the case `title` only, not section headings).",
        "applies_to": ALL,
        "description": (
            "The case `title` is the public headline, so it should read like a "
            "good news headline — not a bureaucratic file label. Judge ONLY the "
            "title string (ignore the slug and the in-body section headings).\n\n"
            "A strong title:\n"
            "1. **Leads with the recognisable subject** — the named scheme "
            "(e.g. `टेरामक्स (TERAMOCS)`), the place / project / institution "
            "(`भरत ताल`, `औरही गाउँपालिका १५ शैय्या अस्पताल`), or a notable "
            "person.\n"
            "2. **Names the offence** (`गैरकानूनी सम्पत्ति आर्जन`, `भ्रष्टाचार`, "
            "`घोटाला`, `ठेक्का अनियमितता`).\n"
            "3. Is a **concise headline, not a clause-stuffed sentence**.\n"
            "4. Uses **clean grammar and spelling**.\n\n"
            "**Memorability hook — REWARD WHEN WARRANTED, never required.** A clean "
            "plain title is already full marks when the case has nothing dramatic "
            "to surface (e.g. a routine procurement case). Reward a hook only when "
            "the case data actually supports one, and only mark a title "
            "'could be stronger' (a MILD deduction, never a fail) when the case "
            "carries a salient fact that the title **buries**. Hooks worth "
            "surfacing, drawn from the case data:\n"
            "- a large `bigo` / loss amount in words (`३.२ अर्ब`, `३३ करोड`) — "
            "e.g. a 3.2-arba procurement case whose title omits the figure is "
            "weaker than it could be;\n"
            "- a count of accused (`X समेत १८ जना विरुद्ध`);\n"
            "- a marquee named scheme or public figure;\n"
            "- a vivid concrete scale (`१५ शैय्याको अस्पताल`).\n"
            "A colon split (`<punchy lead>: <detail>`) is a good way to carry a "
            "hook. Do NOT force a hook onto a small/routine case, and do NOT "
            "deduct for its absence there.\n\n"
            "**Accuracy guard.** A number in the title must not contradict the "
            "case's own figures: if the title states an amount that conflicts with "
            "`bigo` / the cited loss (allowing for the legitimate loss-vs-bigo "
            "distinction and rounding), flag it — a catchy but wrong headline is "
            "worse than a plain one. (Deep figure-vs-source matching stays with "
            "the bigo rule; here just catch an internally contradictory number.)\n\n"
            "**Do NOT penalise** the title for carrying the court case number in "
            "parentheses (e.g. `(081-CR-0095)`) — that is required and enforced "
            "elsewhere. Score the headline quality, not the presence of the "
            "number."
        ),
        "good_examples": (
            "`NTC मा ३३ करोडको घोटाला: सुनिल पौडेलसमेत १८ जनाविरुद्ध मुद्दा "
            "(081-CR-0111)` — subject + amount + count, colon headline. "
            "`टेरामक्स (TERAMOCS) खरिदमा ३.२ अर्बको भ्रष्टाचार मुद्दा (081-CR-0095)` "
            "— named scheme with the big number surfaced. "
            "`औरही गाउँपालिकामा १५ शैय्याको अस्पताल निर्माण ठेक्कामा भ्रष्टाचार "
            "(081-CR-0121)` — concise, vivid scale hook. "
            "`पशुपति शवदाह गृह मेसिन खरिद भ्रष्टाचार मुद्दा (081-CR-0129)` — plain "
            "but clear; full marks because the case has no salient quantifiable to "
            "surface."
        ),
        "bad_examples": (
            "`CIAA Special Court Case 93-068-0194: विश्वनाथ प्रसाद तेली` — "
            "bureaucratic file label: court number + a bare name, no subject or "
            "offence. "
            "`काठमाडौँ महानगरपालिका तत्कालीन इन्जिनियर (उपनिर्देशक) रामबाबु महतो "
            "कोईरी उपर गैरकानूनी सम्पत्ति आर्जन भ्रष्टाचार मुद्दा (081-CR-0116)` — "
            "reads like a clause-stuffed sentence, not a headline. "
            "A title whose stated amount (`छपन्न करोड` / 56 cr) contradicts the "
            "case's own `bigo` (~5.67 cr) — catchy but internally inconsistent. "
            "A title with spelling errors (`बिरुद्द`, `भष्ट्राचार`)."
        ),
        "weight": 1.0,
        "enabled": True,
        "order": 46,
    },
]
