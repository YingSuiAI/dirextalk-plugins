# Plugin Manifest

Every official plugin ships a `plugin.yaml` and appears in `catalog/index.json`.

Required fields:

- `id`: stable reverse-DNS id, such as `io.dirextalk.agent`.
- `name`, `version`, `description`.
- `min_base_version`: minimum compatible `dirextalk-message-server`.
- `runtime.image`: Docker image in the official Docker Hub organization, such as `docker.io/dirextalk/agent-plugin:latest`.
- `runtime.digest`: optional sha256 digest for audit or rollback; first-version installs do not require it.
- `service.health` and `service.invoke`: internal HTTP endpoints.
- `permissions`: declared capability scopes.
- `actions`: plugin-owned action names.
- `events`: base events the plugin can subscribe to.
- `config_schema`: user-visible configuration schema.

The base server installs only official catalog entries and verifies the image belongs to the official `dirextalk` Docker Hub organization before Docker operations. If a digest is present, it must be a valid sha256 digest, but it is not required.
