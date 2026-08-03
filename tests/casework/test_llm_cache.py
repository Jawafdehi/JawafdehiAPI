"""Tests for the local invoke_text response cache.

The load-bearing test here is `test_key_changes_when_the_resolved_model_changes`:
`invoke_text` is called with `tier="premium"` and the tier -> model mapping is
resolved later inside the provider, so a key built from the tier would silently
serve a haiku answer after the operator switched the premium model to opus. Every
other test in this file is about not crashing a 238-case batch; that one is about
not lying.
"""

import json
import types

import pytest

from casework.common import llm_cache
from casework.common.llm_cache import (
    LlmCache,
    build_llm_cache,
    cache_key,
    wrap_invoke_text,
)

SYSTEM = "तपाईं भ्रष्टाचार मुद्दाको विश्लेषक हुनुहुन्छ।"
CONTENT = "विशेष अदालत मुद्दा 076-CR-0182, प्रतिवादी: बिनोद कुमार भूजेल समेत ५"
RESPONSE = "बिगो रु. ३,२०,००,००० — नारायणी अस्पताल ठेक्का अनियमितता"


def _key(**over):
    base = dict(provider="ClaudeCliProvider", model="opus", system=SYSTEM,
                content=CONTENT, max_tokens=900)
    base.update(over)
    return cache_key(**base)


# --------------------------------------------------------------------------
# Key construction
# --------------------------------------------------------------------------

def test_key_is_stable_for_identical_inputs():
    assert _key() == _key()


def test_key_changes_when_the_resolved_model_changes():
    """The whole point of resolving the model at key time. If this ever passes
    with `==`, a haiku answer can be served after switching to opus."""
    assert _key(model="haiku") != _key(model="opus")


@pytest.mark.parametrize("field,value", [
    ("provider", "BedrockProvider"),
    ("system", SYSTEM + " थप निर्देशन"),
    ("content", CONTENT + " (संशोधित)"),
    ("max_tokens", 1200),
])
def test_key_changes_when_any_input_changes(field, value):
    assert _key(**{field: value}) != _key()


def test_key_separates_fields_so_boundaries_cannot_collide():
    """Without a separator between hashed parts, ("ab","c") and ("a","bc") hash
    the same -- one prompt's answer served to a different prompt."""
    assert _key(system="ab", content="c") != _key(system="a", content="bc")


def test_key_is_stable_across_block_key_ordering():
    """cache_control prompts arrive as a list of blocks; a dict built in a
    different order is the same question and must not create a second entry."""
    a = cache_key(provider="P", model="m", max_tokens=10,
                  system="s", content=[{"type": "text", "text": CONTENT}])
    b = cache_key(provider="P", model="m", max_tokens=10,
                  system="s", content=[{"text": CONTENT, "type": "text"}])
    assert a == b


def test_bumping_cache_version_invalidates_every_entry(monkeypatch):
    before = _key()
    monkeypatch.setattr(llm_cache, "CACHE_VERSION", llm_cache.CACHE_VERSION + 1)
    assert _key() != before


# --------------------------------------------------------------------------
# Store behaviour
# --------------------------------------------------------------------------

def test_put_then_get_round_trips_devanagari(tmp_path):
    cache = LlmCache(cache_dir=tmp_path)
    assert cache.put(_key(), RESPONSE) is True
    assert cache.get(_key()) == RESPONSE
    assert (cache.hits, cache.writes) == (1, 1)


def test_miss_on_an_absent_key_is_counted_not_raised(tmp_path):
    cache = LlmCache(cache_dir=tmp_path)
    assert cache.get(_key()) is None
    assert (cache.hits, cache.misses) == (0, 1)


def test_blank_responses_are_never_stored(tmp_path):
    """A blank response is a stored failure; freezing it would replay the
    failure for every later run of the same case."""
    cache = LlmCache(cache_dir=tmp_path)
    for blank in ("", "   ", "\n"):
        assert cache.put(_key(), blank) is False
    assert cache.get(_key()) is None
    assert cache.writes == 0


def test_a_corrupt_entry_degrades_to_a_miss(tmp_path):
    """A run killed mid-write must not crash the next 238-case batch."""
    cache = LlmCache(cache_dir=tmp_path)
    cache.put(_key(), RESPONSE)
    path = cache._path(_key())
    path.write_text("{not json", encoding="utf-8")
    cache.hits = cache.misses = 0
    assert cache.get(_key()) is None
    assert (cache.hits, cache.misses) == (0, 1)


def test_an_entry_missing_the_response_field_is_a_miss(tmp_path):
    cache = LlmCache(cache_dir=tmp_path)
    path = cache._path(_key())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"created_at": "2026-08-03T00:00:00Z"}), encoding="utf-8")
    assert cache.get(_key()) is None


def test_put_leaves_no_temp_files_behind(tmp_path):
    cache = LlmCache(cache_dir=tmp_path)
    cache.put(_key(), RESPONSE)
    assert [p.name for p in cache._path(_key()).parent.iterdir()
            if p.name.endswith(".tmp")] == []


def test_disabled_cache_neither_reads_nor_writes(tmp_path):
    cache = LlmCache(cache_dir=tmp_path, enabled=False)
    assert cache.put(_key(), RESPONSE) is False
    assert cache.get(_key()) is None
    assert (cache.hits, cache.misses, cache.writes) == (0, 0, 0)
    assert not any(tmp_path.iterdir())


def test_summary_distinguishes_off_from_zero_hits(tmp_path):
    """A run with no savings must not read like a run that never tried."""
    assert LlmCache(cache_dir=tmp_path, enabled=False).summary() == "llm cache: off"
    # Not a substring check: tmp_path's own name contains "off" for this test.
    on = LlmCache(cache_dir=tmp_path).summary()
    assert on != "llm cache: off"
    assert on.startswith("llm cache: 0 hit / 0 miss")


def test_env_var_sets_the_default_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEWORK_LLM_CACHE_DIR", str(tmp_path / "shared"))
    assert LlmCache().dir == tmp_path / "shared"


def test_explicit_dir_beats_the_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEWORK_LLM_CACHE_DIR", str(tmp_path / "shared"))
    assert LlmCache(cache_dir=tmp_path / "explicit").dir == tmp_path / "explicit"


# --------------------------------------------------------------------------
# The invoke_text wrapper
# --------------------------------------------------------------------------

class _Usage:
    """Stand-in for llm.usage.UsageAccumulator: only needs to notice a call."""

    def __init__(self):
        self.calls = 0

    def add(self, *a, **kw):
        self.calls += 1


def _fake_routing(monkeypatch, model="opus"):
    """Patch llm.routing so the wrapper resolves a provider+model without Django."""
    provider = types.SimpleNamespace(model_for_tier=lambda tier: model)
    fake = types.SimpleNamespace(provider_for_tier=lambda tier: provider)
    import sys
    monkeypatch.setitem(sys.modules, "llm", types.SimpleNamespace(routing=fake))
    monkeypatch.setitem(sys.modules, "llm.routing", fake)
    return provider


def test_second_identical_call_is_served_from_disk(tmp_path, monkeypatch):
    _fake_routing(monkeypatch)
    calls = []

    def fake_invoke(system, content, max_tokens, tier="premium", usage=None):
        calls.append(system)
        return RESPONSE

    cache = LlmCache(cache_dir=tmp_path)
    wrapped = wrap_invoke_text(fake_invoke, cache)
    assert wrapped(SYSTEM, CONTENT, 900) == RESPONSE
    assert wrapped(SYSTEM, CONTENT, 900) == RESPONSE
    assert len(calls) == 1, "the second call should not have reached the provider"
    assert (cache.hits, cache.misses) == (1, 1)


def test_a_hit_records_no_token_usage(tmp_path, monkeypatch):
    """Counting cached calls in UsageAccumulator would inflate the cost report
    and hide the real spend."""
    _fake_routing(monkeypatch)

    def fake_invoke(system, content, max_tokens, tier="premium", usage=None):
        if usage:
            usage.add()
        return RESPONSE

    cache = LlmCache(cache_dir=tmp_path)
    wrapped = wrap_invoke_text(fake_invoke, cache)
    usage = _Usage()
    wrapped(SYSTEM, CONTENT, 900, "premium", usage)   # miss: provider records
    wrapped(SYSTEM, CONTENT, 900, "premium", usage)   # hit: must record nothing
    assert usage.calls == 1


def test_changing_the_model_forces_a_fresh_call(tmp_path, monkeypatch):
    """End-to-end version of the key test: the operator swaps the premium model
    and must get a real answer from the new one, not the old one replayed."""
    provider = _fake_routing(monkeypatch, model="haiku")
    seen = []

    def fake_invoke(system, content, max_tokens, tier="premium", usage=None):
        seen.append(provider.model_for_tier(tier))
        return f"answer from {provider.model_for_tier(tier)}"

    cache = LlmCache(cache_dir=tmp_path)
    wrapped = wrap_invoke_text(fake_invoke, cache)
    assert wrapped(SYSTEM, CONTENT, 900) == "answer from haiku"
    provider.model_for_tier = lambda tier: "opus"
    assert wrapped(SYSTEM, CONTENT, 900) == "answer from opus"
    assert seen == ["haiku", "opus"]


def test_a_raising_provider_is_not_cached(tmp_path, monkeypatch):
    """A transient provider failure must not be frozen and replayed for the
    rest of the batch."""
    _fake_routing(monkeypatch)
    calls = []

    def flaky(system, content, max_tokens, tier="premium", usage=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("error_max_turns")
        return RESPONSE

    cache = LlmCache(cache_dir=tmp_path)
    wrapped = wrap_invoke_text(flaky, cache)
    with pytest.raises(RuntimeError):
        wrapped(SYSTEM, CONTENT, 900)
    assert cache.writes == 0
    assert wrapped(SYSTEM, CONTENT, 900) == RESPONSE


def test_wrapper_is_a_passthrough_when_disabled(tmp_path, monkeypatch):
    _fake_routing(monkeypatch)

    def fake_invoke(*a, **kw):
        return RESPONSE

    assert wrap_invoke_text(fake_invoke, None) is fake_invoke
    disabled = LlmCache(cache_dir=tmp_path, enabled=False)
    assert wrap_invoke_text(fake_invoke, disabled) is fake_invoke


def test_stored_metadata_holds_hashes_not_prompts(tmp_path, monkeypatch):
    """238 legacy-font press releases would put tens of MB of duplicated source
    text on disk if the prompts themselves were stored."""
    _fake_routing(monkeypatch)
    cache = LlmCache(cache_dir=tmp_path)
    wrap_invoke_text(lambda *a, **kw: RESPONSE, cache)(SYSTEM, CONTENT, 900)
    record = json.loads(next(tmp_path.rglob("*.json")).read_text(encoding="utf-8"))
    assert record["response"] == RESPONSE
    assert record["model"] == "opus"
    assert "system_sha256" in record and "content_sha256" in record
    blob = json.dumps(record, ensure_ascii=False)
    assert CONTENT not in blob and SYSTEM not in blob


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------

def test_build_llm_cache_defaults_to_enabled():
    args = types.SimpleNamespace()
    assert build_llm_cache(args).enabled is True


def test_no_llm_cache_flag_disables_it():
    args = types.SimpleNamespace(llm_cache=False)
    assert build_llm_cache(args).enabled is False


def test_cli_exposes_the_cache_flags(tmp_path):
    import argparse

    from casework.common.cli import add_common_args

    parser = add_common_args(argparse.ArgumentParser())
    assert parser.parse_args([]).llm_cache is True
    assert parser.parse_args(["--no-llm-cache"]).llm_cache is False
    parsed = parser.parse_args(["--llm-cache-dir", str(tmp_path)])
    assert build_llm_cache(parsed).dir == tmp_path
