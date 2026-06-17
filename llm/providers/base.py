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
