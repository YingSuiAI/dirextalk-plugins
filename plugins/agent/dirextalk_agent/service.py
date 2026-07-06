from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from dirextalk_plugins_runtime import AgentPluginSettings, DirextalkClient

from .llm import (
    AgentRuntime,
    ModelInvocationUnavailable,
    PydanticAgentRuntime,
    list_provider_models,
    mcp_servers_runtime_status,
    mcp_server_id,
    resolve_model_settings,
    secret_value,
)
from .registry import search_mcp_servers, search_skills
from .runtime_config import (
    apply_runtime_config_overlay,
    install_mcp_server_setting,
    install_skill_setting,
    uninstall_mcp_server_setting,
    uninstall_skill_setting,
)
from .runtime_tools import (
    install_runtime_tool,
    list_runtime_tool_records,
    run_runtime_tool,
    runtime_tools_status,
    uninstall_runtime_tool,
    which_runtime_tool,
)

if TYPE_CHECKING:
    from .knowledge import EmbeddingClient, KnowledgeStore


KNOWLEDGE_UNSUPPORTED_MESSAGE = "knowledge base is not supported in this version"


class AgentService:
    def __init__(
        self,
        settings: AgentPluginSettings,
        client: DirextalkClient,
        runtime: AgentRuntime | None = None,
        knowledge_store: KnowledgeStore | None = None,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        apply_runtime_config_overlay(settings)
        self.settings = settings
        self.client = client
        self.runtime = runtime or PydanticAgentRuntime(settings=settings, client=client)
        # First release keeps the knowledge code in-tree, but does not load its
        # vector/index dependencies or expose it as a supported runtime feature.
        self.knowledge_store = knowledge_store
        self.embedding_client = embedding_client

    async def invoke(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "agent.tools.list":
            return {
                "tools": self.settings.enabled_tools,
                "model_provider": self.settings.model.provider,
                "model": self.settings.model.model,
            }
        if action == "agent.runtime.inspect":
            model_settings = resolve_model_settings(self.settings, params)
            return {
                "model": safe_model_settings(model_settings),
                "skills": [skill.model_dump(mode="json") for skill in self.settings.skills],
                "mcp_servers": await configured_mcp_servers_with_runtime_status(self.settings.mcp_servers),
                "runtime_tools": runtime_tools_status(),
            }
        if action == "agent.runtime.install":
            return await install_runtime_tool(params)
        if action == "agent.runtime.uninstall":
            return await uninstall_runtime_tool(params)
        if action == "agent.runtime.run":
            return await run_runtime_tool(params)
        if action == "agent.runtime.tools.list":
            return {"tools": list_runtime_tool_records(), "status": runtime_tools_status()}
        if action == "agent.runtime.which":
            return which_runtime_tool(str(params.get("command") or ""))
        if action == "agent.models.list":
            profile = params.get("model_profile") if isinstance(params.get("model_profile"), dict) else {}
            provider = str(params.get("provider") or profile.get("provider") or self.settings.model.provider).strip()
            base_url = str(params.get("base_url") or profile.get("base_url") or self.settings.model.base_url).strip()
            api_key = str(params.get("api_key") or "").strip()
            if not api_key:
                api_key = str(profile.get("api_key") or "").strip()
            if not api_key:
                api_key = secret_value(str(params.get("api_key_ref") or profile.get("api_key_ref") or self.settings.model.api_key_ref))
            return await list_provider_models(provider=provider, base_url=base_url, api_key=api_key)
        if action == "agent.skills.list":
            return {"skills": [skill.model_dump(mode="json") for skill in self.settings.skills]}
        if action == "agent.skills.install":
            result = install_skill_setting(self.settings, params)
            self.runtime = PydanticAgentRuntime(settings=self.settings, client=self.client)
            return result
        if action == "agent.skills.uninstall":
            result = uninstall_skill_setting(self.settings, params)
            self.runtime = PydanticAgentRuntime(settings=self.settings, client=self.client)
            return result
        if action == "agent.skills.registry.search":
            return await search_skills(
                registry_url=str(params.get("registry_url") or self.settings.skills_registry_url),
                query=str(params.get("query") or ""),
                page=int(params.get("page") or 1),
                page_size=int(params.get("page_size") or params.get("pageSize") or 20),
            )
        if action == "agent.mcp.servers.list":
            return {"servers": await configured_mcp_servers_with_runtime_status(self.settings.mcp_servers)}
        if action == "agent.mcp.servers.install":
            result = install_mcp_server_setting(self.settings, params)
            self.runtime = PydanticAgentRuntime(settings=self.settings, client=self.client)
            return result
        if action == "agent.mcp.servers.uninstall":
            result = uninstall_mcp_server_setting(self.settings, params)
            self.runtime = PydanticAgentRuntime(settings=self.settings, client=self.client)
            return result
        if action == "agent.mcp.registry.search":
            return await search_mcp_servers(
                registry_url=str(params.get("registry_url") or self.settings.mcp_registry_url),
                query=str(params.get("query") or ""),
                limit=int(params.get("limit") or 20),
            )
        if action == "agent.config.propose_patch":
            return propose_config_patch(params)
        if action.startswith("agent.knowledge."):
            return await self._invoke_knowledge(action, params)
        if action in {"agent.contacts.list", "agent.contacts.search"}:
            return await self.client.list_contacts(
                query=str(params.get("query") or ""),
                limit=int(params.get("limit") or 20),
            )
        if action == "agent.chat":
            prompt = str(params.get("prompt") or params.get("message") or "").strip()
            if not prompt:
                raise ValueError("prompt is required")
            try:
                prompt, knowledge_sources = await self._prompt_with_knowledge(prompt, params)
                result = await self.runtime.chat(prompt, params)
                if knowledge_sources:
                    result["knowledge_sources"] = knowledge_sources
                return result
            except ModelInvocationUnavailable as exc:
                return {
                    "ok": False,
                    "model_ready": False,
                    "provider": self.settings.model.provider,
                    "model": self.settings.model.model,
                    "error": str(exc),
                }
            except Exception as exc:
                return {
                    "ok": False,
                    "model_ready": True,
                    "provider": self.settings.model.provider,
                    "model": self.settings.model.model,
                    "error": str(exc),
                }
        if action == "agent.rooms.search":
            return await self.client.search_rooms(
                query=str(params.get("query") or ""),
                room_type=str(params.get("type") or "all"),
                limit=int(params.get("limit") or 20),
            )
        if action == "agent.messages.list":
            room_id = str(params.get("room_id") or "")
            if not room_id:
                raise ValueError("room_id is required")
            return await self.client.list_messages(room_id, limit=int(params.get("limit") or 50))
        if action == "agent.messages.send":
            room_id = str(params.get("room_id") or "")
            msg = str(params.get("msg") or params.get("text") or "")
            if not room_id or not msg:
                raise ValueError("room_id and msg are required")
            return await self.client.send_message(room_id, msg)
        if action == "agent.summarize":
            room_id = str(params.get("room_id") or "")
            if not room_id:
                raise ValueError("room_id is required")
            messages = await self.client.list_messages(room_id, limit=int(params.get("limit") or 100))
            return {"room_id": room_id, "summary": summarize_messages(messages)}
        if action == "agent.context.compress":
            messages = context_messages(params)
            if not messages:
                raise ValueError("messages are required")
            prompt = context_compression_prompt(str(params.get("summary") or ""), messages)
            try:
                result = await self.runtime.chat(prompt, params)
                summary = str(result.get("text") or "").strip()
            except ModelInvocationUnavailable as exc:
                return {
                    "ok": False,
                    "model_ready": False,
                    "summary": "",
                    "error": str(exc),
                }
            if not summary:
                summary = summarize_context_messages(messages)
            return {
                "ok": True,
                "summary": summary,
                "compressed_message_count": len(messages),
            }
        raise ValueError(f"unknown agent action {action}")

    async def stream(self, action: str, params: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        if action not in {"agent.chat", "agent.chat.stream"}:
            raise ValueError(f"unknown stream action {action}")
        prompt = str(params.get("prompt") or params.get("message") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        try:
            prompt, knowledge_sources = await self._prompt_with_knowledge(prompt, params)
            async for event in self.runtime.stream_chat(prompt, params):
                name = str(event.get("event") or "message").strip()
                data = event.get("data")
                if not isinstance(data, dict):
                    data = {}
                if name == "done" and knowledge_sources:
                    data = {**data, "knowledge_sources": knowledge_sources}
                yield {"event": name, "data": data}
        except ModelInvocationUnavailable as exc:
            yield {
                "event": "error",
                "data": {
                    "ok": False,
                    "model_ready": False,
                    "provider": self.settings.model.provider,
                    "model": self.settings.model.model,
                    "error": str(exc),
                },
            }
        except Exception as exc:
            yield {
                "event": "error",
                "data": {
                    "ok": False,
                    "model_ready": True,
                    "provider": self.settings.model.provider,
                    "model": self.settings.model.model,
                    "error": str(exc),
                },
            }

    async def _invoke_knowledge(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        unsupported = unsupported_knowledge_payload()
        if action == "agent.knowledge.config.get":
            return unsupported
        if action == "agent.knowledge.config.update":
            if truthy(params.get("enabled")):
                raise ValueError(KNOWLEDGE_UNSUPPORTED_MESSAGE)
            return unsupported
        if action == "agent.knowledge.sources.list":
            return {**unsupported, "sources": []}
        if action == "agent.knowledge.search":
            return {**unsupported, "results": []}
        if action == "agent.knowledge.status":
            return unsupported
        if action in {
            "agent.knowledge.sources.delete",
            "agent.knowledge.upload.start",
            "agent.knowledge.upload.chunk",
            "agent.knowledge.upload.finish",
            "agent.knowledge.memory.create",
        }:
            raise ValueError(KNOWLEDGE_UNSUPPORTED_MESSAGE)
        raise ValueError(f"unknown agent action {action}")

    async def _prompt_with_knowledge(self, prompt: str, params: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        return prompt, []


def unsupported_knowledge_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "supported": False,
        "enabled": False,
        "status": "unsupported",
        "message": KNOWLEDGE_UNSUPPORTED_MESSAGE,
    }


def summarize_messages(messages: dict[str, Any]) -> str:
    rows = messages.get("messages") or []
    if not rows:
        return "No recent messages."
    bodies = []
    for item in rows[-10:]:
        if isinstance(item, dict):
            sender = item.get("sender_display_name") or item.get("sender_mxid") or "unknown"
            body = item.get("msg") or item.get("body") or ""
            if body:
                bodies.append(f"{sender}: {body}")
    if not bodies:
        return "No recent text messages."
    return "Recent discussion:\n" + "\n".join(bodies)


def context_messages(params: dict[str, Any]) -> list[dict[str, str]]:
    raw = params.get("messages")
    if not isinstance(raw, list):
        return []
    messages: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user").strip() or "user"
        text = str(item.get("text") or item.get("content") or "").strip()
        if text:
            messages.append({"role": role, "text": text})
    return messages


def context_compression_prompt(existing_summary: str, messages: list[dict[str, str]]) -> str:
    lines = [
        "Summarize the following chat history into a compact memory for future turns.",
        "Keep stable facts, user preferences, decisions, unresolved tasks, file references, and tool results.",
        "Remove greetings and repetition. Use concise bullet points.",
    ]
    if existing_summary.strip():
        lines.extend(["", "Existing compressed memory:", existing_summary.strip()])
    lines.append("")
    lines.append("Messages to compress:")
    for item in messages:
        lines.append(f"{item['role']}: {item['text'][:4000]}")
    return "\n".join(lines).strip()


def summarize_context_messages(messages: list[dict[str, str]]) -> str:
    rows = [f"- {item['role']}: {item['text'][:300]}" for item in messages[-12:]]
    return "Compressed context:\n" + "\n".join(rows)


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def builtin_dirextalk_mcp_server() -> dict[str, Any]:
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
            "channel_posts.list",
            "channel_comments.list",
            "channel_comments.create",
        ],
    }


def safe_model_settings(model_settings: Any) -> dict[str, Any]:
    data = model_settings.model_dump(mode="json")
    data.pop("api_key", None)
    data.pop("api_key_ref", None)
    return data


async def configured_mcp_servers_with_runtime_status(servers: list[Any]) -> list[dict[str, Any]]:
    statuses = await mcp_servers_runtime_status(servers)
    result = [builtin_dirextalk_mcp_server()]
    for server in servers:
        summary = server.model_dump(mode="json")
        summary.update(statuses.get(mcp_server_id(server), {}))
        result.append(summary)
    return result


def propose_config_patch(params: dict[str, Any]) -> dict[str, Any]:
    kind = str(params.get("kind") or "").strip()
    if kind == "skill":
        skill = params.get("skill")
        if not isinstance(skill, dict):
            raise ValueError("skill must be an object")
        return {
            "requires_confirmation": True,
            "summary": f"Install skill {skill.get('name') or skill.get('path') or skill.get('id')}",
            "config_patch": {"skills_add": [skill]},
        }
    if kind == "mcp_server":
        server = params.get("mcp_server")
        if not isinstance(server, dict):
            raise ValueError("mcp_server must be an object")
        return {
            "requires_confirmation": True,
            "summary": f"Install MCP server {server.get('name') or server.get('url') or server.get('command')}",
            "config_patch": {"mcp_servers_add": [server]},
        }
    raise ValueError("kind must be skill or mcp_server")
