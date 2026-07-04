from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from dirextalk_plugins_runtime import AgentPluginSettings, DirextalkClient, MCPServerConfig, ModelProvider, ModelSettings, SkillSource


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
        self._skill_instruction_cache: dict[str, str] = {}

    async def chat(self, prompt: str, params: dict[str, Any]) -> dict[str, Any]:
        model_settings = resolve_model_settings(self.settings, params)
        ensure_model_ready(model_settings)
        try:
            from pydantic_ai import Agent
        except ModuleNotFoundError as exc:
            raise ModelInvocationUnavailable("pydantic-ai is not installed") from exc

        prepare_provider_environment(model_settings)
        agent = self._create_agent(Agent, model_settings)

        result = await agent.run(prompt_with_attachments(prompt, params))
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
            async with agent.run_stream(prompt_with_attachments(prompt, params)) as result:
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
        agent = agent_type(
            pydantic_model_name(model_settings),
            system_prompt=self._system_prompt(),
            toolsets=build_mcp_toolsets(self.settings),
        )

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

        if "summarize_conversation" in self.settings.enabled_tools:

            @agent.tool_plain
            async def summarize_conversation(room_id: str, limit: int = 100) -> dict[str, Any]:
                messages = await self.client.list_messages(room_id=room_id, limit=limit)
                return {"room_id": room_id, "summary": summarize_messages(messages)}

        @agent.tool_plain
        async def list_installed_skills() -> dict[str, Any]:
            return {"skills": [skill.model_dump(mode="json") for skill in self.settings.skills]}

        @agent.tool_plain
        async def list_mcp_servers() -> dict[str, Any]:
            return {
                "servers": [
                    builtin_mcp_summary(),
                    *[server.model_dump(mode="json") for server in self.settings.mcp_servers],
                ]
            }

        return agent

    def _system_prompt(self) -> str:
        parts = [self.settings.system_prompt.strip() or "You are the local Dirextalk assistant."]
        skills_prompt = skills_system_prompt(self.settings.skills, self._skill_instruction_cache)
        if skills_prompt:
            parts.append(skills_prompt)
        mcp_prompt = mcp_system_prompt(self.settings.mcp_servers)
        if mcp_prompt:
            parts.append(mcp_prompt)
        parts.append(
            "Dirextalk built-in tools can search contacts and rooms, list messages, send messages, and summarize conversations when enabled."
        )
        return "\n\n".join(parts)


def skills_system_prompt(skills: list[SkillSource], cache: dict[str, str] | None = None) -> str:
    enabled = [skill for skill in skills if skill.enabled]
    if not enabled:
        return ""
    cache = cache if cache is not None else {}
    sections = ["Enabled Agent Skills. Follow these skill instructions when relevant:"]
    for skill in enabled:
        key = skill_cache_key(skill)
        text = cache.get(key)
        if text is None:
            text = fetch_skill_instruction(skill)
            cache[key] = text
        sections.append(f"### {skill.path}\nSource: {skill.repo_url}#{skill.ref}\n{text}")
    return "\n\n".join(sections)


def fetch_skill_instruction(skill: SkillSource) -> str:
    raw_url = github_skill_raw_url(skill)
    if not raw_url:
        return "Skill source is configured, but this version can only auto-load public GitHub skill repositories."
    try:
        response = httpx.get(raw_url, timeout=8.0, follow_redirects=True)
        response.raise_for_status()
    except Exception as exc:
        return f"Skill source is configured but could not be loaded: {exc}"
    text = response.text.strip()
    if not text:
        return "Skill source loaded but SKILL.md was empty."
    return text[:12000]


def github_skill_raw_url(skill: SkillSource) -> str:
    parsed = urlparse(str(skill.repo_url))
    if parsed.netloc.lower() != "github.com":
        return ""
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return ""
    owner, repo = parts[0], parts[1]
    path = skill.path.strip("/")
    if not path.lower().endswith("skill.md"):
        path = f"{path}/SKILL.md" if path else "SKILL.md"
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{skill.ref.strip()}/{path}"


def skill_cache_key(skill: SkillSource) -> str:
    return f"{skill.repo_url}|{skill.ref}|{skill.path}"


def build_mcp_toolsets(settings: AgentPluginSettings) -> list[Any]:
    enabled = [server for server in settings.mcp_servers if server.enabled]
    if not enabled:
        return []
    try:
        from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport
        from pydantic_ai.mcp import MCPToolset
    except ModuleNotFoundError:
        return []

    toolsets: list[Any] = []
    for server in enabled:
        try:
            transport: Any
            if server.transport == "stdio":
                if not server.command:
                    continue
                transport = StdioTransport(command=server.command[0], args=server.command[1:])
            elif server.transport == "sse":
                if not server.url:
                    continue
                transport = SSETransport(server.url)
            else:
                if not server.url:
                    continue
                transport = StreamableHttpTransport(server.url)
            toolset = MCPToolset(transport, id=mcp_server_id(server), include_instructions=True)
            if server.tool_allowlist:
                allowed = {tool.strip() for tool in server.tool_allowlist if tool.strip()}

                def allow_tool(_ctx: Any, tool_def: Any, *, allowed: set[str] = allowed) -> bool:
                    return str(getattr(tool_def, "name", "")).strip() in allowed

                toolset = toolset.filtered(allow_tool)
            toolsets.append(toolset.prefixed(mcp_server_id(server)))
        except Exception:
            continue
    return toolsets


def mcp_system_prompt(servers: list[MCPServerConfig]) -> str:
    enabled = [server for server in servers if server.enabled]
    if not enabled:
        return ""
    lines = [
        "Enabled third-party MCP servers are available as tools. Use them when they match the user's request:",
    ]
    for server in enabled:
        prefix = mcp_server_id(server)
        lines.append(f"- {server.name}: tool names are prefixed with `{prefix}`.")
    return "\n".join(lines)


def mcp_server_id(server: MCPServerConfig) -> str:
    raw = server.name.strip() or "mcp"
    value = re.sub(r"[^a-zA-Z0-9_]+", "_", raw).strip("_").lower()
    return value or "mcp"


def prompt_with_attachments(prompt: str, params: dict[str, Any]) -> str:
    attachments = params.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        return prompt
    lines = [prompt.rstrip(), "", "User attachments:"]
    for index, item in enumerate(attachments, start=1):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or f"attachment-{index}").strip()
        mime_type = str(item.get("mime_type") or item.get("mimeType") or "").strip()
        size = str(item.get("size") or "").strip()
        text = str(item.get("text") or item.get("content") or "").strip()
        metadata = ", ".join(part for part in [mime_type, f"{size} bytes" if size else ""] if part)
        lines.append(f"- {name}{f' ({metadata})' if metadata else ''}")
        if text:
            lines.append("```")
            lines.append(text[:20000])
            lines.append("```")
    return "\n".join(lines).strip()


def summarize_messages(messages: dict[str, Any]) -> str:
    rows = messages.get("messages") or []
    if not rows:
        return "No recent messages."
    bodies: list[str] = []
    for item in rows[-10:]:
        if not isinstance(item, dict):
            continue
        sender = item.get("sender_display_name") or item.get("sender_mxid") or "unknown"
        body = item.get("msg") or item.get("body") or ""
        if body:
            bodies.append(f"{sender}: {body}")
    if not bodies:
        return "No recent text messages."
    return "Recent discussion:\n" + "\n".join(bodies)


def builtin_mcp_summary() -> dict[str, Any]:
    return {
        "name": "Dirextalk Built-in MCP",
        "transport": "builtin",
        "enabled": True,
        "locked": True,
        "tools": [
            "contacts.list",
            "contacts.search",
            "rooms.search",
            "messages.list",
            "messages.send",
            "room_members.list",
        ],
    }


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
