from types import SimpleNamespace

import httpx
import pytest

from dirextalk_agent import llm as llm_module
from dirextalk_agent import service as service_module
from dirextalk_agent.llm import (
    ModelInvocationUnavailable,
    PydanticAgentRuntime,
    fetch_skill_instruction,
    mcp_server_id,
    prompt_with_attachments,
    skills_system_prompt,
)
from dirextalk_agent.service import AgentService
from dirextalk_plugins_runtime import (
    AgentPluginSettings,
    DirextalkActionError,
    MCPServerConfig,
    ModelSettings,
    SkillSource,
)


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


class RecordingRuntime:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.params: list[dict] = []

    async def chat(self, prompt: str, params: dict):
        self.prompts.append(prompt)
        self.params.append(params)
        return {"ok": True, "text": "用户偏好中文回答；待办是检查部署。"}

    async def stream_chat(self, prompt: str, params: dict):
        yield {"event": "done", "data": {"text": prompt}}


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
        self.tool_funcs = {}

    def tool_plain(self, func):
        self.tools.append(func.__name__)
        self.tool_funcs[func.__name__] = func
        return func


class StreamingGraphAgent(RecordingAgent):
    instances: list["StreamingGraphAgent"] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.used_run_stream_events = False
        self.used_run_stream = False
        StreamingGraphAgent.instances.append(self)

    def run_stream_events(self, prompt: str):
        self.used_run_stream_events = True
        self.prompt = prompt
        return _FakeEventStream(
            [
                _text_start("我先检查一下当前可用后端。"),
                _tool_call("runtime_tools_status"),
                _tool_result("runtime_tools_status"),
                _tool_call("agent_reach_search"),
                _tool_result("agent_reach_search"),
                _text_delta("已经完成检查，agent-reach 可以继续用于查询。"),
                _run_result("我先检查一下当前可用后端。已经完成检查，agent-reach 可以继续用于查询。"),
            ]
        )

    def run_stream(self, *_args, **_kwargs):
        self.used_run_stream = True
        raise AssertionError("run_stream stops after first text output and must not be used for tool-capable chats")


class _FakeEventStream:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        async def stream():
            for event in self.events:
                yield event

        return stream()

    async def __aexit__(self, *_args):
        return False


def _text_start(text: str):
    return SimpleNamespace(
        event_kind="part_start",
        part=SimpleNamespace(part_kind="text", content=text),
    )


def _text_delta(text: str):
    return SimpleNamespace(
        event_kind="part_delta",
        delta=SimpleNamespace(part_delta_kind="text", content_delta=text),
    )


def _tool_call(name: str):
    return SimpleNamespace(event_kind="function_tool_call", part=SimpleNamespace(tool_name=name))


def _tool_result(name: str):
    return SimpleNamespace(event_kind="function_tool_result", part=SimpleNamespace(tool_name=name))


def _run_result(text: str):
    return SimpleNamespace(event_kind="agent_run_result", result=SimpleNamespace(output=text))


class ErroringDirextalkClient(FakeDirextalkClient):
    async def list_messages(self, room_id: str, limit: int = 50, from_ts: int = 0, to_ts: int = 0):
        raise DirextalkActionError(
            action="mcp.messages.list",
            status_code=403,
            error="room is not allowed for MCP access",
            body={"error": "room is not allowed for MCP access"},
        )


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


def test_fetch_skill_instruction_tries_common_skills_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_urls: list[str] = []

    def fake_get(url: str, **_: object) -> httpx.Response:
        seen_urls.append(url)
        request = httpx.Request("GET", url)
        if url.endswith("/skills/brainstorming/SKILL.md"):
            return httpx.Response(200, request=request, text="Use divergent thinking first.")
        return httpx.Response(404, request=request, text="not found")

    monkeypatch.setattr(llm_module.httpx, "get", fake_get)
    skill = SkillSource(
        repo_url="https://github.com/obra/superpowers",
        ref="main",
        path="brainstorming",
        enabled=True,
    )

    assert fetch_skill_instruction(skill) == "Use divergent thinking first."
    assert seen_urls[:2] == [
        "https://raw.githubusercontent.com/obra/superpowers/main/brainstorming/SKILL.md",
        "https://raw.githubusercontent.com/obra/superpowers/main/skills/brainstorming/SKILL.md",
    ]


def test_fetch_skill_instruction_discovers_single_repo_skill_file(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_urls: list[str] = []

    def fake_get(url: str, **_: object) -> httpx.Response:
        seen_urls.append(url)
        request = httpx.Request("GET", url)
        if url == "https://api.github.com/repos/panniantong/agent-reach/git/trees/main?recursive=1":
            return httpx.Response(
                200,
                request=request,
                json={"tree": [{"path": "agent_reach/skill/SKILL.md", "type": "blob"}]},
            )
        if url.endswith("/agent_reach/skill/SKILL.md"):
            return httpx.Response(200, request=request, text="Reach external resources when asked.")
        return httpx.Response(404, request=request, text="not found")

    monkeypatch.setattr(llm_module.httpx, "get", fake_get)
    skill = SkillSource(
        repo_url="https://github.com/panniantong/agent-reach",
        ref="main",
        path="agent-reach",
        enabled=True,
    )

    assert fetch_skill_instruction(skill) == "Reach external resources when asked."
    assert "https://api.github.com/repos/panniantong/agent-reach/git/trees/main?recursive=1" in seen_urls


def test_runtime_registers_builtin_config_tools_and_summarize_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_module, "build_mcp_toolsets", lambda _settings: ["mcp-toolset"])
    settings = AgentPluginSettings()
    runtime = PydanticAgentRuntime(settings=settings, client=FakeDirextalkClient())

    agent = runtime._create_agent(RecordingAgent, settings.model)

    assert agent.toolsets == ["mcp-toolset"]
    assert "summarize_conversation" in agent.tools
    assert "list_installed_skills" in agent.tools
    assert "list_mcp_servers" in agent.tools
    assert "runtime_tools_status" in agent.tools
    assert "install_runtime_tool" in agent.tools


@pytest.mark.asyncio
async def test_runtime_tools_return_structured_errors_instead_of_raising() -> None:
    settings = AgentPluginSettings()
    runtime = PydanticAgentRuntime(settings=settings, client=ErroringDirextalkClient())

    agent = runtime._create_agent(RecordingAgent, settings.model)
    result = await agent.tool_funcs["list_messages"]("!missing:example.com")

    assert result["ok"] is False
    assert result["status"] == 403
    assert result["action"] == "mcp.messages.list"
    assert "not allowed" in result["error"]


@pytest.mark.asyncio
async def test_stream_chat_uses_full_agent_graph_events(monkeypatch: pytest.MonkeyPatch) -> None:
    import pydantic_ai

    StreamingGraphAgent.instances.clear()
    monkeypatch.setattr(pydantic_ai, "Agent", StreamingGraphAgent)
    settings = AgentPluginSettings(
        model=ModelSettings(provider="openai", model="gpt-4.1", api_key="test-key")
    )
    runtime = PydanticAgentRuntime(settings=settings, client=FakeDirextalkClient())

    events = [event async for event in runtime.stream_chat("帮我查询小红书上海美食攻略", {})]

    agent = StreamingGraphAgent.instances[0]
    assert agent.used_run_stream_events is True
    assert agent.used_run_stream is False
    assert events == [
        {"event": "delta", "data": {"text": "我先检查一下当前可用后端。"}},
        {"event": "delta", "data": {"text": "已经完成检查，agent-reach 可以继续用于查询。"}},
        {
            "event": "done",
            "data": {
                "text": "我先检查一下当前可用后端。已经完成检查，agent-reach 可以继续用于查询。",
                "provider": settings.model.provider,
                "model": settings.model.model,
            },
        },
    ]


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


def test_prompt_includes_compressed_and_recent_conversation_context() -> None:
    prompt = prompt_with_attachments(
        "继续",
        {
            "conversation_context": {
                "summary": "用户偏好中文。",
                "messages": [
                    {"role": "user", "text": "上次说到部署"},
                    {"role": "assistant", "text": "需要检查日志"},
                ],
            }
        },
    )

    assert "Compressed conversation memory" in prompt
    assert "用户偏好中文" in prompt
    assert "user: 上次说到部署" in prompt
    assert "Current user message" in prompt
    assert prompt.endswith("继续")


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
async def test_agent_context_compress_uses_runtime_and_returns_summary() -> None:
    runtime = RecordingRuntime()
    service = AgentService(settings=AgentPluginSettings(), client=FakeDirextalkClient(), runtime=runtime)

    result = await service.invoke(
        "agent.context.compress",
        {
            "summary": "旧摘要",
            "messages": [
                {"role": "user", "text": "我希望中文回答"},
                {"role": "assistant", "text": "好的"},
            ],
            "model_profile": {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "api_key": "client-key",
            },
        },
    )

    assert result["ok"] is True
    assert result["compressed_message_count"] == 2
    assert "用户偏好中文" in result["summary"]
    assert "Existing compressed memory" in runtime.prompts[0]
    assert runtime.params[0]["model_profile"]["api_key"] == "client-key"


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
async def test_agent_mcp_list_includes_runtime_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_runtime_statuses(_servers):
        return {
            "filesystem": {
                "runtime_status": "ready",
                "tool_count": 1,
                "tools": [{"name": "read_file", "description": "Read a file"}],
            }
        }

    monkeypatch.setattr(service_module, "mcp_servers_runtime_status", fake_runtime_statuses)
    settings = AgentPluginSettings(
        mcp_servers=[
            MCPServerConfig(
                name="filesystem",
                transport="stdio",
                command=["npx", "server"],
                enabled=True,
            )
        ],
    )
    service = AgentService(settings=settings, client=FakeDirextalkClient())

    servers = await service.invoke("agent.mcp.servers.list", {})

    assert servers["servers"][1]["runtime_status"] == "ready"
    assert servers["servers"][1]["tool_count"] == 1
    assert servers["servers"][1]["tools"][0]["name"] == "read_file"


@pytest.mark.asyncio
async def test_agent_runtime_inspect_reports_request_model_and_mcp_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_runtime_statuses(_servers):
        return {
            "context7": {
                "runtime_status": "ready",
                "tool_count": 2,
                "tools": [{"name": "resolve-library-id"}, {"name": "get-library-docs"}],
            }
        }

    monkeypatch.setattr(service_module, "mcp_servers_runtime_status", fake_runtime_statuses)
    settings = AgentPluginSettings(
        mcp_servers=[
            MCPServerConfig(
                name="Context7",
                transport="stdio",
                command=["npx", "-y", "@upstash/context7-mcp@1.0.31"],
                enabled=True,
            )
        ],
    )
    service = AgentService(settings=settings, client=FakeDirextalkClient())

    result = await service.invoke(
        "agent.runtime.inspect",
        {
            "model_profile": {
                "id": "deepseek:deepseek-v4-pro",
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "api_key": "sk-client-local",
                "context_window": 128,
                "max_output_tokens": 7368,
                "temperature": 0.2,
            }
        },
    )

    assert result["model"]["provider"] == "deepseek"
    assert result["model"]["model"] == "deepseek-v4-pro"
    assert result["model"]["context_window"] == 128
    assert result["model"]["max_output_tokens"] == 7368
    assert "api_key" not in result["model"]
    assert result["skills"] == []
    assert result["mcp_servers"][1]["runtime_status"] == "ready"
    assert result["mcp_servers"][1]["tool_count"] == 2
    assert "runtime_tools" in result


@pytest.mark.asyncio
async def test_agent_runtime_install_invokes_runtime_tool_installer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    async def fake_install(params):
        calls.append(params)
        return {"ok": True, "target": params["target"], "commands": []}

    monkeypatch.setattr(service_module, "install_runtime_tool", fake_install)
    service = AgentService(settings=AgentPluginSettings(), client=FakeDirextalkClient())

    result = await service.invoke("agent.runtime.install", {"target": "agent-reach-core", "channels": ["xiaohongshu"]})

    assert result["ok"] is True
    assert calls == [{"target": "agent-reach-core", "channels": ["xiaohongshu"]}]


@pytest.mark.asyncio
async def test_agent_chat_managed_skills_persist_by_config_revision(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_CONFIG_PATH", str(tmp_path / "runtime_config.json"))
    service = AgentService(settings=AgentPluginSettings(runtime_config_revision="rev1"), client=FakeDirextalkClient())

    installed = await service.invoke(
        "agent.skills.install",
        {
            "repo_url": "https://github.com/panniantong/agent-reach",
            "ref": "main",
            "path": "agent-reach",
            "enabled": True,
        },
    )
    assert installed["skills"][0]["path"] == "agent-reach"

    reloaded = AgentService(settings=AgentPluginSettings(runtime_config_revision="rev1"), client=FakeDirextalkClient())
    skills = await reloaded.invoke("agent.skills.list", {})
    assert skills["skills"][0]["path"] == "agent-reach"

    stale_revision = AgentService(settings=AgentPluginSettings(runtime_config_revision="rev2"), client=FakeDirextalkClient())
    skills = await stale_revision.invoke("agent.skills.list", {})
    assert skills["skills"] == []


@pytest.mark.asyncio
async def test_agent_can_uninstall_runtime_managed_skill(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_CONFIG_PATH", str(tmp_path / "runtime_config.json"))
    service = AgentService(settings=AgentPluginSettings(runtime_config_revision="rev1"), client=FakeDirextalkClient())
    await service.invoke(
        "agent.skills.install",
        {
            "repo_url": "https://github.com/panniantong/agent-reach",
            "ref": "main",
            "path": "agent-reach",
            "enabled": True,
        },
    )

    removed = await service.invoke("agent.skills.uninstall", {"path": "agent-reach"})

    assert removed["removed"] == 1
    assert removed["skills"] == []


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
