# Agent Runtime Contract

The official Agent plugin owns model orchestration, skills, MCP servers, and standard MCP protocol handling.

The message server remains the capability boundary:

- Agent Matrix bootstrap uses `agent.matrix_session.create`.
- Dirextalk tools call existing `mcp.*` body actions.
- The plugin must not connect directly to the core Matrix/Dirextalk database.
- The plugin must not store owner access tokens.

## Model Providers

The first version supports these provider identifiers:

- `openai`
- `anthropic`
- `deepseek`
- `gemini`
- `vertex`
- `openai_compatible`
- `openrouter`
- `litellm`

## Skills

Skills may be installed from GitHub by pinning `repo_url`, `ref`, and `path`. Installed remote skills default to disabled. The plugin reads manifests and static content only; it must not execute remote installation scripts.

## MCP Servers

External MCP server configs support `stdio`, `streamable_http`, and `sse` transports. Each server has its own enabled flag, timeout, and tool allowlist.
