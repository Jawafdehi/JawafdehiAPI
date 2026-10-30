"""The document-source catalogue served at ``/api/document-sources/``.

What exists in the archive, how big each shelf is, how much of it we have
actually read, and which distinct series sit inside a shelf.

**Counts are derived, labels are curated, and the split is deliberate.** The
list of sources and series comes from the statistics snapshot, so a corpus
ingested tomorrow appears here without a code change and a series minted by a
new upstream classification is never silently dropped. The labels are a short
hand-written map, because no amount of aggregation turns ``ag`` into "the
Office of the Attorney General".

That naming problem is the reason this module exists rather than the caller
reading ``by_source`` directly:

    ``ag``              is the **Attorney** General — 99,750 charge sheets
    ``official_report`` is the **Auditor** General

An LLM asked for "auditor general annual reports" will reach for ``ag`` and
return charge sheets with complete confidence. A human skimming the token list
makes the same mistake. The label is not decoration.

Labels are transcribed from the frontend's reviewed editorial registry
(``src/data/material-series.ts``) and the sourcing README. **A source with no
confirmed label gets none** — it falls back to its token, with ``labelled:
false``, rather than a plausible-sounding guess. An invented label on an
accountability archive is worse than a raw token, because a reader cannot tell
the two apart.

Every label carries a comment naming the record or file it was read from, so a
reviewer can re-check it without re-deriving it. Where a record's own metadata
named nobody, the evidence is its ATTACHED DOCUMENT — ``associatedMedia`` often
carries the original URL, and that domain is frequently the only thing in the
record that names the publisher at all. Exactly one token — ``cib`` — is left
unlabelled, and it says why: its rows are not readable by any route, so there is
no document to open and nothing to confirm. A token that holds documents gets a
label even when the token itself is an ingest accident; four of those are
labelled with the attribution of the token they duplicate, and say so, with the
corrections in ``docs/2026-10-29-document-source-cleanup.md``.
"""

from __future__ import annotations

from typing import Any

#: Human labels for the source tokens we can name from reviewed material.
#: Bilingual where the frontend registry already carries a reviewed Nepali name;
#: English-only where the only confirmation is the sourcing README.
SOURCE_LABELS: dict[str, dict[str, Any]] = {
    "ag": {
        "name": {"en": "Charge sheets", "ne": "अभियोगपत्रहरू"},
        "publisher": {"en": "Office of the Attorney General"},
        "description": (
            "Charge sheets filed at the Special Court in corruption cases, "
            "from the Office of the Attorney General."
        ),
    },
    "official_report": {
        "name": {
            "en": "Auditor General reports and publications",
            "ne": "महालेखापरीक्षकको प्रतिवेदन तथा प्रकाशन",
        },
        "publisher": {
            "en": "Office of the Auditor General",
            "ne": "महालेखापरीक्षकको कार्यालय",
        },
        "description": (
            "Audit reports and periodicals of the Office of the Auditor "
            "General. One token, several distinct series — see `series`."
        ),
    },
    "ciaa_annual_report": {
        "name": {"en": "CIAA annual reports", "ne": "अख्तियारका वार्षिक प्रतिवेदन"},
        "publisher": {
            "en": "Commission for the Investigation of Abuse of Authority",
            "ne": "अख्तियार दुरुपयोग अनुसन्धान आयोग",
        },
        "description": "Annual reports of the CIAA, from fiscal year 2047/48 BS onward.",
    },
    "ciaa_press_release": {
        "name": {"en": "CIAA press releases", "ne": "अख्तियारका प्रेस विज्ञप्ति"},
        "publisher": {
            "en": "Commission for the Investigation of Abuse of Authority",
            "ne": "अख्तियार दुरुपयोग अनुसन्धान आयोग",
        },
        "description": (
            "Press releases issued by the Commission for the Investigation of "
            "Abuse of Authority."
        ),
    },
    "kanun_patrika": {
        "name": {"en": "Kanun Patrika", "ne": "कानुन पत्रिका"},
        # No publisher. These materials carry none of their own and the series
        # registry names none, so there is nothing to cite. An office invented
        # here would be indistinguishable from a reviewed one.
        "description": "Full issues of the Nepal Kanun Patrika law journal.",
    },
    "nkp": {
        "name": {"en": "Supreme Court precedents", "ne": "सर्वोच्च अदालतका नजिर"},
        # From the materials' own publisher field, not from the journal's name:
        # the journal publishes them, the Supreme Court issues them.
        "publisher": {"en": "Supreme Court of Nepal", "ne": "सर्वोच्च अदालत"},
        "description": (
            "Individual precedents from the Nepal Law Journal. Distinct from "
            "`kanun_patrika`, which holds the journal's full issues."
        ),
    },
    "bolpatra": {
        "name": {"en": "e-GP tender notices"},
        "publisher": {
            "en": "Public Procurement Monitoring Office",
            "ne": "सार्वजनिक खरिद अनुगमन कार्यालय",
        },
        "description": "Tender notices published on the PPMO e-GP portal (bolpatra.gov.np).",
    },
    "ppmo": {
        "name": {"en": "PPMO publications"},
        "publisher": {
            "en": "Public Procurement Monitoring Office",
            "ne": "सार्वजनिक खरिद अनुगमन कार्यालय",
        },
        "description": "Procurement bulletins and annual reports published by the PPMO.",
    },
    "court_order": {
        "name": {"en": "Court orders"},
        # No publisher: these carry none, and "the court system" is a category
        # rather than an issuing office. The issuing court is on each document.
        "description": "Orders and verdicts scraped from the Nepal court portals.",
    },
    # ------------------------------------------------------------------
    # Added 2026-10-29. None of the tokens below appear in the frontend's
    # reviewed registry (`src/data/material-series.ts` names only `ag`,
    # `official_report`, `ciaa_press_release`, `ciaa_annual_report` and
    # `kanun_patrika`) or in the sourcing README (five scrapers: `nkp`, `ag`,
    # `ciaa`, `bolpatra`, `ppmo`). So each is confirmed from the remaining
    # evidence the brief allows — the materials' OWN fields, read off
    # `GET /api/materials/?source=<token>`, plus the in-repo scraper where one
    # exists. Every entry cites the record it was read from.
    #
    # English-only throughout except where the Nepali is transcribed verbatim
    # from a cited record. Where a source's Nepali exists only in an inflected
    # form, or its spelling could not be checked against reviewed content, the
    # label stays English-only rather than guessing at the base form.
    # ------------------------------------------------------------------
    "dfmis": {
        "name": {"en": "Development project financing records"},
        # Publisher from the host of EVERY record's own `url`
        # (`dfims.mof.gov.np/projects/<id>`), checked across two pages of 50 —
        # the portal runs on the Ministry of Finance's own domain.
        "publisher": {"en": "Ministry of Finance"},
        # The acronym is deliberately NOT expanded. The portal is a JS-rendered
        # SPA whose served HTML contains only the string "DFIMS", and nothing in
        # this repo expands it, so any expansion here would be invented. Note the
        # token also transposes the host's spelling — see the cleanup write-up.
        "description": (
            "Foreign-aid-funded development projects, each carrying donor, "
            "amount, financing instrument and executing agency, from the "
            "Ministry of Finance's DFIMS portal."
        ),
    },
    # --- The four "type-as-source" tokens ------------------------------
    # These four are not sources at all. Each is a verbatim ``MaterialType``
    # value (`materials/jsonld.py`) standing in the ``source`` column. That is
    # WHY none of them carries a publisher: the token records what KIND of
    # document it is, and nothing whatever about who issued it.
    #
    # The token is free text chosen by whoever uploads. ``source`` is derived
    # from the ``@id`` the CALLER builds (`materials/single_source_ingest.py`),
    # and the only validation is SHAPE — ``is_valid_material_iri`` checks a
    # regex, the host and a length cap, with no allowlist of known tokens
    # anywhere. So any string of the right shape mints a new source on first
    # write. Reaching for the document's type word is what uploaders do with
    # that freedom; it is a human pattern, not a code default.
    #
    # Being a type name is not by itself a defect — `court_order` (43,125) and
    # `official_report` (223) are type names too and are the established,
    # reviewed shelves. The test is whether a more specific source already
    # exists to absorb the rows. For these four it does not, so they stay and
    # are labelled for what they are. For `charge_sheet` and `press_release` it
    # does, which is what makes those two accidents — see the cleanup write-up.
    "news": {
        "name": {"en": "News articles"},
        # No publisher, and here that is a finding rather than caution: the
        # outlet differs per article. A 50-record sample spans Ekantipur, The
        # Kathmandu Post, Nagarik, myRepublica, The Himalayan Times, Rajdhani
        # and others, named in each record's own `name`. Any single publisher
        # here would be false for most of the shelf.
        "description": (
            "Press coverage of corruption and accountability cases, collected "
            "as case evidence. The outlet differs per article and is named on "
            "each record, not on the source."
        ),
    },
    "document": {
        "name": {"en": "Miscellaneous case documents"},
        # 49 of 50 sampled records are `DigitalDocument` with
        # `jawafdehi:sourceType: MISC` — scans, photographs, complaint letters,
        # UN and treaty texts attached to cases as evidence. Saying so is the
        # point of the label: it tells a reader NOT to reach here for a
        # publisher-backed shelf.
        "description": (
            "A heterogeneous bucket of evidence files attached to cases — "
            "scans, photographs, letters and third-party reports. No single "
            "publisher; provenance is on each record."
        ),
    },
    "legal_corpus": {
        "name": {"en": "Laws and bills"},
        # The 13 records span a royal-era land-ceiling exemption, Acts, and a
        # parliamentary bill — different enacting bodies.
        # `@type: Legislation` + `jawafdehi:sourceType: LAW_OR_BILL` on all 13.
        "description": "Acts, bills and ordinances cited as legal background to cases.",
    },
    "social_media": {
        "name": {"en": "Social media posts"},
        # Posts by assorted outlets and individuals.
        # `@type: SocialMediaPosting` + `sourceType: SOCIAL_MEDIA` on all 10.
        "description": (
            "Posts and videos preserved as case evidence, including leaked "
            "footage cited in allegations."
        ),
    },
    "court_filing": {
        "name": {"en": "Court filings"},
        # No publisher, for the same reason as `court_order` above: the five
        # records come from different courts (Tulsipur High Court, Patan High
        # Court, the Special Court) and the issuing court is on each document.
        # `sourceType: COURT_FILING_OTHER` on all 5.
        "description": (
            "Petitions, appeals and bail orders filed in cases, outside the "
            "scraped court-portal corpus."
        ),
    },
    "special_court_order": {
        "name": {"en": "Special Court judgements", "ne": "विशेष अदालतका फैसला"},
        # Publisher and its Nepali from the record's OWN bilingual description
        # (`material/special_court_order/0066`): "Full text of the judgement
        # given by the Special Court" / "विशेष अदालतबाट भएको फैसलाको पूर्णपाठ".
        # The base form विशेष अदालत is attested uninflected in the reviewed
        # frontend registry (the `ag` description), so this is transcription.
        "publisher": {"en": "Special Court", "ne": "विशेष अदालत"},
        "description": "Full-text judgements of the Special Court, uploaded per case.",
    },
    "us_court": {
        "name": {"en": "U.S. federal court records"},
        # Straight from the records' own `publisher` field — present and
        # identical on all 5 (`material/us_court/dcd.24-cr-00302.*`). This is
        # the strongest evidence class available and needs no inference.
        "publisher": {"en": "United States District Court for the District of Columbia"},
        "description": (
            "Dockets, plea agreements and sentencing filings from U.S. "
            "prosecutions touching Nepali cases."
        ),
    },
    "us_sec": {
        "name": {"en": "U.S. SEC enforcement orders"},
        # From the records' own `publisher` field, both records
        # (`material/us_sec/34-101987`, `34-101988`).
        "publisher": {"en": "United States Securities and Exchange Commission"},
        "description": "Settled administrative and cease-and-desist orders of the U.S. SEC.",
    },
    "us_doj": {
        "name": {"en": "U.S. DOJ enforcement agreements"},
        # From the record's own `publisher` field
        # (`material/us_doj/aar-corp-npa-2024`).
        "publisher": {"en": "United States Department of Justice"},
        "description": "Non-prosecution and deferred-prosecution agreements of the U.S. DOJ.",
    },
    "province/koshi": {
        "name": {"en": "Koshi Province budget lines"},
        # No publisher. The records state `jawafdehi:governmentLevel:
        # "provincial"` and `spatialCoverage:
        # entity/location/province/koshi-np01`, which fixes the LEVEL and the
        # province but not the issuing office. The provincial ministry that
        # publishes the red book is not named on any record, and naming one
        # would be exactly the invention this module exists to prevent.
        "description": (
            "Budget sub-heads of the Koshi Province government, carrying fiscal "
            "year (BS), expense head and sub-head. Extracted from a "
            "Preeti-family non-Unicode source, so raw Devanagari needed glyph "
            "remapping rather than OCR."
        ),
    },
    "ppmo_blacklist": {
        "name": {"en": "PPMO blacklist"},
        # The only token here with an in-repo scraper to cite:
        # `courts/management/commands/scrape_ppmo_blacklist.py` ("Refresh the
        # PPMO blacklist (blacklisted firms)"). The four records' own `url`
        # points at `old.ppmo.gov.np/...route=information/black_lists`. The
        # bilingual publisher is reused verbatim from the reviewed `bolpatra`
        # and `ppmo` entries above.
        "publisher": {
            "en": "Public Procurement Monitoring Office",
            "ne": "सार्वजनिक खरिद अनुगमन कार्यालय",
        },
        "description": (
            "Firms debarred from public procurement. Only four records remain "
            "under this token — see the cleanup write-up."
        ),
    },
    "nia": {
        "name": {"en": "Nepal Insurance Authority enforcement notices"},
        # The one record expands its own acronym in BOTH languages
        # (`material/nia/himalayan-re-ban-2026`): "Nepal Insurance Authority —
        # Himalayan Re reinsurance-business ban" / "नेपाल बीमा प्राधिकरण —
        # हिमालयन रि–इन्स्योरेन्स रोक सूचना". Both forms are uninflected, so
        # both are transcribed rather than derived.
        "publisher": {"en": "Nepal Insurance Authority", "ne": "नेपाल बीमा प्राधिकरण"},
        "description": "Regulatory bans and enforcement notices of the insurance regulator.",
    },
    "ratopati": {
        "name": {"en": "Ratopati reporting", "ne": "रातोपाटी"},
        # Outlet named, uninflected, in the record's own bilingual `name`
        # (`material/ratopati/2026-bhatta-seizure-inventory`): "… — Ratopati" /
        # "… — रातोपाटी".
        "publisher": {"en": "Ratopati", "ne": "रातोपाटी"},
        "description": "News reporting from the Ratopati outlet, collected as case evidence.",
    },
    "news_setopati": {
        "name": {"en": "Setopati reporting"},
        # Outlet named in the record's own bilingual description
        # (`material/news_setopati/2082`): "A news article published by
        # Setopati" / "सेतोपाटिले छापेको समाचार". English-only ON PURPOSE: the
        # Nepali appears only inflected (सेतोपाटि-ले), and the base form's
        # spelling — ह्रस्व सेतोपाटि as written here vs दीर्घ सेतोपाटी — could
        # not be checked against any reviewed content. Stripping the case marker
        # would mean picking one spelling on no evidence.
        "publisher": {"en": "Setopati"},
        "description": "News reporting from the Setopati outlet, collected as case evidence.",
    },
    "news_shilaptra": {
        "name": {"en": "Shilapatra reporting"},
        # Settled from the record's ATTACHED DOCUMENT, not from its metadata.
        # `material/news_shilaptra/2082` names no outlet in `name`,
        # `description`, `publisher` or `url` — but its `associatedMedia` RAW
        # link is `https://shilapatra.com/detail/138491`, and fetching that page
        # returns a `<title>` byte-identical to the record's own Nepali name
        # ("बागमतीका मेयर थापाविरुद्ध भ्रष्टाचारको अर्को मुद्दा, भरत ताल बनाउँदा
        # ३० करोड अनियमितताको आरोप"). Outlet confirmed: Shilapatra.
        #
        # Note the TOKEN misspells it — `shilaptra`, missing an `a`. The label
        # follows the outlet's own domain, not the token. English-only: the
        # outlet's masthead spelling in Devanagari was not checked against any
        # reviewed content, and this token has already shown that its own
        # spelling cannot be trusted.
        "publisher": {"en": "Shilapatra"},
        "description": "News reporting from the Shilapatra outlet, collected as case evidence.",
    },
    "occrp": {
        "name": {"en": "OCCRP reporting"},
        # The record names its own publisher as the leading token of its `name`
        # (`material/occrp/2026-bhatta-arrest`): "OCCRP — Nepal arrests power
        # broker Deepak Bhatta…". Left UNEXPANDED: nothing in this repo, the
        # sourcing README or the frontend registry spells the acronym out, so an
        # expansion would be invented. The acronym is still worth recording —
        # it fixes the token to a named outlet instead of leaving it a guess.
        "publisher": {"en": "OCCRP"},
        "description": "Cross-border investigative reporting cited as case evidence.",
    },
    "nrb": {
        "name": {"en": "NRB-FIU reporting-entity lists"},
        # From the record's own `name` (`material/nrb/goaml-member-list`):
        # "NRB-FIU goAML reporting-entity member list". Acronyms left unexpanded
        # for the same reason as `occrp`.
        "publisher": {"en": "NRB-FIU"},
        "description": "Lists of entities required to report to the financial intelligence unit.",
    },
    "sebon": {
        "name": {"en": "SEBON enforcement orders"},
        # From the record's own `name` (`material/sebon/broker55-suspension-2026`):
        # "SEBON — Broker No. 55 (Bhrikuti Stock Broking) 3-month suspension
        # order". Acronym left unexpanded, as for `occrp`.
        "publisher": {"en": "SEBON"},
        "description": "Suspension and enforcement orders of the securities regulator.",
    },
    "dmli": {
        "name": {"en": "DMLI press releases"},
        # Two citations. The record itself (`material/dmli/20260612-abhiyogpatra-pr`)
        # is `additionalType: jawafdehi:PressRelease` and names itself "DMLI
        # press release — charge sheet filed (per-defendant bigo table)". And
        # `docs/ngm/ngm-source-inventory.md` — classed CURRENT and
        # "authoritative" in `docs/DOC-STATUS.md` — places DMLI on the
        # money-laundering track (ALPA 2064, dmli.gov.np, prosecuting to the
        # Special Court). That inventory does NOT expand the acronym either, so
        # neither does this.
        "publisher": {"en": "DMLI"},
        "description": (
            "Press releases of the money-laundering investigation department, "
            "announcing charge sheets filed at the Special Court."
        ),
    },
    # --- Case uploads: the type-as-source convention --------------------
    # `charge_sheet` and `press_release` are NOT accidents, and an earlier
    # revision of this file wrongly called them that. Sourcing a case upload by
    # its ``material_type`` is a deliberate, merged convention — see
    # ``default_policy_for`` in `materials/models.py` ("Case uploads are now
    # sourced by their ``material_type``…"), introduced by #344 on 2026-07-16
    # to move them off the legacy ``jawafdehi`` bucket.
    #
    # So these two are the CASE-UPLOAD counterparts of the scraped corpora
    # (`ag`, `ciaa_press_release`): same document class, genuinely different
    # provenance. Merging them would erase a distinction the platform means to
    # draw. They are labelled as what they are, and each points at its scraped
    # sibling so a reader can tell the two apart.
    "charge_sheet": {
        # Same document class as `ag` — all 33 rows are
        # `additionalType: jawafdehi:ChargeSheet` + `sourceType:
        # AG_ABHIYOG_PATRA`, and the transcript on
        # `charge_sheet/20260324.60019b1d` opens "श्री विशेष अदालत, काठमाडौं
        # समक्ष पेश गरेको आरोपपत्र". Attribution reused from the reviewed `ag`
        # entry; the DIFFERENCE is how the document arrived, not what it is.
        "name": {"en": "Charge sheets (case uploads)", "ne": "अभियोगपत्रहरू"},
        "publisher": {"en": "Office of the Attorney General"},
        "description": (
            "Charge sheets filed at the Special Court in corruption cases, "
            "from the Office of the Attorney General. Uploaded as case "
            "evidence rather than scraped — the scraped corpus is `ag`, which "
            "holds the same kind of document."
        ),
    },
    "press_release": {
        # Same document class as `ciaa_press_release`: all 24 rows are
        # `additionalType: jawafdehi:PressRelease` + `sourceType:
        # CIAA_PRESS_RELEASE`. These 24 carry no `publisher` of their own while
        # the scraped rows do — the shaper sets a field the upload path does
        # not — so the bilingual publisher is reused from that reviewed entry.
        "name": {"en": "CIAA press releases (case uploads)", "ne": "अख्तियारका प्रेस विज्ञप्ति"},
        "publisher": {
            "en": "Commission for the Investigation of Abuse of Authority",
            "ne": "अख्तियार दुरुपयोग अनुसन्धान आयोग",
        },
        "description": (
            "Press releases issued by the Commission for the Investigation of "
            "Abuse of Authority. Uploaded as case evidence rather than "
            "scraped — the scraped corpus is `ciaa_press_release`."
        ),
    },
    # --- Genuinely malformed: labelled, but PENDING A MERGE -------------
    # These two ARE accidents, and the convention above is what proves it: one
    # misspells the convention's own token, the other let a case number leak
    # into it. Both are labelled, because they hold documents a reader can
    # land on, and both state the pending merge so a tidy name does not hide
    # the defect. DELETE THESE ENTRIES WHEN THE MERGE LANDS — a label
    # outliving its token advertises an empty shelf. See
    # `docs/2026-10-29-document-source-cleanup.md`.
    "chargesheet": {
        # A misspelling of `charge_sheet`, not a variant of `ag`: both rows are
        # case-uploaded CIAA charge sheets (Melamchi 080-CR-0106, NTA MDMS
        # 080-CR-0141), so the convention puts them at `charge_sheet`.
        "name": {"en": "Charge sheets (case uploads)", "ne": "अभियोगपत्रहरू"},
        "publisher": {"en": "Office of the Attorney General"},
        "description": (
            "Charge sheets filed at the Special Court in corruption cases, "
            "from the Office of the Attorney General. The token misspells "
            "`charge_sheet`; pending merge into it."
        ),
    },
    "court_order_0133": {
        # Malformed — the case number leaked into the token. Its document is a
        # Special Court judgement
        # (`supremecourt.gov.np/cp/assets/uploads/special_10248.doc`), and the
        # convention sources a case-uploaded court order at `court_order`.
        # NOTE: `special_court_order` is off-convention for the same reason and
        # probably belongs at `court_order` too, but it predates this pass and
        # is not obviously an accident, so it is left alone pending a decision.
        "name": {"en": "Special Court judgements", "ne": "विशेष अदालतका फैसला"},
        "publisher": {"en": "Special Court", "ne": "विशेष अदालत"},
        "description": (
            "Full-text judgements of the Special Court. The token is malformed "
            "— the case number leaked into it — and is pending merge into "
            "`court_order`."
        ),
    },
}

# ----------------------------------------------------------------------
# DELIBERATELY UNLABELLED — do not fill this in without new evidence.
#
# ``cib`` (91 documents)
#     AMBIGUOUS, and ambiguous in exactly the way this module exists to guard
#     against. `docs/ngm/ngm-source-inventory.md` — the authoritative inventory
#     — uses "CIB" for TWO different institutions: the Nepal Police **Central
#     Investigation Bureau** (lines 80, 102: "Nepal Police (CIB)",
#     "cib/ncb.nepalpolice.gov.np") and the **Credit Information Bureau**
#     (line 123: "CIB loan-defaulter blacklist … cibnepal.org.np"). One is a
#     police investigation body, the other a credit registry; a label naming the
#     wrong one would be `ag`-for-Auditor-General all over again.
#
#     THE DOCUMENTS CANNOT BE OPENED TO BREAK THE TIE, and that is worth stating
#     because it is the obvious next move. Two other tokens here WERE settled by
#     following `associatedMedia` to the attached file. That route does not
#     exist for `cib`: all 91 rows are non-`LISTED`, so they return zero from
#     `/api/materials/?source=cib`, zero from `/api/search/` and zero from the
#     MCP control-plane search. No row means no `associatedMedia`, and so no
#     document to open. This is a known property of the corpus rather than a
#     fault here — the meta-repo's `docs/2026-10-11-material-pretty-idents/`
#     `PLAN.md` flags the same 91-vs-0 gap (its risk E12) as a migration hazard.
#
#     WOULD SETTLE IT: an authenticated read of any ONE of the 91 rows. Its
#     `url`, `publisher` or `associatedMedia.contentUrl` would name the body
#     immediately — a domain was exactly what settled `news_shilaptra`. A
#     caseworker bearer (`docs/api-token.md`) or a direct DB read suffices; no
#     new tooling is needed. Failing that, the ingest that minted the token.
# ----------------------------------------------------------------------
#
# ``cib`` is the ONLY unlabelled token. Four others are ingest accidents —
# ``charge_sheet``, ``chargesheet``, ``press_release`` and ``court_order_0133``
# — but they hold real documents, so they are labelled with the attribution of
# the token they duplicate and a description stating the pending merge. See the
# "pending a merge" block at the end of ``SOURCE_LABELS``, and
# `docs/2026-10-29-document-source-cleanup.md` for the corrections themselves.

#: Labels for the series WITHIN a source, keyed ``(source, dataset_bucket)``.
#: Only the Auditor General's token currently holds more than one series.
SERIES_LABELS: dict[tuple[str, str], str] = {
    ("official_report", "report_annual-report"): "Annual reports",
    ("official_report", "report_province-report"): "Province audit reports",
    ("official_report", "publication_audit-journals"): "Audit journal",
    ("official_report", "publication_audit-bulletin"): "Audit bulletins",
    ("official_report", "report_performance-audit-report"): "Performance audit reports",
    ("official_report", "report_it-audit-report"): "IT audit reports",
    (
        "official_report",
        "report_state-owned-enterprises-report",
    ): "State-owned enterprise reports",
    ("official_report", "publication_right-to-information"): "Right-to-information publications",
    ("official_report", "publication_annual-progress-report"): "Annual progress reports",
    ("official_report", "report_report-summary"): "Report summaries",
}


def build_catalogue(materials: dict[str, Any]) -> list[dict[str, Any]]:
    """Join the statistics snapshot's material aggregates into one catalogue.

    ``materials`` is the ``materials`` block of the statistics payload. Reading
    the precomputed snapshot rather than aggregating per request is what keeps
    this an O(1) lookup — the underlying grouped queries run over 346k rows and
    belong in the scheduled refresh, not on a public endpoint.

    A snapshot written before ``by_readability`` existed simply has no such key;
    those sources report ``readable: null``, which a consumer must read as
    "unknown", NOT as zero. Defaulting the absence to 0 would claim we have read
    nothing, which is a different and false statement.
    """
    readable = {row["source"]: row["count"] for row in materials.get("by_readability") or []}
    has_readability = "by_readability" in materials

    series_by_source: dict[str, list[dict[str, Any]]] = {}
    for row in materials.get("by_dataset_bucket") or []:
        source, bucket = row["source"], row["dataset_bucket"]
        series_by_source.setdefault(source, []).append(
            {
                "bucket": bucket,
                "label": SERIES_LABELS.get((source, bucket)),
                "labelled": (source, bucket) in SERIES_LABELS,
                "count": row["count"],
            }
        )

    catalogue = []
    for row in materials.get("by_source") or []:
        source = row["source"]
        label = SOURCE_LABELS.get(source)
        catalogue.append(
            {
                "source": source,
                "name": (label or {}).get("name"),
                "publisher": (label or {}).get("publisher"),
                "description": (label or {}).get("description"),
                # An unlabelled source is marked as such so a consumer can say
                # "I do not know what this token is" rather than guessing from
                # the token's spelling — which is how `ag` becomes "Auditor
                # General".
                "labelled": label is not None,
                "count": row["count"],
                "readable": readable.get(source, 0) if has_readability else None,
                "series": series_by_source.get(source, []),
            }
        )
    return catalogue


# ======================================================================
# The write-path allowlist.
#
# Separate from the labels above, and deliberately in the same file: a new
# source token is a new SHELF in a public archive, so it should cost the same
# one-line reviewed change that a new label costs. Nothing else in the codebase
# constrains the token — ``source`` is derived from the ``@id`` the caller
# builds, and ``is_valid_material_iri`` only checks a regex, the host and a
# length cap. That freedom is how `chargesheet` (a misspelling of
# `charge_sheet`) and `court_order_0133` (a case number that leaked into the
# token) both became permanent public shelves.
# ======================================================================

#: Every source token that may be used to create a NEW material.
#:
#: Seeded 2026-10-29 from the live catalogue — all 34 tokens then in
#: ``/api/document-sources/``, INCLUDING the two known-bad ones. They are listed
#: because their rows exist and must stay writable; see the pending merges in
#: ``docs/2026-10-29-document-source-cleanup.md`` and delete them from here when
#: those land.
KNOWN_SOURCES: frozenset[str] = frozenset(
    {
        # Scraped corpora (one subpackage each under materials/sourcing/).
        "bolpatra",
        "ag",
        "court_order",
        "nkp",
        "ciaa_press_release",
        "ciaa_annual_report",
        "kanun_patrika",
        "official_report",
        "ppmo",
        "ppmo_blacklist",
        "dfmis",
        "cib",
        # Case uploads sourced by their material_type — the #344 convention.
        "news",
        "document",
        "charge_sheet",
        "press_release",
        "legal_corpus",
        "social_media",
        # Named one-off and small sources.
        "court_filing",
        "special_court_order",
        "us_court",
        "us_sec",
        "us_doj",
        "occrp",
        "nrb",
        "nia",
        "sebon",
        "dmli",
        "ratopati",
        "news_setopati",
        "news_shilaptra",
        # Internal/legacy namespaces with no public rows today. Both are live
        # CONSTANTS in the codebase, so omitting them would turn a quiet corner
        # of ingestion into a 3am CronJob failure rather than a CI one:
        #   `court`     — materials.jsonld.COURT_SOURCE, the court-case
        #                 projection (`court_case_to_jsonld`).
        #   `jawafdehi` — jawafdehi_shared.entities.ids.JAWAF_SOURCE, the legacy
        #                 case-upload bucket that #344 moved uploads OFF.
        #                 `default_policy_for` still special-cases it for
        #                 residual rows, so it has to stay writable.
        # Neither appears in /api/document-sources/ (0 public rows), which is
        # exactly why a list seeded only from the live catalogue would miss them.
        "court",
        "jawafdehi",
        # KNOWN BAD — listed only so existing rows stay writable. Remove when
        # the merges land; `chargesheet` → `charge_sheet`,
        # `court_order_0133` → `court_order`.
        "chargesheet",
        "court_order_0133",
    }
)

#: Namespaces where a token is minted per instance rather than enumerated.
#: ``province/<name>`` is the only one: Koshi is live and the other six
#: provinces are an expected, mechanical expansion, so blocking them would be
#: friction with no review value — the editorial decision was made once, for
#: the namespace.
KNOWN_SOURCE_PREFIXES: tuple[str, ...] = ("province/",)


def is_known_source(source: str) -> bool:
    """True iff ``source`` may be used to create a NEW material."""
    if source in KNOWN_SOURCES:
        return True
    # A namespace admits EXACTLY ONE more segment, and it must be non-empty.
    # A bare `startswith` would have accepted `province/koshi/fy2083`, which is
    # the same unreviewed-shelf mistake this gate exists to stop — just one
    # level down.
    for prefix in KNOWN_SOURCE_PREFIXES:
        if not source.startswith(prefix):
            continue
        tail = source[len(prefix) :]
        if tail and "/" not in tail:
            return True
    return False


def unknown_source_error(source: str) -> str:
    """The message a rejected token gets. It must say what to DO.

    A bare "invalid source" would send the reader looking for a bug in their
    payload, when the answer is either a typo on their side or a one-line
    addition here.
    """
    # MOST SPECIFIC match, not the alphabetically first. `court_order_0191`
    # matches both `court` and `court_order`; sorting by name suggested
    # "court", which is useless for precisely the shape this check advertises
    # that it catches.
    near = sorted(
        (k for k in KNOWN_SOURCES if _looks_like(k, source)), key=len, reverse=True
    )
    hint = f" Did you mean {near[0]!r}?" if near else ""
    return (
        f"Unknown material source {source!r}.{hint} A new source token creates a "
        f"new public shelf, so it must be added to KNOWN_SOURCES in "
        f"materials/sources.py (with a label, if one can be confirmed) in a "
        f"reviewed change. Existing materials are unaffected — this check runs "
        f"only when creating a new one."
    )


def _looks_like(known: str, candidate: str) -> bool:
    """Cheap near-miss test, aimed at the two shapes we have actually seen.

    `chargesheet` vs `charge_sheet` differ only by separators; `court_order_0133`
    is a known token with an identifier glued on. Both are caught by comparing
    the separator-stripped forms and by prefix.
    """
    flat = candidate.replace("_", "").replace("-", "").lower()
    return known.replace("_", "") == flat or candidate.startswith(f"{known}_")
