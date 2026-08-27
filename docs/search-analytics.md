# Search analytics — the `search_query` event

Instrumentation that collects the data a future ranking algorithm needs. It does **not** change ranking. Relevance-weight tuning is deferred (unified-search plan §8 Q3); tuning blind — with no record of what people search or which queries return nothing — is guesswork, so this closes the data gap first. When these events have adequate volume we build the algorithm from them.

## What emits it

`GET /api/search/` emits exactly one `search_query` event per request, server-side, in `search/analytics.py` (called from `UnifiedSearchView.get`). Server-side is deliberate: it is the **unbiased** denominator. GA4 is consent-gated (only ~a quarter of humans opt in) so it sees a fraction of traffic and no zero-result signal; this event sees every query.

Emission is best-effort — `emit_search_event` swallows and logs any failure, so a telemetry bug can never turn a good search into a 500.

## Fields

One flat JSON line per query (rendered by the structlog JSON formatter; the log message `search_query` becomes the `event` field, logger name `jawafdehi.search.analytics`):

| field | meaning |
|---|---|
| `search_id` | ephemeral per-response id (hex). The join key to a future client result-click beacon — **not** a user/session id. |
| `q_normalized` | the query text, NFC + trimmed + lowercased + whitespace-collapsed. The demand signal. `""` for a browse. |
| `q_len` | length of `q_normalized`. |
| `has_query` | false for a browse (no query term). |
| `lang` | requested `ne` / `en` / `both`. |
| `types` | requested type filter, sorted; `[]` means all types. |
| `sort` | `relevance` / `newest` / `oldest` / `title`. |
| `page`, `page_size` | paging. |
| `filters` | active refine facets (`case_type` / `entity_type` / `tags` / `status` / `court_level`), taxonomy tokens only; `null` when none. |
| `ranges` | active range bounds (`bigo_min` / `bigo_max` / `date_from` / `date_to`), scalars; `null` when none. |
| `result_count` | total hits. |
| `zero_result` | **the key gap signal** — a real query (`has_query`) that returned nothing. A browse returning nothing is not a miss. |
| `counts_by_type` | per-type hit counts — which index satisfied the demand. |
| `returned` | hits on this page. |
| `took_ms` | wall-clock of the OpenSearch call. |
| `top_type`, `top_score` | type + score of the first hit, **first page only** — the click-through anchor (the best answer shown). |

## Privacy

No user identity — no id, IP, user-agent, session, or referer. `q_normalized` is normalized, not hashed (the query text *is* the signal), and is never attached to a person. Aggregate product telemetry, not an audit trail — route the `jawafdehi.search.analytics` stream to **short retention** (shorter than the general 365-day archive).

## The click loop — `search_click`

`search_id` is echoed in the search response envelope so the SPA can send it back on a result click. `POST /api/search/click` (public, unauthenticated, `SearchClickView`) records that click as a `search_click` event, join-keyed by `search_id` — closing the loop into `(query → shown → clicked)` learning-to-rank judgments, unbiased and consent-free, without ever identifying who clicked. The GA4 `select_search_result` event (rank + term) is the consent-gated mirror; this beacon is the ground truth.

Transport: the SPA uses `navigator.sendBeacon`, which posts `text/plain` (a CORS-safelisted content type → no preflight, survives the click-then-navigate). The view therefore parses the raw request body directly rather than via DRF content negotiation. It is **best-effort**: it always returns `204` (a beacon cannot read the response), emits nothing on a malformed/garbage payload, and never raises.

`search_click` fields:

| field | meaning |
|---|---|
| `search_id` | the id from the search response that produced the clicked list — the join key to its `search_query` event. Not a user/session id. |
| `rank` | 1-based position of the clicked result in the full order (page offset applied). |
| `result_type` | `entity` / `material` / `courtcase` / `case`. |
| `result_id` | the clicked result's public IRI (the envelope `id`). |
| `result_score` | the relevance score it was shown with (optional) — the label side of the LTR signal. |

Same logger/stream/privacy stance as `search_query` (no identity, short retention). Filter the two apart by `event:search_query` vs `event:search_click`.

**Remaining:** the SPA-side `sendBeacon` call on result click (wires `search_id` from the response into the existing click handler that already fires the GA `select_search_result` event).

## Using it to tune relevance (later)

Once volume is adequate:

- **Zero-result queries** (`zero_result:true`, ranked by frequency) → the highest-value backlog: missing synonyms/spellings, analyzer gaps, or genuinely absent corpus. This is what should drive the deferred synonym/fuzziness work — measured, not guessed.
- **`counts_by_type` vs clicks** → whether the cross-type `indices_boost` matches real preference.
- **`top_score` distribution on clicked vs abandoned searches** → a score threshold for "no confident answer".
- **`search_id`-joined clicks by rank** → CTR@k, the training signal for learning-to-rank.

### LogsQL sketches (VictoriaLogs)

```
# top zero-result queries (28d)
event:search_query zero_result:true | stats by (q_normalized) count() hits | sort by (hits) desc | limit 50

# demand by type filter
event:search_query has_query:true | stats by (types) count()

# slow queries
event:search_query took_ms:>500 | fields q_normalized, took_ms, result_count

# clicks by rank → CTR@k shape (are people clicking rank 1, or scrolling?)
event:search_click | stats by (rank) count() clicks | sort by (rank)
```

Joining the two streams by `search_id` (in an offline notebook, not LogsQL) yields the `(query, shown, clicked-rank)` tuples that train a ranking model.
