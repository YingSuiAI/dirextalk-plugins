from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from dirextalk_plugins_runtime import AgentPluginSettings, DirextalkClient, settings_from_environment

from .llm import AgentRuntime
from .service import AgentService


class InvokeRequest(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)


def _secret_value(ref: str) -> str:
    if ref.startswith("env:"):
        return os.getenv(ref.removeprefix("env:"), "")
    return ""


def create_app(
    settings: AgentPluginSettings | None = None,
    client: DirextalkClient | None = None,
    runtime: AgentRuntime | None = None,
) -> FastAPI:
    settings = settings or settings_from_environment()
    token = _secret_value(settings.dirextalk.agent_token_ref)
    client = client or DirextalkClient(settings.dirextalk.base_url, token)
    service = AgentService(settings=settings, client=client, runtime=runtime)
    app = FastAPI(title="Dirextalk Agent Plugin", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "plugin_id": settings.plugin_id, "enabled": settings.enabled}

    @app.get("/config/schema")
    async def config_schema() -> dict[str, Any]:
        return AgentPluginSettings.model_json_schema()

    @app.post("/invoke")
    async def invoke(request: InvokeRequest) -> dict[str, Any]:
        try:
            return await service.invoke(request.action, request.params)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.websocket("/ws")
    async def websocket_invoke(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            frame = await websocket.receive_json()
            if not isinstance(frame, dict) or frame.get("type") != "plugin.invoke.stream":
                await websocket.send_json({"type": "plugin.stream.error", "error": "plugin.invoke.stream is required"})
                return
            action = str(frame.get("action") or "").strip()
            params = frame.get("params") or {}
            if not isinstance(params, dict):
                await websocket.send_json({"type": "plugin.stream.error", "error": "params must be an object"})
                return
            async for event in service.stream(action, params):
                event_name = str(event.get("event") or "message").strip()
                data = event.get("data")
                if not isinstance(data, dict):
                    data = {}
                if event_name == "done":
                    await websocket.send_json({"type": "plugin.stream.done", "data": data})
                elif event_name == "error":
                    await websocket.send_json({"type": "plugin.stream.error", "data": data, "error": str(data.get("error") or "")})
                else:
                    await websocket.send_json({"type": "plugin.stream.event", "event": event_name, "data": data})
        except WebSocketDisconnect:
            return
        except ValueError as exc:
            await websocket.send_json({"type": "plugin.stream.error", "error": str(exc)})

    return app


app = create_app()
