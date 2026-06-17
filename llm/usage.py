"""Thread-safe token usage tracking with per-provider/tier breakdown."""

import threading


class UsageAccumulator:
    """Thread-safe tally of token usage across parallel calls.

    Tracks both flat totals and per-(provider, tier, model) breakdowns for
    detailed usage analysis and cost reporting.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0
        self.cost_usd = 0.0
        # Dict keyed by (provider, tier, model) -> {"calls": int, ...}
        self._by = {}

    def add(
        self,
        input_tokens,
        output_tokens,
        *,
        provider="bedrock",
        tier="premium",
        model="",
        cost_usd=0.0,
    ):
        """Record token usage from an LLM invocation.

        Args:
            input_tokens: Number of input tokens (coerced to int, None->0)
            output_tokens: Number of output tokens (coerced to int, None->0)
            provider: Provider name (default "bedrock")
            tier: Tier name (default "premium")
            model: Model identifier (default "")
            cost_usd: Cost in USD (default 0.0)
        """
        input_tokens = int(input_tokens or 0)
        output_tokens = int(output_tokens or 0)
        cost_usd = float(cost_usd or 0.0)

        with self._lock:
            # Update flat totals
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.calls += 1
            self.cost_usd += cost_usd

            # Update per-bucket breakdown
            key = (provider, tier, model)
            if key not in self._by:
                self._by[key] = {
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                }
            self._by[key]["calls"] += 1
            self._by[key]["input_tokens"] += input_tokens
            self._by[key]["output_tokens"] += output_tokens
            self._by[key]["cost_usd"] += cost_usd

    def as_dict(self):
        """Return usage as a dict with flat totals and per-provider breakdown.

        Returns:
            Dict with keys: model_id, calls, input_tokens, output_tokens,
            total_tokens, cost_usd, by_provider (list of per-bucket dicts)
        """
        # Import locally to avoid circular dependency. Resolving the premium
        # model can raise (e.g. proxy tier with no model id) — usage reporting
        # must never crash an otherwise-successful review, so fall back to "".
        from llm.routing import active_premium_model

        try:
            model_id = active_premium_model()
        except Exception:  # noqa: BLE001 - reporting is best-effort
            model_id = ""

        with self._lock:
            in_tok = self.input_tokens
            out_tok = self.output_tokens
            calls = self.calls
            cost = self.cost_usd
            by_buckets = dict(self._by)

        # Build per-provider list
        by_provider = []
        for (provider, tier, model), bucket in by_buckets.items():
            by_provider.append(
                {
                    "provider": provider,
                    "tier": tier,
                    "model": model,
                    "calls": bucket["calls"],
                    "input_tokens": bucket["input_tokens"],
                    "output_tokens": bucket["output_tokens"],
                    "total_tokens": bucket["input_tokens"] + bucket["output_tokens"],
                    "cost_usd": bucket["cost_usd"],
                }
            )

        return {
            "model_id": model_id,
            "calls": calls,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": in_tok + out_tok,
            "cost_usd": cost,
            "by_provider": by_provider,
        }


class SessionUsage:
    """Accumulates token usage across multiple reviews in a session."""

    def __init__(self):
        self._lock = threading.Lock()
        # Dict: (provider, tier, model) -> {"calls": int, "input_tokens": int, ...}
        self._totals = {}

    def merge_usage_dict(self, token_usage: dict):
        """Merge a result's token_usage dict into session totals.

        If token_usage has a by_provider list, merge each bucket.
        Otherwise (legacy format), bucket the flat totals under
        provider="bedrock", tier="premium", model from token_usage.

        Args:
            token_usage: Dict from result["token_usage"] or similar
        """
        if not token_usage:
            return

        with self._lock:
            if "by_provider" in token_usage:
                # New format with per-bucket breakdown
                for bucket in token_usage.get("by_provider", []):
                    key = (
                        bucket.get("provider", "bedrock"),
                        bucket.get("tier", "premium"),
                        bucket.get("model", ""),
                    )
                    if key not in self._totals:
                        self._totals[key] = {
                            "calls": 0,
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "cost_usd": 0.0,
                        }
                    self._totals[key]["calls"] += int(bucket.get("calls") or 0)
                    self._totals[key]["input_tokens"] += int(
                        bucket.get("input_tokens") or 0
                    )
                    self._totals[key]["output_tokens"] += int(
                        bucket.get("output_tokens") or 0
                    )
                    self._totals[key]["cost_usd"] += float(
                        bucket.get("cost_usd") or 0.0
                    )
            else:
                # Legacy format: flat totals
                key = ("bedrock", "premium", token_usage.get("model_id", ""))
                if key not in self._totals:
                    self._totals[key] = {
                        "calls": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_usd": 0.0,
                    }
                self._totals[key]["calls"] += int(token_usage.get("calls") or 0)
                self._totals[key]["input_tokens"] += int(
                    token_usage.get("input_tokens") or 0
                )
                self._totals[key]["output_tokens"] += int(
                    token_usage.get("output_tokens") or 0
                )
                self._totals[key]["cost_usd"] += float(
                    token_usage.get("cost_usd") or 0.0
                )

    def totals(self) -> list:
        """Return merged usage as a list of per-bucket dicts.

        Returns:
            List of dicts with keys: provider, tier, model, calls,
            input_tokens, output_tokens, total_tokens, cost_usd
        """
        with self._lock:
            result = []
            for (provider, tier, model), bucket in self._totals.items():
                result.append(
                    {
                        "provider": provider,
                        "tier": tier,
                        "model": model,
                        "calls": bucket["calls"],
                        "input_tokens": bucket["input_tokens"],
                        "output_tokens": bucket["output_tokens"],
                        "total_tokens": bucket["input_tokens"]
                        + bucket["output_tokens"],
                        "cost_usd": bucket["cost_usd"],
                    }
                )
            return result


def render_usage_table(by_provider, *, title=None) -> str:
    """Render usage data as a clean fixed-width text table.

    Args:
        by_provider: List of dicts with provider, tier, model, calls, etc.
        title: Optional table title

    Returns:
        Formatted text table (no trailing newline)
    """
    if not by_provider:
        return "(no usage)"

    lines = []
    if title:
        lines.append(f"{title}")
        lines.append("-" * 80)

    # Header
    lines.append(
        "Provider   | Tier     | Model                    | Calls | In      | Out     | Total   | Cost($)"
    )
    lines.append("-" * 95)

    # Data rows
    total_calls = 0
    total_in = 0
    total_out = 0
    total_cost = 0.0

    for row in by_provider:
        provider = row.get("provider", "")[:10].ljust(10)
        tier = row.get("tier", "")[:8].ljust(8)
        model = row.get("model", "")[:24].ljust(24)
        calls = row.get("calls", 0)
        in_tok = row.get("input_tokens", 0)
        out_tok = row.get("output_tokens", 0)
        total_tok = in_tok + out_tok
        cost = row.get("cost_usd", 0.0)

        total_calls += calls
        total_in += in_tok
        total_out += out_tok
        total_cost += cost

        lines.append(
            f"{provider} | {tier} | {model} | {calls:5d} | {in_tok:7d} | {out_tok:7d} | {total_tok:7d} | ${cost:7.4f}"
        )

    # Total row
    lines.append("-" * 95)
    lines.append(
        f"{'TOTAL':<10} | {'':<8} | {'':<24} | {total_calls:5d} | {total_in:7d} | {total_out:7d} | {total_in + total_out:7d} | ${total_cost:7.4f}"
    )

    return "\n".join(lines)
