# Plugin Manifest

Every official plugin ships a `plugin.yaml` and appears in `catalog/index.json`.

Required fields:

- `id`: stable reverse-DNS id, such as `io.dirextalk.agent`.
- `name`, `version`, `description`.
- `min_base_version`: minimum compatible `dirextalk-message-server`.
- `runtime.image` and `runtime.digest`: Docker image and pinned sha256 digest.
- `service.health` and `service.invoke`: internal HTTP endpoints.
- `permissions`: declared capability scopes.
- `actions`: plugin-owned action names.
- `events`: base events the plugin can subscribe to.
- `config_schema`: user-visible configuration schema.

The base server installs only official catalog entries and verifies the pinned digest before Docker operations.

