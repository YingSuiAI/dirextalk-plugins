# Agent Knowledge Base Deferred Plan

## Business Background

Knowledge base code is retained inside the official `io.dirextalk.agent`
plugin repository, but the first production release marks it unsupported. The
runtime returns `supported=false`, rejects enabling/upload/write actions, does
not load vector/index dependencies, and clients hide knowledge UI by default.

The deferred design targets single-user private deployments where the server is
small and should not run local LLM, embedding, reranker, OCR, or standalone
vector database services.

When enabled in a future release, the feature will let users upload documents or add manual memory, then opt into
knowledge retrieval for Agent conversations. Model API keys stay client-local:
the client sends the selected chat model and embedding/OCR profiles only with
the current plugin request.

## Current Release Status

- `agent.knowledge.config.get` and `agent.knowledge.status` return
  `supported=false`, `enabled=false`, and `status="unsupported"`.
- `agent.knowledge.sources.list` and `agent.knowledge.search` return empty
  result lists with the same unsupported status.
- Enabling, upload, delete, and memory-write actions are rejected with a clear
  unsupported error.
- Production Agent and Ops images do not install the `knowledge` dependency
  extra, so LanceDB and Postgres knowledge client packages stay out of the
  first-version runtime.
- Flutter keeps the knowledge code path in place but does not render or invoke
  it while support is disabled.

## Architecture

- Chat generation still uses the existing Agent model profile.
- Knowledge indexing requires an `embedding_profile` with provider, base URL,
  model, and API key.
- PDF and image uploads require an `ocr_profile`; without it, only text-like
  uploads and manual memory are accepted.
- Metadata is stored in the same Postgres service when
  `AGENT_KNOWLEDGE_DATABASE_URL` is provided. Table names use the
  `agent_knowledge_` prefix.
- If Postgres is not configured, the plugin falls back to a local SQLite file
  in the plugin volume for development and tests.
- Vector rows are stored in LanceDB under the Agent plugin data volume, with a
  JSON-vector fallback search path when LanceDB is unavailable.
- The message server does not persist model API keys and does not inject model
  keys into plugin container environment variables.

## Storage Layout

Runtime environment:

- `AGENT_KNOWLEDGE_DIR=/var/lib/dirextalk-agent/knowledge`
- `AGENT_KNOWLEDGE_DATABASE_URL=postgres://...`

Plugin volume:

- `/var/lib/dirextalk-agent/knowledge/files`
- `/var/lib/dirextalk-agent/knowledge/uploads`
- `/var/lib/dirextalk-agent/knowledge/lancedb`
- `/var/lib/dirextalk-agent/knowledge/knowledge.sqlite3` only when Postgres is
  not configured

Postgres tables:

- `agent_knowledge_config`
- `agent_knowledge_sources`
- `agent_knowledge_chunks`

## Action Flow

Configuration:

- `agent.knowledge.config.get`
- `agent.knowledge.config.update`
- `agent.knowledge.status`

Sources:

- `agent.knowledge.sources.list`
- `agent.knowledge.sources.delete`

Chunked upload:

- `agent.knowledge.upload.start`
- `agent.knowledge.upload.chunk`
- `agent.knowledge.upload.finish`

Manual memory and retrieval:

- `agent.knowledge.memory.create`
- `agent.knowledge.search`

Future conversation flow:

When support is re-enabled and the client sends `knowledge_enabled=true`, the
Agent plugin will embed the current prompt, search knowledge chunks, prepend a
compact `Knowledge Context` section to the model prompt, and return
`knowledge_sources` in the stream `done` event so the client can show file-name
references.

## Resource Policy

- Single indexing lock; no parallel indexing jobs.
- Default max file size: 20 MB.
- Default total source bytes: 500 MB.
- Default max chunk count: 50,000.
- Upload chunk size: 256 KB.
- Idle cache release: 10 minutes.
- No local model or OCR process is started by this release.

## Verification Target

- Plugin tests cover the unsupported first-version behavior.
- Message-server tests cover Agent knowledge action allowlist, Agent data
  volume validation, runtime env, and client-local model-key behavior.
- Flutter tests cover that first-version chat requests do not send knowledge
  parameters even if old local knowledge settings exist.
