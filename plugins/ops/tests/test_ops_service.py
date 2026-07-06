from __future__ import annotations

import base64
import asyncio
import json
import tarfile
from pathlib import Path

import pytest

from dirextalk_ops.service import OpsService, OpsSettings


class FakeDockerExecutor:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.restore_payloads: list[bytes] = []

    async def json_lines(self, args: list[str]) -> list[dict[str, object]]:
        self.commands.append(args)
        if args[:2] == ["ps", "-a"]:
            return [
                {
                    "ID": "abc",
                    "Names": "dirextalk-p2p-message-server-1",
                    "Image": "dirextalk/message-server:latest",
                    "State": "running",
                    "Status": "Up 1 hour",
                    "Size": "10MB (virtual 200MB)",
                },
                {
                    "ID": "def",
                    "Names": "dirextalk-plugin-stopped",
                    "Image": "dirextalk/agent-plugin:latest",
                    "State": "exited",
                    "Status": "Exited",
                    "Size": "6MB (virtual 150MB)",
                }
            ]
        if args[:2] == ["stats", "--no-stream"]:
            return [
                {
                    "Name": "dirextalk-p2p-message-server-1",
                    "CPUPerc": "2.5%",
                    "MemUsage": "128MiB / 2GiB",
                    "MemPerc": "6.25%",
                }
            ]
        return []

    async def text(self, args: list[str]) -> str:
        return (await self.bytes(args)).decode()

    async def bytes(self, args: list[str], input_data: bytes | None = None) -> bytes:
        self.commands.append(args)
        if args and args[0] == "logs":
            return b"line one\nline two\n"
        if args[:2] == ["exec", "-e"] and "pg_dumpall" in args:
            return b"-- real pg dump\nCREATE TABLE dirextalk_test(id int);\n"
        if args[:2] == ["exec", "-i"] and "psql" in args:
            self.restore_payloads.append(input_data or b"")
            return b"RESTORE OK\n"
        if args[:2] == ["exec", "dirextalk-p2p-message-server-1"]:
            return b"4096\t/var/dirextalk-message-server/media\n2048\t/tmp\n"
        return b""


def make_service(tmp_path: Path) -> OpsService:
    return OpsService(
        settings=OpsSettings(
            backup_root=tmp_path / "backups",
            max_backups=2,
            message_server_container="dirextalk-p2p-message-server-1",
            postgres_container="dirextalk-p2p-postgres-1",
        ),
        executor=FakeDockerExecutor(),
    )


@pytest.mark.asyncio
async def test_status_collects_host_containers_and_backup_summary(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    backup = service.settings.backup_root / "old.tar.gz"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"backup-bytes")

    result = await service.invoke("ops.status.get", {})

    assert result["ok"] is True
    assert result["server"]["cpu_count"] >= 1
    assert result["disk"]["root"]["total_bytes"] > 0
    assert result["backups"]["count"] == 1
    assert result["containers"][0]["name"] == "dirextalk-p2p-message-server-1"


@pytest.mark.asyncio
async def test_backup_create_writes_manifest_and_downloads_chunks(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    created = await service.invoke("ops.backup.create", {"scope": "full", "confirm": "create_backup"})
    backup_id = str(created["backup"]["backup_id"])
    backup_path = Path(str(created["backup"]["path"]))
    assert backup_path.exists()
    assert created["manifest"]["scope"] == "full"
    assert created["manifest"]["includes"]["postgres_dump"] is True
    assert created["manifest"]["includes"]["plugin_state"] is True
    with tarfile.open(backup_path, "r:gz") as tar:
        dump = tar.extractfile("postgres.sql")
        assert dump is not None
        assert b"CREATE TABLE dirextalk_test" in dump.read()

    chunk = await service.invoke("ops.backup.download_chunk", {"backup_id": backup_id, "offset": 0, "limit": 64})
    data = base64.b64decode(str(chunk["data_base64"]))
    assert data.startswith(b"\x1f\x8b")
    assert chunk["next_offset"] > 0
    assert "sha256" in chunk

    listed = await service.invoke("ops.backups.list", {})
    assert listed["backups"][0]["backup_id"] == backup_id
    assert listed["backups"][0]["size_bytes"] == backup_path.stat().st_size


@pytest.mark.asyncio
async def test_backup_async_job_reports_progress(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    started = await service.invoke("ops.backup.create", {"scope": "full", "confirm": "create_backup", "async": True})
    job_id = started["job"]["job_id"]
    for _ in range(20):
        await asyncio.sleep(0.01)
        status = await service.invoke("ops.backup.status", {"job_id": job_id})
        if status["job"]["state"] == "completed":
            break
    else:
        pytest.fail("backup job did not complete")

    assert status["job"]["progress"] == 1.0
    assert status["job"]["backup"]["size_bytes"] > 0


@pytest.mark.asyncio
async def test_cleanup_run_requires_plan_confirmation_and_recent_backup(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    temp_file = service.settings.backup_root.parent / "tmp" / "stale.tmp"
    temp_file.parent.mkdir(parents=True)
    temp_file.write_text("temp")

    plan = await service.invoke("ops.cleanup.plan", {"targets": ["temp"], "before_days": 0})

    with pytest.raises(ValueError, match="confirm"):
        await service.invoke("ops.cleanup.run", {"plan_id": plan["plan_id"]})

    with pytest.raises(ValueError, match="recent backup"):
        await service.invoke("ops.cleanup.run", {"plan_id": plan["plan_id"], "confirm": "run_cleanup"})

    await service.invoke("ops.backup.create", {"scope": "full", "confirm": "create_backup"})
    result = await service.invoke("ops.cleanup.run", {"plan_id": plan["plan_id"], "confirm": "run_cleanup"})
    assert result["ok"] is True
    assert result["executed"][0]["target"] == "temp"
    assert not temp_file.exists()


@pytest.mark.asyncio
async def test_cleanup_plan_estimates_media_and_stopped_plugins(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    plan = await service.invoke("ops.cleanup.plan", {"targets": ["plugin_stopped", "media_cache"], "before_days": 90})

    assert plan["estimated_bytes"] >= 6_006_144
    assert any(step["target"] == "plugin_stopped" for step in plan["steps"])
    assert any(step["target"] == "media_cache" for step in plan["steps"])


@pytest.mark.asyncio
async def test_room_cleanup_plan_never_physically_purges_matrix_events(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    plan = await service.invoke(
        "ops.rooms.cleanup.plan",
        {
            "room_id": "!room:example.com",
            "before_days": 90,
            "targets": ["chat_cache", "chat_hide", "chat_purge_physical", "media_cache"],
        },
    )

    assert "chat_purge_physical" in plan["rejected_targets"]
    assert all(step["target"] != "chat_purge_physical" for step in plan["steps"])
    assert plan["risk"] == "medium"
    assert plan["estimated_bytes"] > 0


@pytest.mark.asyncio
async def test_restore_run_replays_postgres_dump(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    created = await service.invoke("ops.backup.create", {"scope": "full", "confirm": "create_backup"})
    backup_id = str(created["backup"]["backup_id"])

    restored = await service.invoke("ops.restore.run", {"backup_id": backup_id, "confirm": "restore_backup"})

    assert restored["ok"] is True
    executor = service.executor
    assert isinstance(executor, FakeDockerExecutor)
    assert executor.restore_payloads
    assert b"CREATE TABLE dirextalk_test" in executor.restore_payloads[0]


@pytest.mark.asyncio
async def test_media_orphans_plan_is_preview_only(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    plan = await service.invoke("ops.media.orphans.plan", {"min_age_days": 30})

    assert plan["ok"] is True
    assert plan["preview_only"] is True
    assert plan["run_action_available"] is False
    json.dumps(plan)
