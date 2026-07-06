import pytest

from dirextalk_agent.service import AgentService
from dirextalk_plugins_runtime import AgentPluginSettings


class FakeDirextalkClient:
    pass


class RecordingRuntime:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.params: list[dict] = []

    async def chat(self, prompt: str, params: dict):
        self.prompts.append(prompt)
        self.params.append(params)
        return {"ok": True, "text": "answer"}

    async def stream_chat(self, prompt: str, params: dict):
        self.prompts.append(prompt)
        self.params.append(params)
        yield {"event": "done", "data": {"text": prompt}}


@pytest.mark.asyncio
async def test_knowledge_config_reports_first_version_unsupported() -> None:
    service = AgentService(
        settings=AgentPluginSettings(),
        client=FakeDirextalkClient(),
        runtime=RecordingRuntime(),
    )

    result = await service.invoke("agent.knowledge.config.get", {})

    assert result["ok"] is True
    assert result["supported"] is False
    assert result["enabled"] is False
    assert result["status"] == "unsupported"


@pytest.mark.asyncio
async def test_knowledge_enable_is_rejected_while_unsupported() -> None:
    service = AgentService(
        settings=AgentPluginSettings(),
        client=FakeDirextalkClient(),
        runtime=RecordingRuntime(),
    )

    with pytest.raises(ValueError, match="not supported"):
        await service.invoke("agent.knowledge.config.update", {"enabled": True})


@pytest.mark.asyncio
async def test_agent_chat_ignores_knowledge_params_while_unsupported() -> None:
    runtime = RecordingRuntime()
    service = AgentService(
        settings=AgentPluginSettings(),
        client=FakeDirextalkClient(),
        runtime=runtime,
    )

    result = await service.invoke(
        "agent.chat",
        {
            "prompt": "How should I deploy?",
            "knowledge_enabled": True,
            "embedding_profile": {
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "text-embedding-3-small",
                "api_key": "sk-test",
            },
        },
    )

    assert result["ok"] is True
    assert "knowledge_sources" not in result
    assert runtime.prompts == ["How should I deploy?"]
