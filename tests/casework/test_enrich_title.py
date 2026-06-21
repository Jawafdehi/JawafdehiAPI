"""Tests for the DB-free standalone title enricher (casework/enrich_title.py).

These cover the shared title contract (validation / headcount / parsing, which
live in casework.common and are also used by enrich_description) and the
script's per-case pipeline against a fake CaseworkApi + fake invoke_text. No
database and no network are touched.
"""

from casework import common as c
from casework import enrich_title as et

# ── shared title contract (casework.common) ─────────────────────────────────


class TestValidateTitle:
    def test_requires_court_number(self):
        assert c.validate_title("No number here", "080-CR-0047") is not None

    def test_requires_matching_number(self):
        assert c.validate_title("मुद्दा (081-CR-9999)", "080-CR-0047") is not None

    def test_requires_number_at_end_in_parens(self):
        assert c.validate_title("080-CR-0047 को मुद्दा", "080-CR-0047") is not None

    def test_valid_title_passes(self):
        assert c.validate_title("मुद्दा (080-CR-0047)", "080-CR-0047") is None

    def test_case_insensitive_match(self):
        assert c.validate_title("मुद्दा (080-cr-0047)", "080-CR-0047") is None


class TestHeadcount:
    def test_detects_digit_jana(self):
        assert c.title_has_headcount("१२ जना विरुद्ध (080-CR-0047)")

    def test_detects_pratibadi(self):
        assert c.title_has_headcount("249 प्रतिवादी (080-CR-0047)")

    def test_detects_vyakti(self):
        assert c.title_has_headcount("१२ व्यक्ति (080-CR-0047)")

    def test_court_number_not_flagged(self):
        assert not c.title_has_headcount("मुद्दा (080-CR-0098)")

    def test_spelled_out_count_not_caught(self):
        # The guard only catches DIGIT-prefixed counts; spelled-out numerals
        # ("तीन") are discouraged by the prompt but not by the regex.
        assert not c.title_has_headcount("तीन व्यक्ति (080-CR-0047)")


class TestParseTitle:
    def test_parses_json_object(self):
        assert (
            c.parse_title('{"title": "मुद्दा (080-CR-0047)"}') == "मुद्दा (080-CR-0047)"
        )

    def test_parses_json_in_code_fence(self):
        text = '```json\n{"title": "मुद्दा (080-CR-0047)"}\n```'
        assert c.parse_title(text) == "मुद्दा (080-CR-0047)"

    def test_parses_json_after_prose_with_braces(self):
        text = 'Here is the JSON {title}: {"title": "मुद्दा (080-CR-0047)"}'
        assert c.parse_title(text) == "मुद्दा (080-CR-0047)"

    def test_accepts_bare_single_line(self):
        assert c.parse_title("मुद्दा (080-CR-0047)") == "मुद्दा (080-CR-0047)"

    def test_rejects_multiline_prose(self):
        assert c.parse_title("Here is your title:\nमुद्दा (080-CR-0047)") is None

    def test_empty_returns_none(self):
        assert c.parse_title("") is None


class TestSpecialCourtNumber:
    def test_prefers_special(self):
        case = {"court_cases": ["district:1", "special:080-CR-0047"]}
        assert c.special_court_number(case) == "080-CR-0047"

    def test_falls_back_to_any(self):
        assert c.special_court_number({"court_cases": ["high:079-CR-1"]}) == "079-CR-1"

    def test_none_when_absent(self):
        assert c.special_court_number({"court_cases": []}) is None


def test_format_bigo():
    assert c.format_bigo(1568000) == "1,568,000"
    assert c.format_bigo(0) == "(unknown)"
    assert c.format_bigo(None) == "(unknown)"


# ── script idempotency helper ────────────────────────────────────────────────


class TestTitleIsValid:
    def test_valid(self):
        assert et._title_is_valid("मुद्दा (080-CR-0047)", "080-CR-0047")

    def test_headcount_invalid(self):
        assert not et._title_is_valid("१२ जना (080-CR-0047)", "080-CR-0047")

    def test_missing_court_number_context_invalid(self):
        assert not et._title_is_valid("मुद्दा (080-CR-0047)", None)

    def test_no_trailing_number_invalid(self):
        assert not et._title_is_valid("कुनै मुद्दा", "080-CR-0047")


# ── prompt assembly ──────────────────────────────────────────────────────────


def test_system_prompt_embeds_shared_rules():
    # The script must use the same TITLE_RULES as the description enricher.
    assert c.TITLE_RULES in et.SYSTEM_PROMPT
    assert "समेतविरुद्ध" in et.SYSTEM_PROMPT
    assert '{"title"' in et.SYSTEM_PROMPT


# ── provider default ─────────────────────────────────────────────────────────


def test_defaults_to_claude_cli_provider():
    # Titles must be written by a real Opus via `claude -p`, not the
    # opus->deepseek-relabelling proxy.
    args = et._build_parser().parse_args([])
    assert args.provider == "claude_cli"


def test_provider_is_overridable():
    args = et._build_parser().parse_args(["--provider", "proxy"])
    assert args.provider == "proxy"


# ── per-case pipeline (fake API + fake LLM) ──────────────────────────────────


class FakeApi:
    """Minimal CaseworkApi stand-in recording PATCHes."""

    def __init__(self, detail):
        self._detail = detail
        self.patched = []

    def get_case(self, slug, timeout=30):
        return self._detail

    def patch_field(self, slug, field, value, timeout=30):
        self.patched.append((slug, field, value))


def _case(**overrides):
    base = {
        "case_id": "case-0001",
        "slug": "case-0001-slug",
        "title": "Some draft case",  # invalid (no trailing court number)
        "court_cases": ["special:080-CR-0047"],
        "description": "A long enough description snippet about the case.",
        "key_allegations": ["Allegation one"],
        "entities": [],
        "bigo": 1000000,
    }
    base.update(overrides)
    return base


def _fixed_llm(title):
    def invoke_text(system, content, tier=None, usage=None, max_tokens=None):
        return f'{{"title": "{title}"}}'

    return invoke_text


def _run(case, llm_title, *, dry_run=False, force=False):
    api = FakeApi(case)
    stats = {
        "cases_processed": 0,
        "cases_enriched": 0,
        "cases_skipped": 0,
        "cases_llm_error": 0,
        "cases_already_valid": 0,
    }
    et._process_case(
        case=case,
        idx=1,
        total=1,
        dry_run=dry_run,
        force=force,
        api=api,
        usage=None,
        invoke_text=_fixed_llm(llm_title),
        stats=stats,
    )
    return api, stats


def test_patch_writes_valid_title():
    api, stats = _run(_case(), "नयाँ शीर्षक (080-CR-0047)")
    assert api.patched == [("case-0001-slug", "title", "नयाँ शीर्षक (080-CR-0047)")]
    assert stats["cases_enriched"] == 1


def test_dry_run_does_not_patch():
    api, stats = _run(_case(), "नयाँ शीर्षक (080-CR-0047)", dry_run=True)
    assert api.patched == []
    assert stats["cases_enriched"] == 0


def test_rejects_title_missing_court_number():
    api, stats = _run(_case(), "शीर्षक without number")
    assert api.patched == []
    assert stats["cases_skipped"] == 1


def test_rejects_headcount_title():
    api, stats = _run(_case(), "१२ जना (080-CR-0047)")
    assert api.patched == []
    assert stats["cases_skipped"] == 1


def test_already_valid_skipped_unless_force():
    case = _case(title="मान्य शीर्षक (080-CR-0047)")
    api, stats = _run(case, "नयाँ (080-CR-0047)")
    assert api.patched == []
    assert stats["cases_already_valid"] == 1


def test_force_regenerates_valid_title():
    case = _case(title="मान्य शीर्षक (080-CR-0047)")
    api, stats = _run(case, "नयाँ (080-CR-0047)", force=True)
    assert api.patched == [("case-0001-slug", "title", "नयाँ (080-CR-0047)")]
    assert stats["cases_enriched"] == 1
