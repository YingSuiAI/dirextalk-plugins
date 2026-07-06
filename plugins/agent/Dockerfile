FROM node:22-bookworm-slim AS node-runtime

FROM python:3.12-slim

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git unzip \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/bin/npm /usr/local/bin/npm
COPY --from=node-runtime /usr/local/bin/npx /usr/local/bin/npx
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
ENV AGENT_TOOL_ROOT="/var/lib/dirextalk-agent/tools" \
    AGENT_TOOL_BIN_DIR="/var/lib/dirextalk-agent/bin" \
    UV_TOOL_DIR="/var/lib/dirextalk-agent/tools/uv" \
    UV_TOOL_BIN_DIR="/var/lib/dirextalk-agent/bin" \
    NPM_CONFIG_PREFIX="/var/lib/dirextalk-agent/npm" \
    PATH="/var/lib/dirextalk-agent/bin:/var/lib/dirextalk-agent/npm/bin:/root/.local/bin:${PATH}"
COPY pyproject.toml README.md ./
COPY runtime/python ./runtime/python
COPY plugins/agent ./plugins/agent
RUN pip install --no-cache-dir ".[agent]"

ENV PYTHONPATH=/app/runtime/python:/app/plugins/agent
EXPOSE 8080
CMD ["uvicorn", "dirextalk_agent.app:app", "--host", "0.0.0.0", "--port", "8080"]
