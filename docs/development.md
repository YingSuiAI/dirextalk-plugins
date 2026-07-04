# Development Guide

## Business Context

Dirextalk plugins add optional private-deployment capabilities without changing the core homeserver into a tool runner. The base service keeps authentication, Matrix writes, product rules, and durable state. Plugins run separately and call explicit capability actions.

## Standards

- Keep every official plugin in `catalog/index.json`.
- Pin Docker images by sha256 digest before release.
- Keep remote skills disabled after install until the owner enables them.
- Do not execute remote skill installation scripts.
- Do not log API keys, owner tokens, agent tokens, or Matrix access tokens.

## Release Flow

1. Build the plugin image.
2. Push it to the `dirextalk` Docker organization.
3. Record the resulting digest in `plugins/<name>/plugin.yaml` and `catalog/index.json`.
4. Run tests and compose smoke checks.
5. Tag the repository release.

## DeepSeek Smoke Test

Use an environment variable; do not write the key into tracked files.

```bash
export DEEPSEEK_API_KEY='replace-with-key'
.venv/bin/python - <<'PY'
import asyncio
from dirextalk_agent.llm import PydanticAgentRuntime
from dirextalk_plugins_runtime import AgentPluginSettings, ModelSettings

class FakeDirextalkClient:
    pass

async def main():
    settings = AgentPluginSettings(
        model=ModelSettings(provider="deepseek", model="deepseek-chat"),
        enabled_tools=[],
    )
    result = await PydanticAgentRuntime(settings, FakeDirextalkClient()).chat(
        "用中文回答：请只返回四个字：连通正常",
        {
            "model_profile": {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "api_key": "<api-key>",
            }
        },
    )
    print(result["model_ready"], result["text"])

asyncio.run(main())
PY
```
