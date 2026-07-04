from __future__ import annotations

from typing import Any

from dirextalk_plugins_runtime import AgentPluginSettings, DirextalkClient

from .llm import AgentRuntime, ModelInvocationUnavailable, PydanticAgentRuntime


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
        if action == "agent.skills.list":
            return {"skills": [skill.model_dump(mode="json") for skill in self.settings.skills]}
        if action == "agent.mcp.servers.list":
            return {"servers": [server.model_dump(mode="json") for server in self.settings.mcp_servers]}
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
