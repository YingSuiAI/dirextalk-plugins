# AGENTS.md

This repository contains the official Dirextalk plugin base, shared runtime helpers, and first-party plugins. Treat it as a sibling project to `dirextalk-message-server`, not as part of the homeserver monolith.

## Project Scope

- `spec/` defines the public plugin contract, runtime expectations, Agent capability model, skills, and MCP boundaries.
- `runtime/python/` owns shared Python helpers used by official plugins.
- `plugins/agent/` owns the official Agent plugin implementation.
- `catalog/index.json` is the official plugin catalog consumed by the base service.
- `deploy/` contains optional compose snippets for private single-node deployments.

The message server remains the durable capability boundary for Matrix writes, product policy, owner and agent tokens, `mcp.*` body actions, and `mcp_blocked_room_ids`. Plugins must not connect directly to the homeserver database and must not store owner access tokens.

## Agent Runtime

The official Agent plugin uses Pydantic AI. Supported provider identifiers are:

- `openai`
- `anthropic`
- `deepseek`
- `gemini`
- `vertex`
- `openai_compatible`
- `openrouter`
- `litellm`

Use environment references for secrets, for example `AGENT_API_KEY_REF=env:DEEPSEEK_API_KEY`. Do not commit API keys, owner tokens, agent tokens, Matrix access tokens, or generated secret files.

## Development Workflow

Run commands from this repository root.

Common validation:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
pytest
python3 -m compileall runtime/python plugins/agent
python3 -m json.tool catalog/index.json >/dev/null
docker compose -f deploy/docker-compose.agent.yml config
```

DeepSeek connectivity can be tested with a temporary environment variable:

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
        model=ModelSettings(provider="deepseek", model="deepseek-chat", api_key_ref="env:DEEPSEEK_API_KEY"),
        enabled_tools=[],
    )
    result = await PydanticAgentRuntime(settings, FakeDirextalkClient()).chat("用中文回答：请只返回四个字：连通正常", {})
    print(result["model_ready"], result["text"])

asyncio.run(main())
PY
```

## Index And Memory

This project should keep local CodeGraph and MemPalace support available for future agent runs.

- Code index: `codegraph init /root/dirextalk/dirextalk-plugins`, then `codegraph sync /root/dirextalk/dirextalk-plugins` after edits.
- Memory wing: `mempalace init --yes --no-llm --auto-mine /root/dirextalk/dirextalk-plugins`, then `mempalace sync /root/dirextalk/dirextalk-plugins` after generated files or gitignore rules change.
- Local `.codegraph/` is generated and must stay untracked.

## Release Rules

- Pin every released Docker image by sha256 digest in both `plugins/<name>/plugin.yaml` and `catalog/index.json`.
- Keep remote skills disabled after installation until the owner enables them.
- Do not execute remote skill installation scripts.
- Keep plugin interfaces stable and document compatibility-impacting changes in `spec/` and `docs/`.
