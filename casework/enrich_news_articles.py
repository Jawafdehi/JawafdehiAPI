#!/usr/bin/env python
"""Attach verified independent news coverage to a case's `evidence[]`. LOCAL WRITES ONLY.

Ported from the deleted `casework/enrich_news_articles.py` (recovered at donor
commit `0321a85`, 1,957 lines -- the largest in the donor set), with the
web-archive half of the deleted `cases/management/commands/add_news_permalinks.py`
(recovered at `4c39d8c^`, 323 lines) folded in.

WHY THIS STAGE MATTERS MORE THAN ITS POSITION SUGGESTS. A published case carries
~6.9 documents; the CIAA press release and the court order are the starting pair
and news is most of what gets added (168 of 405 PUBLISHED evidence entries, 41%).
DRAFT has essentially none. This is also the only stage that brings in an
INDEPENDENT publisher, so it is what stops a case resting solely on the CIAA's
own account of it.

THE VERIFICATION GATE IS THE WHOLE POINT. A news article wrongly attached to a
corruption case publicly links named people to a case they may have nothing to do
with. A missed article costs a caseworker five minutes; a wrong one is a legal and
ethical failure. Production already carries two such binds -- see
`tests/casework/news_labelled_set.py` and this task's `findings.md`.

SPLIT IN TWO, AND ONLY ONE HALF CAN WRITE. `casework/news_search.py` does all
searching, fetching, archive lookup and LLM verification and cannot write
anything; it does not import `CaseworkApi`. Everything that mutates server state
is in `apply_plan` below, and `_require_loopback` refuses off-loopback there
regardless of `--allow-remote-writes`. Between them sits `plan_case`, which is
pure: it decides, and writes nothing.

Six deliberate deviations from the donor. Two more (the no-publication-date skip
and the `confidence == "high"` bar) are wholly inside `news_search.py` and are
documented there as deviations A and B.

DEVIATION 1 -- THE DONOR'S WRITE PATH IS GONE, NOT PORTED. The donor created
`NEWS DocumentSource` rows (`api.create_source` + `api.add_evidence`,
donor:1553/1581). The "cases own no documents" ADR removed that model. A news
article now becomes a `Material` (`POST /api/materials/`, JSON-LD, source segment
`news`) and is bound as a `CaseMaterialReference` through the destructive
whole-list `PATCH /evidence`. The material JSON-LD is built by the SERVER'S OWN
shaper, `materials.jsonld.documentsource_to_jsonld`, so what this writes is
byte-compatible with the 48 `/material/news/*` rows already in production; the
IRI form is not invented here (see `news_search.news_material_ident`).

DEVIATION 2 -- THE NOTE IS WRITTEN AT BIND TIME, NEVER BLANK. `bind_materials.py
:143` appends new evidence with `additional_details: ""` and leaves a later stage
to backfill. This stage has the article in hand and the verifier has already
produced the Nepali note, so binding blank would cost a second document fetch of
a document already read and strand the entry meanwhile. The register and length
come from the 33 news notes on the 15 IN_REVIEW cases, read from production
2026-08-05: min 351, median 494, max 759 characters. (The brief says ">120
characters", measured on an earlier snapshot -- the real floor is nearly three
times that, so the prompt asks for 350-500.)

DEVIATION 3 -- `event_type` IS A SELECTION RULE ONLY; IT IS NOT PERSISTED. The
donor stored it on the evidence row and recomputed per-event coverage from it on
the next run (donor:883). `EvidenceItemSerializer` accepts exactly
`{material_iri, additional_details}` -- there is nowhere to put it. So
one-article-per-event-type holds WITHIN a run, and cross-run idempotency rests on
the two guards that survive a round trip: the material IRI is derived from the
article, so the same story can never bind twice; and a case already at
`--max-articles` news entries is skipped whole (the donor's own saturation gate,
donor:1464). What is lost is the ability to say "this case still needs a verdict
article" from the case payload alone.

DEVIATION 4 -- THE TRANSCRIPTION PASS IS NOT PORTED. The donor converted each
saved article to markdown and attached it upstream via `sourcing.converter` and
an `.../sources/<id>/markdown/` endpoint (donor:1606). That endpoint went with
the DocumentSource model, and `casework/convert.py` is now the stage that turns a
bound RAW material into a MARKDOWN role. Re-implementing it here would give this
stage a second write path and duplicate `convert`. Consequence to know: a
freshly bound news material has RAW (+ PERMALINK) and no MARKDOWN, so
`materials.source_text` cannot read it until `convert` has run. That is the
pipeline's ordering, not a gap.

DEVIATION 5 -- THE WRITE IS ONE CONDITIONAL WHOLE-LIST REPLACE, GATED ON `If-Match`.
The donor wrote per article and left an orphan source behind when the second call
failed (donor:1587, "ORPHAN source ... needs manual cleanup"). Here every
material is upserted first, then the FULL merged evidence list goes in one
`replace_list` conditional on the ETag read at plan time. A 412 means the case
changed under us, so the merge is stale and nothing is written. A material
upserted without its bind is not an orphan needing cleanup: its IRI is derived
from the article, so the next run computes the same IRI, re-upserts the same
document, and binds it.

DEVIATION 6 -- THE DESTRUCTIVE WRITE IS DRAFT-ONLY. The donor processed DRAFT and
IN_REVIEW alike (donor:1713). Reads still cover both, so `--slug` on an IN_REVIEW
case produces a full review file to judge; the WRITE refuses, for
`bind_materials.py`'s reason -- `/evidence` is a whole-list replace and a case a
human is actively reviewing is the worst one to clobber.

Usage:
    uv run python -m casework.enrich_news_articles --slug case-0123 --max-articles 3
    uv run python -m casework.enrich_news_articles --limit 3 --verbose
    uv run python -m casework.enrich_news_articles --slug case-0123 --apply
"""

import argparse
import collections
import logging
import os
import sys
import time
import urllib.parse
from dataclasses import dataclass, field

from casework.common.api import CaseworkApi
from casework.common.cli import (
    add_common_args,
    basic_auth_from_env,
    configure_run_logging,
    log_event,
    log_run_footer,
    log_run_header,
    print_summary,
    setup_logging,
)
from casework.common.llm import bootstrap, tier_for
from casework.common.materials import materials_of_type, raw_links, source_chunks
from casework.common.pipeline import PRESS_TYPES, STAGES, RunReport, unmet_prerequisites
from casework.common.review import ReviewRow, build_review_file
from casework.common.select import select_for_run
from casework.news_search import (
    ALL_EVENT_TYPES,
    CANDIDATE_BATCH_SIZE,
    DEFAULT_KEYED_BUDGET,
    DEFAULT_SEARCH_PROVIDER,
    EVENT_LIFECYCLE_ORDER,
    EVENT_OTHER,
    MAX_ARTICLES_PER_EVENT_TYPE,
    PROVIDER_QUOTAS,
    QUERY_LIMIT,
    SEARCH_BUDGET_ENV,
    NearMiss,
    SearchBudget,
    SearchOutcome,
    SearchUnavailable,
    SkipReason,
    WebClient,
    build_queries,
    default_budget_for,
    fallback_queries,
    fetch_article,
    generate_devanagari_names,
    generate_english_queries,
    news_material_ident,
    normalize_article_url,
    resolve_permalink,
    resolve_search_provider,
    search,
    verify_batch,
)

log = logging.getLogger("casework.enrich_news_articles")

STAGE = STAGES["news"]
STAGE_NAME = "news"
EVIDENCE_PATH = "evidence"
NEWS_MATERIAL_TYPE = "news"
NEWS_MATERIAL_SOURCE = "news"
#: The whole-list `/evidence` replace is destructive, so only a case nobody is
#: reviewing may be written. See deviation 6.
WRITABLE_STATE = "DRAFT"
#: Donor default (donor:1823).
DEFAULT_MAX_ARTICLES = 5
#: Retry rounds of broader fallback queries when nothing was accepted (donor:899).
RETRY_MAX = 3


def _require_loopback(api):
    """Refuse any non-loopback host at the write itself.

    `CaseworkApi._request` already guards writes and `--allow-remote-writes` is
    its opt-in. This is a SECOND, unconditional guard that flag cannot open, in
    the style of `casework/convert.py::upload_markdown` -- and for a stronger
    reason. This stage is the one that creates rows in the SHARED materials
    store and binds them to public cases; the credential a production run holds
    is read-only by policy, and a write attempted with it is a policy breach
    whether or not it succeeds. So the script refuses to try.
    """
    host = urllib.parse.urlsplit(api.base_url).hostname
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError(
            f"enrich_news_articles writes to loopback ONLY; refusing to create "
            f"materials or bind evidence on {api.base_url!r}. Production is "
            f"read-only for this stage: run without --apply to produce the "
            f"review file.")


# ---------------------------------------------------------------------------
# Reading the case's current news evidence.
# ---------------------------------------------------------------------------


def current_evidence(case):
    """The case's evidence normalised to the `{material_iri, additional_details}`
    shape the PATCH expects, order preserved.

    Byte-identical to `bind_materials.current_evidence` and imported-by-copy on
    purpose: this is the contract for what a whole-list replace must send back,
    and the two writers of `/evidence` must not be able to disagree about it.
    Consolidating them into `casework/common/` is a separate change that would
    edit a module this port has no other reason to touch.
    """
    return [
        {"material_iri": e.get("material_iri"),
         "additional_details": e.get("additional_details") or ""}
        for e in (case.get("evidence") or [])
        if e.get("material_iri")
    ]


def bound_news_urls(case):
    """Every article URL already bound to this case, normalised for comparison.

    Reads `material.urls` on the resolved evidence entries; the donor read
    `source.urls` (donor:862), which the ADR removed. Normalised through
    `normalize_article_url` so a re-run recognises the same story behind a
    tracking query or a `www.` prefix -- the same normalisation the material
    ident is derived from, so the two cannot disagree.
    """
    urls = set()
    for material in materials_of_type(case, (NEWS_MATERIAL_TYPE,)):
        for link in raw_links(material):
            urls.add(normalize_article_url(link))
    return urls


def count_news_evidence(case):
    return len(materials_of_type(case, (NEWS_MATERIAL_TYPE,)))


def merge_news_evidence(current, additions):
    """Union-merge `additions` into `current`, preserving existing order.

    `bind_materials.merge_evidence` with one difference: an appended entry
    carries its note instead of `""` (deviation 2). Everything else is that
    function's contract, and it is the load-bearing half -- an existing entry is
    never reordered, rewritten or dropped, because the server deletes every row
    and recreates from exactly what is sent, so any omission destroys data. An
    addition whose IRI is already present is skipped rather than allowed to
    overwrite the note a human may have edited.
    """
    have = {e["material_iri"] for e in current}
    merged = list(current)
    for iri, note in additions:
        if iri in have:
            continue
        merged.append({"material_iri": iri, "additional_details": note})
        have.add(iri)
    return merged


# ---------------------------------------------------------------------------
# The read phase. Search -> fetch -> verify -> select. No writes.
# ---------------------------------------------------------------------------


def _select_accepted(pairs, outcome, budget, event_counts):
    """Move bindable verdicts into `outcome.accepted`, capped per event type.

    `event_counts` is a `Counter` of event types, mutated so successive batches
    see what earlier ones took. The cap is `MAX_ARTICLES_PER_EVENT_TYPE` rather
    than a hardcoded 1 so the donor's pinned value is what governs: the point of
    spreading binds across the lifecycle is that five articles about the same
    verdict corroborate less than one each about the filing, hearing, verdict and
    appeal.

    A relevant-but-not-high verdict becomes a `NearMiss` (deviation B); an
    irrelevant one a skip. Returns the number accepted from this batch.

    EVERY pair is classified, including once the budget is full. The budget guards
    only the ACCEPT, and it used to `break` the whole loop instead: remaining pairs
    fell out of `accepted`, `near_misses` and `skipped` alike, so premium tokens
    were spent on verdicts that left no trace in the counts or the review file --
    and a `medium` verdict past the cap vanished, breaking deviation B's promise
    that a near miss is always reported for a human to confirm.
    """
    taken = 0
    for article, verdict in pairs:
        if not verdict.relevant:
            if verdict.failed:
                reason = SkipReason.VERIFY_FAILED
            elif verdict.reason == str(SkipReason.GATE_REJECTED):
                reason = SkipReason.GATE_REJECTED
            else:
                reason = SkipReason.VERIFY_REJECTED
            outcome.add_skip(article.url, reason, verdict.reason)
            continue
        if not verdict.is_bindable:
            outcome.near_misses.append(NearMiss(article=article, verdict=verdict))
            continue
        if event_counts[verdict.event_type] >= MAX_ARTICLES_PER_EVENT_TYPE:
            outcome.add_skip(article.url, SkipReason.EVENT_TYPE_FULL,
                             f"event_type={verdict.event_type}")
            continue
        if len(outcome.accepted) >= budget:
            outcome.add_skip(article.url, SkipReason.BUDGET_REACHED,
                             f"--max-articles budget of {budget} already filled")
            continue
        outcome.accepted.append((article, verdict))
        event_counts[verdict.event_type] += 1
        taken += 1
    return taken


def _run_queries(client, queries, seen_urls, outcome):
    """Search every query, returning de-duplicated candidate dicts."""
    candidates = []
    for query in queries:
        results = search(client, query)
        fresh = 0
        for result in results:
            key = normalize_article_url(result["url"])
            if key in seen_urls:
                continue
            seen_urls.add(key)
            candidates.append(result)
            fresh += 1
        log.info("  query %r -> %d results (%d new)", query[:70], len(results), fresh)
    outcome.n_candidates += len(candidates)
    return candidates


def _consume_candidates(client, candidates, case, outcome, budget, event_counts,
                        already_bound, invoke_json, usage, press_release_text):
    """Fetch and verify `candidates` in batches until `budget` is filled.

    Fetching is batched at `CANDIDATE_BATCH_SIZE` so verification is one gate
    call plus one premium call per batch rather than per article -- the donor's
    batching, and the reason a 12-candidate case costs 2 LLM calls and not 24.

    Stops early once every lifecycle event type is at its cap: there is nothing
    left a further batch could be accepted INTO, so continuing would spend
    premium calls on candidates that can only be skipped (donor:1254).
    """
    for start in range(0, len(candidates), CANDIDATE_BATCH_SIZE):
        if len(outcome.accepted) >= budget:
            return
        if all(event_counts[event] >= MAX_ARTICLES_PER_EVENT_TYPE
               for event in ALL_EVENT_TYPES):
            return
        articles = []
        for candidate in candidates[start:start + CANDIDATE_BATCH_SIZE]:
            if normalize_article_url(candidate["url"]) in already_bound:
                outcome.add_skip(candidate["url"], SkipReason.ALREADY_LINKED)
                continue
            article, reason = fetch_article(client, candidate)
            if article is None:
                outcome.add_skip(candidate["url"], reason)
                continue
            articles.append(article)
        if not articles:
            continue
        pairs = verify_batch(articles, case, invoke_json, usage,
                             press_release_text=press_release_text,
                             tier=tier_for(STAGE_NAME))
        _select_accepted(pairs, outcome, budget, event_counts)


def collect_for_case(case, client, invoke_json, usage, *, max_articles,
                     press_release_text=None, force=False):
    """The whole read phase for one case. Returns a `SearchOutcome`.

    Writes nothing and takes no `api` -- it cannot. Ordering follows the donor:
    the cheap tier romanises the accused names into English queries, those plus
    the template/event/name queries are searched, candidates are fetched and
    verified in batches, and if nothing was accepted the broader fallback
    queries are tried up to `RETRY_MAX` times (donor:1412).
    """
    outcome = SearchOutcome()
    # `max_articles` is a per-case TOTAL, so the budget for THIS run is what is
    # left of it. Using it as the run's addition let a case already carrying 4
    # news entries take 5 more under `--max-articles 5` -- 9 on a case documented
    # to cap at 5, written through a destructive whole-list replace. The
    # saturation skip in `_process_case` reads the same constant as a total
    # (`n_current >= args.max_articles`), so the two disagreed.
    # `--force` means "treat this case as fresh", so it restores the full budget
    # as well as ignoring what is already bound -- otherwise forcing a saturated
    # case would search, verify and then accept nothing.
    budget = max_articles if force else max(0, max_articles - count_news_evidence(case))
    already_bound = set() if force else bound_news_urls(case)
    if budget <= 0:
        return outcome
    # Starts empty rather than seeded from the case, because `event_type` is not
    # persisted on an evidence row (deviation 3) -- there is nothing on the case
    # to seed it FROM. Cross-run safety comes from the saturation gate and the
    # derived material IRI instead.
    event_counts = collections.Counter()

    english = generate_english_queries(case, invoke_json, usage)
    # Second cheap-tier call, for the Devanagari spelling of the accused. NES
    # stores Latin names, so without this every Devanagari template searches
    # "Bikal Poudel विशेष अदालत ठहर" while the article says "विकल पौडेल".
    devanagari = generate_devanagari_names(case, invoke_json, usage)
    outcome.queries = build_queries(case, llm_english_queries=english,
                                    devanagari_names=devanagari)
    if not outcome.queries:
        return outcome

    seen_urls = set()
    candidates = _run_queries(client, outcome.queries, seen_urls, outcome)
    # Longest snippet first: the donor's ordering (donor:1507). A fuller snippet
    # correlates with a real article body rather than a listing stub, so the
    # candidates most likely to survive the prefilter are fetched first.
    candidates.sort(key=lambda c: len(c.get("snippet") or ""), reverse=True)
    _consume_candidates(client, candidates, case, outcome, budget, event_counts,
                        already_bound, invoke_json, usage, press_release_text)

    for attempt in range(RETRY_MAX):
        if outcome.accepted:
            break
        queries = fallback_queries(case, attempt)
        log.info("  retry %d/%d: %d fallback queries", attempt + 1, RETRY_MAX,
                 len(queries))
        outcome.queries += queries
        retry_candidates = _run_queries(client, queries, seen_urls, outcome)
        if not retry_candidates:
            continue
        _consume_candidates(client, retry_candidates, case, outcome, budget,
                            event_counts, already_bound, invoke_json, usage,
                            press_release_text)

    # Bind in lifecycle order -- investigation, filing, hearing, verdict, appeal
    # (donor:1535). Evidence renders in list order on the public case, so the
    # reader gets the case's chronology rather than whichever query happened to
    # return first. `EVENT_OTHER` sorts last by construction.
    outcome.accepted.sort(
        key=lambda pair: EVENT_LIFECYCLE_ORDER.get(pair[1].event_type,
                                                   EVENT_LIFECYCLE_ORDER[EVENT_OTHER]))
    return outcome


# ---------------------------------------------------------------------------
# The plan. Pure -- decides everything, writes nothing.
# ---------------------------------------------------------------------------


@dataclass
class NewsPlan:
    """What one case would have written, and why. Never performs any of it."""
    slug: str
    action: str                  # WOULD_BIND | NOOP | SKIP_STATE | SATURATED | UNMET
    state: str = ""
    if_match: str | None = None
    n_current: int = 0
    materials: list = field(default_factory=list)   # [(iri, jsonld_doc, note, Article)]
    patch_items: list = field(default_factory=list)  # the FULL merged evidence list
    outcome: SearchOutcome = field(default_factory=SearchOutcome)
    reason: str = ""

    @property
    def n_merged(self):
        return len(self.patch_items) if self.action == "WOULD_BIND" else self.n_current

    @property
    def bound_iris(self):
        return [iri for iri, _, _, _ in self.materials]


def _material_doc(article, note, permalink):
    """`(iri, jsonld_doc)` for one accepted article.

    Shaped by the server's own `materials.jsonld.documentsource_to_jsonld`, so
    the document is identical in form to the news materials already in the lake.
    Imported inside the function because it pulls in a Django app module, and
    this module is imported by tests that never call `bootstrap()`.
    """
    from materials.jsonld import documentsource_to_jsonld

    ident = news_material_ident(article.url, article.published)
    links = [{"link": article.url, "role": "RAW"}]
    if permalink:
        links.append({"link": permalink, "role": "PERMALINK"})
    doc, material_type = documentsource_to_jsonld(
        source_id=ident,
        title=article.title or f"{article.outlet} news article",
        source_type="NEWS",
        url=links,
        description=note,
        publication_date=article.published.isoformat(),
    )
    if material_type != NEWS_MATERIAL_TYPE:      # pragma: no cover -- contract check
        raise AssertionError(
            f"source_type=NEWS shaped material_type={material_type!r}, expected "
            f"{NEWS_MATERIAL_TYPE!r}; the JSON-LD shaper's mapping changed")
    # Take the shaper's OWN `@id` rather than re-deriving it. `documentsource_to_
    # jsonld` builds `@id` through `build_source_material_iri`, which reads
    # `iri_base()` ($JAWAFDEHI_IRI_BASE) and normalises the ident (`:`->`.`,
    # lowercase, out-of-grammar chars -> `-`); `materials.material_iri` hardcodes
    # `https://jawafdehi.org` and normalises nothing. The two agree only while
    # JAWAFDEHI_IRI_BASE is unset and the ident needs no normalising -- otherwise
    # the material was created at one IRI and the evidence bound to another, and
    # since the server validates evidence IRI grammar without checking that the
    # material exists, the case would ship a valid-looking reference to nothing.
    doc_iri = doc.get("@id")
    if not doc_iri:                              # pragma: no cover -- contract check
        raise AssertionError(
            "documentsource_to_jsonld returned a document with no @id; the "
            "evidence bind has no authoritative IRI to point at")
    # The source segment is still asserted rather than assumed: 48 of the 50 news
    # materials in the lake are `/material/news/<ident>`, and a shaper change that
    # moved the segment would silently start a second parallel IRI family.
    if f"/material/{NEWS_MATERIAL_SOURCE}/" not in doc_iri:   # pragma: no cover
        raise AssertionError(
            f"expected a /material/{NEWS_MATERIAL_SOURCE}/ IRI, got {doc_iri!r}; "
            f"the JSON-LD shaper's source segment changed")
    return doc_iri, doc


def plan_case(case, etag, outcome, *, client=None, save_permalinks=True):
    """Turn a `SearchOutcome` into a `NewsPlan`. Writes nothing.

    `client` is used only to resolve web-archive permalinks, which is a network
    READ (plus, on `--apply`, a Save Page Now request to archive.org -- not to
    Jawafdehi). Pass `save_permalinks=False` for the dry run so no capture is
    requested; the donor did the same (add_news_permalinks:239).
    """
    slug = case.get("slug")
    state = case.get("state") or ""
    current = current_evidence(case)

    if not outcome.accepted:
        return NewsPlan(slug=slug, action="NOOP", state=state, if_match=etag,
                        n_current=len(current), outcome=outcome,
                        reason="no article cleared the verification gate")

    materials, additions = [], []
    for article, verdict in outcome.accepted:
        permalink = (resolve_permalink(client, article.url, save_missing=save_permalinks)
                     if client is not None else None)
        iri, doc = _material_doc(article, verdict.summary, permalink)
        materials.append((iri, doc, verdict.summary, article))
        additions.append((iri, verdict.summary))

    merged = merge_news_evidence(current, additions)
    if merged == current:
        return NewsPlan(slug=slug, action="NOOP", state=state, if_match=etag,
                        n_current=len(current), outcome=outcome,
                        reason="every accepted article is already bound")
    return NewsPlan(slug=slug, action="WOULD_BIND", state=state, if_match=etag,
                    n_current=len(current), materials=materials,
                    patch_items=merged, outcome=outcome)


# ---------------------------------------------------------------------------
# THE WRITER. The only function in this port that mutates server state.
# ---------------------------------------------------------------------------


def apply_plan(api, plan):
    """Upsert each material, then bind the FULL merged evidence list. Returns the
    number of materials written.

    Refuses, in order:
      * any plan that is not `WOULD_BIND`;
      * any non-loopback host, unconditionally (`_require_loopback`);
      * any case not in `WRITABLE_STATE` (deviation 6);
      * a missing ETag -- without `If-Match` the whole-list replace is
        unconditional and silently clobbers a concurrent edit, which is the
        exact failure `bind_materials.apply_plan` refuses for the same reason.

    Materials first, bind second, and the order is not arbitrary: the server
    validates evidence IRI *grammar* only and never checks that a material
    exists, so binding first would leave a grammatically valid reference to
    nothing. If the bind then fails, the upserted materials are re-derivable
    (deviation 5) -- they are not orphans.
    """
    if plan.action != "WOULD_BIND":
        raise ValueError(f"apply_plan called on a {plan.action} plan for {plan.slug!r}")
    _require_loopback(api)
    if plan.state != WRITABLE_STATE:
        raise RuntimeError(
            f"refusing to write news evidence to {plan.slug!r} in state "
            f"{plan.state!r}: /evidence is a destructive whole-list replace and "
            f"only {WRITABLE_STATE} may be written")
    if not plan.if_match:
        raise RuntimeError(
            f"refusing unconditional whole-list evidence replace for {plan.slug!r}: "
            "no ETag was captured at read time, so a concurrent edit cannot be "
            "detected (If-Match would be absent) and the destructive replace could "
            "silently clobber it")

    for iri, doc, _, _ in plan.materials:
        api.create_material(doc, NEWS_MATERIAL_TYPE)
        log.info("    material upserted: %s", iri)
    api.replace_list(plan.slug, EVIDENCE_PATH, plan.patch_items,
                     if_match=plan.if_match)
    return len(plan.materials)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def build_api(args):
    """Construct the client. Basic (local DEV_AUTH) unless a token is given."""
    if args.api_token:
        return CaseworkApi(args.api_base_url, token=args.api_token,
                           allow_remote_writes=args.allow_remote_writes)
    return CaseworkApi(args.api_base_url, basic=basic_auth_from_env(),
                       allow_remote_writes=args.allow_remote_writes)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Attach LLM-verified news coverage to a case's evidence.",
        epilog="Reads cases and writes materials/evidence over the Jawafdehi HTTP "
               "API. Writes are refused off-loopback unconditionally.")
    add_common_args(parser)
    parser.add_argument("--max-articles", type=int, default=DEFAULT_MAX_ARTICLES,
                        help=f"Max news articles to bind per case "
                             f"(default {DEFAULT_MAX_ARTICLES}).")
    parser.add_argument("--search-delay", type=float, default=1.5,
                        help="Minimum seconds between search queries (default 1.5).")
    parser.add_argument("--fetch-delay", type=float, default=0.5,
                        help="Minimum seconds between article fetches (default 0.5).")
    parser.add_argument("--save-delay", type=float, default=6.0,
                        help="Minimum seconds between Wayback Save Page Now "
                             "requests, which are rate-limited (default 6.0).")
    parser.add_argument("--search-budget", type=int, default=None,
                        help="Hard cap on billable search queries, counted in a "
                             "ledger that survives across runs. 0 disables it. "
                             f"Defaults to ${SEARCH_BUDGET_ENV}, else the "
                             "provider's own free allowance over the window that "
                             "provider refreshes on: "
                             + ", ".join(
                                 f"{name} {cap}/{period}"
                                 for name, (cap, period) in sorted(
                                     PROVIDER_QUOTAS.items()))
                             + f"; {DEFAULT_KEYED_BUDGET}/month for any other "
                             "keyed provider, and uncapped for duckduckgo. Note "
                             "'once' does not refresh. The run aborts rather "
                             "than sending the query that would breach it.")
    parser.add_argument("--no-permalink", dest="permalink", action="store_false",
                        default=True,
                        help="Do not attach a web-archive PERMALINK alongside the "
                             "live URL. Left on, a news citation that 404s in two "
                             "years stops being evidence.")
    return parser


def _review_row(slug, status, plan, note=""):
    """One review-file row for a case.

    `generated` is the human-readable proposal: per accepted article, the event
    type, the URL, the material IRI it would get, and the Nepali note. `sources`
    carries `(label, material_iri, text)` triples so the review file prints the
    article passage the verdict was formed from beside it -- which is what makes
    an accuracy judgement possible at all.
    """
    lines, sources = [], []
    for iri, _, article_note, article in plan.materials:
        lines.append(
            f"- **{_event_of(plan, article)}** · {article.outlet} · "
            f"{article.published.isoformat()}\n"
            f"  - URL: {article.url}\n"
            f"  - material: `{iri}`\n"
            f"  - note ({len(article_note)} chars): {article_note}")
        # `title` is not guaranteed -- `_material_doc` already falls back for
        # exactly this. Slicing None raises TypeError, and `_review_row` runs on
        # the ERROR path too, inside the except block: the TypeError would escape
        # `_process_case` and `main`, aborting before `review.write()` and losing
        # the review file for every case already processed.
        label = article.title or f"{article.outlet} news article"
        sources.append((f"news · {article.outlet} · {label[:70]}",
                        iri, article.text))
    if plan.outcome.near_misses:
        lines.append("")
        lines.append("**Near misses — reported, NOT bound (confidence below `high`):**")
        for miss in plan.outcome.near_misses:
            lines.append(
                f"- `{miss.verdict.confidence or 'unrated'}` · "
                f"{miss.article.url}\n  - verifier reason: {miss.verdict.reason}")
    return ReviewRow(slug=slug, status=status, before=_before_text(plan),
                     generated="\n".join(lines), sources=sources, note=note)


def _event_of(plan, article):
    """The lifecycle event type the verifier gave `article`, or "?".

    Looks it up on `outcome.accepted`, which is where the verdict lives --
    `plan.materials` keeps the note but drops the verdict it came from.
    """
    for accepted_article, verdict in plan.outcome.accepted:
        if accepted_article is article:
            return verdict.event_type
    return "?"


def _before_text(plan):
    """The case's news evidence as it stands, for the review file's Before column."""
    if not plan.n_current:
        return ""
    return f"{plan.n_current} evidence entries bound before this run"


def _skip_summary(outcome):
    counts = {}
    for skip in outcome.skipped:
        counts[skip.reason.value] = counts.get(skip.reason.value, 0) + 1
    return ", ".join(f"{v}x {k}" for k, v in sorted(counts.items()))


def _press_release_text(detail):
    """`(text, unmet)` for the CIAA press release, or `("", reasons)`.

    Optional context, not a prerequisite -- see the `news` stage comment in
    `casework/common/pipeline.py`. When it is missing the verifier is told so
    explicitly, as the donor did (donor:997), and the review file records it.
    """
    chunks, unmet = source_chunks(detail, types=PRESS_TYPES)
    return "\n\n".join(text for _, _, text in chunks), unmet


def _build_budget(args, provider_name):
    """The `SearchBudget` for this run, or None when uncapped.

    Resolution order is --search-budget, then $CASEWORK_SEARCH_BUDGET, then a
    per-provider default. An explicit 0 at either of the first two disables the
    cap; that has to be distinguishable from "not given", which is why the
    argparse default is None rather than 0.
    """
    if args.search_budget is not None:
        limit = args.search_budget
    else:
        raw = (os.environ.get(SEARCH_BUDGET_ENV) or "").strip()
        if raw:
            try:
                limit = int(raw)
            except ValueError as exc:
                raise SystemExit(
                    f"${SEARCH_BUDGET_ENV}={raw!r} is not a whole number") from exc
        else:
            limit = default_budget_for(provider_name)
    if limit < 0:
        raise SystemExit("--search-budget must be non-negative (0 disables it)")
    return SearchBudget(limit, provider_name) if limit else None


def _check_budget_fits(budget, n_cases):
    """Warn before the run if the selected batch cannot finish inside the cap.

    Worth its own check rather than letting `SearchBudget.spend` abort midway:
    stopping at case 19 of 25 leaves a review file that is silently partial,
    and the operator would rather cut the batch than discover that afterwards.
    Only a warning -- a case often spends fewer than `QUERY_LIMIT` queries, so
    the estimate is a ceiling and refusing outright would be wrong.
    """
    if budget is None or not n_cases:
        return
    needed = n_cases * QUERY_LIMIT
    remaining = budget.remaining()
    if needed > remaining:
        affordable = remaining // QUERY_LIMIT
        print(f"  WARNING: {n_cases} cases need up to {needed} queries but only "
              f"{remaining} remain. The run will abort partway. Roughly "
              f"{affordable} cases fit -- consider --limit {affordable}.")


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.max_articles < 0:
        raise SystemExit("--max-articles must be non-negative")
    for name in ("search_delay", "fetch_delay", "save_delay"):
        if getattr(args, name) < 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be non-negative")

    setup_logging(args.verbose)
    logger, run_id, paths = configure_run_logging(STAGE_NAME, verbose=args.verbose)
    started = time.monotonic()

    try:
        bootstrap(args.provider, args.model)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Bootstrap failed: {exc}") from exc

    from llm.invoke import invoke_json
    from llm.usage import UsageAccumulator, render_usage_table

    api = build_api(args)
    usage = UsageAccumulator()
    report = RunReport()
    review = build_review_file(args, stage=STAGE_NAME, field_name="evidence (news)",
                              run_id=run_id)
    # PREFLIGHT, before any case is touched. Both of these read configuration and
    # raise `SearchUnavailable` -- an unknown $CASEWORK_SEARCH_PROVIDER, a keyed
    # provider with no key, a malformed $CASEWORK_SOCKS_PROXY, PySocks not
    # installed. The client is built OUTSIDE the per-case try below, so without
    # this the proxy cases surfaced as a bare traceback; the provider cases
    # surfaced correctly but only after the case list had been fetched and the
    # run header printed, which reads like the run started and then broke.
    try:
        provider_name, _ = resolve_search_provider()
        budget = _build_budget(args, provider_name)
        client = WebClient(search_delay=args.search_delay,
                           fetch_delay=args.fetch_delay,
                           save_delay=args.save_delay,
                           budget=budget)
    except SearchUnavailable as exc:
        # SystemExit, not `return report`. `main()` is invoked bare at the bottom
        # of this file, so returning exits 0 and a misconfigured provider, a
        # missing key or a malformed proxy would report SUCCESS to a scheduler.
        # Matches how --max-articles validation already fails above.
        raise SystemExit(f"search is not configured: {exc}") from exc
    if client.proxy:
        print(f"  search + fetch via SOCKS proxy {client.proxy} "
              f"(the case API and the LLM stay on the local interface)")
    if provider_name != DEFAULT_SEARCH_PROVIDER:
        print(f"  search provider: {provider_name}")
    if budget is not None:
        # Printed BEFORE the run, because the number the operator needs is
        # "will this batch fit", and afterwards is too late to act on it.
        window = {"once": "in total", "day": "today"}.get(budget.period,
                                                          "this month")
        print(f"  search budget: {budget.remaining()} of {budget.limit} "
              f"{provider_name} queries left {window} ({budget.bucket}); "
              f"up to {QUERY_LIMIT} are spent per case")
    else:
        print("  search budget: uncapped")

    cases = select_for_run(list(api.iter_cases()), args)
    total = len(cases)
    _check_budget_fits(budget, total)
    log_run_header(logger, stage=STAGE_NAME, base_url=args.api_base_url,
                   dry_run=args.dry_run, provider=args.provider, model=args.model,
                   n_selected=total, run_id=run_id, paths=paths)
    if total == 0:
        print("No matching CIAA case(s) to process.", file=sys.stderr)
        print_summary(report.summary(), args.dry_run, "News evidence")
        print(f"review file: {review.write()}")
        log_run_footer(logger, stage=STAGE_NAME, stats=report.summary(),
                       duration_s=time.monotonic() - started)
        return report

    print(f"Found {total} matching case(s); up to {args.max_articles} article(s) each.")
    if args.dry_run:
        print("  [DRY RUN] No materials created, no evidence bound.")

    for index, summary in enumerate(cases, 1):
        slug = summary.get("slug") or "?"
        # The web cache earns its hit rate WITHIN a case (the same URL surfaces
        # under several query templates); two cases share no candidates. Clearing
        # per case keeps it bounded -- a run-lifetime cache held every decoded
        # search page and full article body until the process exited, which over
        # 238 cases is thousands of documents at 50-300 KB each for no benefit.
        client.clear_cache()
        try:
            _process_case(api, client, slug, index, total, args, invoke_json, usage,
                          report, review, logger, paths, run_id)
        except SearchUnavailable as exc:
            # ABORT THE WHOLE RUN, not just this case. The backend is down for
            # every query, so continuing would write one "found nothing" row per
            # case -- a review file that looks like a completed run and means
            # nothing. Whatever was already processed still ships.
            report.record(slug, STAGE_NAME, "error", f"search backend down: {exc}")
            review.add(ReviewRow(slug=slug, status="error",
                                 note=f"SEARCH BACKEND DOWN — run aborted: {exc}"))
            log_event(logger, paths["events"], run_id=run_id, stage=STAGE_NAME,
                      slug=slug, step="search", status="aborted", detail=str(exc)[:300],
                      level=logging.ERROR)
            print(f"\nABORTED at case {index}/{total}: {exc}", file=sys.stderr)
            break

    stats = report.summary()
    print_summary(stats, args.dry_run, "News evidence")
    unmet_reasons = report.unmet_reasons()
    if unmet_reasons:
        print("  unmet reasons:")
        for reason, count in unmet_reasons.most_common():
            print(f"    {count} x {reason}")
    print(f"  web reads: {client.calls}")

    usage_summary = ""
    if usage.calls > 0:
        usage_summary = render_usage_table(usage.as_dict()["by_provider"],
                                          title="news enrichment usage")
        print()
        print(usage_summary)
    print(f"review file: {review.write()}")
    log_run_footer(logger, stage=STAGE_NAME, stats=stats,
                   duration_s=time.monotonic() - started, usage_summary=usage_summary)
    return report


def _process_case(api, client, slug, index, total, args, invoke_json, usage,
                  report, review, logger, paths, run_id):
    """One case, end to end. Every exit path records a report row AND a review row.

    A case that produced nothing still gets a review row: "this case was looked
    at and nothing cleared the bar" is a reviewable outcome, and a review file
    that silently omits it cannot be checked against the sample manifest.
    """
    def event(step, status, detail="", level=logging.INFO):
        log_event(logger, paths["events"], run_id=run_id, stage=STAGE_NAME, slug=slug,
                  step=step, status=status, detail=detail, level=level)

    event("start", "start", f"[{index}/{total}]")
    try:
        detail, etag = api.get_case_with_etag(slug)
    except Exception as exc:  # noqa: BLE001 -- one bad case must not sink the batch
        report.record(slug, STAGE_NAME, "error", f"fetch failed: {exc}")
        review.add(ReviewRow(slug=slug, status="error", note=f"fetch failed: {exc}"))
        event("fetch", "error", str(exc)[:200], logging.ERROR)
        return

    n_current = count_news_evidence(detail)
    if n_current >= args.max_articles and not args.force:
        reason = (f"already carries {n_current} news evidence entries "
                  f"(--max-articles {args.max_articles})")
        report.record(slug, STAGE_NAME, "already", reason)
        review.add(ReviewRow(slug=slug, status="already", note=reason))
        event("idempotency", "already", reason)
        return

    unmet = unmet_prerequisites(STAGE, detail)
    if unmet:
        for reason in unmet:
            report.record(slug, STAGE_NAME, "unmet", reason)
        review.add(ReviewRow(slug=slug, status="unmet", note="; ".join(unmet)))
        event("prereq", "unmet", "; ".join(unmet), logging.WARNING)
        return

    press_text, press_unmet = _press_release_text(detail)
    if not press_text:
        event("source", "partial",
              "no converted press release: verifier runs without official context; "
              + "; ".join(press_unmet), logging.WARNING)

    try:
        outcome = collect_for_case(detail, client, invoke_json, usage,
                                  max_articles=args.max_articles,
                                  press_release_text=press_text or None,
                                  force=args.force)
    except SearchUnavailable:
        # NOT this case's error -- the backend is down for every case. Re-raise so
        # `main` aborts the run instead of recording a per-case failure that reads
        # like this case was unlucky.
        raise
    except Exception as exc:  # noqa: BLE001
        report.record(slug, STAGE_NAME, "error", f"search/verify failed: {exc}")
        review.add(ReviewRow(slug=slug, status="error",
                             note=f"search/verify failed: {exc}"))
        event("search", "error", str(exc)[:200], logging.ERROR)
        return

    event("search", "ok",
          f"{len(outcome.queries)} queries, {outcome.n_candidates} candidates, "
          f"{len(outcome.accepted)} accepted, {len(outcome.near_misses)} near-miss, "
          f"{len(outcome.skipped)} skipped ({_skip_summary(outcome)})")

    # `--no-permalink` sets args.permalink=False and MUST be honoured here; it was
    # registered, documented, and then never read, so `--apply --no-permalink`
    # still resolved a snapshot and still fired a 6s-throttled Save Page Now per
    # article. `client=None` is what actually suppresses the archive lookup, since
    # `plan_case` only touches the archive when it has a client.
    plan = plan_case(detail, etag, outcome,
                     client=client if args.permalink else None,
                     save_permalinks=args.permalink and not args.dry_run)

    note_parts = []
    # Lead with this one. A case whose verifier never answered looks exactly like
    # a case with no coverage, so the review file has to say which it was before
    # anyone reads the (empty) proposal below it.
    n_failed = sum(1 for s in outcome.skipped
                   if s.reason is SkipReason.VERIFY_FAILED)
    if n_failed:
        note_parts.append(
            f"VERIFIER FAILED on {n_failed} candidate(s) — this case's result is "
            f"UNRELIABLE, not a finding of 'no coverage'")
        report.record(slug, STAGE_NAME, "error",
                      f"verifier failed on {n_failed} candidate(s)")
        event("verify", "error", f"{n_failed} candidate(s) unanswered",
              logging.ERROR)
    if not press_text:
        note_parts.append("NO PRESS RELEASE CONTEXT — verifier ran without the "
                          "official CIAA account of this case")
    if outcome.skipped:
        note_parts.append(f"skipped: {_skip_summary(outcome)}")
    if plan.reason:
        note_parts.append(plan.reason)

    if plan.action == "NOOP":
        report.record(slug, STAGE_NAME, "skipped", plan.reason)
        review.add(_review_row(slug, "skipped", plan, "; ".join(note_parts)))
        event("plan", "skipped", plan.reason)
        return

    detail_msg = (f"+{len(plan.materials)} news article(s), evidence "
                  f"{plan.n_current} -> {plan.n_merged}")
    if args.dry_run:
        report.record(slug, STAGE_NAME, "would-enrich", detail_msg)
        review.add(_review_row(slug, "would-enrich", plan, "; ".join(note_parts)))
        event("write", "would-enrich", detail_msg)
        return

    try:
        apply_plan(api, plan)
        report.record(slug, STAGE_NAME, "enriched", detail_msg)
        review.add(_review_row(slug, "enriched", plan, "; ".join(note_parts)))
        event("write", "enriched", detail_msg)
    except Exception as exc:  # noqa: BLE001
        report.record(slug, STAGE_NAME, "error", f"write failed: {exc}")
        review.add(_review_row(slug, "error", plan,
                               "; ".join(note_parts + [f"write failed: {exc}"])))
        event("write", "error", str(exc)[:200], logging.ERROR)


if __name__ == "__main__":
    main()
