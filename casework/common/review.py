"""The human-accuracy deliverable: one reviewable file per enricher run.

WHY THIS IS NOT THE EVENTS LOG. `configure_run_logging` already writes
`work/enricher-runs/*.events.jsonl`, but that file answers "did the run
work" -- it is machine-shaped, consumed by `casework/ledger.py`, and its
`detail` field is a repr, not something anyone reads Nepali prose out of. A
green test suite plus a clean events log together still prove only that the
code ran. Whether the generated Nepali is TRUE about named people is a
judgement a person has to make, and they can only make it if the run hands
them the field's old value, its new value, and the source passage side by
side. That is this file.

DRY RUNS WRITE ONE TOO -- they are the point. A dry run makes every LLM call
and skips only the PATCH, so it produces the full proposed output while
writing nothing to any server. It is the read-only path, so it is where
accuracy gets judged; a review file that only appeared on `--apply` would
mean you could never judge output without first writing it somewhere.

"THE PASSAGE THE MODEL USED" IS RECORDED AS WHAT IT WAS FED. A plain text
completion does not report which sentences it drew on, so this module is
careful to label its excerpts as the source text FED to the model, never as
the span the model quoted. Presenting the former as the latter would be a
fabricated provenance claim in the one artefact whose whole job is checking
for fabrication.

Devanagari is written through unescaped (`ensure_ascii=False` where JSON is
involved, plain UTF-8 otherwise). A review file full of `\\u0915` cannot be
reviewed.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# casework/common/review.py -> casework/common -> casework -> <repo-root>
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_REVIEW_DIR = _REPO_ROOT / "work" / "reviews"

#: Per-source excerpt budget, in characters. Long enough to check an amount, a
#: name and a दफा against the generated prose; short enough that a 30-case file
#: stays scrollable. The excerpt is always the HEAD of the fed text -- the
#: charge sheet's opening is where the claim, parties and बिगो are stated.
EXCERPT_CHARS = 1200


@dataclass
class ReviewRow:
    """One case's before/after/source record.

    `sources` is `[(material_type, material_iri, fed_text)]` -- the same
    triples `materials.source_chunks` returns, so a caller passes them
    straight through and cannot accidentally re-derive (and mis-attribute)
    the provenance.
    """
    slug: str
    status: str
    before: str = ""
    generated: str = ""
    sources: list = field(default_factory=list)
    note: str = ""
    #: `(heading, markdown_body)` rendered verbatim in this case's section.
    #: For a stage whose write is a LIST rather than one prose field, `generated`
    #: can only ever summarise it ("accused+21") -- this is where the individual
    #: rows go, so a reviewer can check them.
    detail: tuple = ()


@dataclass
class ReviewFile:
    """Accumulates `ReviewRow`s and renders one Markdown file.

    Markdown, not CSV: the values being reviewed are multi-paragraph Nepali
    prose with Markdown headings of their own, and a CSV cell holding that is
    unreadable in every tool a person would open it in. The summary table at
    the top keeps the file scannable; the per-case sections below it are where
    the actual reading happens.
    """
    stage: str
    field_name: str
    path: Path
    dry_run: bool = True
    base_url: str = ""
    run_id: str = ""
    provider: str = ""
    model: str = ""
    rows: list = field(default_factory=list)

    def add(self, row):
        self.rows.append(row)

    def _header(self):
        mode = "DRY RUN — nothing was written" if self.dry_run else "APPLIED"
        return [
            f"# {self.stage} review — `{self.field_name}`",
            "",
            f"- Mode: **{mode}**",
            f"- Target: `{self.base_url}`",
            f"- Provider/model: `{self.provider}` / `{self.model or '(provider default)'}`",
            f"- Run id: `{self.run_id}`",
            f"- Cases: {len(self.rows)}",
            "",
            "Check every monetary figure, personal name and ऐन/दफा in the generated "
            "value against the source excerpt below it. The excerpt is the text FED "
            "to the model, not a span the model reported quoting.",
            "",
        ]

    def _summary_table(self):
        lines = [
            "## Summary",
            "",
            "| # | Slug | Status | Before | Generated | Sources |",
            "|---|---|---|---|---|---|",
        ]
        for i, r in enumerate(self.rows, 1):
            src = ", ".join(t for t, _, _ in r.sources) or "—"
            lines.append(
                f"| {i} | `{r.slug}` | {r.status} | {len(r.before):,} chars "
                f"| {len(r.generated):,} chars | {src} |"
            )
        lines.append("")
        return lines

    def _case_section(self, i, r):
        lines = [f"## {i}. `{r.slug}`", "", f"Status: **{r.status}**"]
        if r.note:
            lines += ["", f"Note: {r.note}"]
        lines += ["", f"### Before ({len(r.before):,} chars)", ""]
        lines.append(_quote(r.before) if r.before.strip() else "_(empty)_")
        lines += ["", f"### Generated ({len(r.generated):,} chars)", ""]
        lines.append(_quote(r.generated) if r.generated.strip() else "_(nothing generated)_")
        if r.detail:
            heading, body = r.detail
            # NOT blockquoted: this is a Markdown table this code built, not
            # model prose that might carry headings of its own.
            lines += ["", f"### {heading}", "", body]
        lines += ["", "### Sources fed to the model", ""]
        if not r.sources:
            lines.append("_(none)_")
        for mtype, iri, text in r.sources:
            excerpt = (text or "")[:EXCERPT_CHARS]
            capped = "" if len(excerpt) == len(text or "") else \
                f" — excerpt, first {EXCERPT_CHARS:,} of {len(text):,}"
            lines += [
                f"**{mtype}** — `{iri or '(no material_iri)'}`"
                f" ({len(text or ''):,} chars{capped})",
                "",
                _quote(excerpt),
                "",
            ]
        lines.append("")
        return lines

    def render(self):
        lines = self._header() + self._summary_table()
        for i, r in enumerate(self.rows, 1):
            lines += self._case_section(i, r)
        return "\n".join(lines).rstrip() + "\n"

    def write(self):
        """Render to `self.path` and return it. Parent dirs are created."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.render(), encoding="utf-8")
        return self.path


def md_cell(text):
    """One Markdown table cell: pipes escaped, newlines flattened.

    Cells hold court-record names, LLM-extracted names and NES titles -- none of
    which the caller controls. A literal `|` or newline ends the cell early and
    shifts every column after it, so the row a caseworker is meant to act on
    becomes unreadable.
    """
    return (str(text or "").replace("|", r"\|")
            .replace("\r", " ").replace("\n", " "))


def _quote(text):
    """Blockquote `text` so its own Markdown headings (`### क)`) don't become
    headings of the review file and swallow the section structure."""
    body = (text or "").rstrip()
    if not body:
        return ""
    return "\n".join(f"> {line}" if line.strip() else ">" for line in body.splitlines())


def review_path(stage, run_id, override=""):
    """Resolve where the review file goes.

    Precedence: explicit `override` (the `--review-file` flag, so a run can
    drop its file straight into the meta-repo task directory it belongs to),
    then `$CASEWORK_REVIEW_DIR`, then `<repo>/work/reviews/`. `work/` is
    gitignored, so the default never risks committing generated Nepali prose.

    A DIRECTORY-VALUED OVERRIDE IS TREATED AS A DIRECTORY, and gets the same
    `<ts>-<stage>-<run>.md` name the default builds. Without that, the two
    stages of the normal workflow silently destroy each other's evidence:
    `enrich_description --review-file work/reviews/run.md` followed by
    `enrich_card --review-file work/reviews/run.md` leaves only the card's file,
    and the description output the card was judged against is gone. A file-valued
    override still lands exactly where it is pointed -- naming one file is a
    deliberate act, naming a directory is not.
    """
    if override:
        path = Path(override)
        if path.is_dir() or override.endswith(("/", os.sep)):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            return path / f"{stamp}-{stage}-{run_id}.md"
        return path
    base = Path(os.environ.get("CASEWORK_REVIEW_DIR") or _DEFAULT_REVIEW_DIR)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return base / f"{stamp}-{stage}-{run_id}.md"


def build_review_file(args, *, stage, field_name, run_id):
    """Construct a `ReviewFile` from parsed CLI args (see `cli.add_common_args`)."""
    return ReviewFile(
        stage=stage,
        field_name=field_name,
        path=review_path(stage, run_id, getattr(args, "review_file", "")),
        dry_run=getattr(args, "dry_run", True),
        base_url=getattr(args, "api_base_url", "") or "",
        run_id=run_id,
        provider=getattr(args, "provider", "") or "",
        model=getattr(args, "model", "") or "",
    )
