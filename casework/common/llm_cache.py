"""Content-addressed disk cache for `llm.invoke.invoke_text` — local dev runs only.

WHY. A `--dry-run` bills exactly like an `--apply`: the only thing dry-run skips is
the PATCH, every LLM call still happens. So the normal loop -- dry-run 238 cases,
read the review file, spot that the parser drops a section, fix the parser, dry-run
again -- pays twice for byte-identical calls, and the second payment tests nothing
but our own parsing code. This module makes that second run free.

WHERE THIS SITS, AND WHY NOT IN `llm/`. This wraps `invoke_text` from inside
`casework/`, deliberately NOT inside the shared `llm/` package. `llm/` is also
imported by `review/judge.py`, `review/runner.py` and `case_proposals/job_handlers.py`,
which run in production under the Django app. A disk cache that can answer a live
review job should not be constructible, so the wrapper lives on the standalone-script
side of the fence and the Django paths never see it.

The enrichers make this cheap: each one does `from llm.invoke import invoke_text`
inside `main()` and passes it down as a parameter (e.g.
`_extract_bigo(text, detail, invoke_text, usage)`), so rebinding that one local name
routes every downstream call through the cache without touching any extraction
function.

THE KEY INCLUDES THE RESOLVED MODEL, NOT THE TIER. `invoke_text` is called with
`tier="premium"`; the tier -> model mapping is resolved later, inside the provider.
Keying on the tier would mean switching `CLAUDE_CLI_MODEL_PREMIUM` from haiku to opus
leaves the key unchanged -- you would read back the haiku answer and conclude the
model swap did nothing. So the key resolves the provider + model at call time via
`llm.routing`. A cache that lies is worse than no cache.

WHAT IS NOT CACHED. `invoke_with_tools` (timeline's main extraction) is left alone: a
multi-turn tool loop has no stable key, and a half-replayed tool conversation is worse
than a fresh call. Timeline's plain `invoke_text` uses (verdict summarisation) still
benefit.

A CACHE HIT RECORDS NO TOKEN USAGE. `UsageAccumulator` feeds the run's cost report;
counting a call that never happened would inflate it and hide the real spend. So the
hit path deliberately does not touch `usage`.
"""

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# casework/common/llm_cache.py -> casework/common -> casework -> <repo-root>
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_DIR = _REPO_ROOT / "work" / "llm-cache"

#: Bump to invalidate every stored entry at once (e.g. if the record shape changes).
#: Part of the key, so an old entry simply stops being found rather than being
#: misread -- no migration, no deletion pass.
CACHE_VERSION = 1


def _canonical_content(content):
    """`content` is a str for most calls and a list of blocks for cache_control
    prompts (see `llm/providers/cli.py`). Both have to hash stably, and a list
    whose keys happen to be ordered differently must not produce a second entry.
    """
    if isinstance(content, str):
        return content
    return json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)


def cache_key(*, provider, model, system, content, max_tokens):
    """sha256 over everything that can change the answer.

    Fields are NUL-separated: without a separator ("ab", "c") and ("a", "bc")
    would hash identically, which is exactly the kind of collision that makes a
    cache serve one prompt's answer to another.
    """
    h = hashlib.sha256()
    for part in (
        str(CACHE_VERSION),
        str(provider),
        str(model),
        system or "",
        _canonical_content(content),
        str(max_tokens),
    ):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class LlmCache:
    """Read/write of cached responses under `cache_dir`, plus hit/miss counters.

    Never raises on a bad entry. A truncated or non-JSON file is reported once and
    treated as a miss -- a corrupt cache must degrade to "pay for the call", never
    to a crashed run mid-batch.
    """

    def __init__(self, cache_dir=None, enabled=True, logger=None):
        self.enabled = bool(enabled)
        self.dir = Path(
            cache_dir
            or os.environ.get("CASEWORK_LLM_CACHE_DIR")
            or DEFAULT_CACHE_DIR
        )
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self._log = logger or logging.getLogger("casework.llm_cache")

    def _path(self, key):
        # Sharded on the first two hex chars: a 238-case batch across six stages is
        # low thousands of files, and one flat directory of those is unpleasant to
        # inspect by hand.
        return self.dir / key[:2] / f"{key}.json"

    def get(self, key):
        """Stored response text, or None for a miss. Counts the miss."""
        if not self.enabled:
            return None
        path = self._path(key)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.misses += 1
            return None
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            self._log.warning(
                "llm-cache: unreadable entry %s (%s) -- treating as a miss",
                path.name, type(exc).__name__,
            )
            self.misses += 1
            return None
        response = record.get("response") if isinstance(record, dict) else None
        if not isinstance(response, str) or not response.strip():
            # A stored blank is a stored failure. Re-ask rather than replay it.
            self.misses += 1
            return None
        self.hits += 1
        return response

    def put(self, key, response, *, meta=None):
        """Store `response` under `key`. Blank responses are not stored.

        Writes via a temp file + `os.replace` so a run killed mid-write leaves
        either the old entry or the new one, never a truncated file that the next
        run has to recover from.
        """
        if not self.enabled:
            return False
        if not isinstance(response, str) or not response.strip():
            return False
        path = self._path(key)
        record = {
            "version": CACHE_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "response": response,
            **(meta or {}),
        }
        # `tmp_name` is bound BEFORE the write, and the cleanup catches Exception
        # rather than OSError. Both matter: binding it inside the `with` (after
        # json.dump returns) leaves it unbound if the dump raises, so the handler
        # itself would raise NameError -- and a non-OSError escaping here would
        # break this method's contract that an unwritable cache costs a saving,
        # never a run. It also leaves the temp file on disk to be re-found later.
        tmp_name = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(path.parent),
                prefix=f".{key[:8]}-", suffix=".tmp", delete=False,
            ) as fh:
                tmp_name = fh.name
                json.dump(record, fh, ensure_ascii=False)
            os.replace(tmp_name, path)
        except Exception as exc:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            self._log.warning(
                "llm-cache: could not write %s (%s)", path.name, type(exc).__name__
            )
            return False
        self.writes += 1
        return True

    def summary(self):
        """One line for the run footer. Says 'off' rather than '0 hits' when
        disabled, so a run with no savings is distinguishable from a run that
        never tried."""
        if not self.enabled:
            return "llm cache: off"
        looked_up = self.hits + self.misses
        pct = (100.0 * self.hits / looked_up) if looked_up else 0.0
        return (
            f"llm cache: {self.hits} hit / {self.misses} miss "
            f"({pct:.0f}% served from disk), {self.writes} stored, dir={self.dir}"
        )


#: `llm.invoke.invoke_text`'s positional parameter order, used only to read the
#: key fields out of a call. The enrichers call it entirely by keyword
#: (`invoke_text(system=..., content=..., max_tokens=..., tier=..., usage=...)`)
#: and their tests stub it with keyword-only lambdas, so the wrapper must accept
#: either form and forward whatever it was given, verbatim.
_INVOKE_TEXT_PARAMS = ("system", "content", "max_tokens", "tier", "usage")


def _call_fields(args, kwargs):
    """Resolve (system, content, max_tokens, tier) from a call made positionally,
    by keyword, or in any mix. Returns None when a required field is absent --
    the caller then bypasses the cache rather than keying on a guess."""
    bound = dict(zip(_INVOKE_TEXT_PARAMS, args))
    bound.update(kwargs)
    if "system" not in bound or "content" not in bound or "max_tokens" not in bound:
        return None
    return bound["system"], bound["content"], bound["max_tokens"], bound.get(
        "tier", "premium"
    )


def wrap_invoke_text(invoke_text, cache):
    """A call-convention-transparent replacement for `llm.invoke.invoke_text`.

    Returns `invoke_text` unchanged when there is no cache or it is disabled, so
    callers need no conditional of their own.

    The wrapper forwards `*args, **kwargs` UNTOUCHED. It must not normalise a
    keyword call into a positional one: every enricher calls `invoke_text` by
    keyword, and their tests stub it with keyword-only lambdas
    (`lambda **kw: response`), so a positional forward raises TypeError and the
    enricher swallows it as an `llm-error`, silently degrading to the rule-based
    path. That failure is invisible in the run output -- it looks like the model
    simply declined -- which is why the transparency is load-bearing rather than
    stylistic.

    Only successful, non-blank responses are stored: an exception propagates
    untouched and nothing is written, so a transient provider failure cannot be
    frozen into the cache and replayed for the rest of the batch.
    """
    if cache is None or not cache.enabled:
        return invoke_text

    def cached_invoke_text(*args, **kwargs):
        fields = _call_fields(args, kwargs)
        if fields is None:
            return invoke_text(*args, **kwargs)
        system, content, max_tokens, tier = fields

        from llm import routing

        provider = routing.provider_for_tier(tier)
        model = provider.model_for_tier(tier)
        key = cache_key(
            provider=type(provider).__name__,
            model=model,
            system=system,
            content=content,
            max_tokens=max_tokens,
        )
        hit = cache.get(key)
        if hit is not None:
            # NOTE: `usage` is intentionally untouched -- see module docstring.
            return hit
        response = invoke_text(*args, **kwargs)
        cache.put(key, response, meta={
            "provider": type(provider).__name__,
            "model": model,
            "tier": tier,
            "max_tokens": max_tokens,
            # Hashes, not the prompts: a batch of 238 legacy-font press releases
            # would otherwise put tens of MB of duplicated source text on disk.
            "system_sha256": hashlib.sha256((system or "").encode()).hexdigest(),
            "content_sha256": hashlib.sha256(
                _canonical_content(content).encode()
            ).hexdigest(),
        })
        return response

    return cached_invoke_text


def build_llm_cache(args, logger=None):
    """Build an `LlmCache` from parsed CLI args (see `cli.add_common_args`).

    Enabled by default: the cost problem this solves is the default situation, and
    `--no-llm-cache` is the opt-out for the run you actually sign off on.
    """
    return LlmCache(
        cache_dir=getattr(args, "llm_cache_dir", "") or None,
        enabled=getattr(args, "llm_cache", True),
        logger=logger,
    )
