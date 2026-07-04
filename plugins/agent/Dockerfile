FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY runtime/python ./runtime/python
COPY plugins/agent ./plugins/agent
RUN pip install --no-cache-dir .

ENV PYTHONPATH=/app/runtime/python:/app/plugins/agent
EXPOSE 8080
CMD ["uvicorn", "dirextalk_agent.app:app", "--host", "0.0.0.0", "--port", "8080"]

