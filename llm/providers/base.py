"""Base provider interface for LLM invocations."""


def strip_code_fence(text):
    """Remove markdown code fences and json language marker from text."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text


class Provider:
    """Abstract base class for LLM providers."""

    name: str = None
    # Whether the provider can run an agentic tool-use loop (invoke_with_tools).
    # API providers (bedrock/proxy) set True; CLI harnesses leave it False and
    # invoke_with_tools degrades to a single no-tool invoke_text for them.
    supports_tools: bool = False

    def invoke_text(
        self, system, content, max_tokens, model_id, tier, usage=None
    ) -> str:
        """Invoke the LLM and return raw text response.

        Args:
            system: System prompt text
            content: User message content (string or list of blocks)
            max_tokens: Max tokens in response
            model_id: Model identifier
            tier: "premium" or "cheap"
            usage: Optional UsageAccumulator to record token counts

        Returns:
            Raw text response (code fences stripped)

        Raises:
            NotImplementedError: Subclasses must implement
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement invoke_text"
        )

    def model_for_tier(self, tier) -> str:
        """Resolve a tier to the provider's model id.

        Args:
            tier: "premium" or "cheap"

        Returns:
            Model identifier string

        Raises:
            NotImplementedError: Subclasses must implement
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement model_for_tier"
        )

    def invoke_with_tools(
        self,
        system,
        content,
        max_tokens,
        model_id,
        tier,
        tools,
        usage=None,
        max_iterations=8,
    ) -> str:
        """Run an agentic tool-use loop and return the model's final text.

        Only the API providers (bedrock, proxy) implement this. The CLI-harness
        providers are their own agents and cannot accept a Python tool callback
        mid-loop, so they inherit this default and raise.

        Args:
            tools: list of llm.tools.Tool the model may call.
            max_iterations: cap on model<->tool round-trips.
        """
        raise NotImplementedError(
            f"{self.name or self.__class__.__name__} does not support tool-use; "
            "route the tool-using tier to the 'bedrock' or 'proxy' provider."
        )
