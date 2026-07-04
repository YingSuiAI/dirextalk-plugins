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

`agent.models.list` discovers provider models from the configured provider API address and API key. OpenAI-compatible providers use `/models`; provider-specific headers are handled by the Agent runtime.

## Skills

Skills may be installed from GitHub by pinning `repo_url`, `ref`, and `path`. `agent.skills.registry.search` defaults to `https://skills.sh`, tries `/api/skills`, and falls back to the current public `/api/search` shape when needed. Both response formats are normalized into pinned fields. Installed remote skills default to disabled. The plugin reads manifests and static content only; it must not execute remote installation scripts.

## MCP Servers

External MCP server configs support `stdio`, `streamable_http`, and `sse` transports. Each server has its own enabled flag, timeout, and tool allowlist.

`agent.mcp.servers.list` always includes the locked Dirextalk built-in MCP adapter first. It exposes backend capability tools such as contact search, room search, message list/send, member list, and channel post/comment access by calling fixed `mcp.*` body actions with the Agent token.

`agent.mcp.registry.search` defaults to the official/compatible MCP Registry API at `https://registry.modelcontextprotocol.io`. Registry package entries are converted to Agent container launch configs:

- npm package metadata becomes `stdio` command `npx -y <package>@<version>`.
- PyPI/Python package metadata becomes `stdio` command `uvx <package>==<version>`.
- remote server metadata becomes `streamable_http` or `sse` config.

The Agent Docker image includes Node/npm/npx and Python `uv`/`uvx` so registry-installed MCP servers can run inside the Agent container. Third-party MCP servers stay inside the Agent plugin boundary; they do not run in `dirextalk-message-server` and do not receive owner tokens.
