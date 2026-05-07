import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from django.core.cache import cache

logger = logging.getLogger(__name__)


class MCPService:
    """Service for managing MCP server connections and case data retrieval."""

    MCP_TIMEOUT = 10
    CACHE_TIMEOUT = 3600  # 1 hour
    MAX_RETRIES = 3

    def test_connection(self, server):
        try:
            headers = self._get_headers(server)
            response = requests.get(f"{server.url}/health", headers=headers, timeout=5)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"MCP server {server.name} connection test failed: {e}")
            return False

    def _get_headers(self, server):
        if server.auth_type == "bearer":
            return {"Authorization": f"Bearer {server.auth_token}"}
        elif server.auth_type == "api_key":
            return {"X-API-Key": server.auth_token}
        return {}

    def retrieve_case_data(self, case_number):
        cache_key = f"cw_case_data_{case_number}"
        cached = cache.get(cache_key)
        if cached:
            return cached
        data = self.retrieve_case_data_parallel(case_number)
        if data:
            cache.set(cache_key, data, self.CACHE_TIMEOUT)
        return data

    def retrieve_case_data_parallel(self, case_number):
        from .models import MCPServer

        servers = MCPServer.objects.filter(status="connected")
        if not servers.exists():
            logger.warning("No connected MCP servers found")
            return None

        case_data = {}
        with ThreadPoolExecutor(max_workers=len(servers)) as executor:
            futures = {
                executor.submit(self._retrieve_from_server, server, case_number): server
                for server in servers
            }
            for future in as_completed(futures):
                server = futures[future]
                try:
                    data = future.result()
                    if data:
                        case_data.update(data)
                except Exception as e:
                    logger.error(f"Failed to retrieve from {server.name}: {e}")

        return case_data if case_data else None

    def _retrieve_from_server(self, server, case_number, retry_count=0):
        try:
            headers = self._get_headers(server)
            response = requests.get(
                f"{server.url}/api/cases/{case_number}",
                headers=headers,
                timeout=self.MCP_TIMEOUT,
            )
            if response.status_code == 200:
                return response.json()
            return None
        except requests.Timeout:
            if retry_count < self.MAX_RETRIES:
                return self._retrieve_from_server(server, case_number, retry_count + 1)
            return None
        except Exception as e:
            logger.error(f"Error retrieving from {server.name}: {e}")
            return None


class LLMService:
    """Service for LLM provider connections using LangChain chat models."""

    def resolve_default_provider(self):
        from .models import LLMProvider

        provider = (
            LLMProvider.objects.filter(is_active=True, is_default=True).first()
            or LLMProvider.objects.filter(is_active=True).first()
        )
        if not provider:
            raise ValueError("No active LLM provider configured")
        return provider

    def resolve_answer_provider(self, config):
        configured = getattr(config, "llm_provider", None)
        return (
            self._active_provider_or_none(configured) or self.resolve_default_provider()
        )

    def resolve_classifier_provider(self, config):
        return (
            self._active_provider_or_none(
                getattr(config, "classifier_llm_provider", None)
            )
            or self._active_provider_or_none(getattr(config, "llm_provider", None))
            or self.resolve_default_provider()
        )

    def get_chat_model(self, provider=None):
        provider = provider or self.resolve_default_provider()
        provider_type = provider.provider_type
        common_kwargs = self._common_chat_kwargs(provider)
        api_key = self._provider_api_key(provider)

        try:
            if provider_type == "anthropic":
                from langchain_anthropic import ChatAnthropic

                return ChatAnthropic(**common_kwargs)
            if provider_type == "openai":
                from langchain_openai import ChatOpenAI

                kwargs = {**common_kwargs}
                if provider.base_url:
                    kwargs["base_url"] = provider.base_url
                return ChatOpenAI(**kwargs)
            if provider_type == "google":
                from langchain_google_genai import ChatGoogleGenerativeAI

                kwargs = {
                    "model": provider.model,
                    "google_api_key": api_key,
                    "temperature": provider.temperature,
                    "max_output_tokens": provider.max_tokens,
                    **self._extra_config(provider),
                }
                return ChatGoogleGenerativeAI(**kwargs)
            if provider_type == "azure":
                from langchain_openai import AzureChatOpenAI

                return AzureChatOpenAI(
                    api_key=api_key,
                    azure_endpoint=provider.base_url,
                    azure_deployment=provider.deployment_name,
                    api_version=provider.api_version,
                    model=provider.model,
                    temperature=provider.temperature,
                    max_tokens=provider.max_tokens,
                    **self._extra_config(provider),
                )
            if provider_type == "ollama":
                from langchain_ollama import ChatOllama

                kwargs = {
                    "model": provider.model,
                    "temperature": provider.temperature,
                    **self._extra_config(provider),
                }
                if provider.base_url:
                    kwargs["base_url"] = provider.base_url
                return ChatOllama(**kwargs)
            if provider_type == "custom":
                from langchain_openai import ChatOpenAI

                kwargs = {
                    "model": provider.model,
                    "base_url": provider.base_url,
                    "temperature": provider.temperature,
                    "max_tokens": provider.max_tokens,
                    **self._extra_config(provider),
                }
                if api_key:
                    kwargs["api_key"] = api_key
                return ChatOpenAI(**kwargs)
        except ImportError as e:
            raise ValueError(f"LangChain provider not installed: {e}")

        raise ValueError(f"Unsupported provider type: {provider_type}")

    def invoke_text(
        self,
        provider,
        messages: str | list[dict[str, str]] | list[tuple[str, str]],
        *,
        run_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        model = self.get_chat_model(provider)
        response = model.invoke(
            self._normalize_messages(messages),
            config=self._run_config(run_name=run_name, metadata=metadata),
        )
        return self._coerce_text(response)

    def invoke_structured(
        self,
        provider,
        messages: str | list[dict[str, str]] | list[tuple[str, str]],
        schema,
        *,
        run_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        model = self.get_chat_model(provider)
        structured_model = self._with_structured_output(model, provider, schema)
        result = structured_model.invoke(
            self._normalize_messages(messages),
            config=self._run_config(run_name=run_name, metadata=metadata),
        )
        if hasattr(schema, "model_validate") and isinstance(result, dict):
            return schema.model_validate(result)
        return result

    def get_llm(self, provider=None):
        """Backward-compatible alias for existing callers."""
        return self.get_chat_model(provider)

    def test_connection(self, provider):
        return self.test_connection_details(provider)["connected"]

    def test_connection_details(self, provider) -> dict[str, Any]:
        try:
            response = self.invoke_text(provider, "Hello", run_name="llm-test")
            return {"connected": bool(response), "error": ""}
        except Exception as e:
            logger.error(
                f"LLM provider {provider.provider_type} connection test failed: {e}"
            )
            return {"connected": False, "error": str(e)}

    def _call_llm(self, llm, prompt):
        """Call LLM with compatibility for both old (predict) and new (invoke) APIs."""
        if hasattr(llm, "invoke"):
            # New LangChain API (v1.x+)
            response = llm.invoke(prompt)
            # Handle different response types
            if hasattr(response, "content"):
                return response.content
            return str(response)
        elif hasattr(llm, "predict"):
            # Old LangChain API (v0.x)
            return llm.predict(prompt)
        else:
            raise AttributeError("LLM object has neither 'invoke' nor 'predict' method")

    def _common_chat_kwargs(self, provider) -> dict[str, Any]:
        kwargs = {
            "model": provider.model,
            "temperature": provider.temperature,
            "max_tokens": provider.max_tokens,
            **self._extra_config(provider),
        }
        api_key = LLMService._provider_api_key(provider)
        if api_key:
            kwargs["api_key"] = api_key
        return kwargs

    @staticmethod
    def _provider_api_key(provider) -> str:
        if hasattr(provider, "get_api_key"):
            return provider.get_api_key()
        return getattr(provider, "api_key", "") or ""

    @staticmethod
    def _extra_config(provider) -> dict[str, Any]:
        extra_config = getattr(provider, "extra_config", None) or {}
        return extra_config if isinstance(extra_config, dict) else {}

    @staticmethod
    def _normalize_messages(
        messages: str | list[dict[str, str]] | list[tuple[str, str]],
    ) -> str | list[tuple[str, str]]:
        if isinstance(messages, str):
            return messages

        normalized = []
        for message in messages:
            if isinstance(message, tuple):
                role, content = message
            else:
                role = message.get("role", "user")
                content = message.get("content", "")
            normalized.append((role, content))
        return normalized

    @staticmethod
    def _run_config(
        *, run_name: str | None, metadata: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        config: dict[str, Any] = {}
        if run_name:
            config["run_name"] = run_name
        if metadata:
            config["metadata"] = metadata
        return config or None

    @staticmethod
    def _coerce_text(response) -> str:
        content = getattr(response, "content", response)
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                else:
                    parts.append(str(getattr(item, "text", item)))
            return "".join(parts)
        return str(content)

    @staticmethod
    def _active_provider_or_none(provider):
        if provider is not None and getattr(provider, "is_active", False):
            return provider
        return None

    @staticmethod
    def _with_structured_output(model, provider, schema):
        mode = getattr(provider, "structured_output_mode", "auto")
        method = None
        if mode == "provider_native":
            method = "json_schema"
        elif mode == "tool_calling":
            method = "function_calling"

        if method is None:
            return model.with_structured_output(schema)

        try:
            return model.with_structured_output(schema, method=method)
        except TypeError:
            logger.warning(
                "Structured output mode %s is not supported by this provider wrapper; using auto.",
                mode,
            )
            return model.with_structured_output(schema)


class SummaryGenerationService:
    """Service for generating summaries using an LLM and a prompt profile."""

    MAX_RETRIES = 3

    def generate_summary(self, case_data, prompt, query):
        try:
            return self._generate_with_retry(case_data, prompt, query)
        except Exception as e:
            logger.error(f"Failed to generate summary: {e}")
            return f"Error generating summary: {e}"

    def _generate_with_retry(self, case_data, prompt, query, retry_count=0):
        try:
            llm_service = LLMService()
            rendered_prompt = self._render_prompt(prompt.prompt, case_data, query)
            return llm_service.invoke_text(
                None,
                rendered_prompt,
                run_name="caseworker-summary",
                metadata={"feature": "caseworker_summary"},
            )
        except Exception:
            if retry_count < self.MAX_RETRIES:
                return self._generate_with_retry(
                    case_data, prompt, query, retry_count + 1
                )
            raise

    def _render_prompt(self, template, case_data, query):
        try:
            return template.format(
                case_data=str(case_data) if case_data else "",
                query=query or "",
            )
        except KeyError:
            return template

    def validate_prompt(self, prompt):
        if not prompt.prompt:
            return False, "Prompt is empty"
        return True, "Prompt is valid"

    def validate_skill_prompt(self, prompt):
        """Backward-compatible alias for older callers."""
        return self.validate_prompt(prompt)
