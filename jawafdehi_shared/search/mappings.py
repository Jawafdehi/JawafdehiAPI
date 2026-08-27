"""Bilingual (English + Nepali/Devanagari) OpenSearch index settings & mappings.

This is the **infrastructure substrate** for the platform's unified search: the
analyzer/char-filter/token-filter chains and the common index document mapping
that every per-app index (entities, materials, court cases, cases) shares. The
per-app *indexers* and the unified query/merge service are a LATER phase — this
module only produces the JSON dicts you hand to ``indices.create``.

Config provenance: see ``shared/research/opensearch-bilingual-nepali.md`` and the
unified-search plan §3.3/§3.4. Key facts encoded here:

* OpenSearch has **no built-in ``nepali`` analyzer**; we build a custom Devanagari
  chain reusing the script-level ``indic_normalization`` token filter (NOT the
  Hindi stemmer/stopwords, which corrupt Nepali recall).
* Requires the **``analysis-icu``** plugin (Apache-2.0, free, NOT bundled) on
  every node: ``bin/opensearch-plugin install analysis-icu``. It provides
  ``icu_normalizer`` (NFC char filter), ``icu_tokenizer``, ``icu_folding`` and
  ``icu_transform`` (the ``Any-Latin`` transliteration bridge).
* Cross-script matching ("Sher Bahadur Deuba" <-> "शेर बहादुर देउवा") = multi-field
  dual indexing + an index-time ``icu_transform Any-Latin`` ``.translit`` bridge
  field, plus an ingest-side romanized ``title_translit`` (see ``transliterate``).
* Names query with ``most_fields`` (supports fuzziness); OCR/body with
  ``best_fields``. Boost native-script subfields above ``.translit``.

Caveats baked in as comments (no clean OSS answer): no Nepali stemmer, no built-in
Nepali stopword list, Bikram Sambat dates are carried verbatim as ``keyword`` (never
coerced to a ``date`` mapping).
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def index_settings() -> dict[str, Any]:
    """Return the ``settings`` block (analysis chains) for a bilingual index.

    Returns a fresh deep copy each call so callers can mutate (e.g. set
    ``number_of_shards``) without corrupting the shared template.

    NOTE: requires the ``analysis-icu`` plugin installed on every node. The
    ``Any-Latin`` ICU transform is the documented Devanagari->Latin bridge; the
    narrower ``Devanagari-Latin`` id is unverified in-engine (see research §2) and
    is intentionally NOT used here.
    """
    return {
        "analysis": {
            "char_filter": {
                # Unicode NFC canonicalization, run pre-tokenize. Essential so the
                # same grapheme indexed/queried as composed vs decomposed matches.
                "nfc_normalizer": {
                    "type": "icu_normalizer",
                    "name": "nfc",
                    "mode": "compose",
                },
                # Strip stray zero-width joiner / non-joiner (U+200C / U+200D),
                # which OCR frequently introduces in Devanagari conjuncts.
                "strip_zerowidth": {
                    "type": "mapping",
                    "mappings": ["\\u200C=>", "\\u200D=>"],
                },
            },
            "filter": {
                # Devanagari digits ०-९ -> ASCII 0-9 (also Arabic-Indic). Essential.
                # (decimal_digit is built in; listed implicitly by use.)
                # ICU transliteration bridge: any script -> Latin, lowercased.
                # This is what lets a Latin query hit a Devanagari doc in the
                # in-engine .translit field (recall booster, never an exact key).
                "to_latin": {
                    "type": "icu_transform",
                    "id": "Any-Latin; Lower",
                },
                # English side: standard stemmer + stopwords.
                "en_stemmer": {"type": "stemmer", "language": "english"},
                "en_stop": {"type": "stop", "stopwords": "_english_"},
                # NOTE: no Nepali stemmer and no built-in Nepali stopword list ship
                # with OpenSearch. We deliberately ship WITHOUT a Nepali stemmer and
                # without a stopword filter (relying on icu/indic normalization +
                # query-time fuzziness) to avoid silently dropping name particles.
                # To add Nepali stopwords later: define a "stop" filter with
                # stopwords_path="nepali_stopwords.txt" (a per-node config file) and
                # append it to the nepali_text analyzer below.
            },
            "analyzer": {
                # Script-agnostic default for mixed-script OCR/full-text: tokenizes
                # interleaved Latin + Devanagari without language assumptions.
                "mixed_script": {
                    "type": "custom",
                    "char_filter": ["nfc_normalizer", "strip_zerowidth"],
                    "tokenizer": "icu_tokenizer",
                    "filter": ["decimal_digit", "indic_normalization", "icu_folding"],
                },
                # Devanagari / Nepali chain. indic_normalization handles nukta &
                # variant forms (script-level, not Hindi-specific). No stemmer.
                "devanagari": {
                    "type": "custom",
                    "char_filter": ["nfc_normalizer", "strip_zerowidth"],
                    "tokenizer": "icu_tokenizer",
                    "filter": ["decimal_digit", "indic_normalization", "lowercase"],
                },
                # Roman / English chain.
                "roman": {
                    "type": "custom",
                    "char_filter": ["nfc_normalizer"],
                    "tokenizer": "icu_tokenizer",
                    "filter": ["lowercase", "en_stop", "en_stemmer"],
                },
                # In-engine transliteration bridge: emits a Latin form of whatever
                # script was indexed, so cross-script queries match.
                "translit_bridge": {
                    "type": "custom",
                    "char_filter": ["nfc_normalizer", "strip_zerowidth"],
                    "tokenizer": "icu_tokenizer",
                    "filter": [
                        "decimal_digit",
                        "indic_normalization",
                        "to_latin",
                        "lowercase",
                    ],
                },
                # Keyword-style exact-but-normalized (whole-string) analyzer.
                "exact_normalized": {
                    "type": "custom",
                    "char_filter": ["nfc_normalizer", "strip_zerowidth"],
                    "tokenizer": "keyword",
                    "filter": ["icu_folding"],
                },
            },
        }
    }


def common_mappings() -> dict[str, Any]:
    """Return the ``mappings`` block: the common indexed-document fields shared by
    every per-app index (plan §3.3).

    Returns a fresh deep copy each call.

    Bilingual title is dual-indexed: ``title_ne`` (devanagari), ``title_en``
    (roman), and ``title_translit`` (the ingest-side romanized form, analyzed with
    the translit bridge for in-engine cross-script reach). Names are queried with
    ``most_fields`` across these; OCR ``body`` with ``best_fields``.
    """
    bilingual_text_with_translit = {
        "type": "text",
        "analyzer": "mixed_script",
        "fields": {
            "ne": {"type": "text", "analyzer": "devanagari"},
            "en": {"type": "text", "analyzer": "roman"},
            "translit": {"type": "text", "analyzer": "translit_bridge"},
        },
    }
    return deepcopy(
        {
            "properties": {
                # Canonical join/dedup key (entity/material @id IRI; synthesized
                # app:type:id for courtcase/case). Document _id mirrors this.
                "iri": {"type": "keyword"},
                # schema.org @type / record type. Also the facet field the unified
                # search exposes as ``entity_type``.
                "type": {"type": "keyword"},
                "source_app": {"type": "keyword"},
                # Platform case classification (Jawafdehi cases + NGM court cases).
                # Promoted to a top-level keyword (also kept in ``raw``) so it can be
                # both filtered and faceted by the unified search.
                "case_type": {"type": "keyword"},
                # Bilingual title, dual-indexed + translit bridge. A ``.keyword``
                # subfield on each side gives a sortable (untokenized) value for the
                # alphabetical "title" sort — text fields themselves aren't sortable.
                "title_ne": {
                    "type": "text",
                    "analyzer": "devanagari",
                    "fields": {"keyword": {"type": "keyword"}},
                },
                "title_en": {
                    "type": "text",
                    "analyzer": "roman",
                    "fields": {"keyword": {"type": "keyword"}},
                },
                "title_translit": {"type": "text", "analyzer": "translit_bridge"},
                # Free-text / OCR body. Mixed-script default + per-language subfields.
                "body": bilingual_text_with_translit,
                # Tags/keywords: exact (keyword) AND analyzed (text) so both
                # term-precise containment and fuzzy text match work.
                "keywords": {
                    "type": "keyword",
                    "fields": {"text": {"type": "text", "analyzer": "mixed_script"}},
                },
                # Court case_number, material ident, nes_id links, IRI fragments.
                "identifiers": {"type": "keyword"},
                # ISO/Gregorian dates.
                "created_at": {"type": "date"},
                "updated_at": {"type": "date"},
                "date": {"type": "date"},
                # Bikram Sambat carried VERBATIM as a string — never coerced to a
                # date mapping (BS has no OpenSearch calendar support; convert at
                # ingest if a Gregorian value is also needed).
                "date_bs": {"type": "keyword"},
                # NGM court-case verdict/status facets (from the re-added CourtCase
                # columns). verdict_date_bs MUST be keyword for the SAME reason as
                # date_bs: a BS string like "2081-09-32" (day 32 is valid in BS,
                # not Gregorian) would be rejected by a dynamically-inferred `date`
                # mapping and the doc would silently drop from the index. The AD
                # verdict_date is a real Gregorian date.
                "verdict_date_bs": {"type": "keyword"},
                "verdict_date": {"type": "date"},
                "verdict_type": {"type": "keyword"},
                "status": {"type": "keyword"},
                # Coarse Jawafdehi-case lifecycle (ongoing/closed/others). A dedicated
                # keyword so the unified search can facet/filter cases WITHOUT
                # colliding with the generic ``status`` above (which NGM uses for its
                # internal scraper enrichment flag pending/enriched/failed). Only
                # Jawafdehi cases set this; other doc types leave it absent.
                "case_status": {"type": "keyword"},
                # Court tier for NGM court cases (district/high/supreme/special),
                # lower-cased from ``Court.court_type`` at index time. Same
                # single-type pattern as ``case_status`` above: only court-case
                # docs set it. Same rebuild caveat as ``weight`` below: an
                # existing index only gains the mapping on the next --rebuild
                # generation, until which the facet returns zero buckets and a
                # filter matches nothing (safe degradation, no errors).
                "court_level": {"type": "keyword"},
                # Editorial priority behind the ``featured`` sort; cases-only, same
                # single-type pattern as ``case_status`` above. Only FRESH indices get
                # this declared type — an existing one picks the field up by dynamic
                # mapping on reindex, which is why _sort_spec sets ``unmapped_type``.
                "weight": {"type": "integer"},
                # बिगो — the alleged embezzled/disputed amount, in whole NPR.
                # ``long`` mirrors ``Case.bigo`` (a BigIntegerField): the corpus
                # already holds values into the tens of अरब, which overflow a 32-bit
                # ``integer``. Promoted to a TOP-LEVEL field purely so it can be
                # range-queried — the copy inside the card payload lives under
                # ``raw``, which is ``enabled: false`` and therefore not indexed at
                # all. Only Jawafdehi cases set this; other doc types leave it
                # absent (and a ``range`` clause excludes a doc missing the field,
                # which is the behaviour we want for an unrecorded amount).
                "bigo": {"type": "long"},
                # Full serialized JSON-LD / record, return-only (not searchable).
                "raw": {"type": "object", "enabled": False},
            }
        }
    )
