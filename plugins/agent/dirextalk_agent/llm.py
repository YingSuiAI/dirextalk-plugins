from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx

from dirextalk_plugins_runtime import AgentPluginSettings, DirextalkClient, ModelProvider, ModelSettings


class ModelInvocationUnavailable(RuntimeError):
    """Raised when model execution cannot start because runtime or credentials are absent."""


class AgentRuntime(Protocol):
    async def chat(self, prompt: str, params: dict[str, Any]) -> dict[str, Any]:
        ...

    async def stream_chat(self, prompt: str, params: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        ...


class PydanticAgentRuntime:
    def __init__(self, settings: AgentPluginSettings, client: DirextalkClient) -> None:
        self.settings = settings
        self.client = client

    async def chat(self, prompt: str, params: dict[str, Any]) -> dict[str, Any]:
        model_settings = resolve_model_settings(self.settings, params)
        ensure_model_ready(model_settings)
        try:
            from pydantic_ai import Agent
        except ModuleNotFoundError as exc:
            raise ModelInvocationUnavailable("pydantic-ai is not installed") from exc

        prepare_provider_environment(model_settings)
        agent = self._create_agent(Agent, model_settings)

        result = await agent.run(prompt)
        return {
            "ok": True,
            "model_ready": True,
            "provider": model_settings.provider,
            "model": model_settings.model,
            "text": extract_agent_text(result),
        }

    async def stream_chat(self, prompt: str, params: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        model_settings = resolve_model_settings(self.settings, params)
        ensure_model_ready(model_settings)
        try:
            from pydantic_ai import Agent
        except ModuleNotFoundError as exc:
            raise ModelInvocationUnavailable("pydantic-ai is not installed") from exc

        prepare_provider_environment(model_settings)
        agent = self._create_agent(Agent, model_settings)
        if hasattr(agent, "run_stream"):
            text = ""
            async with agent.run_stream(prompt) as result:
                stream_text = getattr(result, "stream_text", None)
                if stream_text is not None:
                    async for delta in stream_text(delta=True):
                        chunk = delta if isinstance(delta, str) else str(delta)
                        if chunk:
                            text += chunk
                            yield {"event": "delta", "data": {"text": chunk}}
                else:
                    text = extract_agent_text(result)
                    if text:
                        yield {"event": "delta", "data": {"text": text}}
            yield {
                "event": "done",
                "data": {
                    "text": text,
                    "provider": model_settings.provider,
                    "model": model_settings.model,
                },
            }
            return

        result = await self.chat(prompt, params)
        text = str(result.get("text") or "")
        if text:
            yield {"event": "delta", "data": {"text": text}}
        yield {"event": "done", "data": result}

    def _create_agent(self, agent_type: Any, model_settings: ModelSettings) -> Any:
        agent = agent_type(pydantic_model_name(model_settings), system_prompt=self.settings.system_prompt)

        if "search_contacts" in self.settings.enabled_tools:

            @agent.tool_plain
            async def search_contacts(
                query: str = "",
                limit: int = 20,
            ) -> dict[str, Any]:
                return await self.client.list_contacts(query=query, limit=limit)

        if "search_rooms" in self.settings.enabled_tools:

            @agent.tool_plain
            async def search_rooms(
                query: str = "",
                room_type: str = "all",
                limit: int = 20,
            ) -> dict[str, Any]:
                return await self.client.search_rooms(query=query, room_type=room_type, limit=limit)

        if "list_messages" in self.settings.enabled_tools:

            @agent.tool_plain
            async def list_messages(room_id: str, limit: int = 50) -> dict[str, Any]:
                return await self.client.list_messages(room_id=room_id, limit=limit)

        if "send_message" in self.settings.enabled_tools:

            @agent.tool_plain
            async def send_message(room_id: str, msg: str) -> dict[str, Any]:
                return await self.client.send_message(room_id=room_id, msg=msg)

        return agent


def resolve_model_settings(settings: AgentPluginSettings, params: dict[str, Any]) -> ModelSettings:
    profile_id = str(params.get("model_profile_id") or settings.default_model_profile_id or "").strip()
    if profile_id:
        for profile in settings.model_profiles:
            if profile.id == profile_id:
                return ModelSettings.model_validate(profile.model_dump())
        raise ModelInvocationUnavailable(f"unknown model profile {profile_id}")
    return settings.model


def pydantic_model_name(settings: AgentPluginSettings | ModelSettings) -> str:
    model_settings = settings.model if isinstance(settings, AgentPluginSettings) else settings
    model = model_settings.model.strip()
    if ":" in model:
        return model
    provider = model_settings.provider
    prefixes = {
        ModelProvider.openai: "openai",
        ModelProvider.anthropic: "anthropic",
        ModelProvider.deepseek: "deepseek",
        ModelProvider.gemini: "google-gla",
        ModelProvider.vertex: "google-vertex",
        ModelProvider.openai_compatible: "openai-chat",
        ModelProvider.openrouter: "openai-chat",
        ModelProvider.litellm: "openai-chat",
    }
    return f"{prefixes[provider]}:{model}"


async def list_provider_models(
    *,
    provider: str,
    base_url: str = "",
    api_key: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    provider = provider.strip()
    base = provider_models_base_url(provider, base_url)
    headers = {"Accept": "application/json"}
    params: dict[str, str] = {}
    if provider == ModelProvider.gemini:
        if api_key:
            params["key"] = api_key
    elif provider == ModelProvider.anthropic:
        if api_key:
            headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
        response = await client.get(f"{base}/models", headers=headers, params=params)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("model list response must be an object")
    raw_models = data.get("data") or data.get("models") or []
    models = []
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or item.get("name") or "").strip()
        if not model_id:
            continue
        models.append(
            {
                "id": model_id,
                "name": str(item.get("display_name") or item.get("name") or model_id).strip(),
                "provider": provider,
                **({"owned_by": item["owned_by"]} if item.get("owned_by") else {}),
            }
        )
    return {"provider": provider, "base_url": base, "models": models}


def provider_models_base_url(provider: str, base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base:
        return base
    defaults = {
        ModelProvider.openai: "https://api.openai.com/v1",
        ModelProvider.deepseek: "https://api.deepseek.com",
        ModelProvider.openrouter: "https://openrouter.ai/api/v1",
        ModelProvider.anthropic: "https://api.anthropic.com/v1",
        ModelProvider.gemini: "https://generativelanguage.googleapis.com/v1beta",
    }
    return defaults.get(provider, "https://api.openai.com/v1")


def prepare_provider_environment(settings: ModelSettings) -> None:
    api_key = secret_value(settings.api_key_ref)
    provider = settings.provider
    env_name = {
        ModelProvider.openai: "OPENAI_API_KEY",
        ModelProvider.anthropic: "ANTHROPIC_API_KEY",
        ModelProvider.deepseek: "DEEPSEEK_API_KEY",
        ModelProvider.gemini: "GEMINI_API_KEY",
        ModelProvider.vertex: "GOOGLE_APPLICATION_CREDENTIALS",
        ModelProvider.openai_compatible: "OPENAI_API_KEY",
        ModelProvider.openrouter: "OPENAI_API_KEY",
        ModelProvider.litellm: "OPENAI_API_KEY",
    }.get(provider)
    if api_key and env_name and not os.getenv(env_name):
        os.environ[env_name] = api_key

    if provider == ModelProvider.openrouter and not os.getenv("OPENAI_BASE_URL"):
        os.environ["OPENAI_BASE_URL"] = settings.base_url or "https://openrouter.ai/api/v1"
    elif provider in {ModelProvider.openai_compatible, ModelProvider.litellm} and settings.base_url and not os.getenv("OPENAI_BASE_URL"):
        os.environ["OPENAI_BASE_URL"] = settings.base_url


def ensure_model_ready(settings: ModelSettings) -> None:
    if settings.provider == ModelProvider.vertex:
        if settings.api_key_ref and not secret_value(settings.api_key_ref):
            raise ModelInvocationUnavailable("model API key reference is configured but empty")
        return
    if not settings.api_key_ref:
        raise ModelInvocationUnavailable("model API key is not configured")
    if not secret_value(settings.api_key_ref):
        raise ModelInvocationUnavailable("model API key reference is configured but empty")


def secret_value(ref: str) -> str:
    if ref.startswith("env:"):
        return os.getenv(ref.removeprefix("env:"), "")
    return ""


def extract_agent_text(result: Any) -> str:
    for attr in ("output", "data"):
        if hasattr(result, attr):
            value = getattr(result, attr)
            return value if isinstance(value, str) else str(value)
    return str(result)
