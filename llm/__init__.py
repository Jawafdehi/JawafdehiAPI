"""LLM invocation and token usage machinery.

Provides a generic, provider-agnostic interface to LLM backends (Bedrock, proxy)
with thread-safe token usage tracking and tiered model routing.
"""
