from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import time
from pathlib import Path
from typing import Any


AGENT_REACH_ZIP = "https://github.com/Panniantong/agent-reach/archive/main.zip"
DEFAULT_TOOL_ROOT = Path("/var/lib/dirextalk-agent/tools")
DEFAULT_TOOL_BIN_DIR = Path("/var/lib/dirextalk-agent/bin")
DEFAULT_NPM_PREFIX = Path("/var/lib/dirextalk-agent/npm")
DEFAULT_RECORD_PATH = Path("/var/lib/dirextalk-agent/runtime_tools.json")
INSTALL_MANAGERS = ["uv", "npm", "pip", "apt", "git", "command"]
AGENT_REACH_CHANNELS = {
    "opencli": "opencli",
    "mcporter": "mcporter",
    "bili": "bili",
    "twitter": "twitter",
    "rdt": "rdt",
    "gh": "gh",
    "yt-dlp": "yt-dlp",
    "ytdlp": "yt-dlp",
}
DEFAULT_AGENT_REACH_CHANNEL = "opencli"


def runtime_path_env() -> dict[str, str]:
	env = os.environ.copy()
	tool_root = Path(env.get("AGENT_TOOL_ROOT") or DEFAULT_TOOL_ROOT)
	tool_bin = Path(env.get("AGENT_TOOL_BIN_DIR") or DEFAULT_TOOL_BIN_DIR)
	npm_prefix = Path(env.get("NPM_CONFIG_PREFIX") or DEFAULT_NPM_PREFIX)
	for path in (tool_root, tool_bin, npm_prefix):
		path.mkdir(parents=True, exist_ok=True)
	env.setdefault("AGENT_TOOL_ROOT", str(tool_root))
	env.setdefault("AGENT_TOOL_BIN_DIR", str(tool_bin))
	env.setdefault("UV_TOOL_DIR", str(tool_root / "uv"))
	env.setdefault("UV_TOOL_BIN_DIR", str(tool_bin))
	env.setdefault("NPM_CONFIG_PREFIX", str(npm_prefix))
	home_bin = str(Path.home() / ".local" / "bin")
	npm_bin = str(npm_prefix / "bin")
	current = env.get("PATH", "")
	prepend = [str(tool_bin), npm_bin, home_bin]
	parts = current.split(os.pathsep) if current else []
	for item in reversed(prepend):
		if item not in parts:
			parts.insert(0, item)
	env["PATH"] = os.pathsep.join(parts)
	return env


def runtime_tools_record_path() -> Path:
    return Path(os.environ.get("AGENT_RUNTIME_TOOLS_RECORD_PATH") or DEFAULT_RECORD_PATH)


def list_runtime_tool_records() -> list[dict[str, Any]]:
    path = runtime_tools_record_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def save_runtime_tool_records(records: list[dict[str, Any]]) -> None:
    path = runtime_tools_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def record_runtime_tool_install(params: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    manager = str(params.get("manager") or "").strip()
    package = str(params.get("package") or params.get("repo_url") or params.get("target") or "").strip()
    commands = parse_command_names(params.get("command") or params.get("commands") or [])
    record = {
        "id": runtime_tool_record_id(manager=manager, package=package, target=str(params.get("target") or "")),
        "manager": manager,
        "target": str(params.get("target") or "").strip(),
        "package": package,
        "commands": commands,
        "installed_at": int(time.time()),
        "ok": all(item.get("returncode") == 0 for item in results),
    }
    records = [item for item in list_runtime_tool_records() if item.get("id") != record["id"]]
    records.append(record)
    save_runtime_tool_records(records)
    return records


def remove_runtime_tool_record(params: dict[str, Any]) -> list[dict[str, Any]]:
    record_id = runtime_tool_record_id(
        manager=str(params.get("manager") or ""),
        package=str(params.get("package") or params.get("repo_url") or ""),
        target=str(params.get("target") or ""),
    )
    explicit_id = str(params.get("id") or "").strip()
    remove_ids = {item for item in [record_id, explicit_id] if item}
    records = [item for item in list_runtime_tool_records() if item.get("id") not in remove_ids]
    save_runtime_tool_records(records)
    return records


def runtime_tool_record_by_id(record_id: str) -> dict[str, Any]:
    record_id = record_id.strip()
    if not record_id:
        return {}
    for record in list_runtime_tool_records():
        if str(record.get("id") or "").strip() == record_id:
            return record
    return {}


def runtime_tool_record_id(*, manager: str, package: str, target: str) -> str:
    value = f"{manager}:{package or target}".strip(":")
    return value or "runtime-tool"


def runtime_tools_status() -> dict[str, Any]:
    commands = [
        "agent-reach",
        "opencli",
        "mcporter",
        "bili",
        "twitter",
        "rdt",
        "gh",
        "yt-dlp",
        "node",
        "npm",
        "uv",
        "python3",
    ]
    env = runtime_path_env()
    command_status = {
        command: {
            "installed": shutil.which(command, path=env.get("PATH")) is not None,
            "path": shutil.which(command, path=env.get("PATH")) or "",
        }
        for command in commands
    }
    return {
        "commands": command_status,
        "install_managers": INSTALL_MANAGERS,
        "install_targets": [
            "agent-reach-core",
            "agent-reach-channel",
            *AGENT_REACH_CHANNELS.keys(),
            "npm-global",
            "uv-tool",
            "pip-package",
            "apt-package",
        ],
        "records": list_runtime_tool_records(),
        "examples": [
            {"manager": "uv", "package": "ruff", "command": "ruff"},
            {"manager": "npm", "package": "opencli", "command": "opencli"},
            {"manager": "pip", "package": "httpie", "command": "http"},
            {"manager": "apt", "package": "jq", "command": "jq"},
        ],
    }


async def install_runtime_tool(params: dict[str, Any]) -> dict[str, Any]:
    target = str(params.get("target") or "").strip()
    manager = str(params.get("manager") or "").strip()
    if not target and not manager:
        raise ValueError("target or manager is required")
    channels = normalize_channels(params.get("channels") or [])
    if not isinstance(channels, list):
        raise ValueError("channels must be a list or comma-separated string")
    package = str(params.get("package") or "").strip()

    request = {**params, "target": target, "manager": manager, "package": package, "channels": channels}
    commands = install_commands_for_request(request)
    results = []
    for command in commands:
        results.append(await run_command(command))
    records = record_runtime_tool_install(request, results) if all(item["returncode"] == 0 for item in results) else list_runtime_tool_records()
    return {
        "ok": all(item["returncode"] == 0 for item in results),
        "target": target,
        "manager": manager,
        "package": package,
        "commands": results,
        "records": records,
        "status": runtime_tools_status(),
    }


async def uninstall_runtime_tool(params: dict[str, Any]) -> dict[str, Any]:
    record = runtime_tool_record_by_id(str(params.get("id") or ""))
    request = {**record}
    for key, value in params.items():
        if value not in (None, ""):
            request[key] = value
    target = str(request.get("target") or "").strip()
    manager = str(request.get("manager") or "").strip()
    if not target and not manager:
        raise ValueError("id, target, or manager is required")
    request = {**request, "target": target, "manager": manager}
    commands = uninstall_commands_for_request(request)
    results = []
    for command in commands:
        results.append(await run_command(command))
    ok = all(item["returncode"] == 0 for item in results)
    records = remove_runtime_tool_record(request) if ok else list_runtime_tool_records()
    return {
        "ok": ok,
        "target": target,
        "manager": normalize_manager(manager),
        "package": str(request.get("package") or "").strip(),
        "commands": results,
        "records": records,
        "status": runtime_tools_status(),
    }


def install_commands_for_request(params: dict[str, Any]) -> list[list[str]]:
    manager = normalize_manager(str(params.get("manager") or ""))
    if manager:
        package = str(params.get("package") or "").strip()
        return manager_install_commands(manager=manager, package=package, params=params)
    target = str(params.get("target") or "").strip()
    channels = normalize_channels(params.get("channels") or [])
    if not isinstance(channels, list):
        raise ValueError("channels must be a list or comma-separated string")
    return install_commands(target=target, package=str(params.get("package") or "").strip(), channels=channels)


def uninstall_commands_for_request(params: dict[str, Any]) -> list[list[str]]:
    manager = normalize_manager(str(params.get("manager") or ""))
    if manager:
        package = str(params.get("package") or "").strip()
        return manager_uninstall_commands(manager=manager, package=package, params=params)
    target = str(params.get("target") or "").strip()
    channels = normalize_channels(params.get("channels") or [])
    if not isinstance(channels, list):
        raise ValueError("channels must be a list or comma-separated string")
    return uninstall_commands(target=target, package=str(params.get("package") or "").strip(), channels=channels)


def manager_install_commands(*, manager: str, package: str, params: dict[str, Any]) -> list[list[str]]:
    if manager == "uv":
        if not package:
            raise ValueError("package is required for manager=uv")
        return [["uv", "tool", "install", "--force", package]]
    if manager == "npm":
        if not package:
            raise ValueError("package is required for manager=npm")
        return [["npm", "install", "-g", package]]
    if manager == "pip":
        if not package:
            raise ValueError("package is required for manager=pip")
        return [["python3", "-m", "pip", "install", "--upgrade", package]]
    if manager == "apt":
        if not package:
            raise ValueError("package is required for manager=apt")
        packages = [item for item in package.split() if item]
        return [["apt-get", "update"], ["apt-get", "install", "-y", "--no-install-recommends", *packages]]
    if manager == "git":
        repo_url = str(params.get("repo_url") or params.get("repo") or package).strip()
        if not repo_url:
            raise ValueError("repo_url or package is required for manager=git")
        name = str(params.get("name") or Path(repo_url.rstrip("/")).stem.replace(".git", "")).strip() or "repo"
        dest = str(Path(runtime_path_env()["AGENT_TOOL_ROOT"]) / "git" / name)
        commands = [["git", "clone", "--depth", "1", repo_url, dest]]
        install_command = parse_command(params.get("install_command") or [])
        if install_command:
            commands.append(install_command)
        return commands
    if manager == "command":
        command = parse_command(params.get("install_command") or params.get("command") or [])
        if not command:
            raise ValueError("install_command or command is required for manager=command")
        return [command]
    raise ValueError(f"unknown install manager {manager}")


def manager_uninstall_commands(*, manager: str, package: str, params: dict[str, Any]) -> list[list[str]]:
    if manager == "uv":
        name = str(params.get("name") or package).strip()
        if not name:
            raise ValueError("package or name is required for manager=uv")
        return [["uv", "tool", "uninstall", name]]
    if manager == "npm":
        if not package:
            raise ValueError("package is required for manager=npm")
        return [["npm", "uninstall", "-g", package]]
    if manager == "pip":
        if not package:
            raise ValueError("package is required for manager=pip")
        return [["python3", "-m", "pip", "uninstall", "-y", package]]
    if manager == "apt":
        if not package:
            raise ValueError("package is required for manager=apt")
        packages = [item for item in package.split() if item]
        return [["apt-get", "remove", "-y", "--auto-remove", *packages]]
    if manager == "git":
        repo_url = str(params.get("repo_url") or params.get("repo") or package).strip()
        name = str(params.get("name") or Path(repo_url.rstrip("/")).stem.replace(".git", "")).strip() or "repo"
        dest = str(Path(runtime_path_env()["AGENT_TOOL_ROOT"]) / "git" / name)
        return [["rm", "-rf", dest]]
    if manager == "command":
        command = parse_command(params.get("uninstall_command") or params.get("command") or [])
        if not command:
            raise ValueError("uninstall_command or command is required for manager=command")
        return [command]
    raise ValueError(f"unknown uninstall manager {manager}")


def install_commands(*, target: str, package: str, channels: list[str]) -> list[list[str]]:
    target, package, channels = normalize_install_request(target=target, package=package, channels=channels)
    if target == "agent-reach-core":
        commands = [
            ["uv", "tool", "install", "--force", AGENT_REACH_ZIP],
            [agent_reach_executable(), "install", "--env=auto"],
        ]
        if channels:
            commands.append([agent_reach_executable(), "install", "--env=auto", "--channels=" + ",".join(channels)])
        return commands
    if target == "agent-reach-channel":
        if not channels:
            channels = [DEFAULT_AGENT_REACH_CHANNEL]
        return [[agent_reach_executable(), "install", "--env=auto", "--channels=" + ",".join(channels)]]
    if target == "npm-global":
        if not package:
            raise ValueError("package is required for npm-global")
        return [["npm", "install", "-g", package]]
    if target == "uv-tool":
        if not package:
            raise ValueError("package is required for uv-tool")
        return [["uv", "tool", "install", "--force", package]]
    if target == "pip-package":
        if not package:
            raise ValueError("package is required for pip-package")
        return [["python3", "-m", "pip", "install", "--upgrade", package]]
    if target == "apt-package":
        if not package:
            raise ValueError("package is required for apt-package")
        packages = [item for item in package.split() if item]
        return [["apt-get", "update"], ["apt-get", "install", "-y", "--no-install-recommends", *packages]]
    raise ValueError(f"unknown install target {target}")


def uninstall_commands(*, target: str, package: str, channels: list[str]) -> list[list[str]]:
    target, package, channels = normalize_install_request(target=target, package=package, channels=channels)
    if target == "agent-reach-core":
        commands = []
        if shutil.which("agent-reach", path=runtime_path_env().get("PATH")):
            commands.append([agent_reach_executable(), "uninstall", "--env=auto"])
        commands.append(["uv", "tool", "uninstall", "agent-reach"])
        return commands
    if target == "agent-reach-channel":
        if not channels:
            channels = [DEFAULT_AGENT_REACH_CHANNEL]
        return [[agent_reach_executable(), "uninstall", "--env=auto", "--channels=" + ",".join(channels)]]
    if target == "npm-global":
        if not package:
            raise ValueError("package is required for npm-global")
        return [["npm", "uninstall", "-g", package]]
    if target == "uv-tool":
        if not package:
            raise ValueError("package is required for uv-tool")
        return [["uv", "tool", "uninstall", package]]
    if target == "pip-package":
        if not package:
            raise ValueError("package is required for pip-package")
        return [["python3", "-m", "pip", "uninstall", "-y", package]]
    if target == "apt-package":
        if not package:
            raise ValueError("package is required for apt-package")
        packages = [item for item in package.split() if item]
        return [["apt-get", "remove", "-y", "--auto-remove", *packages]]
    raise ValueError(f"unknown uninstall target {target}")


def normalize_install_request(*, target: str, package: str, channels: list[str]) -> tuple[str, str, list[str]]:
    target_key = install_key(target)
    normalized_channels = normalize_channels(channels)
    if target_key in {"agentreach", "agentreachcore"}:
        return "agent-reach-core", package, normalized_channels
    channel = canonical_agent_reach_channel(target_key)
    if channel:
        return "agent-reach-channel", package, merge_channels([channel], normalized_channels)
    if target_key == "agentreachchannel":
        if not normalized_channels:
            package_channel = canonical_agent_reach_channel(install_key(package))
            if package_channel:
                normalized_channels = [package_channel]
                package = ""
        if not normalized_channels:
            normalized_channels = [DEFAULT_AGENT_REACH_CHANNEL]
        return "agent-reach-channel", package, normalized_channels
    return target, package, normalized_channels


def normalize_channels(channels: Any) -> list[str]:
    if isinstance(channels, str):
        raw = [item.strip() for item in channels.split(",") if item.strip()]
    elif isinstance(channels, list):
        raw = [str(item).strip() for item in channels if str(item).strip()]
    else:
        return channels
    normalized: list[str] = []
    for item in raw:
        normalized.append(canonical_agent_reach_channel(install_key(item)) or item)
    return merge_channels([], normalized)


def merge_channels(primary: list[str], secondary: list[str]) -> list[str]:
    result: list[str] = []
    for item in [*primary, *secondary]:
        if item and item not in result:
            result.append(item)
    return result


def canonical_agent_reach_channel(value: str) -> str:
    return AGENT_REACH_CHANNELS.get(value, "")


def install_key(value: str) -> str:
    return value.strip().lower().replace("-", "").replace("_", "")


def normalize_manager(value: str) -> str:
    key = value.strip().lower().replace("_", "-")
    aliases = {
        "uv-tool": "uv",
        "uvtool": "uv",
        "npm-global": "npm",
        "npmglobal": "npm",
        "pip-package": "pip",
        "pippackage": "pip",
        "apt-package": "apt",
        "aptpackage": "apt",
        "shell": "command",
        "cmd": "command",
    }
    key = aliases.get(key, key)
    return key if key in INSTALL_MANAGERS else ""


def parse_command(value: Any) -> list[str]:
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def parse_command_names(value: Any) -> list[str]:
    command = parse_command(value)
    if command:
        return [command[0]]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


async def run_runtime_tool(params: dict[str, Any]) -> dict[str, Any]:
    command = parse_command(params.get("command") or params.get("args") or [])
    if not command:
        raise ValueError("command is required")
    timeout_seconds = int(params.get("timeout_seconds") or params.get("timeout") or 60)
    result = await run_command(command, timeout_seconds=timeout_seconds)
    return {"ok": result["returncode"] == 0, **result}


def which_runtime_tool(command: str) -> dict[str, Any]:
    command = command.strip()
    if not command:
        raise ValueError("command is required")
    env = runtime_path_env()
    path = shutil.which(command, path=env.get("PATH")) or ""
    return {"ok": bool(path), "command": command, "path": path}


def agent_reach_executable() -> str:
	env = runtime_path_env()
	return shutil.which("agent-reach", path=env.get("PATH")) or str(Path(env["AGENT_TOOL_BIN_DIR"]) / "agent-reach")


async def run_command(command: list[str], *, timeout_seconds: int = 120) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=runtime_path_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=max(1, timeout_seconds))
    except TimeoutError:
        process.kill()
        stdout, stderr = await process.communicate()
        return {
            "command": command,
            "returncode": -1,
            "stdout": stdout.decode(errors="replace")[-12000:],
            "stderr": (stderr.decode(errors="replace") + f"\ncommand timed out after {timeout_seconds}s")[-12000:],
        }
    return {
        "command": command,
        "returncode": process.returncode,
        "stdout": stdout.decode(errors="replace")[-12000:],
        "stderr": stderr.decode(errors="replace")[-12000:],
    }
