from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from dirextalk_plugins_runtime import AgentPluginSettings, DirextalkClient, settings_from_environment

from .service import AgentService


class InvokeRequest(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)


def _secret_value(ref: str) -> str:
    if ref.startswith("env:"):
        return os.getenv(ref.removeprefix("env:"), "")
    return ""


def create_app(settings: AgentPluginSettings | None = None, client: DirextalkClient | None = None) -> FastAPI:
    settings = settings or settings_from_environment()
    token = _secret_value(settings.dirextalk.agent_token_ref)
    client = client or DirextalkClient(settings.dirextalk.base_url, token)
    service = AgentService(settings=settings, client=client)
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

    return app


app = create_app()
