from dirextalk_agent.llm import pydantic_model_name
from dirextalk_plugins_runtime import AgentPluginSettings, MCPServerConfig, ModelProfileSettings, ModelSettings, SkillSource, settings_from_environment


def test_agent_settings_accept_multiple_model_providers() -> None:
    for provider in ["openai", "anthropic", "deepseek", "gemini", "vertex", "openai_compatible", "openrouter", "litellm"]:
        settings = AgentPluginSettings(model=ModelSettings(provider=provider, model="test-model"))
        assert settings.model.provider == provider


def test_deepseek_uses_provider_shorthand() -> None:
    settings = AgentPluginSettings(model=ModelSettings(provider="deepseek", model="deepseek-chat"))
    assert pydantic_model_name(settings) == "deepseek:deepseek-chat"


def test_remote_skills_default_disabled() -> None:
    skill = SkillSource(repo_url="https://github.com/YingSuiAI/example-skills", ref="abc123", path="skills/demo")
    assert skill.enabled is False


def test_mcp_server_config_has_tool_allowlist() -> None:
    server = MCPServerConfig(name="filesystem", transport="stdio", command=["npx", "server"], tool_allowlist=["read_file"])
    assert server.tool_allowlist == ["read_file"]


def test_settings_from_environment_accepts_deepseek(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "deepseek")
    monkeypatch.setenv("AGENT_MODEL", "deepseek-chat")
    monkeypatch.setenv("AGENT_API_KEY_REF", "env:DEEPSEEK_API_KEY")
    monkeypatch.setenv("DIREXTALK_BASE_URL", "http://message-server:8008")

    settings = settings_from_environment()

    assert settings.model.provider == "deepseek"
    assert settings.model.model == "deepseek-chat"
    assert settings.model.api_key_ref == "env:DEEPSEEK_API_KEY"


def test_settings_from_environment_accepts_model_profiles(monkeypatch) -> None:
    monkeypatch.setenv(
        "AGENT_MODEL_PROFILES_JSON",
        """
        [
          {
            "id": "work",
            "name": "Work",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "api_key_ref": "env:AGENT_PROFILE_API_KEY_WORK"
          }
        ]
        """,
    )
    monkeypatch.setenv("AGENT_DEFAULT_MODEL_PROFILE_ID", "work")

    settings = settings_from_environment()

    assert settings.default_model_profile_id == "work"
    assert settings.model_profiles == [
        ModelProfileSettings(
            id="work",
            name="Work",
            provider="deepseek",
            model="deepseek-chat",
            api_key_ref="env:AGENT_PROFILE_API_KEY_WORK",
        )
    ]
