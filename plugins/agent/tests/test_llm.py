import pytest

from dirextalk_agent.llm import (
    ModelInvocationUnavailable,
    PydanticAgentRuntime,
    ensure_model_ready,
    resolve_model_settings,
)
from dirextalk_plugins_runtime import AgentPluginSettings, ModelProfileSettings, ModelSettings


class FakeDirextalkClient:
    pass


def test_resolve_model_settings_selects_requested_profile() -> None:
    settings = AgentPluginSettings(
        model=ModelSettings(provider="openai", model="gpt-4.1", api_key_ref="env:OPENAI_API_KEY"),
        model_profiles=[
            ModelProfileSettings(
                id="work",
                name="Work",
                provider="deepseek",
                model="deepseek-chat",
                api_key_ref="env:DEEPSEEK_API_KEY",
            )
        ],
    )

    model = resolve_model_settings(settings, {"model_profile_id": "work"})

    assert model.provider == "deepseek"
    assert model.model == "deepseek-chat"
    assert model.api_key_ref == "env:DEEPSEEK_API_KEY"


def test_resolve_model_settings_accepts_request_profile_api_key() -> None:
    settings = AgentPluginSettings(
        model=ModelSettings(provider="openai", model="gpt-4.1"),
    )

    model = resolve_model_settings(
        settings,
        {
            "model_profile": {
                "id": "deepseek:deepseek-chat",
                "provider": "deepseek",
                "model": "deepseek-chat",
                "api_key": "sk-client-local",
                "temperature": 0.4,
            }
        },
    )

    assert model.provider == "deepseek"
    assert model.model == "deepseek-chat"
    assert model.api_key == "sk-client-local"
    ensure_model_ready(model)


def test_resolve_model_settings_uses_request_api_key_for_profile_fallback() -> None:
    settings = AgentPluginSettings(
        model=ModelSettings(provider="openai", model="gpt-4.1"),
    )

    model = resolve_model_settings(
        settings,
        {
            "api_key": "sk-client-local",
            "model_profile": {
                "id": "deepseek:deepseek-chat",
                "provider": "deepseek",
                "model": "deepseek-chat",
                "temperature": 0.4,
            },
        },
    )

    assert model.provider == "deepseek"
    assert model.model == "deepseek-chat"
    assert model.api_key == "sk-client-local"
    ensure_model_ready(model)


def test_create_agent_passes_temperature_and_token_settings() -> None:
    captured: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, model: str, **kwargs: object) -> None:
            captured["model"] = model
            captured.update(kwargs)

        def tool_plain(self, func: object) -> object:
            return func

    runtime = PydanticAgentRuntime(
        settings=AgentPluginSettings(
            enabled_tools=[],
            model=ModelSettings(
                provider="deepseek",
                model="deepseek-chat",
                temperature=0.7,
                max_output_tokens=4096,
                top_p=0.8,
                top_k=40,
            )
        ),
        client=FakeDirextalkClient(),
    )

    runtime._create_agent(FakeAgent, runtime.settings.model)

    assert captured["model"] == "deepseek:deepseek-chat"
    assert captured["model_settings"] == {
        "temperature": 0.7,
        "max_tokens": 4096,
        "top_p": 0.8,
        "top_k": 40,
    }


@pytest.mark.asyncio
async def test_runtime_reports_missing_api_key_before_model_import() -> None:
    settings = AgentPluginSettings(model=ModelSettings(provider="deepseek", model="deepseek-chat", api_key_ref=""))
    runtime = PydanticAgentRuntime(settings=settings, client=FakeDirextalkClient())

    with pytest.raises(ModelInvocationUnavailable, match="API key"):
        await runtime.chat("hello", {})
