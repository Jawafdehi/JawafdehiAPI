# OpenSearch config for bilingual English + Nepali (Devanagari) full-text & entity-name search

**Scope:** Self-hosted OpenSearch (verified against OpenSearch docs, current as of 2.x / mid-2026), free/OSS plugins only. Single platform serving BOTH entity-name matching ("Sher Bahadur Deuba" ↔ "शेर बहादुर देउवा") and document/OCR full-text.
**Verification note:** Load-bearing claims below are cited to official OpenSearch docs. Where OpenSearch and Elasticsearch diverged, only OpenSearch behavior is asserted. Items I could NOT verify are flagged "UNVERIFIED".

---

## 1. Devanagari / Nepali analysis in OpenSearch

- **No built-in `nepali` analyzer.** The full built-in language-analyzer list is: Arabic, Armenian, Basque, Bengali, Brazilian, Bulgarian, Catalan, CJK, Czech, Danish, Dutch, English, Estonian, Finnish, French, Galician, German, Greek, **Hindi**, Hungarian, ICU, Indonesian, Irish, Italian, Latvian, Lithuanian, Norwegian, Persian, Polish, Portuguese, Romanian, Russian, Sorani, Spanish, Swedish, Thai, Turkish, Ukrainian. **Nepali is absent.** (https://docs.opensearch.org/latest/analyzers/language-analyzers/index/)
- **`hindi` analyzer is the closest reusable Devanagari chain.** Its internal pipeline = `standard` tokenizer → `lowercase` → `decimal_digit` → `keyword_marker` → **indic_normalization** → **hindi_normalization** → `stop` (`_hindi_`) → `hindi` stemmer. Supports `stem_exclusion` and custom `stopwords`. (https://docs.opensearch.org/latest/analyzers/language-analyzers/hindi/)
  - **Reuse the `indic_normalization` token filter** (Indic-script char canonicalization) for Nepali — it is script-level, not Hindi-specific.
  - **Do NOT reuse `hindi` stemmer or `_hindi_` stopwords for Nepali.** Hindi morphology/stopwords ≠ Nepali; applying them silently corrupts Nepali recall. See §6.

- **analysis-icu plugin — free, OSS, self-host installable (NOT bundled).** It is an Apache-2.0 *core* plugin (same Apache-2.0 license as OpenSearch), installed per node with:
  ```
  bin/opensearch-plugin install analysis-icu
  ```
  Restart the node after install; install on every data node. It is not in the default bundled-plugin set, so it must be installed explicitly. (https://docs.opensearch.org/latest/install-and-configure/plugins/ ; https://docs.opensearch.org/latest/analyzers/tokenizers/icu-tokenizer/)
  Components it provides:
  - **`icu_tokenizer`** — Unicode UAX#29 word segmentation; better word boundaries for Asian/complex scripts than `standard`. (https://docs.opensearch.org/latest/analyzers/tokenizers/icu-tokenizer/)
  - **`icu_normalizer`** char filter — Unicode normalization; `name` ∈ {`nfc`,`nfd`,`nfkc`,`nfkc_cf`(default)}, `mode` ∈ {`compose`(default),`decompose`}, optional `unicode_set_filter`. (https://docs.opensearch.org/latest/analyzers/character-filters/icu-normalization/)
  - **`icu_folding`** token filter — UTR#30 case-fold + diacritic removal + ligature/width normalization; already does Unicode normalization (no separate normalizer needed after it). Optional `unicode_set_filter`. (https://docs.opensearch.org/latest/analyzers/token-filters/icu-folding/)
  - **`icu_transform`** token filter — script transliteration / case mapping via ICU transform `id` (chainable with `;`). (https://docs.opensearch.org/latest/analyzers/token-filters/icu-transform/)

- **Recommended Nepali Devanagari analyzer chain:**
  `icu_normalizer` (char filter, NFC) → `icu_tokenizer` → `decimal_digit` → `indic_normalization` → `lowercase` → (optional Nepali stopword `stop` filter from a custom list) → (NO automatic stemmer).
  **Gaps:** no OSS Nepali stemmer and no built-in Nepali stopword list ship with OpenSearch — both are CUSTOM (§6).

---

## 2. Cross-script matching ("Sher Bahadur Deuba" ↔ "शेर बहादुर देउवा")

Three patterns; recommended is a **hybrid of dual-indexing + index-time transliteration**:

| Pattern | How | Recall | Precision | Verdict |
|---|---|---|---|---|
| Query-time transliteration | transliterate the query, OR both forms | medium | medium | brittle; avoid as sole strategy |
| Multi-field dual indexing | store raw + per-script subfields | high | high | **adopt** |
| Index-time transliteration | add a `.translit` Latin field via `icu_transform` (`Devanagari-Latin`/`Any-Latin`) so Devanagari docs are findable by Latin query, and vice-versa | high | medium | **adopt as the bridge field** |

- **`icu_transform` is the in-engine bridge.** OpenSearch docs confirm `icu_transform` "enabling operations such as transliteration"; example uses `id: "Any-Latin"`. The docs explicitly list `Any-Latin`, `Latin-Cyrillic`, `Lower`/`Upper`, `Hiragana-Katakana`, and `NFD; [:Nonspacing Mark:] Remove; NFC`. **`Devanagari-Latin` is a standard ICU transform ID but is NOT named in the OpenSearch doc page** — treat it as UNVERIFIED in-engine and TEST it; `Any-Latin` IS documented and covers Devanagari→Latin generically. (https://docs.opensearch.org/latest/analyzers/token-filters/icu-transform/)
- **For Latin→Devanagari at index time, ICU is weaker** (Latin→Indic transliteration is ambiguous). Prefer to handle that direction at ingest with an external library.
- **External OSS transliteration libraries (use at ingest, not in-engine):**
  - **`indic-transliteration` (Python, MIT)** — Devanagari ⇄ Latin (IAST/Harvard-Kyoto/SLP1/WX/ITRANS etc.). Permissive, safe to vendor. v2.3.82 (Apr 2026). **Recommended for ingest-side romanization.**
  - **Aksharamukha (Python, `pip install aksharamukha`)** — 120 scripts, 21 romanizations, highest quality. **License is AGPL-3.0** — a copyleft/network-copyleft flag for a hosted platform; prefer running it as an isolated batch/offline preprocessing step or as a separate boundaried service, and get legal sign-off before embedding in the main service. v2.3 (Oct 2024).
  - **IndicXlit (AI4Bharat, MIT)** — neural transliteration, best for noisy/phonetic Latin→Indic; heavier (model weights), use offline if needed. (UNVERIFIED specifics here; treat as optional.)
- **Recommended:** index-time `icu_transform Any-Latin` for the in-engine bridge field (cheap, no extra service) PLUS, for high-value entity names, an ingest-side `indic-transliteration` romanized form stored in `name.translit`. This maximizes recall; precision is recovered via field boosting (raw/native scripts boosted above `.translit`).

---

## 3. Field mapping & querying

- **Multi-field a bilingual name/title** as: base `name` (script-agnostic, mixed-script tolerant), `name.ne` (Nepali chain), `name.en` (English chain), `name.translit` (romanized bridge), `name.exact` (keyword, normalized).
- **Mixed-script single field:** the base `name` field uses the `icu_*` mixed-script analyzer (icu_normalizer + icu_tokenizer + icu_folding) which tokenizes Latin and Devanagari in one string without language assumptions — good default for OCR full-text where scripts interleave.
- **Querying:**
  - **`most_fields`** for entity-name search across `name.ne`/`name.en`/`name.translit` — "multiple fields hold the same text analyzed differently"; sums per-field scores so a hit in any analysis form contributes. **This is the recommended default for names.**
  - **`cross_fields`** when a name is split across separate fields (e.g., `first_name`/`last_name`); term-centric, but **only works on fields sharing one analyzer** and **does not support fuzziness**. Use only for structured split-name fields.
  - **`best_fields`** for full-text document/OCR relevance where matching words should co-occur in one field.
  (https://docs.opensearch.org/latest/query-dsl/full-text/multi-match/)
  - Boost native-script subfields above `.translit` to keep precision high while transliteration provides recall.

---

## 4. Normalization essentials (which char/token filters)

| Concern | Filter | Notes |
|---|---|---|
| Unicode NFC canonicalization | **`icu_normalizer`** char filter (`name: nfc`) | run as a CHAR filter (pre-tokenize). Essential. |
| Devanagari digit folding ०-९ ↔ 0-9 | **`decimal_digit`** token filter | docs confirm it folds Devanagari & Arabic-Indic digits to ASCII 0–9. Essential. |
| Zero-width joiner / non-joiner (ZWJ/ZWNJ) | `icu_normalizer` (NFC handles most) + optional `mapping`/`pattern_replace` char filter to strip U+200C/U+200D | partial; add explicit strip if OCR introduces stray ZW chars. |
| Nukta normalization | `indic_normalization` token filter | Indic-script canonicalization (nukta, variant forms). Essential for Devanagari. |
| Case folding / diacritics (Latin side) | `lowercase` or `icu_folding` | `icu_folding` is heavier (also normalizes); use `lowercase` on Devanagari-only chains to avoid over-folding. |

Order matters: char filters (NFC, ZW strip) → tokenizer → `decimal_digit` → `indic_normalization` → case-folding → stopwords.

---

## 5. Concrete index settings + mappings + query DSL

> Requires `analysis-icu` installed on every node. `ne_stop` uses a custom Nepali stopword file (`config/nepali_stopwords.txt`) — see §6; remove the filter if you have no list yet.

```json
PUT /entities
{
  "settings": {
    "analysis": {
      "char_filter": {
        "nfc_normalizer": { "type": "icu_normalizer", "name": "nfc", "mode": "compose" },
        "strip_zerowidth": { "type": "mapping", "mappings": ["\\u200C=>", "\\u200D=>"] }
      },
      "filter": {
        "ne_stop": { "type": "stop", "stopwords_path": "nepali_stopwords.txt" },
        "to_latin": { "type": "icu_transform", "id": "Any-Latin; Lower" },
        "en_stemmer": { "type": "stemmer", "language": "english" },
        "en_stop": { "type": "stop", "stopwords": "_english_" }
      },
      "analyzer": {
        "mixed_script": {
          "type": "custom",
          "char_filter": ["nfc_normalizer", "strip_zerowidth"],
          "tokenizer": "icu_tokenizer",
          "filter": ["decimal_digit", "indic_normalization", "icu_folding"]
        },
        "nepali_text": {
          "type": "custom",
          "char_filter": ["nfc_normalizer", "strip_zerowidth"],
          "tokenizer": "icu_tokenizer",
          "filter": ["decimal_digit", "indic_normalization", "lowercase", "ne_stop"]
        },
        "english_text": {
          "type": "custom",
          "char_filter": ["nfc_normalizer"],
          "tokenizer": "icu_tokenizer",
          "filter": ["lowercase", "en_stop", "en_stemmer"]
        },
        "translit_latin": {
          "type": "custom",
          "char_filter": ["nfc_normalizer", "strip_zerowidth"],
          "tokenizer": "icu_tokenizer",
          "filter": ["decimal_digit", "indic_normalization", "to_latin", "lowercase"]
        },
        "exact_normalized": {
          "type": "custom",
          "char_filter": ["nfc_normalizer", "strip_zerowidth"],
          "tokenizer": "keyword",
          "filter": ["icu_folding"]
        }
      }
    }
  },
  "mappings": {
    "properties": {
      "name": {
        "type": "text",
        "analyzer": "mixed_script",
        "fields": {
          "ne":      { "type": "text", "analyzer": "nepali_text" },
          "en":      { "type": "text", "analyzer": "english_text" },
          "translit":{ "type": "text", "analyzer": "translit_latin" },
          "exact":   { "type": "keyword", "normalizer": "lowercase" }
        }
      },
      "body": {
        "type": "text",
        "analyzer": "mixed_script",
        "fields": {
          "ne": { "type": "text", "analyzer": "nepali_text" },
          "en": { "type": "text", "analyzer": "english_text" }
        }
      }
    }
  }
}
```

**Entity-name query (bilingual, cross-script via translit bridge):**
```json
GET /entities/_search
{
  "query": {
    "bool": {
      "should": [
        { "multi_match": {
            "query": "Sher Bahadur Deuba",
            "type": "most_fields",
            "fields": ["name^3", "name.ne^4", "name.en^2", "name.translit^2"],
            "fuzziness": "AUTO"
        }},
        { "match": { "name.exact": { "query": "Sher Bahadur Deuba", "boost": 5 } } }
      ],
      "minimum_should_match": 1
    }
  }
}
```
The same query works for the Devanagari input "शेर बहादुर देउवा": native scripts hit `name`/`name.ne`, and the `to_latin` (`Any-Latin`) `name.translit` field lets a Latin query match Devanagari docs (and vice-versa). Note `most_fields` permits `fuzziness` (unlike `cross_fields`).

**Full-text/OCR document query** — use `best_fields` over `body` + `body.ne` + `body.en`.

---

## 6. What stays CUSTOM / has no clean OSS answer

- **Nepali stemming.** No OSS Nepali stemmer ships in OpenSearch (only Hindi). Hindi stemmer ≠ Nepali and will degrade recall — do not substitute. Options: ship without stemming (rely on icu/indic normalization + fuzziness — recommended baseline), or integrate an external Nepali morphological analyzer offline (no production-grade OSS standard exists; UNVERIFIED quality).
- **Nepali stopwords.** No built-in `_nepali_` list. Provide a curated `config/nepali_stopwords.txt` per node (CUSTOM); start small (postpositions/conjunctions) to avoid dropping name particles.
- **Transliteration ambiguity.** Devanagari→Latin is many-to-many (schwa deletion, vowel length, अ/आ). `icu_transform Any-Latin` is deterministic but lossy; `indic-transliteration`/Aksharamukha differ by scheme. Treat `.translit` as a recall booster only, never as an exact key.
- **Honorifics / name particles.** "श्री", "जी", "माननीय", "Dr.", "Hon." etc. should be stripped or down-weighted via a CUSTOM `synonym`/`stop` filter or ingest normalization — no OSS list covers Nepali honorifics.
- **Bikram Sambat dates in free text.** OpenSearch has no BS calendar support; BS↔Gregorian conversion must happen at ingest (the codebase already has a `convert_date` capability) and be indexed as structured `date` fields — full-text search alone will not reconcile २०८१ BS vs 2024-25 CE.
- **Aksharamukha AGPL-3.0** is a licensing constraint for a hosted service — keep it offline/boundaried or use MIT `indic-transliteration` instead.

---

## Summary (6 lines)
1. No `nepali` analyzer in OpenSearch; build a CUSTOM Nepali chain reusing `indic_normalization` + `icu_*` (verified, no Nepali stemmer/stopwords ship).
2. `analysis-icu` is free Apache-2.0, NOT bundled — install via `bin/opensearch-plugin install analysis-icu` on every node (verified).
3. Cross-script matching = multi-field dual indexing + an index-time `icu_transform Any-Latin` `.translit` bridge field (`Any-Latin` verified; `Devanagari-Latin` unverified in-engine — test it).
4. Query names with `most_fields` (supports fuzziness) over boosted native-script + translit subfields; `best_fields` for OCR full-text; `cross_fields` only for split structured names.
5. Essential normalizers: `icu_normalizer` (NFC) char filter, `decimal_digit` (०-९→0-9, verified), `indic_normalization` (nukta), ZW-strip mapping filter.
6. CUSTOM/no clean OSS: Nepali stemming, Nepali stopwords, honorifics, transliteration ambiguity, Bikram Sambat dates (convert at ingest).

**Adopt (OSS, in-engine):** analysis-icu (`icu_normalizer`/`icu_tokenizer`/`icu_folding`/`icu_transform`), `indic_normalization`, `decimal_digit`, multi-field mapping, `most_fields`/`best_fields` queries.
**Custom (no clean OSS):** Nepali stemmer, Nepali stopword list, honorific handling, transliteration-quality tuning + ingest-side `indic-transliteration` (MIT), Bikram Sambat date conversion at ingest.
