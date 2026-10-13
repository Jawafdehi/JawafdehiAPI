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
}

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
