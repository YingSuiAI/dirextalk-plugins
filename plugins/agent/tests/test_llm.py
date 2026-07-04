import pytest

from dirextalk_agent.llm import ModelInvocationUnavailable, PydanticAgentRuntime, resolve_model_settings
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


@pytest.mark.asyncio
async def test_runtime_reports_missing_api_key_before_model_import() -> None:
    settings = AgentPluginSettings(model=ModelSettings(provider="deepseek", model="deepseek-chat", api_key_ref=""))
    runtime = PydanticAgentRuntime(settings=settings, client=FakeDirextalkClient())

    with pytest.raises(ModelInvocationUnavailable, match="API key"):
        await runtime.chat("hello", {})
