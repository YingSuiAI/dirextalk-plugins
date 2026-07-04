import httpx
import pytest

from dirextalk_agent.llm import list_provider_models
from dirextalk_agent.registry import search_mcp_servers, search_skills


@pytest.mark.asyncio
async def test_list_provider_models_uses_openai_compatible_models_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://api.deepseek.com/models"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "deepseek-chat", "owned_by": "deepseek"},
                    {"id": "deepseek-reasoner"},
                ]
            },
        )

    transport = httpx.MockTransport(handler)

    result = await list_provider_models(
        provider="deepseek",
        base_url="https://api.deepseek.com",
        api_key="test-key",
        transport=transport,
    )

    assert result["models"] == [
        {"id": "deepseek-chat", "name": "deepseek-chat", "provider": "deepseek", "owned_by": "deepseek"},
        {"id": "deepseek-reasoner", "name": "deepseek-reasoner", "provider": "deepseek"},
    ]


@pytest.mark.asyncio
async def test_search_skills_uses_skills_api_registry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://skills.example/api/skills?query=browser&page=1&pageSize=10"
        return httpx.Response(
            200,
            json={
                "skills": [
                    {
                        "id": "browser",
                        "name": "Browser",
                        "description": "Browser testing skill",
                        "owner": "vercel-labs",
                        "repo": "agent-skills",
                        "path": "skills/browser",
                        "installs": 42,
                    }
                ],
                "pagination": {"page": 1, "pageSize": 10, "total": 1},
            },
        )

    transport = httpx.MockTransport(handler)

    result = await search_skills(
        registry_url="https://skills.example",
        query="browser",
        page_size=10,
        transport=transport,
    )

    assert result["skills"][0]["repo_url"] == "https://github.com/vercel-labs/agent-skills"
    assert result["skills"][0]["path"] == "skills/browser"
    assert result["pagination"]["total"] == 1


@pytest.mark.asyncio
async def test_search_skills_falls_back_to_skills_sh_legacy_search_api() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if request.url.path == "/api/skills":
            return httpx.Response(404)
        assert str(request.url) == "https://skills.sh/api/search?q=browser&limit=3"
        return httpx.Response(
            200,
            json={
                "query": "browser",
                "skills": [
                    {
                        "id": "vercel-labs/agent-browser/agent-browser",
                        "skillId": "agent-browser",
                        "name": "agent-browser",
                        "installs": 510319,
                        "source": "vercel-labs/agent-browser",
                    }
                ],
                "count": 1,
            },
        )

    transport = httpx.MockTransport(handler)

    result = await search_skills(
        registry_url="https://skills.sh",
        query="browser",
        page_size=3,
        transport=transport,
    )

    assert seen_urls == [
        "https://skills.sh/api/skills?query=browser&page=1&pageSize=3",
        "https://skills.sh/api/search?q=browser&limit=3",
    ]
    assert result["skills"][0]["repo_url"] == "https://github.com/vercel-labs/agent-browser"
    assert result["skills"][0]["path"] == "agent-browser"


@pytest.mark.asyncio
async def test_search_mcp_servers_normalizes_registry_packages_and_remotes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://registry.example/v0.1/servers?search=memory&version=latest&limit=10"
        return httpx.Response(
            200,
            json={
                "servers": [
                    {
                        "server": {
                            "name": "io.github.example/memory",
                            "title": "Example Memory",
                            "description": "Persistent memory",
                            "version": "1.2.3",
                            "packages": [
                                {
                                    "registryType": "npm",
                                    "identifier": "@example/memory-mcp",
                                    "version": "1.2.3",
                                    "transport": {"type": "stdio"},
                                }
                            ],
                        }
                    },
                    {
                        "server": {
                            "name": "com.example/remote",
                            "description": "Remote tools",
                            "version": "2.0.0",
                            "remotes": [
                                {
                                    "type": "streamable-http",
                                    "url": "https://mcp.example/mcp",
                                }
                            ],
                        }
                    },
                ],
                "metadata": {"count": 2},
            },
        )

    transport = httpx.MockTransport(handler)

    result = await search_mcp_servers(
        registry_url="https://registry.example",
        query="memory",
        limit=10,
        transport=transport,
    )

    assert result["servers"][0]["mcp_server"] == {
        "name": "Example Memory",
        "transport": "stdio",
        "enabled": True,
        "command": ["npx", "-y", "@example/memory-mcp@1.2.3"],
        "url": "",
        "timeout_ms": 30000,
        "tool_allowlist": [],
    }
    assert result["servers"][1]["mcp_server"]["transport"] == "streamable_http"
    assert result["servers"][1]["mcp_server"]["url"] == "https://mcp.example/mcp"
