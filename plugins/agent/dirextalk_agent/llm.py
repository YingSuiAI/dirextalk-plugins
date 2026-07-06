from __future__ import annotations

import os
import re
import asyncio
from collections.abc import AsyncIterator
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from dirextalk_plugins_runtime import (
    AgentPluginSettings,
    DirextalkActionError,
    DirextalkClient,
    MCPServerConfig,
    ModelProvider,
    ModelSettings,
    SkillSource,
)

from .runtime_config import (
    install_mcp_server_setting,
    install_skill_setting,
    uninstall_mcp_server_setting,
    uninstall_skill_setting,
)
from .runtime_tools import (
    install_runtime_tool as install_runtime_tool_action,
    list_runtime_tool_records as list_runtime_tool_records_action,
    run_runtime_tool as run_runtime_tool_action,
    runtime_tools_status as runtime_tools_status_action,
    uninstall_runtime_tool as uninstall_runtime_tool_action,
    which_runtime_tool as which_runtime_tool_action,
)


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
        if hasattr(agent, "run_stream_events"):
            text = ""
            final_text = ""
            async with agent.run_stream_events(prompt_with_attachments(prompt, params)) as events:
                async for event in events:
                    chunk = agent_stream_event_text_delta(event)
                    if chunk:
                        text += chunk
                        yield {"event": "delta", "data": {"text": chunk}}
                    result_text = agent_run_result_event_text(event)
                    if result_text is not None:
                        final_text = result_text
            if final_text:
                if not text:
                    yield {"event": "delta", "data": {"text": final_text}}
                text = final_text
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
            model_settings=pydantic_model_settings(model_settings),
        )

        if "search_contacts" in self.settings.enabled_tools:

            @agent.tool_plain
            async def search_contacts(
                query: str = "",
                limit: int = 20,
            ) -> dict[str, Any]:
                return await safe_tool_result("mcp.contacts.search", self.client.list_contacts(query=query, limit=limit))

        if "search_rooms" in self.settings.enabled_tools:

            @agent.tool_plain
            async def search_rooms(
                query: str = "",
                room_type: str = "all",
                limit: int = 20,
            ) -> dict[str, Any]:
                return await safe_tool_result(
                    "mcp.rooms.search",
                    self.client.search_rooms(query=query, room_type=room_type, limit=limit),
                )

        if "list_messages" in self.settings.enabled_tools:

            @agent.tool_plain
            async def list_messages(room_id: str, limit: int = 50) -> dict[str, Any]:
                return await safe_tool_result("mcp.messages.list", self.client.list_messages(room_id=room_id, limit=limit))

        if "send_message" in self.settings.enabled_tools:

            @agent.tool_plain
            async def send_message(room_id: str, msg: str) -> dict[str, Any]:
                return await safe_tool_result("mcp.messages.send", self.client.send_message(room_id=room_id, msg=msg))

        if "summarize_conversation" in self.settings.enabled_tools:

            @agent.tool_plain
            async def summarize_conversation(room_id: str, limit: int = 100) -> dict[str, Any]:
                messages = await safe_tool_result("mcp.messages.list", self.client.list_messages(room_id=room_id, limit=limit))
                if messages.get("ok") is False:
                    return messages
                return {"room_id": room_id, "summary": summarize_messages(messages)}

        @agent.tool_plain
        async def list_installed_skills() -> dict[str, Any]:
            return {"skills": [skill.model_dump(mode="json") for skill in self.settings.skills]}

        @agent.tool_plain
        async def install_skill(
            repo_url: str,
            path: str,
            ref: str = "main",
            enabled: bool = True,
            install_runtime_target: str = "",
            runtime_manager: str = "",
            runtime_package: str = "",
            runtime_command: str = "",
            runtime_repo_url: str = "",
            runtime_install_command: str = "",
            channels: list[str] | None = None,
        ) -> dict[str, Any]:
            result = install_skill_setting(
                self.settings,
                {
                    "repo_url": repo_url,
                    "path": path,
                    "ref": ref,
                    "enabled": enabled,
                },
            )
            self._skill_instruction_cache.clear()
            runtime_params: dict[str, Any] = {}
            has_runtime_request = any(
                value.strip()
                for value in [
                    runtime_manager,
                    runtime_package,
                    runtime_command,
                    runtime_repo_url,
                    runtime_install_command,
                ]
            )
            if has_runtime_request:
                runtime_params = {
                    "target": install_runtime_target.strip(),
                    "manager": runtime_manager.strip(),
                    "package": runtime_package.strip(),
                    "command": runtime_command.strip(),
                    "repo_url": runtime_repo_url.strip(),
                    "install_command": runtime_install_command.strip(),
                    "channels": channels or [],
                }
            elif install_runtime_target.strip():
                runtime_params = {"target": install_runtime_target.strip(), "channels": channels or []}
            elif "agent-reach" in repo_url.lower() or path == "agent-reach":
                runtime_params = {"target": "agent-reach-core", "channels": channels or []}
            if runtime_params:
                result["runtime_install"] = await safe_tool_result(
                    "agent.runtime.install",
                    install_runtime_tool_action(runtime_params),
                )
            return result

        @agent.tool_plain
        async def uninstall_skill(key: str = "", repo_url: str = "", path: str = "") -> dict[str, Any]:
            self._skill_instruction_cache.clear()
            return uninstall_skill_setting(self.settings, {"key": key or path, "repo_url": repo_url})

        @agent.tool_plain
        async def list_mcp_servers() -> dict[str, Any]:
            return {"servers": await mcp_servers_with_runtime_status(self.settings.mcp_servers)}

        @agent.tool_plain
        async def install_mcp_server(
            name: str,
            transport: str = "stdio",
            command: list[str] | None = None,
            url: str = "",
            enabled: bool = True,
            timeout_ms: int = 30000,
        ) -> dict[str, Any]:
            return install_mcp_server_setting(
                self.settings,
                {
                    "mcp_server": {
                        "name": name,
                        "transport": transport,
                        "command": command or [],
                        "url": url,
                        "enabled": enabled,
                        "timeout_ms": timeout_ms,
                    }
                },
            )

        @agent.tool_plain
        async def uninstall_mcp_server(name: str) -> dict[str, Any]:
            return uninstall_mcp_server_setting(self.settings, {"name": name})

        @agent.tool_plain
        async def runtime_tools_status() -> dict[str, Any]:
            return runtime_tools_status_action()

        @agent.tool_plain
        async def install_runtime_tool(
            target: str = "",
            manager: str = "",
            package: str = "",
            command: str = "",
            repo_url: str = "",
            install_command: str = "",
            channels: list[str] | None = None,
        ) -> dict[str, Any]:
            return await safe_tool_result(
                "agent.runtime.install",
                install_runtime_tool_action(
                    {
                        "target": target,
                        "manager": manager,
                        "package": package,
                        "command": command,
                        "repo_url": repo_url,
                        "install_command": install_command,
                        "channels": channels or [],
                    }
                ),
            )

        @agent.tool_plain
        async def uninstall_runtime_tool(
            id: str = "",
            target: str = "",
            manager: str = "",
            package: str = "",
            command: str = "",
            repo_url: str = "",
            uninstall_command: str = "",
            channels: list[str] | None = None,
        ) -> dict[str, Any]:
            return await safe_tool_result(
                "agent.runtime.uninstall",
                uninstall_runtime_tool_action(
                    {
                        "id": id,
                        "target": target,
                        "manager": manager,
                        "package": package,
                        "command": command,
                        "repo_url": repo_url,
                        "uninstall_command": uninstall_command,
                        "channels": channels or [],
                    }
                ),
            )

        @agent.tool_plain
        async def run_runtime_tool(command: str, timeout_seconds: int = 60) -> dict[str, Any]:
            return await safe_tool_result(
                "agent.runtime.run",
                run_runtime_tool_action({"command": command, "timeout_seconds": timeout_seconds}),
            )

        @agent.tool_plain
        async def which_runtime_tool(command: str) -> dict[str, Any]:
            return which_runtime_tool_action(command)

        @agent.tool_plain
        async def list_runtime_tools() -> dict[str, Any]:
            return {"tools": list_runtime_tool_records_action(), "status": runtime_tools_status_action()}

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
            "Dirextalk built-in tools can search contacts and rooms, list messages, send messages, and summarize conversations when enabled. "
            "Runtime tools can inspect, install, verify, and run CLI capabilities inside the Agent container. "
            "When a command is missing, call runtime_tools_status first, then install_runtime_tool with a general manager: "
            "manager=uv for Python CLI tools via `uv tool install`, manager=npm for Node CLIs via `npm install -g`, "
            "manager=pip for Python packages, manager=apt for OS packages, manager=git for repositories, or manager=command for a custom install command. "
            "After installing, call which_runtime_tool or run_runtime_tool with a harmless verification command such as `<tool> --help`. "
            "Use uninstall_runtime_tool with the installed record id or manager/package when the user asks to remove a CLI or runtime tool. "
            "Use run_runtime_tool for user-approved command execution and list_runtime_tools to inspect installed records. "
            "When installing a skill that needs a CLI, pass runtime_manager/runtime_package/runtime_command to install_skill so the skill and tool are configured together. "
            "You may install or uninstall Agent skills and MCP servers with the built-in configuration tools when the user asks."
        )
        return "\n\n".join(parts)


async def safe_tool_result(action: str, awaitable: Any) -> dict[str, Any]:
    try:
        result = await awaitable
        if isinstance(result, dict):
            return result
        return {"ok": True, "result": result}
    except DirextalkActionError as exc:
        return {
            "ok": False,
            "action": exc.action or action,
            "status": exc.status_code,
            "error": exc.error,
        }
    except httpx.HTTPError as exc:
        return {"ok": False, "action": action, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "action": action, "error": str(exc)}


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
    raw_urls = github_skill_raw_url_candidates(skill)
    if not raw_urls:
        return "Skill source is configured, but this version can only auto-load public GitHub skill repositories."
    seen: set[str] = set()
    last_error: Exception | None = None
    for raw_url in raw_urls:
        if raw_url in seen:
            continue
        seen.add(raw_url)
        try:
            return fetch_skill_raw_text(raw_url)
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    discovered_url = discover_github_skill_raw_url(skill)
    if discovered_url and discovered_url not in seen:
        try:
            return fetch_skill_raw_text(discovered_url)
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    if last_error is None:
        return "Skill source is configured but no SKILL.md file could be found."
    return f"Skill source is configured but could not be loaded: {last_error}"


def fetch_skill_raw_text(raw_url: str) -> str:
    try:
        response = httpx.get(raw_url, timeout=8.0, follow_redirects=True)
        response.raise_for_status()
    except Exception as exc:
        raise exc
    text = response.text.strip()
    if not text:
        return "Skill source loaded but SKILL.md was empty."
    return text[:12000]


def github_skill_raw_url(skill: SkillSource) -> str:
    candidates = github_skill_raw_url_candidates(skill)
    return candidates[0] if candidates else ""


def github_skill_raw_url_candidates(skill: SkillSource) -> list[str]:
    parsed = urlparse(str(skill.repo_url))
    if parsed.netloc.lower() != "github.com":
        return []
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return []
    owner, repo = parts[0], parts[1]
    path = skill.path.strip("/")
    candidate_paths: list[str]
    if not path:
        candidate_paths = ["SKILL.md"]
    elif path.lower().endswith("skill.md"):
        candidate_paths = [path]
    else:
        candidate_paths = [
            f"{path}/SKILL.md",
            f"skills/{path}/SKILL.md",
            f".claude/skills/{path}/SKILL.md",
            f".codex/skills/{path}/SKILL.md",
            f"{path}/skill/SKILL.md",
        ]
    ref = skill.ref.strip()
    urls: list[str] = []
    seen_paths: set[str] = set()
    for candidate_path in candidate_paths:
        candidate_path = candidate_path.strip("/")
        if not candidate_path or candidate_path in seen_paths:
            continue
        seen_paths.add(candidate_path)
        urls.append(f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{candidate_path}")
    return urls


def discover_github_skill_raw_url(skill: SkillSource) -> str:
    parsed = urlparse(str(skill.repo_url))
    if parsed.netloc.lower() != "github.com":
        return ""
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return ""
    owner, repo = parts[0], parts[1]
    tree_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{skill.ref.strip()}?recursive=1"
    response = httpx.get(tree_url, timeout=8.0, follow_redirects=True)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        return ""
    raw_tree = data.get("tree")
    if not isinstance(raw_tree, list):
        return ""
    skill_paths = [
        str(item.get("path") or "").strip()
        for item in raw_tree
        if isinstance(item, dict)
        and str(item.get("type") or "") == "blob"
        and str(item.get("path") or "").lower().endswith("skill.md")
    ]
    if not skill_paths:
        return ""
    selected = select_skill_path(skill.path, skill_paths)
    if not selected:
        return ""
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{skill.ref.strip()}/{selected}"


def select_skill_path(configured_path: str, skill_paths: list[str]) -> str:
    if len(skill_paths) == 1:
        return skill_paths[0]
    target = configured_path.strip().strip("/")
    if target.lower().endswith("skill.md"):
        target = target.rsplit("/", 1)[0] if "/" in target else ""
    target_token = skill_path_token(target.rsplit("/", 1)[-1] if target else "")
    if not target_token:
        return ""
    best_path = ""
    best_score = 0
    for path in skill_paths:
        parts = [part for part in path.strip("/").split("/") if part]
        path_tokens = {skill_path_token(part) for part in parts if part.lower() != "skill.md"}
        score = 0
        if target_token in path_tokens:
            score = 100
        elif any(target_token in token or token in target_token for token in path_tokens if token):
            score = 50
        if score > best_score:
            best_score = score
            best_path = path
    return best_path if best_score > 0 else ""


def skill_path_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


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
            transport = mcp_transport_for_server(server, StdioTransport, SSETransport, StreamableHttpTransport)
            if transport is None:
                continue
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


def mcp_transport_for_server(
    server: MCPServerConfig,
    stdio_transport: Any,
    sse_transport: Any,
    streamable_http_transport: Any,
) -> Any | None:
    if server.transport == "stdio":
        if not server.command:
            return None
        return stdio_transport(command=server.command[0], args=server.command[1:])
    if server.transport == "sse":
        if not server.url:
            return None
        return sse_transport(server.url)
    if not server.url:
        return None
    return streamable_http_transport(server.url)


async def mcp_servers_with_runtime_status(servers: list[MCPServerConfig]) -> list[dict[str, Any]]:
    statuses = await mcp_servers_runtime_status(servers)
    result = [builtin_mcp_summary()]
    for server in servers:
        summary = server.model_dump(mode="json")
        summary.update(statuses.get(mcp_server_id(server), {}))
        result.append(summary)
    return result


async def mcp_servers_runtime_status(servers: list[MCPServerConfig]) -> dict[str, dict[str, Any]]:
    return {mcp_server_id(server): await mcp_server_runtime_status(server) for server in servers}


async def mcp_server_runtime_status(server: MCPServerConfig) -> dict[str, Any]:
    if not server.enabled:
        return {"runtime_status": "disabled", "tool_count": 0, "tools": []}
    try:
        from fastmcp import Client
        from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport

        transport = mcp_transport_for_server(server, StdioTransport, SSETransport, StreamableHttpTransport)
        if transport is None:
            return {"runtime_status": "invalid", "tool_count": 0, "tools": [], "error": "missing MCP transport target"}

        async def list_tools() -> list[Any]:
            async with Client(transport) as client:
                return await client.list_tools()

        timeout = max(1.0, min(float(server.timeout_ms or 30000) / 1000.0, 20.0))
        tools = await asyncio.wait_for(list_tools(), timeout=timeout)
        normalized_tools = [
            {
                "name": str(getattr(tool, "name", "")).strip(),
                "description": str(getattr(tool, "description", "") or "").strip(),
            }
            for tool in tools
        ]
        normalized_tools = [tool for tool in normalized_tools if tool["name"]]
        return {
            "runtime_status": "ready",
            "tool_count": len(normalized_tools),
            "tools": normalized_tools,
        }
    except ModuleNotFoundError as exc:
        return {"runtime_status": "unavailable", "tool_count": 0, "tools": [], "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"runtime_status": "error", "tool_count": 0, "tools": [], "error": str(exc)}


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
    lines = conversation_context_lines(params)
    if lines:
        lines.extend(["", "Current user message:", prompt.rstrip()])
        prompt = "\n".join(lines).strip()
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


def conversation_context_lines(params: dict[str, Any]) -> list[str]:
    context = params.get("conversation_context")
    if not isinstance(context, dict):
        return []
    summary = str(context.get("summary") or "").strip()
    raw_messages = context.get("messages")
    messages = raw_messages if isinstance(raw_messages, list) else []
    lines: list[str] = []
    if summary:
        lines.extend(["Compressed conversation memory:", summary])
    recent: list[str] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user").strip() or "user"
        text = str(item.get("text") or item.get("content") or "").strip()
        if text:
            recent.append(f"{role}: {text[:4000]}")
    if recent:
        if lines:
            lines.append("")
        lines.append("Recent conversation messages:")
        lines.extend(recent)
    return lines


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


def _request_api_key(params: dict[str, Any]) -> str:
    return str(params.get("api_key") or "").strip()


def resolve_model_settings(settings: AgentPluginSettings, params: dict[str, Any]) -> ModelSettings:
    raw_profile = params.get("model_profile")
    if isinstance(raw_profile, dict):
        profile = dict(raw_profile)
        if not str(profile.get("api_key") or "").strip():
            api_key = _request_api_key(params)
            if api_key:
                profile["api_key"] = api_key
        return ModelSettings.model_validate(profile)
    profile_id = str(params.get("model_profile_id") or settings.default_model_profile_id or "").strip()
    if profile_id:
        for profile in settings.model_profiles:
            if profile.id == profile_id:
                profile_data = profile.model_dump()
                api_key = _request_api_key(params)
                if api_key and not str(profile_data.get("api_key") or "").strip():
                    profile_data["api_key"] = api_key
                return ModelSettings.model_validate(profile_data)
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


def pydantic_model_settings(settings: ModelSettings) -> dict[str, Any]:
    model_settings = {
        "temperature": settings.temperature,
        "max_tokens": settings.max_output_tokens,
    }
    if settings.top_p > 0:
        model_settings["top_p"] = settings.top_p
    if settings.top_k > 0:
        model_settings["top_k"] = settings.top_k
    return model_settings


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
                **model_metadata(item),
            }
        )
    return {"provider": provider, "base_url": base, "models": models}


def model_metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for target, aliases in {
        "context_length": [
            "context_length",
            "context_window",
            "max_context_tokens",
            "max_input_tokens",
            "input_token_limit",
        ],
        "max_output_tokens": [
            "max_output_tokens",
            "max_tokens",
            "max_completion_tokens",
            "output_token_limit",
        ],
    }.items():
        for alias in aliases:
            value = item.get(alias)
            if isinstance(value, int) and value > 0:
                metadata[target] = value
                break
            if isinstance(value, float) and value > 0:
                metadata[target] = int(value)
                break
            if isinstance(value, str) and value.strip().isdigit():
                metadata[target] = int(value.strip())
                break
    return metadata


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
    api_key = model_api_key(settings)
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
        if (settings.api_key or settings.api_key_ref) and not model_api_key(settings):
            raise ModelInvocationUnavailable("model API key reference is configured but empty")
        return
    if not settings.api_key and not settings.api_key_ref:
        raise ModelInvocationUnavailable("model API key is not configured")
    if not model_api_key(settings):
        raise ModelInvocationUnavailable("model API key reference is configured but empty")


def model_api_key(settings: ModelSettings) -> str:
    return settings.api_key.strip() or secret_value(settings.api_key_ref)


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


def agent_stream_event_text_delta(event: Any) -> str:
    event_kind = str(getattr(event, "event_kind", "") or "")
    if event_kind == "part_start":
        part = getattr(event, "part", None)
        if str(getattr(part, "part_kind", "") or "") == "text":
            return str(getattr(part, "content", "") or "")
    if event_kind == "part_delta":
        delta = getattr(event, "delta", None)
        if str(getattr(delta, "part_delta_kind", "") or "") == "text":
            return str(getattr(delta, "content_delta", "") or "")
    return ""


def agent_run_result_event_text(event: Any) -> str | None:
    if str(getattr(event, "event_kind", "") or "") != "agent_run_result":
        return None
    result = getattr(event, "result", None)
    if result is None:
        return ""
    return extract_agent_text(result)
