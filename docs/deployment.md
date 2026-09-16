# Container deployment

This builds the Python application and serves the native frontend without a Node build. Use one application worker and one replica: SQLite transactions and subsystem locks are process-local.

## Local Docker Compose

Docker must run Linux containers. In PowerShell, from the project directory:

```powershell
$env:INFERENCE_BOOTSTRAP_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:INFERENCE_SECURE_COOKIES = 'false'
docker compose config --quiet
docker compose up --build -d
docker compose ps
```

Open http://localhost:3000. In first-run setup, expand **Have a setup token?** and supply the value in your `INFERENCE_BOOTSTRAP_TOKEN` environment variable. Keep it private. Container bridge requests are not host loopback requests, so remote bootstrap protection still applies. Compose reads its `.env` file if present; the Python application itself does not load `.env`.

The service publishes on host loopback only. A named volume retains `/data`, including the vault key, database, notes and local archives. `docker compose down` retains it; deleting the volume destroys that data. Use application encrypted backups before upgrades. Existing bind-mounted data must be writable by UID/GID 10001; do not replace its vault key.

## HTTPS and fleet connections

For a reverse proxy, forward the original Host and support WebSocket upgrades. Set `INFERENCE_SECURE_COOKIES=true` and `INFERENCE_ALLOWED_ORIGINS=https://your-exact-hostname`. Connect the proxy to the service network or its loopback port. Uvicorn ignores forwarded client headers by default. Do not add workers or replicas.

Configure `INFERENCE_EGRESS_ALLOW_CIDRS` for the exact private networks hosting n8n, Ollama or MCP endpoints. Inside this container, `localhost` refers to Inference itself. Use a reachable service/host address for other services. A saved Ollama configuration does not install or download a model.

## Local model servers

Outbound connections to loopback and private addresses are refused unless a CIDR entry in `INFERENCE_EGRESS_ALLOW_CIDRS` covers them. Metadata, link-local, multicast, unspecified and reserved addresses stay blocked whatever the allowlist says. The variable is comma-separated.

- **Inference from source, Ollama on the same host:** set `INFERENCE_EGRESS_ALLOW_CIDRS=127.0.0.1/32` and use the base URL `http://127.0.0.1:11434/v1`.
- **Inference in this container, Ollama on the host or another machine:** loopback inside the container is the container. Point the base URL at an address of the Ollama host that the container can reach, permit exactly that address (for example `/32`), and make Ollama listen on that interface.

Without the entry, saving the provider succeeds but the connection fails with `provider_configuration_invalid`. The **Connect a model** form shows a hint for local base URLs. For administrators, it says whether this deployment already permits egress to that loopback or private address. The setting is read at startup, so restart after changing it (`docker compose up -d` recreates the container with the new value).

Expect slow answers from CPU-only model servers. With `qwen2.5:3b` on CPU, a follow-up turn carrying a skill guide took between 123 and 228 seconds. Local providers (kind `ollama`, or kind `custom` on `localhost` or a loopback or private IP literal) get a 600-second read timeout and a 630-second total limit per provider call; hosted providers keep 45 and 60 seconds. A chat can make up to three provider calls. For a skill guide to fit, raise the Ollama server's context window (for example `OLLAMA_CONTEXT_LENGTH=8192` for the Ollama process); otherwise Ollama can truncate the prompt and drop the question. Verified with `OLLAMA_CONTEXT_LENGTH=8192`: a 6,440-token skill-guided prompt fitted without truncation and the answer addressed the question. See [Assistant API: Ollama setup](api-assistant.md#ollama-setup).

## Skill library in the image

The n8n skill guides in `backend/skills/` are copied into the image with the rest of `backend/`. At every start they are synced into the database: new and changed guides are written, and guides whose folders are gone are deleted. The container's root filesystem is read-only, so skills cannot be added or edited in a running container. To change them, replace folders in `backend/skills/`, rebuild the image and restart. There is no user-added skill feature. Details: [Skill library](skills.md).

## Schema version 7

This release adds the `skills` table and raises the database schema to version 7. Migration is automatic at startup and needs no operator action. Take an encrypted application backup before upgrading, as usual. Encrypted backups from schema 5 or 6 restore and are migrated to version 7. An older application refuses to open a version 7 database ("Database schema is newer than or unsupported by this application"), so downgrading means restoring a backup taken before the upgrade.

## Optional Docker manager

The default deployment does not mount the host Docker socket. To provision owned n8n bundles on a Linux Docker host, set `INFERENCE_DOCKER_GID` to the numeric group owning `/var/run/docker.sock`, then use:

```text
docker compose -f docker-compose.yml -f docker-compose.docker.yml up --build -d
```

Socket access grants control of the host Docker daemon despite the non-root application user. The application additionally checks ownership labels and persisted bundle IDs; this is not an isolation boundary against a compromised process. Do not mount a Windows named pipe. See [Docker API/setup](api-docker.md) for bundle requirements and telemetry connectivity.

## Verification limits

Read IMPLEMENTATION.md for actual build/runtime results. Compose validation does not prove an image builds or runs. The Docker base is pinned by manifest digest. requirements-linux-amd64.lock pins 43 runtime wheels by version and SHA-256; installation requires matching hashes and binary wheels. Compose explicitly targets Linux amd64 / CPython 3.13. Other architectures and Python versions need separately generated and verified locks. This pins dependency inputs; it does not claim byte-identical image layers or an offline build. The image includes frontend assets explicitly; installing the Python wheel alone does not package the frontend as a standalone distribution.

## Updating the release lock

Review dependency changes in a disposable candidate image first. Export its runtime `pip freeze` manifest (exclude the inference distribution itself), and use `pip download --only-binary=:all: --no-deps -r MANIFEST --dest WHEELS` inside that same Linux amd64/Python 3.13 environment. Downloaded wheels stay outside git.

Generate the lock from that exact manifest and wheel directory:

```text
python scripts/hash_release_wheels.py MANIFEST WHEELS requirements-linux-amd64.lock
docker build --platform linux/amd64 -t inference:local .
python scripts/smoke_container.py
```

The generator rejects missing, duplicate or mismatched wheels. Review the lock diff and the candidate image's compatibility before release. Update the base digest deliberately and repeat verification after changing it. The image runs the copied application source directly; it does not invoke an unpinned setuptools build to install the application. Source-environment pyproject.toml ranges remain useful for development but do not override the release lock. Keep the lock consistent with those requirements.

## Linux backend regression suite

Build the current release image first, then derive a test-only image with pinned pytest tools. The test image installs tools with --no-deps and runs pip check so the locked runtime dependency versions are not upgraded.

```text
docker build --platform linux/amd64 -t inference:local .
docker build --platform linux/amd64 -f tests/docker/Dockerfile -t inference:tests .
docker run --rm --read-only --tmpfs /tmp:rw,nosuid,size=512m --cap-drop ALL --security-opt no-new-privileges:true inference:tests
```

The suite runs as UID 10001. Tests write only to temporary storage, use local/simulated external services, and do not receive a Docker socket or host data volume. This backend suite does not run the browser or live managed n8n/model/S3 compatibility checks. See IMPLEMENTATION.md for the actual result; a successful test-image build alone does not prove the tests pass.
