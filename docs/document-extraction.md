# Document extraction: tables and charts lifted out of a material

A `Material` is a published document — its JSON-LD metadata and, where we have
one, a flat transcript. This feature adds the **structure** recovered from inside
that document: the ruled tables, and the charts together with the series behind
them, as rows rather than prose.

The first corpus is the 35 CIAA annual reports (BS 2047/48 – 2081/82), from the
`damo-da/ciaa-annual-reports` dataset: 1,735 tables, 477 charts, 6,116 chart data
points. The dataset is CC BY-NC 4.0, the same licence as our case data.

## Shape

```
Material (iri)
 └── DocumentExtraction        one per material; carries provenance
      ├── ExtractedTable       ordinal, page, caption, header, dims, markdown
      └── ExtractedFigure      ordinal, page, title, chart_type, unit, notes
           └── ExtractedFigurePoint   label, series, value, is_estimated
```

All four live in the `ngm` database beside `Material`, so no FK crosses a
database (see `config/db_router.py`).

**The schema knows nothing about the CIAA.** Everything corpus-specific is in the
ingest command and its map file. A second corpus needs a second map, not a
migration.

**`is_estimated` is load-bearing.** A value read off a chart image is a weaker
claim than one read from a printed figure, and the two must never be rendered as
equivalent. It rides on every point, not on the figure, because a single chart
routinely mixes both. 478 of the corpus's 6,116 points are estimates.

## Read plane

| Route | Returns |
|---|---|
| `GET /api/materials/<source>/<ident>/extraction` | provenance, counts, every figure with its full series, a stub per table |
| `GET /api/materials/<source>/<ident>/extraction/tables/<key>` | one table, markdown included |

Split because of size, not taste. One report's table markdown reaches 830 KiB and
a single table reaches 170 KiB, so an inlined manifest would be unservable. With
the markdown deferred, the worst manifest in the corpus is **129 KiB in 5
queries**, and a table is ~0.4 KiB in 2 — both constant in the number of tables.

Keys are snake_case (the convention for our non-JSON-LD endpoints, cf.
`/api/cases/`). This is a projection of a material, not a JSON-LD document, so it
carries no `@context`.

**`404` when a material has no extraction** — the common case, since most
materials are a single scraped page. A client keys "should I show the tab?" off
that 404.

**The visibility gate is inherited.** Both routes resolve the material through
`_resolve_material` before touching the extraction, so a PRIVATE (draft-case
evidence) document's tables are not readable by anyone who guesses the sub-path.
Both carry `Vary: Authorization`, because *whether the URL exists* depends on the
caller.

### `key`, not `ordinal`

Every table and figure carries both, and they are not interchangeable:

- **`ordinal`** is 1-based document order, for display. It is recomputed on every
  ingest, so an upstream revision that recovers a table missed in the *middle* of
  a report shifts every later one.
- **`key`** (`p0018-t1` — page 18, first table on it) is what a client addresses a
  table by. It describes where the table physically sits, so it survives the
  document gaining or losing tables elsewhere.

Keying the URL on `ordinal` would mean a reader's saved link quietly resolving to
a *different* table after a re-ingest — a 200 with the wrong content, which is
worse than a 404. `(page_no, index_on_page)` is unique within a document across
the whole corpus, and a unique constraint holds it, so a corpus that broke the
assumption would fail loudly at ingest rather than silently serve collisions.
(The upstream `uid` would also be stable, but carries a `#` that cannot sit in a
path unescaped.)

## Ingest

The command fetches the dataset itself, so running it in the cluster is one
`kubectl exec` with nothing to stage:

```bash
kubectl -n platform exec deploy/jawafdehi-platform -- \
  /app/.venv/bin/python manage.py ingest_document_extraction \
    --map materials/data/ciaa_annual_report_docmap.json \
    --download \
    --dry-run
```

Drop `--dry-run` for the real run. The revision is read from the dataset and
recorded on every row, so there is no sha to look up and paste.

The recorded revision is the sha of **`refs/convert/parquet`**, not of the
dataset's default branch. Hugging Face auto-converts a dataset to parquet on a
separate ref with its own sha — for this corpus main is `eafef6d7` and the
parquet branch is `c3c6c673`, a minute apart. Recording main would stamp every
row with a revision whose bytes were never loaded. For the same reason
`--revision` is *rejected* with `--download`: a hand-pasted sha silently
beating the real one is the same failure.

For offline work and the tests, point it at parquet already on disk instead —
`--download` and `--parquet-dir` are mutually exclusive and one is required:

```bash
uv run python manage.py ingest_document_extraction \
    --map materials/data/ciaa_annual_report_docmap.json \
    --parquet-dir /path/to/dataset \
    --revision <upstream commit sha>
```

Reads parquet via duckdb (already a dependency). All of a config's shards are
downloaded and read together — Hugging Face splits a large config across several
files, and a hardcoded `0.parquet` would load a prefix and silently drop the
rest. Only the four configs it actually needs are downloaded — `pages` and `table_cells` are an order of
magnitude larger and unused, since the transcript is not this command's to load
and a table's content rides in its `markdown`. The download lands in a temporary
directory that is removed on the way out, including when the ingest fails, so a
long-lived pod does not accumulate a few MB per run.

**No Kubernetes manifest is needed for this.** An earlier version of this work
shipped a suspended CronJob with a ConfigMap'd staging script; it was withdrawn
unmerged once the pod was confirmed to have egress to Hugging Face. The "run bulk
work as a Job, never `exec`" convention in `infra` exists for the `reindex-*`
jobs, which are twenty-minute, 345k-document grinds that get OOM-killed — this is
seconds and one transaction.

**Resolve everything, then write, or write nothing.** The whole write phase is one
transaction. A partially-ingested corpus would put the tab on some reports and not
others with nothing in the response to say which — far worse than a failed run,
because nobody would notice.

**Re-ingest is delete-then-insert**, not upsert. The upstream can drop a table
between revisions, and an upsert keyed on `uid` would leave the dropped row behind
forever. Verified idempotent against the real corpus.

**A soft-deleted material is skipped, not fatal.** A takedown is a deliberate
editorial act; aborting on it would mean one withdrawn report permanently blocks
upstream corrections to the other thirty-four until somebody hand-edits a reviewed
file. A material that is *absent* entirely still aborts — that is a broken map.

### Why the map is a file and not a parser

The upstream keys documents on fiscal year (`ciaa-2074-75`); our materials are
keyed on a hash of the original PDF filename. Deriving one from the other looks
easy and is not:

- **41 archived records against 35 upstream documents.** Six are executive
  *summaries* that share a fiscal year with the full report they summarise. They
  are deliberately **excluded** — the extraction is of the full report's PDF, and
  attaching it to the summary would claim that shorter document contains tables it
  does not.
- **One title carries the wrong year.** The 2nd annual report is labelled
  2050/051; it is BS 2048/49. The 4th report also claims 2050/051 and is the one
  that is right.
- **One title carries no year at all** (`18._24th_annual_report_mlv2kj_66654f1c`).
- **Four titles are in Devanagari** with the ordinal spelled out
  (`तेत्तिसौँ वार्षिक प्रतिवेदन आर्थिक वर्ष २०७९/८०`).

A parser would mis-attach silently. The map is written by hand, reviewed, and then
checked by the command for being total and 1:1 before a single row is written; a
dataset document absent from the map aborts the run, so a newly published report
cannot quietly fail to appear.

## Rollout

1. Merge, deploy. **The migration is manual** — Keel auto-deploys the image but
   does not run migrations:
   `kubectl -n platform exec deploy/jawafdehi-platform -- /app/.venv/bin/python manage.py migrate materials --database=ngm`
   (done for `0006` on 2026-10-08).
2. Run the ingest with `--dry-run`, then for real.
3. The frontend tab ships separately and degrades to "no tab" on the 404, so the
   order of 2 and 3 does not matter.

Between 1 and 2 the `/extraction` routes answer a JSON 404 (`No extracted data
for this material`), which is also the steady state for the ~everything else in
the archive that has no extraction. A **500** there means step 1 was skipped: the
image shipped without the tables.
