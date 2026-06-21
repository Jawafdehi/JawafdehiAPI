"""Tests for the DB-free standalone slug enricher (casework/enrich_slug.py).

Cover slug parsing, the light validate_slug gate, and the per-case pipeline
against a fake CaseworkApi + fake invoke_text. No database and no network are
touched (validate_slug / slugify run under pytest-django's configured settings).
"""

import json

from casework import enrich_slug as es

# ── slug parsing ─────────────────────────────────────────────────────────────


class TestParseSlug:
    def test_parses_json_object(self):
        assert es.parse_slug('{"slug": "sunil-poudel-080-cr-0047"}') == (
            "sunil-poudel-080-cr-0047"
        )

    def test_parses_json_in_code_fence(self):
        text = '```json\n{"slug": "ram-thapa-080-cr-0047"}\n```'
        assert es.parse_slug(text) == "ram-thapa-080-cr-0047"

    def test_parses_json_after_prose_with_braces(self):
        text = 'Here {x}: {"slug": "ram-thapa-080-cr-0047"}'
        assert es.parse_slug(text) == "ram-thapa-080-cr-0047"

    def test_lowercases(self):
        assert es.parse_slug('{"slug": "Ram-Thapa-080-CR-0047"}') == (
            "ram-thapa-080-cr-0047"
        )

    def test_accepts_bare_single_token(self):
        assert es.parse_slug("ram-thapa-080-cr-0047") == "ram-thapa-080-cr-0047"

    def test_rejects_bare_line_with_spaces(self):
        assert es.parse_slug("here is your slug") is None

    def test_empty_returns_none(self):
        assert es.parse_slug("") is None


# ── validate_slug gate ───────────────────────────────────────────────────────


class TestValidateSlug:
    def test_valid(self):
        assert es._validate_slug("sunil-poudel-land-fraud-080-cr-0047") is None

    def test_rejects_leading_digit(self):
        assert es._validate_slug("080-cr-0047-sunil") is not None

    def test_rejects_too_long(self):
        assert es._validate_slug("a" * 51) is not None

    def test_rejects_spaces(self):
        assert es._validate_slug("sunil poudel") is not None


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


class Args:
    def __init__(self, *, apply=False, dry_run=False, force=False):
        self.apply = apply
        self.dry_run = dry_run
        self.force = force
        self.verbose = False


def _case(**overrides):
    base = {
        "case_id": "case-0001",
        "slug": "case-0001-old-hash",
        "title": "Some draft case",
        "court_cases": ["special:080-CR-0047"],
        "entities": [],
        "bigo": 1000000,
    }
    base.update(overrides)
    return base


def _fixed_llm(slug):
    def invoke_text(system, content, tier=None, usage=None, max_tokens=None):
        return json.dumps({"slug": slug})

    return invoke_text


def _run(case, llm_slug, *, apply=False, dry_run=False, force=False):
    api = FakeApi(case)
    args = Args(apply=apply, dry_run=dry_run, force=force)
    stats = {
        "cases_processed": 0,
        "slugs_proposed": 0,
        "slugs_invalid": 0,
        "slugs_applied": 0,
        "cases_skipped": 0,
        "cases_llm_error": 0,
    }
    proposals = []
    es._process_case(
        case=case,
        idx=1,
        total=1,
        args=args,
        api=api,
        usage=None,
        invoke_text=_fixed_llm(llm_slug),
        stats=stats,
        proposals=proposals,
    )
    return api, stats, proposals


def test_valid_slug_recorded():
    api, stats, proposals = _run(_case(), "sunil-poudel-land-fraud-080-cr-0047")
    assert api.patched == []  # default does not apply
    assert stats["slugs_proposed"] == 1
    assert len(proposals) == 1
    record = proposals[0]
    assert record["valid"] is True
    assert record["slug_proposed"] == "sunil-poudel-land-fraud-080-cr-0047"
    assert "patch_ops" not in record


def test_apply_patches_valid_slug():
    api, stats, proposals = _run(
        _case(), "sunil-poudel-land-fraud-080-cr-0047", apply=True
    )
    assert api.patched == [
        ("case-0001-old-hash", "slug", "sunil-poudel-land-fraud-080-cr-0047")
    ]
    assert stats["slugs_applied"] == 1


def test_apply_with_dry_run_does_not_patch():
    api, stats, proposals = _run(
        _case(), "sunil-poudel-land-fraud-080-cr-0047", apply=True, dry_run=True
    )
    assert api.patched == []
    assert stats["slugs_applied"] == 0
    assert proposals[0]["valid"] is True  # still recorded


def test_invalid_slug_recorded_not_applied():
    api, stats, proposals = _run(_case(), "080-cr-0047-leading-digit", apply=True)
    assert api.patched == []
    assert stats["slugs_invalid"] == 1
    record = proposals[0]
    assert record["valid"] is False
    assert record["validation_error"]
    assert "patch_ops" not in record


def test_idempotent_skip_when_slug_ends_with_court_number():
    case = _case(slug="ram-thapa-080-cr-0047")
    api, stats, proposals = _run(case, "new-slug-080-cr-0047")
    assert proposals == []
    assert stats["cases_skipped"] == 1


def test_force_regenerates_already_enriched_slug():
    case = _case(slug="ram-thapa-080-cr-0047")
    api, stats, proposals = _run(case, "new-slug-080-cr-0047", force=True)
    assert len(proposals) == 1
    assert proposals[0]["slug_proposed"] == "new-slug-080-cr-0047"


def test_skips_case_without_court_number():
    case = _case(court_cases=[])
    api, stats, proposals = _run(case, "whatever-080-cr-0047")
    assert proposals == []
    assert stats["cases_skipped"] == 1


# ── file writing ─────────────────────────────────────────────────────────────


def test_write_proposals_is_well_formed_json(tmp_path):
    proposals = [
        {
            "case_id": "case-0001",
            "slug_current": "case-0001-old",
            "slug_proposed": "sunil-poudel-080-cr-0047",
            "valid": True,
            "validation_error": None,
        }
    ]
    out = tmp_path / "proposals.json"
    es._write_proposals(str(out), proposals)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded == proposals


# ── provider default ─────────────────────────────────────────────────────────


def test_defaults_to_claude_cli_provider():
    args = es._build_parser().parse_args([])
    assert args.provider == "claude_cli"


def test_output_default():
    args = es._build_parser().parse_args([])
    assert args.output == es.DEFAULT_OUTPUT
