from pathlib import Path

from dirextalk_agent.runtime_tools import runtime_path_env


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
