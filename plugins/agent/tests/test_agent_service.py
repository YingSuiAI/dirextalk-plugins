import pytest

from dirextalk_agent.llm import ModelInvocationUnavailable
from dirextalk_agent.service import AgentService
from dirextalk_plugins_runtime import AgentPluginSettings, MCPServerConfig, SkillSource


class FakeDirextalkClient:
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


@pytest.mark.asyncio
async def test_agent_service_wraps_dirextalk_tools() -> None:
    service = AgentService(settings=AgentPluginSettings(), client=FakeDirextalkClient())

    rooms = await service.invoke("agent.rooms.search", {"query": "gen", "type": "group"})
    assert rooms["rooms"][0]["room_id"] == "!room:example.com"

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
    assert servers["servers"][0]["tool_allowlist"] == ["read_file"]
