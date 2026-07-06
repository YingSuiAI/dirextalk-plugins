FROM docker:28-cli AS docker-cli

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml /app/pyproject.toml
COPY runtime/python /app/runtime/python
COPY plugins/ops /app/plugins/ops

RUN pip install --no-cache-dir ".[ops]"

EXPOSE 8080
CMD ["uvicorn", "dirextalk_ops.app:app", "--host", "0.0.0.0", "--port", "8080"]
