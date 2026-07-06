from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dirextalk_plugins_runtime import AgentPluginSettings, MCPServerConfig, SkillSource


def runtime_config_path() -> Path:
    return Path(os.getenv("AGENT_RUNTIME_CONFIG_PATH", "/var/lib/dirextalk-agent/runtime_config.json"))


def apply_runtime_config_overlay(settings: AgentPluginSettings) -> None:
    path = runtime_config_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    if str(data.get("base_revision") or "") != settings.runtime_config_revision:
        return
    skills = data.get("skills")
    if isinstance(skills, list):
        settings.skills = [SkillSource.model_validate(item) for item in skills if isinstance(item, dict)]
    mcp_servers = data.get("mcp_servers")
    if isinstance(mcp_servers, list):
        settings.mcp_servers = [MCPServerConfig.model_validate(item) for item in mcp_servers if isinstance(item, dict)]


def save_runtime_config_overlay(settings: AgentPluginSettings) -> None:
    path = runtime_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "base_revision": settings.runtime_config_revision,
        "skills": [skill.model_dump(mode="json") for skill in settings.skills],
        "mcp_servers": [server.model_dump(mode="json") for server in settings.mcp_servers],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def install_skill_setting(settings: AgentPluginSettings, params: dict[str, Any]) -> dict[str, Any]:
    raw_skill = params.get("skill")
    if isinstance(raw_skill, dict):
        skill_data = raw_skill
    else:
        skill_data = {
            "repo_url": params.get("repo_url"),
            "ref": params.get("ref") or "main",
            "path": params.get("path"),
            "enabled": params.get("enabled", True),
        }
    skill = SkillSource.model_validate(skill_data)
    key = skill_key(skill)
    settings.skills = [existing for existing in settings.skills if skill_key(existing) != key]
    settings.skills.append(skill)
    save_runtime_config_overlay(settings)
    return {"ok": True, "skill": skill.model_dump(mode="json"), "skills": [item.model_dump(mode="json") for item in settings.skills]}


def uninstall_skill_setting(settings: AgentPluginSettings, params: dict[str, Any]) -> dict[str, Any]:
    key = str(params.get("key") or params.get("path") or "").strip()
    repo_url = str(params.get("repo_url") or "").strip()
    before = len(settings.skills)
    settings.skills = [
        skill
        for skill in settings.skills
        if not (
            (key and (skill.path == key or skill_key(skill) == key))
            or (repo_url and str(skill.repo_url).rstrip("/") == repo_url.rstrip("/") and (not key or skill.path == key))
        )
    ]
    save_runtime_config_overlay(settings)
    return {
        "ok": True,
        "removed": before - len(settings.skills),
        "skills": [item.model_dump(mode="json") for item in settings.skills],
    }


def install_mcp_server_setting(settings: AgentPluginSettings, params: dict[str, Any]) -> dict[str, Any]:
    raw_server = params.get("mcp_server") or params.get("server")
    if isinstance(raw_server, dict):
        server_data = raw_server
    else:
        command = params.get("command")
        if isinstance(command, str):
            command = [item for item in command.split(" ") if item]
        server_data = {
            "name": params.get("name"),
            "transport": params.get("transport") or "stdio",
            "command": command or [],
            "url": params.get("url") or "",
            "enabled": params.get("enabled", True),
            "timeout_ms": params.get("timeout_ms") or 30000,
            "tool_allowlist": params.get("tool_allowlist") or [],
        }
    server = MCPServerConfig.model_validate(server_data)
    key = mcp_server_key(server)
    settings.mcp_servers = [existing for existing in settings.mcp_servers if mcp_server_key(existing) != key]
    settings.mcp_servers.append(server)
    save_runtime_config_overlay(settings)
    return {
        "ok": True,
        "mcp_server": server.model_dump(mode="json"),
        "mcp_servers": [item.model_dump(mode="json") for item in settings.mcp_servers],
    }


def uninstall_mcp_server_setting(settings: AgentPluginSettings, params: dict[str, Any]) -> dict[str, Any]:
    key = str(params.get("key") or params.get("name") or "").strip().lower()
    if not key:
        raise ValueError("name or key is required")
    before = len(settings.mcp_servers)
    settings.mcp_servers = [
        server
        for server in settings.mcp_servers
        if mcp_server_key(server) != key and server.name.strip().lower() != key
    ]
    save_runtime_config_overlay(settings)
    return {
        "ok": True,
        "removed": before - len(settings.mcp_servers),
        "mcp_servers": [item.model_dump(mode="json") for item in settings.mcp_servers],
    }


def skill_key(skill: SkillSource) -> str:
    return f"{str(skill.repo_url).rstrip('/')}#{skill.ref}:{skill.path}"


def mcp_server_key(server: MCPServerConfig) -> str:
    return server.name.strip().lower()
