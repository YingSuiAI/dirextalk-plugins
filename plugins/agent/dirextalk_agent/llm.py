from __future__ import annotations

import os
from typing import Any, Protocol

from dirextalk_plugins_runtime import AgentPluginSettings, DirextalkClient, ModelProvider


class ModelInvocationUnavailable(RuntimeError):
    """Raised when model execution cannot start because runtime or credentials are absent."""


class AgentRuntime(Protocol):
    async def chat(self, prompt: str, params: dict[str, Any]) -> dict[str, Any]:
        ...


class PydanticAgentRuntime:
    def __init__(self, settings: AgentPluginSettings, client: DirextalkClient) -> None:
        self.settings = settings
        self.client = client

    async def chat(self, prompt: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            from pydantic_ai import Agent, RunContext
        except ModuleNotFoundError as exc:
            raise ModelInvocationUnavailable("pydantic-ai is not installed") from exc

        model_name = pydantic_model_name(self.settings)
        prepare_provider_environment(self.settings)
        agent = Agent(model_name, system_prompt=self.settings.system_prompt)

        if "search_rooms" in self.settings.enabled_tools:
            @agent.tool
            async def search_rooms(
                ctx: RunContext[None],
                query: str = "",
                room_type: str = "all",
                limit: int = 20,
            ) -> dict[str, Any]:
                return await self.client.search_rooms(query=query, room_type=room_type, limit=limit)

        if "list_messages" in self.settings.enabled_tools:
            @agent.tool
            async def list_messages(ctx: RunContext[None], room_id: str, limit: int = 50) -> dict[str, Any]:
                return await self.client.list_messages(room_id=room_id, limit=limit)

        if "send_message" in self.settings.enabled_tools:
            @agent.tool
            async def send_message(ctx: RunContext[None], room_id: str, msg: str) -> dict[str, Any]:
                return await self.client.send_message(room_id=room_id, msg=msg)

        result = await agent.run(prompt)
        return {
            "ok": True,
            "model_ready": True,
            "provider": self.settings.model.provider,
            "model": self.settings.model.model,
            "text": extract_agent_text(result),
        }


def pydantic_model_name(settings: AgentPluginSettings) -> str:
    model = settings.model.model.strip()
    if ":" in model:
        return model
    provider = settings.model.provider
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


def prepare_provider_environment(settings: AgentPluginSettings) -> None:
    api_key = secret_value(settings.model.api_key_ref)
    provider = settings.model.provider
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
        os.environ["OPENAI_BASE_URL"] = settings.model.base_url or "https://openrouter.ai/api/v1"
    elif provider in {ModelProvider.openai_compatible, ModelProvider.litellm} and settings.model.base_url and not os.getenv("OPENAI_BASE_URL"):
        os.environ["OPENAI_BASE_URL"] = settings.model.base_url


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
