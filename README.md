# Dirextalk Official Plugins

Official plugin repository for Dirextalk private deployments.

This repository contains the plugin contract, shared Python runtime helpers, the first-party Agent plugin, catalog metadata, and Docker Compose snippets used by `dirextalk-message-server`.

## First Plugin

`io.dirextalk.agent` is the official Agent plugin. It runs as a separate container, uses Pydantic AI for model orchestration, discovers provider model lists, installs skills from skills.sh-compatible registries, including current `/api/search` fallback responses, installs third-party MCP servers from MCP Registry-compatible metadata, and calls Dirextalk through backend capability actions instead of reading or writing the homeserver database directly.

## Layout

- `spec/` - plugin manifest, skills, MCP, and capability interface documentation.
- `runtime/python/` - shared Python runtime helpers.
- `plugins/agent/` - official Agent plugin.
- `catalog/` - official plugin catalog consumed by the base server.
- `deploy/` - Docker Compose examples.
- `docs/` - development, release, operation, and recovery docs.

## Local Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
pytest
```
