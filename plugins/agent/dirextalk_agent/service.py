from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from dirextalk_plugins_runtime import AgentPluginSettings, DirextalkClient

from .llm import AgentRuntime, ModelInvocationUnavailable, PydanticAgentRuntime, list_provider_models, secret_value
from .registry import search_mcp_servers, search_skills


class AgentService:
    def __init__(
        self,
        settings: AgentPluginSettings,
        client: DirextalkClient,
        runtime: AgentRuntime | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.runtime = runtime or PydanticAgentRuntime(settings=settings, client=client)

    async def invoke(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "agent.tools.list":
            return {
                "tools": self.settings.enabled_tools,
                "model_provider": self.settings.model.provider,
                "model": self.settings.model.model,
            }
        if action == "agent.models.list":
            provider = str(params.get("provider") or self.settings.model.provider).strip()
            base_url = str(params.get("base_url") or self.settings.model.base_url).strip()
            api_key = str(params.get("api_key") or "").strip()
            if not api_key:
                api_key = secret_value(str(params.get("api_key_ref") or self.settings.model.api_key_ref))
            return await list_provider_models(provider=provider, base_url=base_url, api_key=api_key)
        if action == "agent.skills.list":
            return {"skills": [skill.model_dump(mode="json") for skill in self.settings.skills]}
        if action == "agent.skills.registry.search":
            return await search_skills(
                registry_url=str(params.get("registry_url") or self.settings.skills_registry_url),
                query=str(params.get("query") or ""),
                page=int(params.get("page") or 1),
                page_size=int(params.get("page_size") or params.get("pageSize") or 20),
            )
        if action == "agent.mcp.servers.list":
            return {
                "servers": [
                    builtin_dirextalk_mcp_server(),
                    *[server.model_dump(mode="json") for server in self.settings.mcp_servers],
                ]
            }
        if action == "agent.mcp.registry.search":
            return await search_mcp_servers(
                registry_url=str(params.get("registry_url") or self.settings.mcp_registry_url),
                query=str(params.get("query") or ""),
                limit=int(params.get("limit") or 20),
            )
        if action == "agent.config.propose_patch":
            return propose_config_patch(params)
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
                return await self.runtime.chat(prompt, params)
            except ModelInvocationUnavailable as exc:
                return {
                    "ok": False,
                    "model_ready": False,
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
        raise ValueError(f"unknown agent action {action}")

    async def stream(self, action: str, params: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        if action not in {"agent.chat", "agent.chat.stream"}:
            raise ValueError(f"unknown stream action {action}")
        prompt = str(params.get("prompt") or params.get("message") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        try:
            async for event in self.runtime.stream_chat(prompt, params):
                name = str(event.get("event") or "message").strip()
                data = event.get("data")
                if not isinstance(data, dict):
                    data = {}
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
