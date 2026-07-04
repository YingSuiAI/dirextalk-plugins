import pytest

from dirextalk_agent import llm as llm_module
from dirextalk_agent.llm import (
    ModelInvocationUnavailable,
    PydanticAgentRuntime,
    mcp_server_id,
    prompt_with_attachments,
    skills_system_prompt,
)
from dirextalk_agent.service import AgentService
from dirextalk_plugins_runtime import AgentPluginSettings, MCPServerConfig, SkillSource


class FakeDirextalkClient:
    async def list_contacts(self, query: str = "", limit: int = 20):
        return {
            "contacts": [
                {
                    "peer_mxid": "@alice:example.com",
                    "display_name": "Alice",
                    "room_id": "!alice:example.com",
                    "query": query,
                    "limit": limit,
                }
            ]
        }

    async def search_rooms(self, query: str = "", room_type: str = "all", limit: int = 20):
        return {"rooms": [{"room_id": "!room:example.com", "name": "General", "type": room_type, "query": query}]}

    async def list_messages(self, room_id: str, limit: int = 50, from_ts: int = 0, to_ts: int = 0):
        return {
            "room_id": room_id,
            "messages": [
                {"sender_display_name": "Alice", "msg": "Hello"},
                {"sender_display_name": "Agent", "msg": "Hi"},
            ],
        }

    async def send_message(self, room_id: str, msg: str):
        return {"ok": True, "room_id": room_id, "event_id": "$event"}


class UnavailableRuntime:
    async def chat(self, prompt: str, params: dict):
        raise ModelInvocationUnavailable("missing API key")


class StreamingRuntime:
    async def chat(self, prompt: str, params: dict):
        return {"ok": True, "text": prompt, "model_profile_id": params.get("model_profile_id")}

    async def stream_chat(self, prompt: str, params: dict):
        yield {"event": "delta", "data": {"text": "hel", "model_profile_id": params.get("model_profile_id")}}
        yield {"event": "done", "data": {"text": "hello"}}


class FailingRuntime:
    async def chat(self, prompt: str, params: dict):
        raise RuntimeError("tool failed")

    async def stream_chat(self, prompt: str, params: dict):
        raise RuntimeError("tool failed")
        yield {"event": "done", "data": {}}


class RecordingAgent:
    def __init__(self, model: str, *, system_prompt: str = "", toolsets=None, **_kwargs):
        self.model = model
        self.system_prompt = system_prompt
        self.toolsets = toolsets or []
        self.tools: list[str] = []

    def tool_plain(self, func):
        self.tools.append(func.__name__)
        return func


def test_pydantic_agent_runtime_registers_dirextalk_tools_with_real_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_ai import Agent

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    settings = AgentPluginSettings()
    runtime = PydanticAgentRuntime(settings=settings, client=FakeDirextalkClient())

    runtime._create_agent(Agent, settings.model)


def test_enabled_skills_are_loaded_into_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_module, "fetch_skill_instruction", lambda _skill: "Always review security-sensitive code.")
    skill = SkillSource(
        repo_url="https://github.com/example/agent-skills",
        ref="main",
        path="skills/code-review",
        enabled=True,
    )

    prompt = skills_system_prompt([skill], {})

    assert "Always review security-sensitive code." in prompt
    assert "skills/code-review" in prompt


def test_disabled_skills_are_not_loaded_into_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_module, "fetch_skill_instruction", lambda _skill: "should not appear")
    skill = SkillSource(
        repo_url="https://github.com/example/agent-skills",
        ref="main",
        path="skills/code-review",
        enabled=False,
    )

    assert skills_system_prompt([skill], {}) == ""


def test_runtime_registers_builtin_config_tools_and_summarize_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_module, "build_mcp_toolsets", lambda _settings: ["mcp-toolset"])
    settings = AgentPluginSettings()
    runtime = PydanticAgentRuntime(settings=settings, client=FakeDirextalkClient())

    agent = runtime._create_agent(RecordingAgent, settings.model)

    assert agent.toolsets == ["mcp-toolset"]
    assert "summarize_conversation" in agent.tools
    assert "list_installed_skills" in agent.tools
    assert "list_mcp_servers" in agent.tools


def test_prompt_with_attachments_includes_text_content() -> None:
    prompt = prompt_with_attachments(
        "summarize this",
        {
            "attachments": [
                {
                    "name": "notes.txt",
                    "mime_type": "text/plain",
                    "size": 12,
                    "text": "hello from file",
                }
            ]
        },
    )

    assert "notes.txt" in prompt
    assert "hello from file" in prompt


def test_mcp_server_id_is_stable_and_safe() -> None:
    server = MCPServerConfig(
        name="Context 7 MCP",
        transport="stdio",
        command=["npx", "-y", "@upstash/context7-mcp"],
        enabled=True,
    )

    assert mcp_server_id(server) == "context_7_mcp"


@pytest.mark.asyncio
async def test_agent_service_wraps_dirextalk_tools() -> None:
    service = AgentService(settings=AgentPluginSettings(), client=FakeDirextalkClient())

    rooms = await service.invoke("agent.rooms.search", {"query": "gen", "type": "group"})
    assert rooms["rooms"][0]["room_id"] == "!room:example.com"

    contacts = await service.invoke("agent.contacts.search", {"query": "ali"})
    assert contacts["contacts"][0]["peer_mxid"] == "@alice:example.com"

    sent = await service.invoke("agent.messages.send", {"room_id": "!room:example.com", "msg": "ok"})
    assert sent["ok"] is True

    summary = await service.invoke("agent.summarize", {"room_id": "!room:example.com"})
    assert "Alice: Hello" in summary["summary"]


@pytest.mark.asyncio
async def test_agent_chat_reports_model_configuration_problem() -> None:
    service = AgentService(settings=AgentPluginSettings(), client=FakeDirextalkClient(), runtime=UnavailableRuntime())

    result = await service.invoke("agent.chat", {"prompt": "hello"})

    assert result["ok"] is False
    assert result["model_ready"] is False
    assert "missing API key" in result["error"]


@pytest.mark.asyncio
async def test_agent_chat_stream_returns_event_shape_and_profile_id() -> None:
    service = AgentService(settings=AgentPluginSettings(), client=FakeDirextalkClient(), runtime=StreamingRuntime())

    events = [event async for event in service.stream("agent.chat.stream", {"prompt": "hello", "model_profile_id": "work"})]

    assert events == [
        {"event": "delta", "data": {"text": "hel", "model_profile_id": "work"}},
        {"event": "done", "data": {"text": "hello"}},
    ]


@pytest.mark.asyncio
async def test_agent_chat_stream_reports_tool_errors() -> None:
    service = AgentService(settings=AgentPluginSettings(), client=FakeDirextalkClient(), runtime=FailingRuntime())

    events = [event async for event in service.stream("agent.chat.stream", {"prompt": "hello"})]

    assert events[0]["event"] == "error"
    assert "tool failed" in events[0]["data"]["error"]


@pytest.mark.asyncio
async def test_agent_lists_skills_and_mcp_servers() -> None:
    settings = AgentPluginSettings(
        skills=[
            SkillSource(
                repo_url="https://github.com/YingSuiAI/example-skills",
                ref="main",
                path="skills/summarize",
                enabled=True,
            )
        ],
        mcp_servers=[
            MCPServerConfig(
                name="filesystem",
                transport="stdio",
                command=["npx", "server"],
                tool_allowlist=["read_file"],
                enabled=True,
            )
        ],
    )
    service = AgentService(settings=settings, client=FakeDirextalkClient())

    skills = await service.invoke("agent.skills.list", {})
    servers = await service.invoke("agent.mcp.servers.list", {})

    assert skills["skills"][0]["enabled"] is True
    assert servers["servers"][0]["name"] == "Dirextalk Built-in MCP"
    assert "contacts.search" in servers["servers"][0]["tools"]
    assert servers["servers"][1]["tool_allowlist"] == ["read_file"]


@pytest.mark.asyncio
async def test_agent_searches_mcp_curated_market_when_registry_has_no_results() -> None:
    service = AgentService(settings=AgentPluginSettings(mcp_registry_url="local"), client=FakeDirextalkClient())

    result = await service.invoke("agent.mcp.registry.search", {"query": "memory"})

    assert result["servers"][0]["source"] == "dirextalk_curated"
    assert result["servers"][0]["mcp_server"]["command"][0] == "npx"


@pytest.mark.asyncio
async def test_agent_config_proposes_skill_and_mcp_patches() -> None:
    service = AgentService(settings=AgentPluginSettings(), client=FakeDirextalkClient())

    skill_patch = await service.invoke(
        "agent.config.propose_patch",
        {
            "kind": "skill",
            "skill": {
                "repo_url": "https://github.com/vercel-labs/agent-skills",
                "ref": "main",
                "path": "skills/browser",
                "enabled": True,
            },
        },
    )
    assert skill_patch["requires_confirmation"] is True
    assert skill_patch["config_patch"]["skills_add"][0]["path"] == "skills/browser"

    mcp_patch = await service.invoke(
        "agent.config.propose_patch",
        {
            "kind": "mcp_server",
            "mcp_server": {
                "name": "filesystem",
                "transport": "stdio",
                "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem"],
                "enabled": True,
            },
        },
    )
    assert mcp_patch["config_patch"]["mcp_servers_add"][0]["name"] == "filesystem"
