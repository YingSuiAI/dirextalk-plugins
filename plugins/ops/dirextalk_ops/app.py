from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .service import OpsService, OpsSettings


class InvokeRequest(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)


def create_app(service: OpsService | None = None) -> FastAPI:
    service = service or OpsService()
    app = FastAPI(title="Dirextalk Ops Plugin", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "plugin_id": "io.dirextalk.ops",
            "backup_root": str(service.settings.backup_root),
        }

    @app.get("/config/schema")
    async def config_schema() -> dict[str, Any]:
        return {
            "plugin_id": "io.dirextalk.ops",
            "settings": {
                "backup_root": "OPS_BACKUP_ROOT",
                "max_backups": "OPS_MAX_BACKUPS",
                "message_server_container": "OPS_MESSAGE_SERVER_CONTAINER",
                "postgres_container": "OPS_POSTGRES_CONTAINER",
            },
            "model_schema": OpsSettings.__name__,
        }

    @app.post("/invoke")
    async def invoke(request: InvokeRequest) -> dict[str, Any]:
        try:
            return await service.invoke(request.action, request.params)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return app


app = create_app()
