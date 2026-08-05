#!/usr/bin/env python
"""Explain each of a case's bound documents, and its role in that case. LOCAL WRITES ONLY.

Ported from the deleted `casework/enrich_evidence.py` (recovered at donor commit
`0321a85`, 540 lines). From ONE document fetch and ONE LLM call it produces two
descriptions, exactly as the donor did:

  1. A REUSABLE, case-agnostic abstract of the DOCUMENT -- what it is, who issued
     it, when, its identifiers, the parties named in it, and a short factual
     precis. Written at most once per material per run and only when blank,
     because a material cited by 40 cases must not be rewritten 40 times. The
     prompt hard-guards it against any case-specific framing.
  2. A CASE-SPECIFIC note on that document's probative role for THIS case's
     allegations -- which allegation it bears on, and whether it is a primary
     record, corroboration, or background.

`bind_materials.py:143` appends every new binding as
`{"material_iri": iri, "additional_details": ""}`. It has no note to write and
correctly does not invent one, so each binding batch adds blank entries and this
is the downstream pass that fills them. Run it AFTER a binding batch.

THIS IS A REWIRE, NOT A STRAIGHT PORT. The donor's extraction and prompts are
worth keeping; its write half is architecturally dead. Both of its targets --
`PATCH /api/sources/{source_id}/` and `DocumentSource.description` -- were
removed by the "cases own no documents" ADR
(`docs/jawafdehi/adr-cases-own-no-documents.md`). Hence the deviations below.

DEVIATION 1 -- THE WRITE TARGETS ARE DIFFERENT ENDPOINTS. The document abstract
goes to the Material JSON-LD via `PATCH /api/materials/?iri=<full-iri>` with an
RFC-6902 `patch_ops` list (`CaseworkApi.patch_material`), and the case-specific
note goes to `CaseMaterialReference.additional_details` via the `evidence` array
on `PATCH /api/cases/{slug}/`. Two consequences a reader should not have to
rediscover:
  * The material's key is `description` and there is NO `abstract` key anywhere
    in the materials app. `description` is a LANGUAGE MAP (`{"ne": "..."}`), not
    a string -- `materials/jsonld.py::MATERIAL_CONTEXT` declares it
    `{"@container": "@language"}`. Verified against the live store on
    2026-08-05: a charge_sheet carries `description: {"ne": "नारायणी अस्पताल,
    ..."}`, while press releases and court orders carry no `description` key at
    all, which is why `materials.description_ops` uses `add` and not `replace`.
  * `/evidence` is a whole-list REPLACE. This reads the current list, edits
    `additional_details` in place and PATCHes the whole list back under the
    ETag it read -- a clobber here would drop bindings `bind_materials.py`
    spent a batch establishing.

DEVIATION 2 -- THE "ALREADY ENRICHED" GATE IS DETERMINISTIC, NOT AN LLM JUDGE.
The donor called `judge_description_adequacy` -- an LLM call per entry per
field -- to decide whether either description needed regenerating. This port
classifies note content with `classify_note`, on length plus a label list. Two
reasons. The classification is the most consequential decision in the run (it
decides whether ~250 real, sometimes hand-written notes get overwritten), so it
has to be reproducible and reviewable rather than a model's opinion that can
differ between two runs over the same text. And the donor's judge cost one extra
call per entry per field before any work was done, on a stage that is already
the most expensive per case in the pipeline. `--force` overrides the gate
entirely, which is the donor's behaviour unchanged.

DEVIATION 3 -- THE GATE IS ASYMMETRIC BETWEEN THE TWO TARGETS. A note is
regenerated when its content is filler (see `classify_note`); an abstract is
written ONLY when the material has none at all. The asymmetry is deliberate: the
note belongs to one case and its old value is filler by construction, while the
abstract is shared by every case citing the document, so overwriting one is a
write whose blast radius this run cannot see. `--force` still overrides both.

DEVIATION 4 -- SOURCE TEXT COMES FROM `casework/common/materials.py`. The donor
reached for `sourcing.jds_client.download_source_file` and
`sourcing.converter.convert_source` against a `DocumentSource.urls` list. Both
are gone; the MARKDOWN-role fetch in `common/materials.py::source_chunks` is the
replacement (and it sends a browser User-Agent, because the WAF 403s anything
else). The donor's one deliberate refusal is preserved: the text NEVER falls
back to the existing evidence description, which would be circular -- we are
writing a description ABOUT the document.

DEVIATION 5 -- `--target source` IS NOW `--target material`. Same three-way
flag, renamed for the endpoint it actually writes; "source" named a model that
no longer exists.

DEVIATION 6 -- THE CASE WRITE IS CONDITIONAL. The donor patched `/evidence`
unconditionally. This sends `If-Match` with the ETag read at the top of the
case, so a concurrent caseworker edit 412s instead of being silently destroyed
by the whole-list replace -- the same contract `bind_materials.py` already
holds for the same array.

Usage:
    uv run python -m casework.enrich_evidence --dry-run --limit 3
    uv run python -m casework.enrich_evidence --slug case-0123 --target material
    uv run python -m casework.enrich_evidence --apply --api-base-url http://127.0.0.1:48010
"""

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass

from casework.bind_materials import current_evidence
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
from casework.common.format import format_entities, format_list
from casework.common.llm import bootstrap, tier_for
from casework.common.materials import (
    description_ops,
    description_text,
    source_chunks,
)
from casework.common.parse import parse_object_response
from casework.common.pipeline import STAGES, RunReport, unmet_prerequisites
from casework.common.review import ReviewRow, build_review_file
from casework.common.select import court_number, select_for_run

log = logging.getLogger("casework.enrich_evidence")

STAGE = STAGES["evidence_notes"]
STAGE_NAME = STAGE.name

#: Donor-pinned (`0321a85:casework/enrich_evidence.py`): `SOURCE_TEXT_BUDGET =
#: 40000` and `max_tokens=1500`. Kept at the donor's own numbers -- this stage
#: describes ONE document per call, so it needs neither `enrich_description`'s
#: 60,000-char multi-source budget nor its 8,000-token output.
SOURCE_TEXT_BUDGET = 40000
MAX_TOKENS = 1500

#: A note at or above this many characters counts as real prose and is left
#: alone. NOT a free parameter: both of the brief's production tables count
#: "entries with a note >120 chars", and the 61-120 character band measured on
#: the 419 publicly readable entries (2026-08-05) is outlet+date provenance
#: lines ("Onlinekhabar — Article published on Baisakh 14, 2082") and headline
#: restatements, while above it the notes are consistently probative ("...
#: पुष्टि गर्छ", "प्राथमिक न्यायिक अभिलेख हो"). The 100-120 band is genuinely
#: mixed; 120 resolves it toward regenerating rather than skipping, because a
#: skipped filler entry stays filler forever while a regenerated short note is
#: reviewed before it is applied.
PROSE_CHARS = 120

#: Type labels that occupy `additional_details` instead of describing anything.
#: Normalised (casefolded, whitespace-collapsed) at comparison time, so the
#: three casings of "Nepal Law Commission" in production match one entry here.
#: Counts are production measurements: `CIB press release` 90 entries,
#: `Nepal Law Commission` 18, `News Source` 11, `CIAA` 8.
TYPE_LABELS = frozenset({
    "ciaa",
    "ciaa source",
    "cib press release",
    "nepal law commission",
    "news source",
    "supreme court",
    "special court",
    "office of auditor general",
    "office of the attorney general",
    "office of attorney general",
    "power point presentation",
    "bolpatra.gov.np",
})

#: The importer's template labels, which end in a document count:
#: `CIAA Press Release (2 documents)` (153 entries) and
#: `Court Order/Verdict (Document 1)` (140). Matched by shape rather than
#: enumerated, because the count varies per entry and an exact-string list
#: would silently miss `(3 documents)`.
TEMPLATE_LABEL_RE = re.compile(
    r"\((?:document\s+\d+|\d+\s+documents?)\)\s*$", re.IGNORECASE)

#: Characters that carry no content. A note made only of these is filler --
#: 105 production entries are a bare `.`, and the Devanagari danda `।` is the
#: same gesture.
_CONTENT_FREE = set(".।-—–_ \t\r\n") | {"…"}

#: Short strings that mean "nothing here" rather than naming anything.
_NULL_WORDS = frozenset({"n/a", "na", "none", "nil", "-", "tbd"})


@dataclass(frozen=True)
class NoteVerdict:
    """Whether one `additional_details` value needs regenerating, and why.

    `reason` is carried, not derived by the caller, because it is printed
    verbatim into the human review file and the events log -- a reviewer
    deciding whether the gate did the right thing needs the gate's own account
    of the decision, not a re-derivation of it.
    """
    needs_work: bool
    reason: str


def classify_note(note):
    """Classify an evidence note's CONTENT: does it need a real note written?

    Deterministic and side-effect-free by design -- see deviation 2 in the
    module docstring. The rules, in order:

      1. empty / whitespace-only              -> needs work
      2. punctuation or a null word only      -> needs work  (the bare `.`)
      3. a known or templated type label      -> needs work
      4. at least `PROSE_CHARS` characters    -> DONE, leave it alone
      5. anything shorter                     -> needs work

    Rule 3 sits BEFORE rule 4 on purpose. Every label measured in production is
    also short, so today rule 5 would catch them all anyway; putting the label
    test first means a template label that ever grows past the threshold is
    still recognised as a label rather than promoted to prose by its length.
    """
    text = (note or "").strip()
    if not text:
        return NoteVerdict(True, "empty")
    if all(ch in _CONTENT_FREE for ch in text) or text.casefold() in _NULL_WORDS:
        return NoteVerdict(True, f"punctuation/null only ({text!r})")
    normalised = " ".join(text.split()).casefold()
    if normalised in TYPE_LABELS or TEMPLATE_LABEL_RE.search(text):
        return NoteVerdict(True, f"type label ({len(text)} chars)")
    if len(text) >= PROSE_CHARS:
        return NoteVerdict(False, f"prose ({len(text)} chars)")
    return NoteVerdict(True, f"short ({len(text)} chars < {PROSE_CHARS})")


def merge_notes(case, notes_by_iri):
    """The case's FULL evidence list with `additional_details` edited in place.

    `/evidence` is a whole-list replace (`CaseworkApi.replace_list`): the server
    deletes every join row for the case and recreates from exactly the list it
    is given, so anything omitted here is DELETED with no warning and no way to
    recover it from the call. This therefore starts from
    `bind_materials.current_evidence` -- the same normaliser the binder writes
    through, imported rather than re-implemented so the two cannot drift on what
    a writable entry looks like -- and only overwrites the notes it was handed.

    An IRI in `notes_by_iri` that this case does not cite is IGNORED, never
    appended: appending would bind a new document to the case as a side effect
    of writing a note, which is `bind_materials.py`'s job and needs that
    script's existence probe behind it.
    """
    merged = current_evidence(case)
    for entry in merged:
        note = notes_by_iri.get(entry["material_iri"])
        if note:
            entry["additional_details"] = note
    return merged


# ---------------------------------------------------------------------------
# Prompts. Donor text (`0321a85`), with the two JSON keys renamed for the
# endpoints they now write (deviation 1) and "source document" -> "document",
# since `DocumentSource` no longer exists.
#
# THE DONOR'S AIM WAS CHECKED, NOT ASSUMED. The ~250 genuine notes in the
# finished cases are the reference standard, and a read of the 163 notes over
# 120 characters in the publicly readable cohort (2026-08-05) shows caseworkers
# writing exactly what the donor asks for: a primary/secondary framing in the
# corpus's own words -- "यो ... प्राथमिक न्यायिक अभिलेख हो", "माध्यमिक स्रोत
# हो", "... पुष्टि गर्छ". So the donor's instruction survives unedited; that
# was a finding, not a default.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a Nepali legal editor for Jawafdehi, a civic accountability archive of \
Nepal's anti-corruption cases. You are given ONE document (its type, title and \
full text) and the case that cites it (title, key allegations, named \
entities). Produce TWO descriptions in formal Nepali (देवनागरी; keep English \
technical and proper terms — "CR", company names — as-is).

1. material_description — a REUSABLE, CASE-AGNOSTIC abstract of the DOCUMENT ITSELF:
   what kind of document it is (अभियोगपत्र / फैसला / press release / audit report /
   news article), its issuer or outlet, the key date stated in it, any identifiers
   (CR / नि.नं. / मुद्दा नं.), the parties named in it, and a 1–3 line factual précis
   of its contents.
   CRITICAL: this text is shared by EVERY case that cites this document. It MUST NOT
   mention "this case" (यस मुद्दा), the citing case's allegations, or frame the
   document as evidence/proof. Describe ONLY the document. 2–5 sentences.

2. evidence_note — a CASE-SPECIFIC note on the document's PROBATIVE ROLE for
   THIS case: which allegation(s) it bears on and whether it is a primary record,
   corroboration, or background context. Frame the role/weight relative to the
   allegation; do NOT merely re-summarise the document's facts. 1–3 sentences.

Ground every statement in the provided document text and case data; never invent
names, amounts, sections, dates, or outcomes. If the document text is insufficient
for a field, write a faithful minimal description from what is available rather than
fabricating.

OUTPUT FORMAT — return ONLY a single JSON object, no markdown fences, no prose:
{"material_description": "…", "evidence_note": "…"}
"""

USER_PROMPT = """\
{targets_note}

DOCUMENT
Type: {material_type}
Title: {material_title}

CITING CASE
Title: {case_title}
Court case: {court_number}
Key allegations:
{allegations}
Named entities:
{entities}

DOCUMENT TEXT:
{source_text}

Return ONLY the JSON object described in the system prompt.
"""


def _targets_note(need_abstract, need_note):
    """The donor's `targets_note`: tell the model which field is actually wanted
    so a single-field regeneration does not pay for prose that gets thrown away.
    Both keys are still requested in the reply, as the donor did -- a model told
    to omit a key tends to omit the JSON object too."""
    wanted = [name for name, needed in
              (("material_description", need_abstract), ("evidence_note", need_note))
              if needed]
    if len(wanted) == 2:
        return "Produce BOTH descriptions."
    return (f"Only the {wanted[0]} is needed (still return the JSON object with "
            "both keys; the other may be an empty string).")


def _generate(*, material_type, material_title, case_title, court_no, allegations,
              entities, source_text, need_abstract, need_note, invoke_text, usage):
    """ONE premium-tier call yielding BOTH descriptions. Returns a dict or None.

    The shared fetch/call is the donor's design and the reason this stage is
    affordable at all: converting the document to text is the expensive step, so
    the abstract and the note are produced from the same read rather than from
    two.
    """
    prompt = USER_PROMPT.format(
        targets_note=_targets_note(need_abstract, need_note),
        material_type=material_type or "MISC",
        material_title=material_title or "(untitled)",
        case_title=case_title or "(unknown)",
        # UPPERCASED, for the reason `enrich_description` documents: the number
        # is read off the canonical lowercase IRI, and the prompt tells the model
        # to prefer specifics from the context, so `078-cr-0001` otherwise lands
        # verbatim in prose a reader sees.
        court_number=(court_no or "").upper() or "(unknown)",
        allegations=format_list(allegations),
        entities=format_entities(entities),
        source_text=source_text[:SOURCE_TEXT_BUDGET],
    )
    response_text = invoke_text(
        system=SYSTEM_PROMPT, content=prompt, max_tokens=MAX_TOKENS,
        tier=tier_for(STAGE_NAME), usage=usage,
    )
    return _parse_response(response_text)


def _parse_response(response_text):
    """Pull the two descriptions out of the JSON object reply, or None.

    Accepts an object carrying EITHER key: a single-target run legitimately
    returns one filled key and one empty string, and rejecting that would throw
    away a good answer.
    """
    obj = parse_object_response(
        response_text, predicate=lambda o: (
            "material_description" in o or "evidence_note" in o))
    if obj is None:
        log.warning("No description JSON object found in the LLM response")
    return obj


def _material_title(material):
    """The material's display name for the prompt. `name` is a language map
    (`{"ne": ...}`) on the JSON-LD, but the case serializer's embedded `material`
    block flattens it to `display_name`; accept either rather than guessing."""
    name = material.get("display_name") or material.get("name") or ""
    if isinstance(name, dict):
        for value in name.values():
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    return str(name).strip()


def _writes_material(target):
    return target in ("material", "both")


def _writes_evidence(target):
    return target in ("evidence", "both")


def build_api(args):
    """Construct the client. Basic (local DEV_AUTH) unless a token is given."""
    if args.api_token:
        return CaseworkApi(
            args.api_base_url, token=args.api_token,
            allow_remote_writes=args.allow_remote_writes,
        )
    return CaseworkApi(
        args.api_base_url, basic=basic_auth_from_env(),
        allow_remote_writes=args.allow_remote_writes,
    )


def build_parser():
    ap = argparse.ArgumentParser(
        description="Write evidence notes + document abstracts via LLM (DB-free).",
        epilog="Reads/writes cases and materials entirely over the Jawafdehi HTTP API.",
    )
    add_common_args(ap)
    ap.add_argument(
        "--target", choices=("material", "evidence", "both"), default="both",
        help="Which description(s) to write (default: both). `material` is the "
             "donor's `source`, renamed for the endpoint it now writes.")
    return ap


def _process_entry(*, ctx, entry, text_by_iri):
    """Decide and (optionally) perform both writes for ONE evidence entry.

    Returns `(iri, new_note)` when the case's `additional_details` should
    change, else `(iri, None)`. The material write happens here rather than
    being returned, because it targets a different endpoint and a different
    document -- batching it into the case write is exactly the conflation
    deviation 1 exists to prevent.
    """
    iri = entry.get("material_iri") or ""
    material = entry.get("material") or {}
    mtype = material.get("material_type") or "?"
    note_before = entry.get("additional_details") or ""
    verdict = classify_note(note_before)

    need_note = _writes_evidence(ctx.target) and (ctx.force or verdict.needs_work)

    # The abstract is read ONLY when this run might write one. `materials_done`
    # is the dedup set: one abstract per material per run, so the SECOND case
    # citing a material neither re-reads nor re-describes it.
    need_abstract = False
    abstract_before = ""
    material_etag = None
    if _writes_material(ctx.target) and iri and iri not in ctx.materials_done:
        try:
            doc, material_etag = ctx.api.get_material_with_etag(iri)
            abstract_before = description_text(doc)
        except Exception as exc:  # noqa: BLE001 - a read failure is not a crash
            ctx.record(iri, "error", f"material GET failed: {exc}",
                       level=logging.WARNING)
            ctx.materials_done.add(iri)   # don't re-probe it for every case
        else:
            need_abstract = ctx.force or not abstract_before

    if not need_note and not need_abstract:
        reason = f"note {verdict.reason}" + (
            f"; abstract {len(abstract_before)} chars" if abstract_before else "")
        ctx.record(iri, "already", reason)
        ctx.review(ReviewRow(slug=ctx.slug, status="already", before=note_before,
                             note=f"{mtype} {iri} — {reason}"))
        return iri, None

    chunk = text_by_iri.get(iri)
    if not chunk:
        reason = "no MARKDOWN-role text for this material; run `convert` first"
        ctx.record(iri, "unmet", reason, level=logging.WARNING)
        ctx.review(ReviewRow(slug=ctx.slug, status="unmet", before=note_before,
                             note=f"{mtype} {iri} — {reason}"))
        return iri, None
    fed = [(mtype, iri, chunk)]

    try:
        result = _generate(
            material_type=mtype, material_title=_material_title(material),
            case_title=ctx.case_title, court_no=ctx.court_no,
            allegations=ctx.allegations, entities=ctx.entities,
            source_text=chunk, need_abstract=need_abstract, need_note=need_note,
            invoke_text=ctx.invoke_text, usage=ctx.usage,
        )
    except Exception as exc:  # noqa: BLE001 - one entry must not abort the case
        ctx.record(iri, "error", f"LLM generation failed: {exc}",
                   level=logging.ERROR)
        ctx.review(ReviewRow(slug=ctx.slug, status="error", before=note_before,
                             sources=fed, note=f"{mtype} {iri} — LLM failed: {exc}"))
        return iri, None

    if not result:
        ctx.record(iri, "skipped", "LLM returned no parseable JSON",
                   level=logging.WARNING)
        ctx.review(ReviewRow(slug=ctx.slug, status="skipped", before=note_before,
                             sources=fed,
                             note=f"{mtype} {iri} — no parseable JSON in the reply"))
        return iri, None

    new_abstract = (result.get("material_description") or "").strip()
    new_note = (result.get("evidence_note") or "").strip()

    if need_abstract and new_abstract:
        _write_abstract(ctx, iri, mtype, new_abstract, abstract_before,
                        material_etag, fed)

    if not (need_note and new_note):
        return iri, None

    status = "would-enrich" if ctx.dry_run else "enriched"
    ctx.record(iri, status, f"note={len(new_note)} chars ({verdict.reason})")
    ctx.review(ReviewRow(slug=ctx.slug, status=status, before=note_before,
                         generated=new_note, sources=fed,
                         note=f"{mtype} {iri} — target=evidence_note; "
                              f"gate: {verdict.reason}"))
    return iri, new_note


def _write_abstract(ctx, iri, mtype, new_abstract, abstract_before, material_etag, fed):
    """Write (or, on a dry run, only report) the shared document abstract.

    Marks the material done EITHER WAY. On a dry run that keeps the run's
    reported call count honest -- the second case citing the material would
    otherwise pay another premium call to propose the same abstract again.

    UNCONDITIONAL WHEN THE SERVER GAVE NO ETag, unlike the case write, which
    refuses. The asymmetry is deliberate: the case write is a whole-LIST replace
    that can destroy many bindings, while this is a single-key `add` performed
    only when the field read blank, so the worst case is overwriting an abstract
    written in the last instant. Refusing instead would permanently strand any
    material the server does not version (a derived court-case material has no
    stored row and therefore no token). It is logged, never silent.
    """
    ctx.materials_done.add(iri)
    status = "would-write-abstract" if ctx.dry_run else "abstract-written"
    if not material_etag:
        log.warning("%s: no material ETag; the abstract write is unconditional", iri)
    if ctx.dry_run:
        ctx.record(iri, status, f"abstract={len(new_abstract)} chars")
    else:
        try:
            ctx.api.patch_material(iri, description_ops(new_abstract),
                                   if_match=material_etag)
            ctx.record(iri, status, f"abstract={len(new_abstract)} chars")
        except Exception as exc:  # noqa: BLE001
            ctx.record(iri, "error", f"material PATCH failed: {exc}",
                       level=logging.ERROR)
            ctx.review(ReviewRow(slug=ctx.slug, status="error",
                                 before=abstract_before, generated=new_abstract,
                                 sources=fed,
                                 note=f"{mtype} {iri} — target=material_abstract; "
                                      f"PATCH failed: {exc}"))
            return
    ctx.review(ReviewRow(slug=ctx.slug, status=status, before=abstract_before,
                         generated=new_abstract, sources=fed,
                         note=f"{mtype} {iri} — target=material_abstract; "
                              "written once per material per run"))


@dataclass
class _RunCtx:
    """Everything one case's processing needs, so `_process_entry` takes an
    argument list a reader can hold in their head.

    `materials_done` is run-scoped, not case-scoped -- that is the dedup rule.
    """
    api: object
    invoke_text: object
    usage: object
    report: RunReport
    review_file: object
    logger: object
    events_path: str
    run_id: str
    target: str
    force: bool
    dry_run: bool
    materials_done: set
    slug: str = ""
    case_title: str = ""
    court_no: str = ""
    allegations: object = None
    entities: object = None

    def record(self, iri, status, reason, level=logging.INFO):
        self.report.record(self.slug, STAGE_NAME, status, reason)
        log_event(self.logger, self.events_path, run_id=self.run_id,
                  stage=STAGE_NAME, slug=self.slug, step=_short_iri(iri),
                  status=status, detail=reason, level=level)

    def review(self, row):
        self.review_file.add(row)


def _short_iri(iri):
    """`.../material/court_order/special.078-cr-0001` -> `court_order/special...`.
    The events log is read per line; a full IRI on every line buries the status."""
    return iri.split("/material/", 1)[1] if "/material/" in iri else (iri or "-")


def main(argv=None):
    args = build_parser().parse_args(argv)

    setup_logging(args.verbose)
    logger, run_id, paths = configure_run_logging(STAGE_NAME, verbose=args.verbose)
    start_time = time.monotonic()

    try:
        bootstrap(args.provider, args.model)
    except Exception as exc:  # noqa: BLE001
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        sys.exit(1)

    from llm.invoke import invoke_text
    from llm.usage import UsageAccumulator, render_usage_table

    api = build_api(args)
    usage = UsageAccumulator()
    report = RunReport()
    review = build_review_file(
        args, stage=STAGE_NAME, field_name="evidence[].additional_details",
        run_id=run_id)

    cases = select_for_run(list(api.iter_cases()), args)
    total = len(cases)
    log_run_header(
        logger, stage=STAGE_NAME, base_url=args.api_base_url, dry_run=args.dry_run,
        provider=args.provider, model=args.model, n_selected=total,
        run_id=run_id, paths=paths,
    )

    ctx = _RunCtx(
        api=api, invoke_text=invoke_text, usage=usage, report=report,
        review_file=review, logger=logger, events_path=paths["events"],
        run_id=run_id, target=args.target, force=args.force, dry_run=args.dry_run,
        materials_done=set(),
    )

    if total == 0:
        print("No matching CIAA case(s) to process.", file=sys.stderr)
    else:
        print(f"Found {total} matching case(s). Target: {args.target}")
        if args.force:
            print("  --force: regenerating even where a real note already exists")
        if args.dry_run:
            print("  [DRY RUN] No changes will be saved.")

    for idx, case in enumerate(cases, 1):
        try:
            _process_case(ctx, case, idx, total)
        except Exception as exc:  # noqa: BLE001 - one case must not sink the batch
            ctx.slug = case.get("slug") or "?"
            ctx.record("-", "error", f"unhandled: {exc}", level=logging.ERROR)
            if args.verbose:
                import traceback

                traceback.print_exc()

    stats = report.summary()
    print_summary(stats, args.dry_run, "Evidence note + document abstract enrichment")
    unmet_reasons = report.unmet_reasons()
    if unmet_reasons:
        print("  unmet reasons:")
        for reason, count in unmet_reasons.most_common():
            print(f"    {count} x {reason}")

    usage_summary = ""
    if usage.calls > 0:
        usage_summary = render_usage_table(
            usage.as_dict()["by_provider"], title="evidence_notes usage")
        print()
        print(usage_summary)

    print(f"review file: {review.write()}")
    log_run_footer(
        logger, stage=STAGE_NAME, stats=stats,
        duration_s=time.monotonic() - start_time, usage_summary=usage_summary,
    )
    return report


def _process_case(ctx, case, idx, total):
    """One case: read it, walk its entries, then write the merged list once."""
    slug = case.get("slug") or "?"
    ctx.slug = slug
    log_event(ctx.logger, ctx.events_path, run_id=ctx.run_id, stage=STAGE_NAME,
              slug=slug, step="start", status="start",
              detail=f"[{idx}/{total}] {(case.get('title') or '')[:80]}")

    fetch_ok = True
    etag = None
    try:
        detail, etag = ctx.api.get_case_with_etag(slug)
    except Exception as exc:  # noqa: BLE001 - donor-preserved fallback
        fetch_ok = False
        detail = case
        log_event(ctx.logger, ctx.events_path, run_id=ctx.run_id, stage=STAGE_NAME,
                  slug=slug, step="fetch", status="fallback", detail=str(exc),
                  level=logging.WARNING)

    # A successful fetch with no ETag cannot be written: `/evidence` is a
    # destructive whole-list replace and `If-Match` is the only thing standing
    # between it and a concurrent caseworker edit. Scoped to runs that write the
    # case -- `--target material` never touches the array, so the case's ETag is
    # irrelevant to it.
    if fetch_ok and not etag and _writes_evidence(ctx.target):
        reason = ("case detail returned no ETag; refusing the whole-list "
                  "/evidence replace (would clobber a concurrent edit)")
        ctx.record("-", "error", reason, level=logging.ERROR)
        ctx.review(ReviewRow(slug=slug, status="error", note=reason))
        return

    ctx.case_title = detail.get("title") or ""
    ctx.court_no = court_number(detail)
    ctx.allegations = detail.get("key_allegations")
    ctx.entities = detail.get("entities")

    unmet = unmet_prerequisites(STAGE, detail)
    if unmet:
        for reason in unmet:
            ctx.record("-", "unmet", reason, level=logging.WARNING)
        ctx.review(ReviewRow(slug=slug, status="unmet", note="; ".join(unmet)))
        return

    # Text for EVERY entry that has a MARKDOWN role, not only the press/court
    # ones. The stage's `requires_materials` gate above decides whether the CASE
    # is ready; which ENTRIES get a note is a separate question, and the donor
    # described every source it could convert. Narrowing it here would leave
    # news entries -- 41% of a finished case's evidence -- permanently blank.
    chunks, text_unmet = source_chunks(detail)
    text_by_iri = {iri: text for _, iri, text in chunks}
    for reason in text_unmet:
        log_event(ctx.logger, ctx.events_path, run_id=ctx.run_id, stage=STAGE_NAME,
                  slug=slug, step="source", status="partial", detail=reason,
                  level=logging.WARNING)

    notes_by_iri = {}
    for entry in detail.get("evidence") or []:
        iri, new_note = _process_entry(ctx=ctx, entry=entry, text_by_iri=text_by_iri)
        if new_note:
            notes_by_iri[iri] = new_note

    if not notes_by_iri or not _writes_evidence(ctx.target):
        return

    merged = merge_notes(detail, notes_by_iri)
    detail_msg = f"{len(notes_by_iri)} of {len(merged)} entries renoted"
    if ctx.dry_run:
        log_event(ctx.logger, ctx.events_path, run_id=ctx.run_id, stage=STAGE_NAME,
                  slug=slug, step="write", status="would-replace", detail=detail_msg)
        return
    try:
        ctx.api.replace_list(slug, "evidence", merged, if_match=etag)
        log_event(ctx.logger, ctx.events_path, run_id=ctx.run_id, stage=STAGE_NAME,
                  slug=slug, step="write", status="replaced", detail=detail_msg)
    except Exception as exc:  # noqa: BLE001
        ctx.record("-", "error", f"/evidence PATCH failed: {exc}",
                   level=logging.ERROR)


if __name__ == "__main__":
    main()
