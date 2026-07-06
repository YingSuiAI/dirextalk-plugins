from __future__ import annotations

from typing import Any, Literal

import httpx


class DirextalkActionError(RuntimeError):
    def __init__(self, *, action: str, status_code: int, error: str, body: Any | None = None) -> None:
        self.action = action
        self.status_code = status_code
        self.error = error
        self.body = body
        super().__init__(f"{action} failed with status {status_code}: {error}")


class DirextalkClient:
    def __init__(self, base_url: str, agent_token: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_token = agent_token
        self.timeout = timeout

    async def action(self, kind: Literal["query", "command"], action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.agent_token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/_p2p/{kind}",
                headers=headers,
                json={"action": action, "params": params or {}},
            )
        if response.status_code < 200 or response.status_code >= 300:
            body: Any
            try:
                body = response.json()
            except Exception:  # noqa: BLE001
                body = response.text
            if isinstance(body, dict):
                error = str(body.get("error") or body.get("detail") or response.text or response.reason_phrase)
            else:
                error = str(body or response.reason_phrase)
            raise DirextalkActionError(action=action, status_code=response.status_code, error=error, body=body)
        data = response.json()
        if not isinstance(data, dict):
            raise TypeError(f"expected object response for {action}")
        return data

    async def search_rooms(self, query: str = "", room_type: str = "all", limit: int = 20) -> dict[str, Any]:
        return await self.action("query", "mcp.rooms.search", {"query": query, "type": room_type, "limit": limit})

    async def list_contacts(self, query: str = "", limit: int = 20) -> dict[str, Any]:
        action = "mcp.contacts.search" if query.strip() else "mcp.contacts.list"
        return await self.action("query", action, {"query": query, "limit": limit})

    async def list_messages(self, room_id: str, limit: int = 50, from_ts: int = 0, to_ts: int = 0) -> dict[str, Any]:
        return await self.action("query", "mcp.messages.list", {"room_id": room_id, "limit": limit, "from_ts": from_ts, "to_ts": to_ts})

    async def send_message(self, room_id: str, msg: str) -> dict[str, Any]:
        return await self.action("command", "mcp.messages.send", {"room_id": room_id, "msg": msg, "agent_gateway": True})
