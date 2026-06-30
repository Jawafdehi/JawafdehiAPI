# Entity-Resolution / Record-Linkage Scoring Layer — Technology Comparison

**Status:** Research input for a human decision. Not a decision.
**Date:** 2026-06-27
**Author:** automated deep-research (web search + doc fetch), to be reviewed by a human.

## Context / constraints this writeup is fitted to

We are building a bilingual (Nepali Devanagari + Romanized + English) entity-resolution /
record-linkage service. The hard constraints that shape the recommendation:

- **Candidate generation already exists.** OpenSearch does blocking / candidate-gen via
  index-time transliteration. We only need a **scoring/matching layer** that consumes
  candidate pairs.
- **DuckDB is already available** as the lakehouse engine.
- **~1M records** (single-machine scale; not 100M+).
- **Explainability is mandatory.** This is a corruption-accountability platform; every
  match/no-match decision must be *defensible and auditable*.
- **Confidence bands required:** auto-accept / human-review (clerical) / auto-reject.
- **Bilingual / cross-script names** are the core matching difficulty.

> Scope note: this document covers the **scoring/match-decision layer only**. Blocking and
> candidate generation are assumed solved by OpenSearch.

---

## 1. Executive summary / recommendation

**Recommended architecture: a probabilistic Fellegi-Sunter scorer (Splink on DuckDB) for
the match-weight computation and confidence bands, fed by candidate pairs from OpenSearch,
with a deterministic high-confidence pre-pass and a Nepali-specific name-normalization stage
in front of it.**

Rationale, mapped to constraints:

- **Explainability + confidence bands (the hardest constraint):** Fellegi-Sunter is the
  approach official statistics agencies use (UK ONS Census 2021; US Census Bureau / Winkler
  tradition). It produces an **additive log2 match-weight** that decomposes per field and is
  explainable per-pair via Splink's **waterfall chart**, and it natively expresses the
  **two-threshold** auto-accept / clerical-review / auto-reject decision. This is the single
  most defensible option for a corruption-accountability setting. (See §6, §7.)
- **DuckDB already available + 1M scale:** Splink's recommended single-machine backend *is*
  DuckDB; it advertises "linking a million records on a laptop in around a minute." No new
  infra. (See §8.)
- **No labeling burden:** Splink trains m/u probabilities largely **unsupervised** (EM +
  direct estimation). We do not need a labeled training set, unlike `dedupe` (active
  learning) or an ML classifier. (See §3, §8.)
- **Bilingual names:** handled in a **pre-normalization stage** (transliterate
  Devanagari<->Roman to one canonical scheme, strip honorifics, then use Jaro-Winkler /
  Soft-TFIDF comparison levels inside Splink). The scorer itself stays script-agnostic. (See
  §4, §5.)

**The one important caveat (verified, see §8/§9):** Splink does **not** have a first-class way
to inject a pre-computed external candidate-pair list and skip its own blocking. The
maintainer has stated this directly ("there's no easy way to do this in Splink"). The
practical integration is to **encode OpenSearch's candidate pairs as a blockable key**
(a cluster-ID or a neighbour-array column) on the input rows, so Splink's blocking SQL
reconstructs exactly the OpenSearch pairs. If we are unwilling to do that, the fallback is
**`recordlinkage`** (Python toolkit), whose classifiers natively consume pre-computed
comparison vectors / pandas MultiIndex pairs — but it is not designed for 1M scale and is
lightly maintained.

---

## 2. Library comparison table

| Library | Match model | Consumes OpenSearch candidate pairs? | 1M scale | Training / labels | Explainability | License | Maintained 2024-26? |
|---|---|---|---|---|---|---|---|
| **Splink** (MoJ) | Probabilistic Fellegi-Sunter | **Not first-class** — must encode pairs as a blockable key (cluster-id / neighbour-array). Maintainer: "no easy way" | Excellent — DuckDB single-node "~1M on a laptop in ~1 min"; Spark/Athena for 100M+ | None required (unsupervised EM + direct estimation) | **Strong** — additive log2 match weights, waterfall chart, comparison viewer, parameter charts | MIT | **Yes — very active**, v4.0.16 (2026-03-11), v5 in dev |
| **recordlinkage** | FS/ECM, Naive Bayes, logistic reg, SVM, k-means | **Yes, natively** — pairs are a pandas MultiIndex; classifiers take precomputed comparison vectors | Designed for "small/medium" files; pandas in-memory | Mixed; ECM is unsupervised | Good — probabilistic weights | BSD-3 | Slow — v0.16 (2023-07), no 2024-26 release |
| **dedupe** | Active-learning logistic regression | Yes, via lower-level `pairs()`/`score()` | "millions" with lower-level API + Postgres | **Heavy — mandatory human active labeling** | Weaker — confidence score, no per-field weight decomposition | MIT | Yes-ish — v3.0.3 (2024-08), Beta |
| **rltk** (USC ISI) | sklearn classifiers + features | Undocumented | Claims scalable (unverifiable) | sklearn training | Unknown | MIT | **No — abandoned**, still alpha, last commit 2021-10 |
| **custom scoring** | Whatever you build | Yes (by definition) | Your infra | None / your own | As good as you build | n/a | n/a |

Sources: Splink — https://moj-analytical-services.github.io/splink/ , https://github.com/moj-analytical-services/splink , https://pypi.org/project/splink/ ;
recordlinkage — https://recordlinkage.readthedocs.io/en/latest/ref-classifiers.html , https://recordlinkage.readthedocs.io/en/latest/about.html , https://pypi.org/project/recordlinkage/ ;
dedupe — https://pypi.org/project/dedupe/ , https://docs.dedupe.io/en/latest/ , https://github.com/dedupeio/dedupe/blob/main/docs/API-documentation.rst ;
rltk — https://github.com/usc-isi-i2/rltk , https://pypi.org/pypi/rltk/json .

### Per-library notes

- **Splink** — best maintained, most scalable, most explainable, no labels. Implements
  Fellegi-Sunter with term-frequency adjustments. Its weakness for us is purely the
  external-pair injection gap (§8/§9), which has a known workaround.
- **recordlinkage** — the *most natural* fit for "I already have candidate pairs from
  OpenSearch": you build a pandas MultiIndex of the OpenSearch pairs, compute comparison
  vectors, and hand them to a classifier (its own blocking is entirely optional). But it is
  explicitly "developed for ... small or medium sized files," is pandas-in-memory (no
  horizontal scaling), and its last release was July 2023. Viable as a fallback or for a
  prototype.
- **dedupe** — distinguishing cost is a **mandatory human active-learning labeling loop**.
  Wrong choice if we want to avoid labeling. Explainability is weaker than FS.
- **rltk** — **eliminate.** Unmaintained since 2021, never left alpha.
- **custom scoring** — viable and maximally controllable, but you reimplement FS weight
  calibration, threshold tuning, term-frequency adjustment, and transitive clustering that
  Splink gives for free. Reasonable only if the Splink integration friction proves
  unacceptable.

---

## 3. The match-decision model: deterministic vs probabilistic vs ML

This is the core of the "defensibility" question.

### Deterministic rules
- Rules like "if forename AND surname AND DOB agree -> match." Cheap, high precision, but
  "lacking in subtlety" and prone to low recall; brittle and hand-tuned, with rule complexity
  growing as data quality drops.
  (https://moj-analytical-services.github.io/splink/topic_guides/theory/probabilistic_vs_deterministic.html ,
  https://en.wikipedia.org/wiki/Record_linkage)
- Government practice: used as a **high-confidence first pass** with auto-acceptance reserved
  for *unique* deterministic matches. ONS Census 2021 auto-accepted on unique deterministic
  matches, reaching 93.1% (person) automatic match rate, using 35 matchkeys.
  (https://www.ons.gov.uk/peoplepopulationandcommunity/populationandmigration/populationestimates/methodologies/linkagemethodsforcensus2021inenglandandwales)

### Probabilistic — Fellegi-Sunter (1969) — RECOMMENDED CORE
- Origin: Fellegi & Sunter, "A Theory for Record Linkage," JASA 1969.
  (https://www.tandfonline.com/doi/abs/10.1080/01621459.1969.10501049)
- **m-probability** = P(field agrees | true match) (data quality); **u-probability** =
  P(field agrees | non-match) (coincidence / field cardinality).
- Per-field **match weight** = log2 Bayes factor: log2(m/u) on agreement,
  log2((1-m)/(1-u)) on disagreement. Under conditional independence these are **additive**:
  total = prior weight + sum of partial weights; Pr(match) = 2^M / (1 + 2^M).
  (https://moj-analytical-services.github.io/splink/topic_guides/theory/fellegi_sunter.html ,
  https://en.wikipedia.org/wiki/Record_linkage)
- **Two-threshold decision rule** -> three regions: above upper cutoff = match, below lower
  cutoff = non-match, **in between = "possible match" needing clerical/human review**. F&S
  proved this minimizes the clerical region for fixed error levels (given conditional
  independence). This *is* the auto-accept / human-review / auto-reject structure we need.
  (https://en.wikipedia.org/wiki/Record_linkage ,
  https://link.springer.com/rwe/10.1007/978-1-4899-7687-1_712)

### ML classifier (random forest / gradient boosting / neural)
- Can beat FS on accuracy **when sufficient labeled training data exists** — which is
  frequently scarce in ER. (https://en.wikipedia.org/wiki/Record_linkage)
- Gradient boosting etc. "often operate as black boxes" and are "difficult to trust in
  high-stakes applications." Post-hoc explainers (SHAP) exist but are approximations of a
  black box, not intrinsic transparency — an active debate that itself becomes an attack
  surface for a *defensibility* argument.
  (https://shap.readthedocs.io/en/latest/index.html ,
  https://arxiv.org/html/2410.19098v1)
- Nuance: classic FS "is equivalent to the Naive Bayes algorithm," so FS is itself a
  (transparent, additive) statistical classifier — you get probabilistic rigor *and*
  interpretability. (https://en.wikipedia.org/wiki/Record_linkage)

### Confidence bands / clerical review — how thresholds are set
- Three-way (link / possible-link / non-link) with a lower and upper threshold is standard
  (e.g. R `RecordLinkage::optimalThreshold`).
  (https://search.r-project.org/CRAN/refmans/RecordLinkage/html/optimalThreshold.html)
- Thresholds tuned via **precision/recall and ROC**; higher threshold favors precision, lower
  favors recall. Splink generates ROC / precision-recall / confusion-matrix outputs to tune
  them. (https://moj-analytical-services.github.io/splink/topic_guides/theory/probabilistic_vs_deterministic.html)
- Vendors/standards bodies treat clerical review as a configured band (IBM InfoSphere MDM has
  distinct "clerical review" and "autolink" thresholds; US Census Standard C4 governs clerical
  linkage). (https://www.ibm.com/docs/en/imdm/11.5.0?topic=algorithms-setting-clerical-review-autolink-thresholds ,
  https://www.census.gov/about/policies/quality/standards/standardc4.html)
- Even top-scoring automatic matches are not error-free, which is *why* a clerical band exists:
  ONS Census 2021 could not hit target precision/recall by automatic methods alone; clerical
  review handled the genuinely hard cases (automatic ~0.005% false positives vs clerical
  ~0.648%). (ONS link above)

**Decision-model recommendation:** a **hybrid** — deterministic unique-match auto-accept
pre-pass, then Fellegi-Sunter (Splink) probabilistic scoring with two thresholds and an
explicit clerical-review band. This is exactly the ONS Census 2021 design and is the most
defensible. Avoid a black-box ML classifier as the primary decision-maker; if ML is ever
added, keep it advisory and use SHAP, never as the auto-accept authority.

---

## 4. Nepali / Devanagari name-matching specifics

These feed the **normalization stage in front of the scorer**, not the scorer's math.

### Transliteration normalization
- **`indic-transliteration`** (`sanscript`): rule-based conversion among Devanagari, IAST,
  ITRANS, HK, SLP1, WX, Velthuis, OPTITRANS, plus other Indic scripts. API
  `transliterate(data, source, target)`. **Caveat: no documented source-scheme
  auto-detection** — you must declare the source scheme, which matters for mixed input.
  (https://github.com/indic-transliteration/indic_transliteration_py ,
  https://pypi.org/project/indic-transliteration/)
- **Aksharamukha** — broader rule-based converter ("120 scripts, 21 romanization methods"),
  pip-installable, handles vowel length / gemination / nasalization.
  (https://github.com/virtualvinodh/aksharamukha)
- **AI4Bharat IndicXlit** — *learned* transformer transliterator, covers Nepali (`nep`),
  bidirectional Roman<->Devanagari; reported ~80-82% top-1 accuracy for Nepali. MIT.
  Handles messy/colloquial romanization better than rule-based, but ~18-20% error means it
  should feed a *fuzzy* match, not an exact join.
  (https://github.com/AI4Bharat/IndicXlit)
- **IndicTrans2 is machine *translation*, NOT transliteration** — wrong tool for name
  normalization; do not conflate. (https://github.com/AI4Bharat/IndicTrans2)
- **Practical pattern:** transliterate both sides to one canonical scheme (IAST / ISO-15919)
  via rule-based libs (deterministic, reversible, explainable), reserve IndicXlit for messy
  romanization, then run string similarity on the normalized form.

### Phonetic algorithms — do Soundex / Metaphone work on Devanagari?
- **No — standard Soundex/Metaphone/Double Metaphone are Latin-oriented and do not operate on
  Devanagari directly.** The `indic-soundex` library itself states it "does not handle native
  scripts ... works with romanized/transliterated text only."
  (https://github.com/maverickMehul/indic-soundex)
- American Soundex's coarse consonant grouping is phonetically wrong for Indic phonology
  (e.g. it maps c/g/j/k/q/s/x/z to one code), motivating **IndicSOUNDEX** (Amazon Alexa AI,
  DiPersio, NLP4ConvAI 2020) which gives a shared phonemic code matching Devanagari and
  romanized spellings of the same name. **Caveat: measured gains were modest / sometimes not
  statistically significant** (Hindi overall -0.07%, n.s.), so treat it as a helper, not a
  silver bullet. (https://aclanthology.org/2020.nlp4convai-1.1/)
- Alternatives: (a) romanize/transliterate first then apply Soundex/Metaphone; (b)
  Indic-specific Soundex (`indic-soundex`, "SoundEx Algorithm Revisited for Indian Language");
  (c) **Beider-Morse (BMPM)** for personal names — but BMPM's supported language list does
  **not include any Indic language** (only English, French, German, Greek, Hebrew, Hungarian,
  Italian, Polish, Romanian, Russian, Spanish, Turkish), so it only applies post-romanization
  with a generic ruleset. (https://solr.apache.org/guide/solr/latest/indexing-guide/phonetic-matching.html ,
  https://link.springer.com/chapter/10.1007/978-981-13-2354-6_6)

### Honorifics / titles (normalize as stopwords)
- "Shri/Shree" (श्री) honorific prefix (Mr./respected), many romanizations: Sri, Sree, Shri,
  Shree, Shiri; "Shrimati/Smt" (married women); "Sushri" (women). Appended honorifics:
  "Ji/Jee", "Jiyu", "Sir/Madam", kinship terms "Dai/Didi/Uncle/Aunty". None are part of the
  legal name -> strip before scoring, in both Latin and Devanagari, collapsing spelling
  variants to one token. (https://en.wikipedia.org/wiki/Shri ,
  https://nepyork.com/2024/06/29/honoring-tradition-using-nepali-honorifics-even-in-english-communication/)
  - *Gap:* "Babu" as a Nepali honorific specifically was not confirmed (it is a known broader
    South Asian honorific); add with low confidence.

### Married-name / surname changes
- Conventionally a Nepali wife's surname changes to the husband's after marriage, though now
  flexible (retain natal, adopt husband's, or both). **Implication: surname is an unreliable
  match key for married women across time** — a surname-only change should not by itself
  defeat a match; weight given+middle name and external attributes (DOB, relations) higher.
  (https://www.kuragraphy.com/2023/01/kuragraphy-of-names-and-naming.html ,
  https://culturalatlas.sbs.com.au/nepalese-culture/nepalese-culture-naming)

### Naming structure / patronymics
- Typical structure: first/personal name + optional middle + surname (thar/clan), e.g.
  "Mohan Bahadur Limbu." Middle names common and semi-stopword-like (Prasad, Bahadur, Devi,
  Kumari) -> weight lower; a missing middle name should not block a match. Surnames are
  patrilineal and encode caste/ethnicity (Sherpa, Tharu/Chaudhary, Limbu/Rai). **False-friend
  risk:** occupational/positional titles (Pradhan, Subedar, Thekedar) can become hereditary
  and look like surnames. (https://www.kuragraphy.com/2023/01/kuragraphy-of-names-and-naming.html ,
  https://culturalatlas.sbs.com.au/nepalese-culture/nepalese-culture-naming)

---

## 5. String similarity metrics for transliterated names

- **Jaro-Winkler** — adds a prefix bonus to Jaro (prefix capped at 4 chars, p=0.1). Favored
  for short personal names because the first ~4 chars carry identifying weight; Splink:
  "particularly useful for names." Originated in US Census record linkage (Jaro 1989, Winkler
  1990). **Weaknesses:** not a true metric (violates triangle inequality); the prefix weighting
  is a *liability* when the discriminating difference is at the start (a real risk for
  transliteration variants that diverge in the first letter, e.g. Yusuf/Jusuf).
  (https://en.wikipedia.org/wiki/Jaro%E2%80%93Winkler_distance ,
  https://moj-analytical-services.github.io/splink/topic_guides/comparisons/comparators.html)
- **Levenshtein / Damerau-Levenshtein** — Damerau adds adjacent-transposition; >80% of human
  spelling errors are one of the four edit types. Beats Jaro-Winkler when errors are interior
  or end-of-string (data-entry miskeys, transpositions). Normalize to similarity via
  `1 - dist/max(len)`. (https://en.wikipedia.org/wiki/Damerau%E2%80%93Levenshtein_distance)
- **Token-based** — Jaccard/Cosine are order-independent (handle given/surname swaps); TF-IDF
  down-weights common tokens (frequent surnames). Good for multi-word full names.
- **Hybrid (the benchmark winners)** — **Monge-Elkan** and **Soft-TFIDF** tokenize, then use a
  secondary char-level similarity (Jaro-Winkler) to match similar-but-not-identical tokens —
  tolerating **both reordering and per-token spelling drift**, exactly the transliterated-name
  case. *Caveat:* py_stringmatching exposes Monge-Elkan / Soft-TFIDF only as raw,
  un-normalized scores. (https://anhaidgroup.github.io/py_stringmatching/v0.4.x/Tutorial.html)
- **The classic benchmark — Cohen, Ravikumar, Fienberg (2003)**, "A Comparison of String
  Distance Metrics for Name-Matching Tasks": the best overall is the **hybrid Soft-TFIDF**
  (TF-IDF weighting + Jaro-Winkler secondary). Among edit methods Monge-Elkan was best but
  Jaro-Winkler was close and ~10x faster. Pure token methods do poorly on misspelling-heavy
  data — i.e. char-aware methods matter when there is intra-token noise (the transliteration
  case). **Caveat:** this benchmark is English/Latin-script and pre-neural, so it proves
  "best among classic metrics for English names," not "best for cross-script."
  (https://www.cs.cmu.edu/~wcohen/postscript/ijcai-ws-2003.pdf)
- **Embedding / neural cross-script** — fine-tuned multilingual sentence-transformers can
  materially beat string distance for true cross-script matching (e.g. eridu reports
  Latin<->Cyrillic name similarity rising from ~0.74 pretrained to ~0.99 fine-tuned). **Strong
  caveats:** eridu self-describes as "not yet ready for production" and unbenchmarked; neural
  methods trade away explainability and add per-comparison cost — a poor fit for the
  auditability requirement *as the decision-maker*. Use only as an advisory recall booster.
  (https://github.com/Graphlet-AI/eridu , https://arxiv.org/abs/1607.04606)
- **Python libs:** `rapidfuzz` (C++, fast — edit + Jaro-Winkler + token scorers), `jellyfish`
  (Rust — Levenshtein/Damerau/Jaro-Winkler + phonetic), `py_stringmatching` (the full
  token/hybrid family incl. Monge-Elkan, Soft-TFIDF), `textdistance` (broad wrapper, install
  with extras for speed). (https://github.com/maxbachmann/RapidFuzz ,
  https://jamesturk.github.io/jellyfish/ , https://pypi.org/project/textdistance/)

**Metric recommendation:** single-token romanized names -> Jaro-Winkler (Damerau-Levenshtein
when transpositions dominate); multi-token full names with reordering + spelling drift ->
**Soft-TFIDF** (TF-IDF + Jaro-Winkler secondary). All of these are available as Splink
comparison levels (Jaro-Winkler, Levenshtein, Damerau-Levenshtein, Jaccard) with
**term-frequency adjustments** that approximate TF-IDF down-weighting of common surnames.

---

## 6. How Splink works with DuckDB (our lakehouse engine)

- **Backends:** DuckDB, Spark, Athena, Postgres, SQLite. **DuckDB is the recommended
  single-machine backend**; Spark/Athena are for 100M+.
  (https://github.com/moj-analytical-services/splink , https://pypi.org/project/splink/)
- **Scale:** README — "Capable of linking a million records on a laptop in around a minute."
  Author benchmark: dedup a 7M-record dataset (~1B comparisons) in just over 2 minutes for
  under $1 on EC2; 300k records in under a minute on 8 cores. **No stated hard record cap**;
  docs give parallelism tuning (`input_rows / 122,880 x rule parallelism`), salting for
  <500k rows, and spill-to-disk for larger-than-memory. Our ~1M scale is comfortably inside
  the headline figure. (https://www.robinlinacre.com/fast_deduplication/ ,
  https://moj-analytical-services.github.io/splink/topic_guides/performance/optimising_duckdb.html)
- **Training:** hybrid FS estimation — direct estimation for the prior (lambda) and u
  probabilities (random sampling), **EM** for m probabilities (run round-robin over different
  blocking columns, since you can't estimate the column you block on). **Largely unsupervised
  — no labeled data required**; user-supplied deterministic rules act as believed-true match
  heuristics to anchor the estimate.
  (https://moj-analytical-services.github.io/splink/topic_guides/training/training_rationale.html)
- **Comparison library:** `splink.comparison_level_library` confirmed to include
  `LevenshteinLevel`, `DamerauLevenshteinLevel`, `JaroLevel`, `JaroWinklerLevel`,
  `JaccardLevel`, `CosineSimilarityLevel`, etc.; higher-level `NameComparison`,
  `ForenameSurnameComparison`, `DateOfBirthComparison`. **Term-frequency adjustments**
  supported (incl. joint forename+surname TF).
  (https://moj-analytical-services.github.io/splink/api_docs/comparison_level_library.html ,
  https://moj-analytical-services.github.io/splink/topic_guides/comparisons/out_of_the_box_comparisons.html)
- **Explainability features:** match-weights chart, **waterfall chart** (per-pair decision
  decomposition), comparison viewer dashboard, parameter-estimate / m-u parameter charts,
  cluster studio. (https://moj-analytical-services.github.io/splink/charts/index.html ,
  https://moj-analytical-services.github.io/splink/demos/tutorials/06_Visualising_predictions.html)
- **Version / license:** Splink 4 is current (v4.0.16, 2026-03-11; Splink 5 in dev). MIT.
  Actively maintained by MoJ Analytical Services (~136 releases). Python >=3.9,<4.0.
  (https://pypi.org/project/splink/ , https://github.com/moj-analytical-services/splink)

---

## 7. The OpenSearch -> Splink integration gap (read this before committing)

This is the most decision-relevant finding and was **cross-checked between two independent
sub-agents, which disagreed** — reconciled here:

- One source suggested Splink's `compare_records` realtime API could score externally-supplied
  pairs. **However**, the dedicated Splink investigation found **maintainer-confirmed
  statements that there is no first-class way to inject a pre-computed external candidate-pair
  set and bypass Splink's own blocking**: RobinL (maintainer) — "there's no easy way to do
  this in Splink - you'd have to look at the source code and make changes" (discussion #2822)
  and "Not easily" (#1922). An open, unanswered feature request "Scoring a provided
  blocked_id_pairs table" (#2950, Mar 2026) confirms it remains a gap.
  (https://github.com/moj-analytical-services/splink/discussions/2822 ,
  https://github.com/moj-analytical-services/splink/discussions/1922)
- `compare_records` is real but framed as a **realtime, one-pair-at-a-time** API, not a bulk
  pathway for 1M pairs. Treat the "score external pairs in bulk" path as **not first-class**.

**The supported workaround (maintainer-recommended):** resolve OpenSearch's candidate pairs
into either (a) **cluster IDs** attached to each input record, or (b) a **neighbour-array
column** of each row's OpenSearch-matched IDs, then write a Splink blocking rule
(`block_on(cluster_id)` or a `CustomRule` using `array_contains`) so Splink's blocking SQL
**reconstructs exactly the OpenSearch pairs**. This keeps OpenSearch as the blocker and Splink
as the FS scorer, at the cost of one transformation step.

**If that friction is unacceptable:** `recordlinkage` consumes pre-computed pairs natively
(pandas MultiIndex + comparison vectors -> ECM/FS classifier), at the cost of 1M-scale
suitability and maintenance freshness; or build a **custom FS scorer in DuckDB SQL** (we own
DuckDB anyway) — more work, but full control and the same explainable log2-weight math.

---

## 8. Proposed end-to-end pipeline (fitted to constraints)

1. **Normalize (bilingual stage):** transliterate Devanagari<->Roman to one canonical scheme
   (indic-transliteration / Aksharamukha; IndicXlit for messy romanization); strip honorific/
   title stopwords (Shri/Shree/Sri/Ji/Jiyu/Mr/Dr/Dai/Didi/Pradhan...) in both scripts; split
   name components; flag surname as low-confidence for married women.
2. **Block / candidate-gen:** OpenSearch (already built).
3. **Bridge:** materialize OpenSearch candidate pairs as a cluster-ID / neighbour-array key on
   the input rows (see §7).
4. **Score:** Splink on DuckDB — FS match weights using Jaro-Winkler / Damerau-Levenshtein /
   Jaccard comparison levels + term-frequency adjustments; deterministic unique-match pre-pass
   auto-accepts the easy cases first.
5. **Decide with confidence bands:** two thresholds tuned via precision/recall + ROC ->
   auto-accept / clerical-review / auto-reject.
6. **Explain / audit:** Splink waterfall chart per decision; comparison viewer for QA;
   persist match weights for defensibility.

---

## 9. Key risks / open questions for the human reviewer

- **Splink external-pair bridging** adds an engineering step and is not the library's
  designed-for path. Validate the cluster-ID/array-blocking workaround on a real sample early.
- **Conditional-independence assumption** of FS is violated by correlated fields (e.g.
  forename+middle); Splink's TF adjustments and combined comparisons mitigate but don't
  eliminate this.
- **Nepali phonetic matching** has only modest measured gains (IndicSOUNDEX); don't over-invest
  before measuring on our data. BMPM has no Indic support.
- **Cohen 2003** "Soft-TFIDF is best" is English/Latin-script and pre-neural — confirm on a
  labeled Nepali sample rather than assuming it transfers to cross-script.
- **Embedding/neural matching** can boost cross-script recall but is unbenchmarked here and
  hurts explainability; keep it advisory, never the auto-accept authority on a corruption-
  accountability platform.

---

## 10. Cited sources

**Libraries / Splink**
- https://moj-analytical-services.github.io/splink/
- https://github.com/moj-analytical-services/splink
- https://pypi.org/project/splink/
- https://moj-analytical-services.github.io/splink/topic_guides/theory/fellegi_sunter.html
- https://moj-analytical-services.github.io/splink/topic_guides/theory/probabilistic_vs_deterministic.html
- https://moj-analytical-services.github.io/splink/topic_guides/blocking/blocking_rules.html
- https://moj-analytical-services.github.io/splink/topic_guides/training/training_rationale.html
- https://moj-analytical-services.github.io/splink/api_docs/comparison_level_library.html
- https://moj-analytical-services.github.io/splink/topic_guides/comparisons/out_of_the_box_comparisons.html
- https://moj-analytical-services.github.io/splink/topic_guides/comparisons/comparators.html
- https://moj-analytical-services.github.io/splink/topic_guides/performance/optimising_duckdb.html
- https://moj-analytical-services.github.io/splink/charts/index.html
- https://moj-analytical-services.github.io/splink/demos/tutorials/06_Visualising_predictions.html
- https://github.com/moj-analytical-services/splink/discussions/2822
- https://github.com/moj-analytical-services/splink/discussions/1922
- https://www.robinlinacre.com/fast_deduplication/
- https://recordlinkage.readthedocs.io/en/latest/ref-classifiers.html
- https://recordlinkage.readthedocs.io/en/latest/about.html
- https://pypi.org/project/recordlinkage/
- https://pypi.org/project/dedupe/ , https://docs.dedupe.io/en/latest/
- https://github.com/usc-isi-i2/rltk

**Decision model / explainability / official statistics**
- https://www.tandfonline.com/doi/abs/10.1080/01621459.1969.10501049 (Fellegi-Sunter 1969)
- https://en.wikipedia.org/wiki/Record_linkage
- https://www.ons.gov.uk/peoplepopulationandcommunity/populationandmigration/populationestimates/methodologies/linkagemethodsforcensus2021inenglandandwales
- https://www.census.gov/about/policies/quality/standards/standardc4.html
- https://www.ibm.com/docs/en/imdm/11.5.0?topic=algorithms-setting-clerical-review-autolink-thresholds
- https://search.r-project.org/CRAN/refmans/RecordLinkage/html/optimalThreshold.html
- https://shap.readthedocs.io/en/latest/index.html
- https://arxiv.org/html/2410.19098v1
- https://en.wikipedia.org/wiki/William_E._Winkler

**Name matching — Nepali / Devanagari / metrics**
- https://github.com/indic-transliteration/indic_transliteration_py , https://pypi.org/project/indic-transliteration/
- https://github.com/virtualvinodh/aksharamukha
- https://github.com/AI4Bharat/IndicXlit , https://github.com/AI4Bharat/IndicTrans2
- https://github.com/maverickMehul/indic-soundex
- https://aclanthology.org/2020.nlp4convai-1.1/ (IndicSOUNDEX)
- https://link.springer.com/chapter/10.1007/978-981-13-2354-6_6
- https://solr.apache.org/guide/solr/latest/indexing-guide/phonetic-matching.html (BMPM)
- https://en.wikipedia.org/wiki/Shri
- https://nepyork.com/2024/06/29/honoring-tradition-using-nepali-honorifics-even-in-english-communication/
- https://www.kuragraphy.com/2023/01/kuragraphy-of-names-and-naming.html
- https://culturalatlas.sbs.com.au/nepalese-culture/nepalese-culture-naming
- https://www.cs.cmu.edu/~wcohen/postscript/ijcai-ws-2003.pdf (Cohen-Ravikumar-Fienberg 2003)
- https://en.wikipedia.org/wiki/Jaro%E2%80%93Winkler_distance
- https://en.wikipedia.org/wiki/Damerau%E2%80%93Levenshtein_distance
- https://anhaidgroup.github.io/py_stringmatching/v0.4.x/Tutorial.html
- https://github.com/maxbachmann/RapidFuzz , https://jamesturk.github.io/jellyfish/ , https://pypi.org/project/textdistance/
- https://github.com/Graphlet-AI/eridu , https://arxiv.org/abs/1607.04606

---

*Research limitations: sub-agents relied primarily on direct documentation/PyPI/GitHub/PDF
fetches (web search was intermittently unavailable). Self-reported/vendor numbers (IndicXlit
accuracy, eridu similarity gains) are flagged inline and not independently benchmarked. The
Splink external-pair finding was the one place two sub-agents disagreed; it is reconciled in
§7 in favor of the maintainer-confirmed "not first-class" position.*
