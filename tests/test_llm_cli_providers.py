"""Unit tests for CLI-based LLM providers.

Tests the parsers without invoking real CLIs by monkeypatching _run().
"""

import json
import unittest
from unittest.mock import patch

from llm.providers.cli import ClaudeCliProvider, CodexCliProvider
from llm.usage import UsageAccumulator


class TestClaudeCliProvider(unittest.TestCase):
    """Test ClaudeCliProvider JSON parsing."""

    def test_invoke_text_parses_claude_output(self):
        """Test parsing of real claude -p --output-format json output."""
        provider = ClaudeCliProvider()

        # Real output sample from the spec
        sample_output = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": '{"score": 91}',
                "total_cost_usd": 0.05334775,
                "usage": {
                    "input_tokens": 6,
                    "cache_creation_input_tokens": 7167,
                    "cache_read_input_tokens": 16498,
                    "output_tokens": 11,
                },
                "modelUsage": {
                    "claude-opus-4-7[1m]": {
                        "inputTokens": 6,
                        "outputTokens": 11,
                        "costUSD": 0.05334775,
                    }
                },
            }
        )

        with patch.object(provider, "_run", return_value=sample_output):
            usage = UsageAccumulator()
            result = provider.invoke_text(
                system="grade this",
                content="test",
                max_tokens=900,
                model_id="claude-opus-4-7",
                tier="cheap",
                usage=usage,
            )

            # Should return the result field (JSON string)
            self.assertEqual(result, '{"score": 91}')

            # Verify usage was recorded
            self.assertEqual(
                usage.input_tokens, 6 + 7167 + 16498
            )  # input + cache creation + cache read
            self.assertEqual(usage.output_tokens, 11)
            self.assertAlmostEqual(usage.cost_usd, 0.05334775, places=6)
            self.assertEqual(usage.calls, 1)

            # Verify per-bucket breakdown
            by_provider = usage.as_dict()["by_provider"]
            self.assertEqual(len(by_provider), 1)
            self.assertEqual(by_provider[0]["provider"], "claude_cli")
            self.assertEqual(by_provider[0]["tier"], "cheap")
            self.assertEqual(by_provider[0]["model"], "claude-opus-4-7[1m]")

    def test_multi_model_usage_labels_bucket_with_the_dominant_model(self):
        """When claude -p reports usage for more than one model, the usage bucket
        must be labelled with the model that did the real work (highest cost),
        not whichever key the CLI happens to list first. Regression: a review
        graded on opus was mislabelled as the auxiliary haiku the CLI runs for
        internal housekeeping, because the parser took modelUsage's first key."""
        provider = ClaudeCliProvider()

        # The auxiliary (haiku) key is listed FIRST but is a tiny side-task; the
        # requested opus model produced the graded result and dominates by cost.
        sample_output = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": '{"score": 88}',
                "total_cost_usd": 2.85,
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 500000,
                    "cache_read_input_tokens": 44000,
                    "output_tokens": 11000,
                },
                "modelUsage": {
                    "claude-haiku-4-5-20251001": {
                        "inputTokens": 300,
                        "outputTokens": 40,
                        "costUSD": 0.0007,
                    },
                    "claude-opus-4-8[1m]": {
                        "inputTokens": 10,
                        "outputTokens": 11000,
                        "costUSD": 2.8493,
                    },
                },
            }
        )

        with patch.object(provider, "_run", return_value=sample_output):
            usage = UsageAccumulator()
            provider.invoke_text(
                system="grade this",
                content="test",
                max_tokens=900,
                model_id="claude-opus-4-8[1m]",
                tier="cheap",
                usage=usage,
            )

            by_provider = usage.as_dict()["by_provider"]
            self.assertEqual(len(by_provider), 1)
            self.assertEqual(by_provider[0]["model"], "claude-opus-4-8[1m]")

    def test_empty_model_usage_falls_back_to_requested_model(self):
        """With no modelUsage map, the bucket is labelled with the model we asked
        the CLI to run (``--model``), not the "claude" last-resort default."""
        provider = ClaudeCliProvider()
        sample_output = json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": '{"score": 70}',
                "total_cost_usd": 0.01,
                "usage": {"input_tokens": 5, "output_tokens": 5},
                # no modelUsage key at all
            }
        )

        with patch.object(provider, "_run", return_value=sample_output):
            usage = UsageAccumulator()
            provider.invoke_text(
                system="grade this",
                content="test",
                max_tokens=900,
                model_id="claude-opus-4-8[1m]",
                tier="cheap",
                usage=usage,
            )

            by_provider = usage.as_dict()["by_provider"]
            self.assertEqual(by_provider[0]["model"], "claude-opus-4-8[1m]")

    def test_invoke_text_handles_error(self):
        """Test error handling when is_error is true."""
        provider = ClaudeCliProvider()
        error_output = json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": "Something went wrong",
            }
        )

        with patch.object(provider, "_run", return_value=error_output):
            usage = UsageAccumulator()
            with self.assertRaises(RuntimeError) as ctx:
                provider.invoke_text(
                    system="sys",
                    content="msg",
                    max_tokens=900,
                    model_id="m1",
                    tier="cheap",
                    usage=usage,
                )
            self.assertIn("claude_cli error", str(ctx.exception))

    def test_invoke_text_handles_missing_result(self):
        """Test error handling when result field is missing."""
        provider = ClaudeCliProvider()
        bad_output = json.dumps(
            {
                "type": "result",
                "is_error": False,
                # missing "result" field
            }
        )

        with patch.object(provider, "_run", return_value=bad_output):
            with self.assertRaises(RuntimeError) as ctx:
                provider.invoke_text(
                    system="sys",
                    content="msg",
                    max_tokens=900,
                    model_id="m1",
                    tier="cheap",
                    usage=None,
                )
            self.assertIn("claude_cli error", str(ctx.exception))

    def test_invoke_text_handles_malformed_json(self):
        """Test error handling for malformed JSON output."""
        provider = ClaudeCliProvider()
        bad_json = "not valid json at all"

        with patch.object(provider, "_run", return_value=bad_json):
            with self.assertRaises(RuntimeError) as ctx:
                provider.invoke_text(
                    system="sys",
                    content="msg",
                    max_tokens=900,
                    model_id="m1",
                    tier="cheap",
                    usage=None,
                )
            self.assertIn("failed to parse JSON", str(ctx.exception))

    def test_model_for_tier_returns_string(self):
        """Test model_for_tier returns a string (configured or empty)."""
        provider = ClaudeCliProvider()
        # Test that it returns the configured value from settings (or empty by default)
        model = provider.model_for_tier("premium")
        self.assertIsInstance(model, str)


class TestCodexCliProvider(unittest.TestCase):
    """Test CodexCliProvider JSONL parsing."""

    def test_invoke_text_parses_codex_output(self):
        """Test parsing of real codex exec --json JSONL output."""
        provider = CodexCliProvider()

        # Real output sample from the spec (JSONL format)
        sample_output = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t123"}),
                json.dumps({"type": "turn.started"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_0",
                            "type": "agent_message",
                            "text": '{"score":91}',
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 13275,
                            "cached_input_tokens": 4992,
                            "output_tokens": 51,
                            "reasoning_output_tokens": 40,
                        },
                    }
                ),
            ]
        )

        with patch.object(provider, "_run", return_value=sample_output):
            usage = UsageAccumulator()
            result = provider.invoke_text(
                system="grade this",
                content="test",
                max_tokens=900,
                model_id="codex",
                tier="cheap",
                usage=usage,
            )

            # Should return the agent_message text
            self.assertEqual(result, '{"score":91}')

            # Verify usage was recorded
            self.assertEqual(usage.input_tokens, 13275)
            self.assertEqual(usage.output_tokens, 51 + 40)  # output + reasoning
            self.assertEqual(usage.cost_usd, 0.0)  # subscription-based
            self.assertEqual(usage.calls, 1)

            # Verify per-bucket breakdown
            by_provider = usage.as_dict()["by_provider"]
            self.assertEqual(len(by_provider), 1)
            self.assertEqual(by_provider[0]["provider"], "codex_cli")
            self.assertEqual(by_provider[0]["tier"], "cheap")

    def test_invoke_text_skips_non_json_lines(self):
        """Test that non-JSON lines in JSONL are skipped."""
        provider = CodexCliProvider()

        # JSONL with some non-JSON noise
        sample_output = "\n".join(
            [
                "Reading additional input from stdin...",
                json.dumps({"type": "turn.started"}),
                "Some other text that's not JSON",
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": '{"result":"ok"}',
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 50,
                            "reasoning_output_tokens": 0,
                        },
                    }
                ),
            ]
        )

        with patch.object(provider, "_run", return_value=sample_output):
            usage = UsageAccumulator()
            result = provider.invoke_text(
                system="sys",
                content="msg",
                max_tokens=900,
                model_id="codex",
                tier="cheap",
                usage=usage,
            )

            # Should successfully extract the agent message
            self.assertEqual(result, '{"result":"ok"}')
            self.assertEqual(usage.input_tokens, 100)

    def test_invoke_text_handles_missing_agent_message(self):
        """Test error when no agent_message is found."""
        provider = CodexCliProvider()

        # JSONL with usage but no agent_message
        sample_output = "\n".join(
            [
                json.dumps({"type": "turn.started"}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 100, "output_tokens": 50},
                    }
                ),
            ]
        )

        with patch.object(provider, "_run", return_value=sample_output):
            with self.assertRaises(RuntimeError) as ctx:
                provider.invoke_text(
                    system="sys",
                    content="msg",
                    max_tokens=900,
                    model_id="codex",
                    tier="cheap",
                    usage=None,
                )
            self.assertIn("no agent message found", str(ctx.exception))

    def test_invoke_text_uses_last_agent_message(self):
        """Test that the LAST agent_message is used (if multiple exist)."""
        provider = CodexCliProvider()

        sample_output = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "first"},
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "last"},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 100, "output_tokens": 50},
                    }
                ),
            ]
        )

        with patch.object(provider, "_run", return_value=sample_output):
            result = provider.invoke_text(
                system="sys",
                content="msg",
                max_tokens=900,
                model_id="codex",
                tier="cheap",
                usage=None,
            )
            # Should use the last one
            self.assertEqual(result, "last")

    def test_model_for_tier_returns_string(self):
        """Test model_for_tier returns a string (configured or empty)."""
        provider = CodexCliProvider()
        # Test that it returns the configured value from settings (or empty by default)
        model = provider.model_for_tier("premium")
        self.assertIsInstance(model, str)

    def test_flatten_content_string(self):
        """Test _flatten with a plain string."""
        from llm.providers.cli import _flatten

        content = "test content"
        result = _flatten(content)
        self.assertEqual(result, "test content")

    def test_flatten_content_blocks(self):
        """Test _flatten with cache_control blocks."""
        from llm.providers.cli import _flatten

        content = [
            {"type": "text", "text": "block 1"},
            {"type": "text", "text": "block 2"},
        ]
        result = _flatten(content)
        self.assertEqual(result, "block 1\n\nblock 2")


if __name__ == "__main__":
    unittest.main()
