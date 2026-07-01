"""Enrich missing BIGO values for DRAFT cases using press releases + LLM extraction."""

from __future__ import annotations

import os

from django.core.management.base import BaseCommand

MAX_LIMIT = 1000


def _first_env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


class Command(BaseCommand):
    help = (
        "Find DRAFT cases with missing BIGO, extract amount from CIAA press release "
        "content, and PATCH BIGO via API."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help=f"Max cases to process (1-{MAX_LIMIT}).",
        )
        parser.add_argument(
            "--case-id",
            type=str,
            default=None,
            help="Optional exact slug to process.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Preview eligible cases and selected source without downloading, "
                "calling the LLM, or PATCHing cases. Use --dry-run-extract to "
                "also run source conversion and LLM extraction."
            ),
        )
        parser.add_argument(
            "--dry-run-extract",
            action="store_true",
            help=(
                "With --dry-run, run source download/conversion and LLM extraction, "
                "but still do not PATCH cases."
            ),
        )
        parser.add_argument(
            "--allow-production",
            action="store_true",
            help="Required when DEBUG=False to run this command in production.",
        )
        parser.add_argument(
            "--api-base-url",
            type=str,
            default=os.getenv("JAWAFDEHI_API_BASE_URL", "http://127.0.0.1:8000"),
            help="Jawafdehi API base URL (root or /api).",
        )
        parser.add_argument(
            "--api-token",
            type=str,
            default=os.getenv("JAWAFDEHI_API_TOKEN"),
            help="Jawafdehi API token. Defaults to JAWAFDEHI_API_TOKEN.",
        )
        parser.add_argument(
            "--llm-api-key",
            type=str,
            default=os.getenv("JAWAFDEHI_LLM_API_KEY"),
            help="LLM API key for native Anthropic or the OpenAI-compatible proxy. Defaults to JAWAFDEHI_LLM_API_KEY.",
        )
        parser.add_argument(
            "--anthropic-api-key",
            type=str,
            default=os.getenv("ANTHROPIC_API_KEY"),
            help="Deprecated alias for --llm-api-key. Defaults to ANTHROPIC_API_KEY.",
        )
        parser.add_argument(
            "--llm-model",
            type=str,
            default=_first_env(
                "BIGO_ENRICHMENT_MODEL",
                "JAWAFDEHI_CASEWORK_MODEL",
                default="claude-sonnet-4-5",
            ),
            help=(
                "LLM model used for BIGO extraction. Defaults to BIGO_ENRICHMENT_MODEL, "
                "JAWAFDEHI_CASEWORK_MODEL, or claude-sonnet-4-5."
            ),
        )
        parser.add_argument(
            "--llm-base-url",
            type=str,
            default=_first_env(
                "BIGO_ENRICHMENT_LLM_BASE_URL",
                "BIGO_ENRICHMENT_BASE_URL",
                "JAWAFDEHI_CASEWORK_BASE_URL",
                "JAWAFDEHI_LLM_PROXY_URL",
            ),
            help=(
                "LLM API base URL (for OpenAI-compatible proxy). Defaults to "
                "BIGO_ENRICHMENT_LLM_BASE_URL, BIGO_ENRICHMENT_BASE_URL, "
                "JAWAFDEHI_CASEWORK_BASE_URL, or JAWAFDEHI_LLM_PROXY_URL."
            ),
        )
        parser.add_argument(
            "--llm-timeout",
            type=float,
            default=float(os.getenv("BIGO_ENRICHMENT_LLM_TIMEOUT", "120")),
            help="LLM request timeout in seconds. Defaults to BIGO_ENRICHMENT_LLM_TIMEOUT or 120.",
        )
        parser.add_argument(
            "--llm-max-tokens",
            type=int,
            default=int(os.getenv("BIGO_ENRICHMENT_LLM_MAX_TOKENS", "2000")),
            help="LLM response token budget. Defaults to BIGO_ENRICHMENT_LLM_MAX_TOKENS or 2000.",
        )
        parser.add_argument(
            "--download-timeout",
            type=float,
            default=float(os.getenv("BIGO_ENRICHMENT_DOWNLOAD_TIMEOUT", "30")),
            help="Source download timeout in seconds. Defaults to BIGO_ENRICHMENT_DOWNLOAD_TIMEOUT or 30.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable detailed per-case logging for enrichment flow.",
        )
        parser.add_argument(
            "--min-confidence",
            choices=["high", "medium", "low"],
            default="medium",
            help="Minimum accepted extraction confidence.",
        )
        parser.add_argument(
            "--priority",
            action="store_true",
            help="Enrich only cases in the priority case list.",
        )

    def handle(self, *args, **options):
        raise NotImplementedError(
            "This command creates/reads DocumentSource rows, which have been "
            "removed (ADR: cases own no documents). It must be rewired to create "
            "Material + CaseMaterialReference records before use. See "
            "docs/jawafdehi/sources-to-materials-prod-migration.md."
        )
