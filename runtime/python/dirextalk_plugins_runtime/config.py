from __future__ import annotations

import json
import os
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


class ModelProvider(str, Enum):
    openai = "openai"
    anthropic = "anthropic"
    deepseek = "deepseek"
    gemini = "gemini"
    vertex = "vertex"
    openai_compatible = "openai_compatible"
    openrouter = "openrouter"
    litellm = "litellm"


class ModelSettings(BaseModel):
    provider: ModelProvider
    model: str
    api_key_ref: str = ""
    base_url: str = ""
    temperature: float = 0.2
    max_output_tokens: int = 2048
    context_window: int = 30

    @field_validator("model")
    @classmethod
    def model_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("model is required")
        return value


class SkillSource(BaseModel):
    repo_url: HttpUrl
    ref: str
    path: str
    enabled: bool = False

    @field_validator("ref", "path")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value is required")
        return value


class MCPServerConfig(BaseModel):
    name: str
    transport: Literal["stdio", "streamable_http", "sse"]
    enabled: bool = False
    command: list[str] = Field(default_factory=list)
    url: str = ""
    timeout_ms: int = 30000
    tool_allowlist: list[str] = Field(default_factory=list)


class DirextalkSettings(BaseModel):
    base_url: str = "http://message-server:8008"
    agent_token_ref: str = "env:DIREXTALK_AGENT_TOKEN"


class AgentPluginSettings(BaseModel):
    plugin_id: str = "io.dirextalk.agent"
    enabled: bool = True
    display_name: str = "Dirextalk Agent"
    system_prompt: str = "You are the local Dirextalk assistant."
    model: ModelSettings = Field(default_factory=lambda: ModelSettings(provider=ModelProvider.openai, model="gpt-4.1"))
    dirextalk: DirextalkSettings = Field(default_factory=DirextalkSettings)
    skills: list[SkillSource] = Field(default_factory=list)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    enabled_tools: list[str] = Field(default_factory=lambda: ["search_rooms", "list_messages", "send_message", "summarize_conversation"])
    extra: dict[str, Any] = Field(default_factory=dict)


def settings_from_environment() -> AgentPluginSettings:
    return AgentPluginSettings(
        enabled=env_bool("AGENT_ENABLED", True),
        display_name=os.getenv("AGENT_DISPLAY_NAME", "Dirextalk Agent"),
        system_prompt=os.getenv("AGENT_SYSTEM_PROMPT", "You are the local Dirextalk assistant."),
        model=ModelSettings(
            provider=ModelProvider(os.getenv("AGENT_MODEL_PROVIDER", "openai")),
            model=os.getenv("AGENT_MODEL", "gpt-4.1"),
            api_key_ref=os.getenv("AGENT_API_KEY_REF", ""),
            base_url=os.getenv("AGENT_BASE_URL", ""),
            temperature=env_float("AGENT_TEMPERATURE", 0.2),
            max_output_tokens=env_int("AGENT_MAX_OUTPUT_TOKENS", 2048),
            context_window=env_int("AGENT_CONTEXT_WINDOW", 30),
        ),
        dirextalk=DirextalkSettings(
            base_url=os.getenv("DIREXTALK_BASE_URL", "http://message-server:8008"),
            agent_token_ref=os.getenv("DIREXTALK_AGENT_TOKEN_REF", "env:DIREXTALK_AGENT_TOKEN"),
        ),
        skills=parse_model_list("AGENT_SKILLS_JSON", SkillSource),
        mcp_servers=parse_model_list("AGENT_MCP_SERVERS_JSON", MCPServerConfig),
        enabled_tools=env_csv(
            "AGENT_ENABLED_TOOLS",
            ["search_rooms", "list_messages", "send_message", "summarize_conversation"],
        ),
    )


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return float(value)


def env_csv(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name)
    if value is None:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_model_list(name: str, model_type: type[BaseModel]) -> list[Any]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return []
    data = json.loads(value)
    if not isinstance(data, list):
        raise ValueError(f"{name} must be a JSON list")
    return [model_type.model_validate(item) for item in data]
