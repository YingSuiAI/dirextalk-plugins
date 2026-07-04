from __future__ import annotations

from typing import Any

import httpx

DEFAULT_MCP_REGISTRY_URL = "https://registry.modelcontextprotocol.io"


def _registry_api_base(registry_url: str) -> str:
    base = registry_url.strip().rstrip("/")
    if not base:
        base = "https://skills.sh"
    if base.endswith("/api"):
        return base
    return base + "/api"


def _mcp_registry_base(registry_url: str) -> str:
    base = registry_url.strip().rstrip("/")
    return base or DEFAULT_MCP_REGISTRY_URL


async def search_skills(
    *,
    registry_url: str,
    query: str,
    page: int = 1,
    page_size: int = 20,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    page = max(1, int(page or 1))
    page_size = min(100, max(1, int(page_size or 20)))
    params = {
        "query": query.strip(),
        "page": str(page),
        "pageSize": str(page_size),
    }
    async with httpx.AsyncClient(timeout=20.0, transport=transport) as client:
        response = await client.get(f"{_registry_api_base(registry_url)}/skills", params=params)
        if response.status_code == 404:
            response = await client.get(
                f"{_registry_api_base(registry_url)}/search",
                params={"q": query.strip(), "limit": str(page_size)},
            )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("skills registry response must be an object")
    raw_skills = data.get("skills") or data.get("data") or data.get("items") or []
    skills = [_normalize_skill(item) for item in raw_skills if isinstance(item, dict)]
    return {
        "skills": skills,
        "pagination": data.get("pagination") or data.get("meta") or {},
        "registry_url": registry_url.strip().rstrip("/") or "https://skills.sh",
    }


async def search_mcp_servers(
    *,
    registry_url: str,
    query: str,
    limit: int = 20,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    limit = min(100, max(1, int(limit or 20)))
    base = _mcp_registry_base(registry_url)
    if base in {"local", "curated", "dirextalk_curated"}:
        return {
            "servers": _curated_mcp_servers(query.strip(), limit),
            "metadata": {"source": "dirextalk_curated"},
            "registry_url": base,
        }
    params = {
        "search": query.strip(),
        "version": "latest",
        "limit": str(limit),
    }
    async with httpx.AsyncClient(timeout=20.0, transport=transport) as client:
        response = await client.get(f"{base}/v0.1/servers", params=params)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("mcp registry response must be an object")
    servers = []
    for item in data.get("servers") or []:
        if not isinstance(item, dict):
            continue
        server = item.get("server")
        if not isinstance(server, dict):
            continue
        servers.extend(_normalize_mcp_server(server))
    if not servers:
        servers = _curated_mcp_servers(query.strip(), limit)
    return {
        "servers": servers[:limit],
        "metadata": data.get("metadata") or {},
        "registry_url": base,
    }


def _normalize_skill(item: dict[str, Any]) -> dict[str, Any]:
    owner = str(item.get("owner") or item.get("github_owner") or "").strip()
    repo = str(item.get("repo") or item.get("repository") or item.get("github_repo") or "").strip()
    source = str(item.get("source") or "").strip()
    if (not owner or not repo) and source.count("/") >= 1:
        source_parts = source.split("/")
        owner = owner or source_parts[0]
        repo = repo or source_parts[1]
    repo_url = str(item.get("repo_url") or item.get("repository_url") or "").strip()
    if not repo_url and owner and repo:
        repo_url = f"https://github.com/{owner}/{repo}"
    path = str(item.get("path") or item.get("skill_path") or item.get("directory") or "").strip()
    if not path:
        path = str(item.get("skillId") or "").strip()
    ref = str(item.get("ref") or item.get("branch") or item.get("default_branch") or "main").strip()
    return {
        "id": str(item.get("id") or item.get("skillId") or item.get("name") or path).strip(),
        "name": str(item.get("name") or item.get("id") or path).strip(),
        "description": str(item.get("description") or "").strip(),
        "repo_url": repo_url,
        "owner": owner,
        "repo": repo,
        "ref": ref or "main",
        "path": path,
        "installs": item.get("installs") or item.get("install_count") or 0,
        "enabled": False,
    }


def _normalize_mcp_server(server: dict[str, Any]) -> list[dict[str, Any]]:
    name = str(server.get("name") or "").strip()
    title = str(server.get("title") or name).strip()
    description = str(server.get("description") or "").strip()
    version = str(server.get("version") or "").strip()
    items: list[dict[str, Any]] = []
    for package in server.get("packages") or []:
        if not isinstance(package, dict):
            continue
        normalized = _mcp_server_from_package(package, title, version)
        if normalized:
            items.append(
                {
                    "id": _mcp_template_id(name, normalized),
                    "name": title,
                    "description": description,
                    "source": "registry",
                    "version": version,
                    "mcp_server": normalized,
                }
            )
    for remote in server.get("remotes") or []:
        if not isinstance(remote, dict):
            continue
        normalized = _mcp_server_from_remote(remote, title)
        if normalized:
            items.append(
                {
                    "id": _mcp_template_id(name, normalized),
                    "name": title,
                    "description": description,
                    "source": "registry",
                    "version": version,
                    "mcp_server": normalized,
                }
            )
    return items


def _mcp_server_from_package(package: dict[str, Any], title: str, version: str) -> dict[str, Any] | None:
    registry_type = str(package.get("registryType") or package.get("registry_type") or "").strip().lower()
    identifier = str(package.get("identifier") or package.get("name") or "").strip()
    package_version = str(package.get("version") or version).strip()
    transport = _mcp_transport_type(package.get("transport"))
    if transport != "stdio" or not identifier:
        return None
    if registry_type == "npm":
        command = ["npx", "-y", _package_with_version(identifier, package_version)]
    elif registry_type in {"pypi", "python"}:
        command = ["uvx", _python_package_with_version(identifier, package_version)]
    else:
        return None
    return {
        "name": title,
        "transport": "stdio",
        "enabled": True,
        "command": command,
        "url": "",
        "timeout_ms": 30000,
        "tool_allowlist": [],
    }


def _mcp_server_from_remote(remote: dict[str, Any], title: str) -> dict[str, Any] | None:
    url = str(remote.get("url") or "").strip()
    if not url:
        return None
    transport = _mcp_transport_type(remote)
    if transport == "stdio":
        transport = "streamable_http"
    return {
        "name": title,
        "transport": transport,
        "enabled": True,
        "command": [],
        "url": url,
        "timeout_ms": 30000,
        "tool_allowlist": [],
    }


def _mcp_transport_type(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("type")
    transport = str(value or "").strip().lower().replace("-", "_")
    if transport in {"streamable_http", "sse", "stdio"}:
        return transport
    if transport == "http":
        return "streamable_http"
    return "stdio"


def _package_with_version(identifier: str, version: str) -> str:
    if not version or identifier.endswith(f"@{version}"):
        return identifier
    return f"{identifier}@{version}"


def _python_package_with_version(identifier: str, version: str) -> str:
    if not version or "==" in identifier:
        return identifier
    return f"{identifier}=={version}"


def _mcp_template_id(name: str, server: dict[str, Any]) -> str:
    command = " ".join(server.get("command") or [])
    url = str(server.get("url") or "")
    source = command or url or name
    return source.replace(" ", "-").replace("/", "_")


def _curated_mcp_servers(query: str, limit: int) -> list[dict[str, Any]]:
    templates = [
        {
            "id": "dirextalk-curated-fetch",
            "name": "Fetch",
            "description": "Fetch web pages over HTTP through a Python MCP server.",
            "source": "dirextalk_curated",
            "version": "",
            "mcp_server": {
                "name": "Fetch",
                "transport": "stdio",
                "enabled": True,
                "command": ["uvx", "mcp-server-fetch"],
                "url": "",
                "timeout_ms": 30000,
                "tool_allowlist": [],
            },
        },
        {
            "id": "dirextalk-curated-memory",
            "name": "Memory",
            "description": "Persistent local memory using the official Node MCP server.",
            "source": "dirextalk_curated",
            "version": "",
            "mcp_server": {
                "name": "Memory",
                "transport": "stdio",
                "enabled": True,
                "command": ["npx", "-y", "@modelcontextprotocol/server-memory"],
                "url": "",
                "timeout_ms": 30000,
                "tool_allowlist": [],
            },
        },
    ]
    needle = query.lower()
    if needle:
        templates = [
            item
            for item in templates
            if needle in item["name"].lower() or needle in item["description"].lower()
        ]
    return templates[:limit]
