"""Enrich missing BIGO values for DRAFT cases using press releases + LLM extraction."""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from cases.models import Case, CaseState, DocumentSource, SourceType

MAX_LIMIT = 1000

_NEPALI_TO_ASCII_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


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
            help="Optional exact case_id to process.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview enrichment results without PATCHing cases.",
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
            "--anthropic-api-key",
            type=str,
            default=os.getenv("ANTHROPIC_API_KEY"),
            help="Anthropic API key. Defaults to ANTHROPIC_API_KEY.",
        )
        parser.add_argument(
            "--llm-model",
            type=str,
            default=os.getenv("BIGO_ENRICHMENT_MODEL", "claude-sonnet-4-5"),
            help="LLM model used for BIGO extraction.",
        )
        parser.add_argument(
            "--min-confidence",
            choices=["high", "medium", "low"],
            default="medium",
            help="Minimum accepted extraction confidence.",
        )

    def handle(self, *args, **options):
        self._validate_guardrails(options)
        self._validate_runtime_inputs(options)

        queryset = (
            Case.objects.filter(state=CaseState.DRAFT)
            .filter(Q(bigo__isnull=True) | Q(bigo=0))
            .order_by("-created_at")
        )
        if options["case_id"]:
            queryset = queryset.filter(case_id=options["case_id"])

        cases = list(queryset[: options["limit"]])
        if not cases:
            self.stdout.write("No eligible DRAFT case found for BIGO enrichment.")
            return

        updated = 0
        skipped = 0
        failed = 0

        for case in cases:
            try:
                source = self._select_press_release_source(case)
                if source is None:
                    skipped += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"[SKIP] {case.case_id}: no press release source found."
                        )
                    )
                    continue

                markdown = self._convert_source_to_markdown(source)
                bigo = self._extract_bigo_from_markdown(
                    markdown=markdown,
                    case=case,
                    model=options["llm_model"],
                    anthropic_api_key=options["anthropic_api_key"],
                    min_confidence=options["min_confidence"],
                )
                if bigo is None:
                    skipped += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"[SKIP] {case.case_id}: could not extract a reliable BIGO."
                        )
                    )
                    continue

                if options["dry_run"]:
                    self.stdout.write(
                        self.style.WARNING(
                            f"[DRY-RUN] {case.case_id}: would PATCH BIGO={bigo}"
                        )
                    )
                else:
                    self._patch_case_bigo(
                        case=case,
                        bigo=bigo,
                        api_base_url=options["api_base_url"],
                        api_token=options["api_token"],
                    )
                    self.stdout.write(
                        self.style.SUCCESS(f"[UPDATED] {case.case_id}: BIGO={bigo}")
                    )
                updated += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self.stdout.write(
                    self.style.ERROR(
                        f"[FAIL] {case.case_id}: {type(exc).__name__}: {exc}"
                    )
                )

        self.stdout.write(
            f"Processed={len(cases)} Updated={updated} Skipped={skipped} Failed={failed}"
        )

    def _validate_guardrails(self, options: dict[str, Any]) -> None:
        limit = options["limit"]
        if limit < 1 or limit > MAX_LIMIT:
            raise CommandError(f"--limit must be between 1 and {MAX_LIMIT}.")

        if not settings.DEBUG and not options["allow_production"]:
            raise CommandError(
                "This command refuses to run in production unless --allow-production is provided."
            )

    def _validate_runtime_inputs(self, options: dict[str, Any]) -> None:
        if options["dry_run"]:
            return

        if not options["api_token"]:
            raise CommandError(
                "JAWAFDEHI API token is required. Set --api-token or JAWAFDEHI_API_TOKEN."
            )
        if not options["anthropic_api_key"]:
            raise CommandError(
                "Anthropic API key is required. Set --anthropic-api-key or ANTHROPIC_API_KEY."
            )

    def _select_press_release_source(self, case: Case) -> DocumentSource | None:
        source_ids = [
            item["source_id"]
            for item in (case.evidence or [])
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        ]
        if not source_ids:
            return None

        sources = list(
            DocumentSource.objects.filter(
                source_id__in=source_ids,
                is_deleted=False,
            ).prefetch_related("uploaded_files")
        )
        if not sources:
            return None

        ranked = sorted(
            (
                (self._score_source_for_press_release(source), source)
                for source in sources
            ),
            key=lambda row: row[0],
            reverse=True,
        )
        best_score, best_source = ranked[0]
        return best_source if best_score > 0 else None

    def _score_source_for_press_release(self, source: DocumentSource) -> int:
        upload_names = [
            file.filename or Path(file.file.name).name
            for file in source.uploaded_files.all()
        ]
        url_text = " ".join(source.url or [])
        corpus = " ".join(
            [
                source.title or "",
                source.description or "",
                source.uploaded_filename or "",
                url_text,
                " ".join(upload_names),
            ]
        ).lower()

        score = 0
        press_keywords = [
            "press release",
            "pressrelease",
            "press-release",
            "प्रेस विज्ञप्ति",
            "विज्ञप्ति",
        ]
        ciaa_keywords = ["ciaa", "अख्तियार"]

        if any(keyword in corpus for keyword in press_keywords):
            score += 5
        if any(keyword in corpus for keyword in ciaa_keywords):
            score += 3
        if source.source_type == SourceType.OFFICIAL_GOVERNMENT:
            score += 1
        return score

    def _convert_source_to_markdown(self, source: DocumentSource) -> str:
        try:
            from markitdown import MarkItDown
        except ImportError as exc:  # pragma: no cover - env dependent
            raise CommandError(
                "markitdown is required for BIGO enrichment conversion. "
                "Install conversion dependencies (markitdown + likhit plugin)."
            ) from exc

        converter = MarkItDown(enable_plugins=True)
        with tempfile.TemporaryDirectory(prefix="bigo-enrichment-") as tmp_dir:
            temp_path = self._download_source_to_path(source, Path(tmp_dir))
            if temp_path:
                result = converter.convert_uri(temp_path.resolve().as_uri())
                return result.markdown

            source_url = self._pick_source_url(source)
            if not source_url:
                raise CommandError(
                    f"No downloadable source found for source_id={source.source_id}."
                )
            result = converter.convert_uri(source_url)
            return result.markdown

    def _download_source_to_path(
        self, source: DocumentSource, output_dir: Path
    ) -> Path | None:
        if source.uploaded_file:
            filename = source.uploaded_filename or Path(source.uploaded_file.name).name
            if not filename:
                filename = f"{source.source_id}.bin"
            out_path = output_dir / filename
            with source.uploaded_file.open("rb") as in_file:
                out_path.write_bytes(in_file.read())
            return out_path

        uploaded = source.uploaded_files.first()
        if uploaded and uploaded.file:
            filename = uploaded.filename or Path(uploaded.file.name).name
            if not filename:
                filename = f"{source.source_id}.bin"
            out_path = output_dir / filename
            with uploaded.file.open("rb") as in_file:
                out_path.write_bytes(in_file.read())
            return out_path

        source_url = self._pick_source_url(source)
        if not source_url:
            return None

        parsed = urllib.parse.urlparse(source_url)
        guessed_name = Path(parsed.path).name or f"{source.source_id}.bin"
        out_path = output_dir / guessed_name
        try:
            request = urllib.request.Request(
                source_url,
                headers={"User-Agent": "jawafdehi-bigo-enrichment/1.0"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                out_path.write_bytes(response.read())
            return out_path
        except urllib.error.URLError:
            return None

    def _pick_source_url(self, source: DocumentSource) -> str | None:
        urls = [
            url for url in (source.url or []) if isinstance(url, str) and url.strip()
        ]
        return urls[0].strip() if urls else None

    def _extract_bigo_from_markdown(
        self,
        markdown: str,
        case: Case,
        model: str,
        anthropic_api_key: str,
        min_confidence: str,
    ) -> int | None:
        import anthropic

        client = anthropic.Anthropic(api_key=anthropic_api_key)
        prompt = self._build_bigo_prompt(markdown=markdown, case=case)
        response = client.messages.create(
            model=model,
            max_tokens=500,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text
            for block in response.content
            if getattr(block, "type", "") == "text"
        )
        payload = self._parse_json_response(text)
        confidence = str(payload.get("confidence", "")).strip().lower()
        if self._confidence_rank(confidence) < self._confidence_rank(min_confidence):
            return None
        return self._coerce_bigo_int(payload.get("bigo"))

    def _build_bigo_prompt(self, markdown: str, case: Case) -> str:
        return f"""You extract BIGO (बिगो) amount from CIAA press release content.

Return STRICT JSON only with this schema:
{{
  "bigo": <integer or null>,
  "confidence": "high" | "medium" | "low",
  "evidence_quote": "<short quote from text that supports the amount>"
}}

Rules:
1. BIGO must be NPR integer only.
2. Remove commas/currency words/symbols before returning integer.
3. If no reliable BIGO exists in the text, return null with low confidence.
4. Do not return ranges, floats, or formatted strings.
5. Prefer explicit BIGO claims; ignore unrelated monetary figures if ambiguous.

Case ID: {case.case_id}
Case title: {case.title}

Press release markdown:
{markdown[:100000]}
"""

    def _parse_json_response(self, content: str) -> dict[str, Any]:
        content = content.strip()
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise ValueError("LLM response did not contain a JSON object.")
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise ValueError("LLM response JSON root must be an object.")
        return parsed

    def _confidence_rank(self, confidence: str) -> int:
        rank = {"low": 1, "medium": 2, "high": 3}
        return rank.get(confidence, 0)

    def _coerce_bigo_int(self, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, float):
            return int(value) if value > 0 else None
        if not isinstance(value, str):
            return None

        normalized = value.translate(_NEPALI_TO_ASCII_DIGITS)
        digits_only = re.sub(r"[^\d]", "", normalized)
        if not digits_only:
            return None
        bigo = int(digits_only)
        return bigo if bigo > 0 else None

    def _patch_case_bigo(
        self,
        case: Case,
        bigo: int,
        api_base_url: str,
        api_token: str,
    ) -> None:
        url = self._case_patch_url(api_base_url, case.id)
        payload = [{"op": "replace", "path": "/bigo", "value": bigo}]
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url=url,
            method="PATCH",
            data=data,
            headers={
                "Authorization": f"Token {api_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30):
                return
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise CommandError(
                f"PATCH failed for case {case.case_id} (status {exc.code}): {body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise CommandError(
                f"PATCH failed for case {case.case_id}: {exc.reason}"
            ) from exc

    def _case_patch_url(self, api_base_url: str, case_db_id: int) -> str:
        base = api_base_url.rstrip("/")
        if base.endswith("/api"):
            return f"{base}/cases/{case_db_id}/"
        return f"{base}/api/cases/{case_db_id}/"
