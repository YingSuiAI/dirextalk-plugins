# Dirextalk Ops Plugin

`io.dirextalk.ops` is the official operations plugin for single-node private Dirextalk deployments. It provides server status, Docker container visibility, backups, migration export planning, and safe cleanup planning.

## Runtime Environment

The message-server starts Ops with these environment variables:

- `DIREXTALK_BASE_URL`: stable backend URL inside the Docker network or an explicitly configured public URL.
- `OPS_BACKUP_ROOT`: backup directory inside the Ops backup volume.
- `OPS_MAX_BACKUPS`: number of recent backups kept by automatic pruning.
- `OPS_MESSAGE_SERVER_CONTAINER`: Docker container name used for status and logs.
- `OPS_POSTGRES_CONTAINER`: Docker container name used for status and backup metadata.
- `OPS_POSTGRES_USER`: PostgreSQL user used for `pg_dumpall` and restore.
- `OPS_POSTGRES_PASSWORD`: PostgreSQL password passed to Docker exec through `PGPASSWORD`.

Ops is the only official plugin allowed to mount:

- `/var/run/docker.sock:/var/run/docker.sock`
- `<ops-backup-volume>:/var/lib/dirextalk-ops`

It does not receive owner access token or Agent token.

## Actions

- `ops.status.get`
- `ops.containers.list`
- `ops.logs.tail`
- `ops.backups.list`
- `ops.backup.create`
- `ops.backup.status`
- `ops.backup.download_chunk`
- `ops.backup.delete`
- `ops.cleanup.plan`
- `ops.cleanup.run`
- `ops.rooms.cleanup.plan`
- `ops.rooms.cleanup.run`
- `ops.media.orphans.plan`
- `ops.migration.export`
- `ops.restore.plan`
- `ops.restore.run`

## Safety Rules

All cleanup is plan-first. `ops.cleanup.run` and `ops.rooms.cleanup.run` require an existing plan id plus `confirm="run_cleanup"`. Cleanup execution also requires a recent backup when the plan says `requires_backup=true`.

Backups include a manifest, Postgres dump, plugin state marker, and restore notes. Clients can request async backup creation and poll `ops.backup.status` for progress. Backup export/download uses `ops.backup.download_chunk`; clients assemble chunks locally. `ops.restore.run` restores the Postgres dump from an existing backup package and requires `confirm="restore_backup"`.

First-version chat cleanup does not physically purge Matrix events. `chat_purge_physical` is rejected by room cleanup planning. Room cleanup can plan local cache cleanup, local hiding, archive indexing, and media cache cleanup only.

Media orphan scanning is preview-only in the first version. Referenced media is not deleted by Ops.

## Tests

```bash
.venv/bin/python -m pytest plugins/ops/tests/test_ops_service.py -q
.venv/bin/python -m compileall runtime/python plugins/agent plugins/ops
python3 -m json.tool catalog/index.json >/dev/null
```
