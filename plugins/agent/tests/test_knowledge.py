import base64

import pytest

from dirextalk_agent.knowledge import KnowledgeStore
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


async def fake_embedder(_profile: dict, texts: list[str]) -> list[list[float]]:
    vectors = []
    for text in texts:
        lower = text.lower()
        vectors.append(
            [
                1.0 if "deploy" in lower or "backup" in lower else 0.0,
                1.0 if "invoice" in lower else 0.0,
                0.1,
            ]
        )
    return vectors


def embedding_profile() -> dict:
    return {
        "provider": "openai_compatible",
        "base_url": "https://embeddings.example/v1",
        "model": "text-embedding-3-small",
        "api_key": "sk-test",
    }


@pytest.mark.asyncio
async def test_knowledge_requires_embedding_profile_to_enable(tmp_path) -> None:
    service = AgentService(
        settings=AgentPluginSettings(),
        client=FakeDirextalkClient(),
        runtime=RecordingRuntime(),
        knowledge_store=KnowledgeStore(tmp_path),
        embedding_client=fake_embedder,
    )

    with pytest.raises(ValueError, match="embedding_profile is required"):
        await service.invoke("agent.knowledge.config.update", {"enabled": True})


@pytest.mark.asyncio
async def test_text_upload_indexes_and_searches_knowledge(tmp_path) -> None:
    service = AgentService(
        settings=AgentPluginSettings(),
        client=FakeDirextalkClient(),
        runtime=RecordingRuntime(),
        knowledge_store=KnowledgeStore(tmp_path),
        embedding_client=fake_embedder,
    )
    profile = embedding_profile()

    start = await service.invoke(
        "agent.knowledge.upload.start",
        {
            "filename": "runbook.md",
            "mime_type": "text/markdown",
            "size": 35,
        },
    )
    upload_id = start["upload_id"]
    await service.invoke(
        "agent.knowledge.upload.chunk",
        {
            "upload_id": upload_id,
            "data": base64.b64encode(b"Deployment backups run every night.").decode(),
        },
    )
    finish = await service.invoke(
        "agent.knowledge.upload.finish",
        {
            "upload_id": upload_id,
            "embedding_profile": profile,
        },
    )

    assert finish["source"]["status"] == "ready"
    result = await service.invoke(
        "agent.knowledge.search",
        {
            "query": "deploy backup",
            "embedding_profile": profile,
        },
    )
    assert result["results"][0]["source_title"] == "runbook.md"
    assert "Deployment backups" in result["results"][0]["text"]


@pytest.mark.asyncio
async def test_pdf_upload_requires_ocr_profile(tmp_path) -> None:
    service = AgentService(
        settings=AgentPluginSettings(),
        client=FakeDirextalkClient(),
        runtime=RecordingRuntime(),
        knowledge_store=KnowledgeStore(tmp_path),
        embedding_client=fake_embedder,
    )

    with pytest.raises(ValueError, match="ocr_profile is required"):
        await service.invoke(
            "agent.knowledge.upload.start",
            {
                "filename": "scan.pdf",
                "mime_type": "application/pdf",
                "size": 10,
            },
        )


@pytest.mark.asyncio
async def test_agent_chat_injects_knowledge_only_when_enabled(tmp_path) -> None:
    runtime = RecordingRuntime()
    service = AgentService(
        settings=AgentPluginSettings(),
        client=FakeDirextalkClient(),
        runtime=runtime,
        knowledge_store=KnowledgeStore(tmp_path),
        embedding_client=fake_embedder,
    )
    profile = embedding_profile()
    await service.invoke(
        "agent.knowledge.memory.create",
        {
            "title": "Ops memory",
            "text": "Deployment backups must be verified before restart.",
            "embedding_profile": profile,
        },
    )

    await service.invoke("agent.chat", {"prompt": "How should I deploy?"})
    assert "Knowledge Context" not in runtime.prompts[-1]

    result = await service.invoke(
        "agent.chat",
        {
            "prompt": "How should I deploy?",
            "knowledge_enabled": True,
            "embedding_profile": profile,
        },
    )
    assert "Knowledge Context" in runtime.prompts[-1]
    assert "Deployment backups" in runtime.prompts[-1]
    assert result["knowledge_sources"][0]["title"] == "Ops memory"
