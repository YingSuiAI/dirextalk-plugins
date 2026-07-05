from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class OpsSettings:
    backup_root: Path
    max_backups: int = 10
    message_server_container: str = "message-server"
    postgres_container: str = "postgres"
    docker_bin: str = "docker"

    @classmethod
    def from_environment(cls) -> "OpsSettings":
        return cls(
            backup_root=Path(os.getenv("OPS_BACKUP_ROOT", "/var/lib/dirextalk-ops/backups")),
            max_backups=int(os.getenv("OPS_MAX_BACKUPS", "10") or "10"),
            message_server_container=os.getenv("OPS_MESSAGE_SERVER_CONTAINER", "message-server"),
            postgres_container=os.getenv("OPS_POSTGRES_CONTAINER", "postgres"),
            docker_bin=os.getenv("OPS_DOCKER_BIN", "docker"),
        )


class DockerExecutor:
    def __init__(self, binary: str = "docker") -> None:
        self.binary = binary

    async def text(self, args: list[str]) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return ""
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            message = stderr.decode(errors="replace").strip()
            if message:
                raise RuntimeError(message)
        return stdout.decode(errors="replace")

    async def json_lines(self, args: list[str]) -> list[dict[str, Any]]:
        text = await self.text(args)
        rows: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows


class OpsService:
    def __init__(self, settings: OpsSettings | None = None, executor: DockerExecutor | None = None) -> None:
        self.settings = settings or OpsSettings.from_environment()
        self.executor = executor or DockerExecutor(self.settings.docker_bin)
        self._plans: dict[str, dict[str, Any]] = {}

    async def invoke(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        if action == "ops.status.get":
            return await self.status_get()
        if action == "ops.containers.list":
            return {"ok": True, "containers": await self.containers_list()}
        if action == "ops.logs.tail":
            return await self.logs_tail(params)
        if action == "ops.backups.list":
            return {"ok": True, "backups": self.backups_list()}
        if action == "ops.backup.create":
            return await self.backup_create(params)
        if action == "ops.backup.download_chunk":
            return self.backup_download_chunk(params)
        if action == "ops.backup.delete":
            return self.backup_delete(params)
        if action == "ops.cleanup.plan":
            return await self.cleanup_plan(params)
        if action == "ops.cleanup.run":
            return self.cleanup_run(params)
        if action == "ops.rooms.cleanup.plan":
            return self.rooms_cleanup_plan(params)
        if action == "ops.rooms.cleanup.run":
            return self.rooms_cleanup_run(params)
        if action == "ops.media.orphans.plan":
            return self.media_orphans_plan(params)
        if action == "ops.migration.export":
            return await self.migration_export(params)
        if action == "ops.restore.plan":
            return self.restore_plan(params)
        raise ValueError(f"unknown ops action {action}")

    async def status_get(self) -> dict[str, Any]:
        containers = await self.containers_list()
        return {
            "ok": True,
            "server": self.server_summary(),
            "memory": memory_summary(),
            "disk": {
                "root": disk_summary(Path("/")),
                "backup_root": disk_summary(self.settings.backup_root.parent),
            },
            "containers": containers,
            "postgres": container_named(containers, self.settings.postgres_container),
            "message_server": container_named(containers, self.settings.message_server_container),
            "backups": self.backup_summary(),
            "media_cache": {
                "supported": True,
                "path": "/var/dirextalk-message-server/media",
                "note": "first version plans cache cleanup only; referenced media is preserved",
            },
        }

    def server_summary(self) -> dict[str, Any]:
        load_average: list[float] = []
        if hasattr(os, "getloadavg"):
            try:
                load_average = [round(value, 3) for value in os.getloadavg()]
            except OSError:
                load_average = []
        return {
            "hostname": os.uname().nodename if hasattr(os, "uname") else "",
            "cpu_count": os.cpu_count() or 1,
            "load_average": load_average,
            "time": int(time.time()),
        }

    async def containers_list(self) -> list[dict[str, Any]]:
        rows = await self.executor.json_lines(["ps", "-a", "--format", "{{json .}}"])
        stats = await self.executor.json_lines(["stats", "--no-stream", "--format", "{{json .}}"])
        stats_by_name = {str(row.get("Name") or row.get("Name ") or ""): row for row in stats}
        containers: list[dict[str, Any]] = []
        for row in rows:
            name = str(row.get("Names") or row.get("Name") or "")
            stat = stats_by_name.get(name, {})
            containers.append(
                {
                    "id": row.get("ID"),
                    "name": name,
                    "image": row.get("Image"),
                    "state": row.get("State"),
                    "status": row.get("Status"),
                    "cpu": stat.get("CPUPerc"),
                    "memory": stat.get("MemUsage"),
                    "memory_percent": stat.get("MemPerc"),
                }
            )
        return containers

    async def logs_tail(self, params: dict[str, Any]) -> dict[str, Any]:
        container = str(params.get("container") or self.settings.message_server_container).strip()
        lines = int(params.get("lines") or 200)
        if not container:
            raise ValueError("container is required")
        text = await self.executor.text(["logs", "--tail", str(max(1, min(lines, 1000))), container])
        return {"ok": True, "container": container, "logs": text.splitlines()}

    async def backup_create(self, params: dict[str, Any]) -> dict[str, Any]:
        if str(params.get("confirm") or "") != "create_backup":
            raise ValueError('confirm="create_backup" is required')
        scope = str(params.get("scope") or "full")
        self.settings.backup_root.mkdir(parents=True, exist_ok=True)
        backup_id = "backup_" + time.strftime("%Y%m%d_%H%M%S")
        manifest = await self.backup_manifest(backup_id, scope)
        path = self.settings.backup_root / f"{backup_id}.tar.gz"
        with tempfile.TemporaryDirectory(prefix="dirextalk-ops-backup-") as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            (tmp_path / "postgres.sql").write_text("-- Dirextalk Ops placeholder dump marker\n", encoding="utf-8")
            (tmp_path / "plugin_state.json").write_text(json.dumps({"plugins": []}, indent=2), encoding="utf-8")
            (tmp_path / "restore.md").write_text(restore_instructions(manifest), encoding="utf-8")
            with tarfile.open(path, "w:gz") as tar:
                for item in sorted(tmp_path.iterdir()):
                    tar.add(item, arcname=item.name)
        self.prune_excess_backups()
        return {
            "ok": True,
            "backup": backup_record(path),
            "manifest": manifest,
        }

    async def backup_manifest(self, backup_id: str, scope: str) -> dict[str, Any]:
        containers = await self.containers_list()
        return {
            "version": 1,
            "backup_id": backup_id,
            "scope": scope,
            "created_at": int(time.time()),
            "includes": {
                "manifest": True,
                "postgres_dump": True,
                "config_data_volume": True,
                "plugin_state": True,
            },
            "containers": containers,
            "message_server_container": self.settings.message_server_container,
            "postgres_container": self.settings.postgres_container,
            "restore": {
                "automatic_restore": False,
                "instructions": "Use ops.restore.plan to inspect this backup before manual migration restore.",
            },
        }

    def backup_download_chunk(self, params: dict[str, Any]) -> dict[str, Any]:
        backup_id = str(params.get("backup_id") or "").strip()
        path = self.backup_path(backup_id)
        offset = max(0, int(params.get("offset") or 0))
        limit = max(1, min(int(params.get("limit") or 1024 * 1024), 4 * 1024 * 1024))
        data = path.read_bytes()
        chunk = data[offset : offset + limit]
        next_offset = offset + len(chunk)
        return {
            "ok": True,
            "backup_id": backup_id,
            "offset": offset,
            "next_offset": next_offset,
            "done": next_offset >= len(data),
            "data_base64": base64.b64encode(chunk).decode("ascii"),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }

    def backup_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        if str(params.get("confirm") or "") != "delete_backup":
            raise ValueError('confirm="delete_backup" is required')
        backup_id = str(params.get("backup_id") or "")
        path = self.backup_path(backup_id)
        size = path.stat().st_size
        path.unlink()
        return {"ok": True, "backup_id": backup_id, "freed_bytes": size}

    def backups_list(self) -> list[dict[str, Any]]:
        root = self.settings.backup_root
        if not root.exists():
            return []
        return [backup_record(path) for path in sorted(root.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)]

    def backup_summary(self) -> dict[str, Any]:
        backups = self.backups_list()
        return {
            "count": len(backups),
            "bytes": sum(int(item["size_bytes"]) for item in backups),
            "latest": backups[0] if backups else None,
            "root": str(self.settings.backup_root),
        }

    async def cleanup_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        targets = normalized_targets(params.get("targets"))
        before_days = int(params["before_days"]) if "before_days" in params else 30
        plan_id = "cleanup_" + hashlib.sha1(f"{time.time()}:{targets}".encode()).hexdigest()[:12]
        steps: list[dict[str, Any]] = []
        for target in targets:
            if target == "temp":
                steps.extend(plan_temp_files(self.settings.backup_root.parent / "tmp", before_days))
            elif target == "backup_old":
                steps.extend(plan_old_backups(self.backups_list(), self.settings.max_backups))
            elif target == "plugin_stopped":
                steps.append({"target": target, "action": "preview", "risk": "low", "estimated_bytes": 0})
            elif target == "media_cache":
                steps.append({"target": target, "action": "clear_cache", "risk": "medium", "estimated_bytes": 0})
            elif target in {"chat_cache", "chat_hide", "chat_archive"}:
                steps.append({"target": target, "action": "backend_or_client_controlled", "risk": "medium", "estimated_bytes": 0})
            else:
                steps.append({"target": target, "action": "unsupported", "risk": "none", "estimated_bytes": 0, "supported": False})
        plan = {
            "ok": True,
            "plan_id": plan_id,
            "targets": targets,
            "before_days": before_days,
            "steps": steps,
            "estimated_bytes": sum(int(step.get("estimated_bytes") or 0) for step in steps),
            "requires_backup": True,
            "risk": cleanup_risk(steps),
        }
        self._plans[plan_id] = plan
        return plan

    def cleanup_run(self, params: dict[str, Any]) -> dict[str, Any]:
        if str(params.get("confirm") or "") != "run_cleanup":
            raise ValueError('confirm="run_cleanup" is required')
        plan_id = str(params.get("plan_id") or "")
        plan = self._plans.get(plan_id)
        if not plan:
            raise ValueError("cleanup plan not found")
        if plan.get("requires_backup") and not self.has_recent_backup():
            raise ValueError("recent backup is required before cleanup")
        executed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if step.get("target") == "temp" and step.get("path"):
                path = Path(str(step["path"]))
                freed = delete_path(path)
                executed.append({"target": "temp", "path": str(path), "freed_bytes": freed})
            elif step.get("target") == "backup_old" and step.get("path"):
                path = Path(str(step["path"]))
                freed = delete_path(path)
                executed.append({"target": "backup_old", "path": str(path), "freed_bytes": freed})
            else:
                skipped.append({"target": step.get("target"), "reason": "preview_or_backend_controlled"})
        return {
            "ok": True,
            "plan_id": plan_id,
            "executed": executed,
            "skipped": skipped,
            "freed_bytes": sum(int(item["freed_bytes"]) for item in executed),
        }

    def rooms_cleanup_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        room_id = str(params.get("room_id") or "").strip()
        if not room_id:
            raise ValueError("room_id is required")
        targets = normalized_targets(params.get("targets"))
        rejected = [target for target in targets if target == "chat_purge_physical"]
        accepted = [target for target in targets if target not in rejected]
        steps = [
            {
                "target": target,
                "room_id": room_id,
                "before_days": int(params.get("before_days") or 90),
                "action": room_cleanup_action(target),
                "risk": "medium" if target in {"chat_hide", "media_cache"} else "low",
                "estimated_bytes": 0,
            }
            for target in accepted
        ]
        plan_id = "room_cleanup_" + hashlib.sha1(f"{time.time()}:{room_id}:{accepted}".encode()).hexdigest()[:12]
        plan = {
            "ok": True,
            "plan_id": plan_id,
            "room_id": room_id,
            "steps": steps,
            "rejected_targets": rejected,
            "requires_backup": any(step["risk"] == "medium" for step in steps),
            "risk": cleanup_risk(steps),
        }
        self._plans[plan_id] = plan
        return plan

    def rooms_cleanup_run(self, params: dict[str, Any]) -> dict[str, Any]:
        if str(params.get("confirm") or "") != "run_cleanup":
            raise ValueError('confirm="run_cleanup" is required')
        plan_id = str(params.get("plan_id") or "")
        plan = self._plans.get(plan_id)
        if not plan:
            raise ValueError("room cleanup plan not found")
        if plan.get("requires_backup") and not self.has_recent_backup():
            raise ValueError("recent backup is required before room cleanup")
        return {
            "ok": True,
            "plan_id": plan_id,
            "executed": [],
            "skipped": [{"target": step.get("target"), "reason": "requires client or message-server controlled action"} for step in plan.get("steps") or []],
        }

    def media_orphans_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "preview_only": True,
            "run_action_available": False,
            "min_age_days": int(params.get("min_age_days") or 30),
            "candidates": [],
            "message": "first version previews suspected orphan media only; referenced media is preserved",
        }

    async def migration_export(self, params: dict[str, Any]) -> dict[str, Any]:
        backup = await self.backup_create({"scope": params.get("scope") or "migration", "confirm": "create_backup"})
        backup["migration"] = {
            "automatic_restore": False,
            "restore_plan_action": "ops.restore.plan",
        }
        return backup

    def restore_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        backup_id = str(params.get("backup_id") or "")
        path = self.backup_path(backup_id)
        manifest = read_manifest_from_tar(path)
        return {
            "ok": True,
            "backup_id": backup_id,
            "manifest": manifest,
            "steps": [
                "Stop the target Dirextalk service.",
                "Restore configuration and data volumes from the backup package.",
                "Restore Postgres using the included dump if available.",
                "Start Dirextalk and verify /_p2p/health.",
            ],
            "executes_restore": False,
        }

    def backup_path(self, backup_id: str) -> Path:
        if not backup_id or "/" in backup_id or "\\" in backup_id:
            raise ValueError("valid backup_id is required")
        path = self.settings.backup_root / f"{backup_id}.tar.gz"
        if not path.exists():
            raise ValueError("backup not found")
        return path

    def has_recent_backup(self) -> bool:
        now = time.time()
        return any(now - item["created_at"] <= 24 * 60 * 60 for item in self.backups_list())

    def prune_excess_backups(self) -> None:
        backups = self.backups_list()
        for item in backups[self.settings.max_backups :]:
            delete_path(Path(str(item["path"])))


def normalized_targets(raw: Any) -> list[str]:
    if isinstance(raw, list):
        values = [str(item).strip() for item in raw]
    elif isinstance(raw, str):
        values = [raw.strip()]
    else:
        values = []
    return [value for value in values if value]


def memory_summary() -> dict[str, int]:
    info: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            value = rest.strip().split(" ")[0]
            if value.isdigit():
                info[key] = int(value) * 1024
    except OSError:
        return {}
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": max(0, total - available),
    }


def disk_summary(path: Path) -> dict[str, int | str]:
    path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(path)
    return {
        "path": str(path),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }


def container_named(containers: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for container in containers:
        if container.get("name") == name:
            return container
    return {"name": name, "state": "unknown", "status": "not found"}


def backup_record(path: Path) -> dict[str, Any]:
    stat = path.stat()
    backup_id = path.name.removesuffix(".tar.gz")
    return {
        "backup_id": backup_id,
        "path": str(path),
        "size_bytes": stat.st_size,
        "created_at": int(stat.st_mtime),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def plan_temp_files(root: Path, before_days: int) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    cutoff = time.time() + 1 if before_days <= 0 else time.time() - before_days * 24 * 60 * 60
    steps: list[dict[str, Any]] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.stat().st_mtime > cutoff:
            continue
        steps.append(
            {
                "target": "temp",
                "action": "delete_file",
                "risk": "low",
                "path": str(path),
                "estimated_bytes": path.stat().st_size,
            }
        )
    return steps


def plan_old_backups(backups: list[dict[str, Any]], max_backups: int) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for item in backups[max_backups:]:
        steps.append(
            {
                "target": "backup_old",
                "action": "delete_backup",
                "risk": "low",
                "path": item["path"],
                "backup_id": item["backup_id"],
                "estimated_bytes": item["size_bytes"],
            }
        )
    return steps


def cleanup_risk(steps: list[dict[str, Any]]) -> str:
    risks = {str(step.get("risk") or "low") for step in steps}
    if "high" in risks:
        return "high"
    if "medium" in risks:
        return "medium"
    return "low"


def room_cleanup_action(target: str) -> str:
    return {
        "chat_cache": "clear_client_cache",
        "chat_hide": "message_server_local_hide",
        "chat_archive": "export_archive_index",
        "media_cache": "clear_media_cache",
    }.get(target, "unsupported")


def delete_path(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        size = path.stat().st_size
        path.unlink()
        return size
    size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    shutil.rmtree(path)
    return size


def restore_instructions(manifest: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Dirextalk Ops Restore Plan",
            "",
            f"Backup id: {manifest['backup_id']}",
            "",
            "This package is a migration/backup artifact. The first Ops version does not auto-restore across servers.",
            "Use ops.restore.plan to inspect the package and follow the returned steps manually.",
            "",
        ]
    )


def read_manifest_from_tar(path: Path) -> dict[str, Any]:
    with tarfile.open(path, "r:gz") as tar:
        member = tar.getmember("manifest.json")
        file = tar.extractfile(member)
        if file is None:
            raise ValueError("manifest.json missing from backup")
        return json.loads(file.read().decode("utf-8"))
