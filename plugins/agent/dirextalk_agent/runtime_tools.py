from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any


AGENT_REACH_ZIP = "https://github.com/Panniantong/agent-reach/archive/main.zip"
DEFAULT_TOOL_ROOT = Path("/var/lib/dirextalk-agent/tools")
DEFAULT_TOOL_BIN_DIR = Path("/var/lib/dirextalk-agent/bin")
DEFAULT_NPM_PREFIX = Path("/var/lib/dirextalk-agent/npm")


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
        "install_targets": [
            "agent-reach-core",
            "agent-reach-channel",
            "npm-global",
            "uv-tool",
            "pip-package",
            "apt-package",
        ],
    }


async def install_runtime_tool(params: dict[str, Any]) -> dict[str, Any]:
    target = str(params.get("target") or "").strip()
    if not target:
        raise ValueError("target is required")
    channels = params.get("channels") or []
    if isinstance(channels, str):
        channels = [item.strip() for item in channels.split(",") if item.strip()]
    if not isinstance(channels, list):
        raise ValueError("channels must be a list or comma-separated string")
    package = str(params.get("package") or "").strip()

    commands = install_commands(target=target, package=package, channels=[str(item).strip() for item in channels if str(item).strip()])
    results = []
    for command in commands:
        results.append(await run_command(command))
    return {"ok": all(item["returncode"] == 0 for item in results), "target": target, "commands": results, "status": runtime_tools_status()}


def install_commands(*, target: str, package: str, channels: list[str]) -> list[list[str]]:
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
            raise ValueError("channels are required for agent-reach-channel")
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


def agent_reach_executable() -> str:
	env = runtime_path_env()
	return shutil.which("agent-reach", path=env.get("PATH")) or str(Path(env["AGENT_TOOL_BIN_DIR"]) / "agent-reach")


async def run_command(command: list[str]) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=runtime_path_env(),
    )
    stdout, stderr = await process.communicate()
    return {
        "command": command,
        "returncode": process.returncode,
        "stdout": stdout.decode(errors="replace")[-12000:],
        "stderr": stderr.decode(errors="replace")[-12000:],
    }
