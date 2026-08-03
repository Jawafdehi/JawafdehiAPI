#!/usr/bin/env python
"""Freeze prod /api/search/ responses for the resolver's labelled test set.

Run this only to REGENERATE the fixture. It is a GET-only production read; it
never writes. The frozen file is what makes the resolver tests offline and
deterministic -- "the same NES snapshot" from the design note is literally this
file.

    uv run python scripts/capture_entity_candidates.py \
        work/2026-08-03-Fix-related_entities-enricher/extracted_names.txt \
        tests/casework/fixtures/entity_candidates.json

The paging rule below is a deliberate copy of
`CaseworkApi.search_entities` (casework/common/api.py): keep paging while the
last page's LOWEST score still ties the first page's top score, so a block of
identical-name entities is never truncated mid-tie. If the two drift apart the
fixture stops describing what the resolver sees at runtime, and the measured
precision stops meaning anything. Kept as a standalone urllib copy rather than
importing CaseworkApi so regenerating the fixture needs no Django settings.
"""
import json
import sys
import urllib.parse
import urllib.request

BASE = "https://api.jawafdehi.org/api/search/"
# The prod WAF rejects a default urllib user-agent.
UA = "Mozilla/5.0 (X11; Linux x86_64) curl"

# Two false-positive shapes the recovered names do not exhibit, captured
# alongside them so the labelled set covers every shape the design note names.
# Hardcoded rather than read from the names file because they are not
# recoverable from the extraction logs -- the second one in particular is a
# deliberate one-character corruption of the first, and the pair only means
# anything together.
EXTRA_SHAPES = (
    "अनिष श्रेष्ठ",   # two NES people share this name -> must review, never bind
    "अनिष श्रेष्ट",   # one character off the above -> must never bind
)


def search(query, page_size=50, pages=4):
    out, top = [], None
    for page in range(1, pages + 1):
        url = BASE + "?" + urllib.parse.urlencode(
            {"q": query, "type": "entity", "page_size": page_size, "page": page})
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/json"})
        batch = json.load(urllib.request.urlopen(req, timeout=60)).get("results") or []
        # Keep only what the resolver reads, so the fixture stays small and the
        # tests cannot accidentally depend on a field resolve() never sees.
        out.extend({"id": r["id"], "title": r.get("title") or {},
                    "score": r.get("score")} for r in batch)
        if not batch:
            break
        scores = [r.get("score") or 0.0 for r in batch]
        top = max(scores) if top is None else top
        if min(scores) < top or len(batch) < page_size:
            break
    return out


def main():
    names_path, out_path = sys.argv[1], sys.argv[2]
    names = [line.strip() for line in open(names_path, encoding="utf-8") if line.strip()]
    names += [name for name in EXTRA_SHAPES if name not in names]
    captured = {name: search(name) for name in names}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(captured, fh, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"captured {len(captured)} names -> {out_path}")


if __name__ == "__main__":
    main()
