from pathlib import Path

import pytest

from dirextalk_agent import runtime_tools as runtime_tools_module
from dirextalk_agent.runtime_tools import (
    agent_reach_executable,
    install_commands,
    install_commands_for_request,
    install_runtime_tool,
    list_runtime_tool_records,
    run_runtime_tool,
    runtime_path_env,
    uninstall_commands_for_request,
    uninstall_runtime_tool,
)


def test_runtime_path_env_uses_agent_data_volume(monkeypatch, tmp_path):
    tool_root = tmp_path / "tools"
    tool_bin = tmp_path / "bin"
    npm_prefix = tmp_path / "npm"
    monkeypatch.setenv("AGENT_TOOL_ROOT", str(tool_root))
    monkeypatch.setenv("AGENT_TOOL_BIN_DIR", str(tool_bin))
    monkeypatch.setenv("NPM_CONFIG_PREFIX", str(npm_prefix))
    monkeypatch.setenv("PATH", "/usr/bin")

    env = runtime_path_env()

    assert env["UV_TOOL_DIR"] == str(tool_root / "uv")
    assert env["UV_TOOL_BIN_DIR"] == str(tool_bin)
    assert env["NPM_CONFIG_PREFIX"] == str(npm_prefix)
    assert env["PATH"].split(":")[:3] == [
        str(tool_bin),
        str(npm_prefix / "bin"),
        str(Path.home() / ".local" / "bin"),
    ]
    assert tool_root.exists()
    assert tool_bin.exists()
    assert npm_prefix.exists()


def test_opencli_is_supported_as_agent_reach_install_target() -> None:
    assert install_commands(target="opencli", package="", channels=[]) == [
        [agent_reach_executable(), "install", "--env=auto", "--channels=opencli"]
    ]


def test_agent_reach_channel_defaults_to_opencli_when_channels_are_missing() -> None:
    assert install_commands(target="agent-reach-channel", package="", channels=[]) == [
        [agent_reach_executable(), "install", "--env=auto", "--channels=opencli"]
    ]


def test_general_install_managers_plan_commands() -> None:
    assert install_commands_for_request({"manager": "uv", "package": "ruff"}) == [
        ["uv", "tool", "install", "--force", "ruff"]
    ]
    assert install_commands_for_request({"manager": "npm", "package": "opencli"}) == [
        ["npm", "install", "-g", "opencli"]
    ]
    assert install_commands_for_request({"manager": "pip", "package": "httpie"}) == [
        ["python3", "-m", "pip", "install", "--upgrade", "httpie"]
    ]
    assert install_commands_for_request({"manager": "apt", "package": "jq curl"}) == [
        ["apt-get", "update"],
        ["apt-get", "install", "-y", "--no-install-recommends", "jq", "curl"],
    ]


def test_general_uninstall_managers_plan_commands() -> None:
    assert uninstall_commands_for_request({"manager": "uv", "package": "ruff"}) == [
        ["uv", "tool", "uninstall", "ruff"]
    ]
    assert uninstall_commands_for_request({"manager": "npm", "package": "opencli"}) == [
        ["npm", "uninstall", "-g", "opencli"]
    ]
    assert uninstall_commands_for_request({"manager": "pip", "package": "httpie"}) == [
        ["python3", "-m", "pip", "uninstall", "-y", "httpie"]
    ]
    assert uninstall_commands_for_request({"manager": "apt", "package": "jq curl"}) == [
        ["apt-get", "remove", "-y", "--auto-remove", "jq", "curl"]
    ]


@pytest.mark.asyncio
async def test_install_runtime_tool_records_custom_cli(monkeypatch, tmp_path) -> None:
    record_path = tmp_path / "runtime_tools.json"
    monkeypatch.setenv("AGENT_RUNTIME_TOOLS_RECORD_PATH", str(record_path))

    async def fake_run_command(command, **_kwargs):
        return {"command": command, "returncode": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(runtime_tools_module, "run_command", fake_run_command)

    result = await install_runtime_tool(
        {"manager": "npm", "package": "opencli", "command": "opencli"}
    )

    assert result["ok"] is True
    assert result["records"][0]["manager"] == "npm"
    assert result["records"][0]["package"] == "opencli"
    assert result["records"][0]["commands"] == ["opencli"]
    assert list_runtime_tool_records() == result["records"]


@pytest.mark.asyncio
async def test_uninstall_runtime_tool_removes_custom_cli_record(monkeypatch, tmp_path) -> None:
    record_path = tmp_path / "runtime_tools.json"
    monkeypatch.setenv("AGENT_RUNTIME_TOOLS_RECORD_PATH", str(record_path))
    record_path.write_text(
        '[{"id":"npm:opencli","manager":"npm","package":"opencli","commands":["opencli"],"target":"","ok":true}]',
        encoding="utf-8",
    )
    seen = []

    async def fake_run_command(command, **_kwargs):
        seen.append(command)
        return {"command": command, "returncode": 0, "stdout": "removed", "stderr": ""}

    monkeypatch.setattr(runtime_tools_module, "run_command", fake_run_command)

    result = await uninstall_runtime_tool({"id": "npm:opencli"})

    assert result["ok"] is True
    assert seen == [["npm", "uninstall", "-g", "opencli"]]
    assert result["records"] == []
    assert list_runtime_tool_records() == []


@pytest.mark.asyncio
async def test_run_runtime_tool_accepts_string_commands(monkeypatch) -> None:
    seen = []

    async def fake_run_command(command, **kwargs):
        seen.append((command, kwargs))
        return {"command": command, "returncode": 0, "stdout": "help", "stderr": ""}

    monkeypatch.setattr(runtime_tools_module, "run_command", fake_run_command)

    result = await run_runtime_tool({"command": "opencli --help", "timeout_seconds": 5})

    assert result["ok"] is True
    assert seen == [(["opencli", "--help"], {"timeout_seconds": 5})]
