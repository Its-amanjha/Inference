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

## Upgrading from VectisOps

Inference was previously named VectisOps. Existing deployments keep working, with these compatibility rules:

- **Environment variables.** Old `VECTIS_*` variables still work. If both a `VECTIS_*` and an `INFERENCE_*` variable are set, the `INFERENCE_*` value wins. Compose also falls back to `VECTIS_BOOTSTRAP_TOKEN` and the other old keys from an existing `.env`.
- **Sessions.** Existing `vectis_session` cookies stay valid until they expire. New logins issue `inference_session`.
- **Bootstrap header.** `X-Vectis-Bootstrap` is still accepted alongside `X-Inference-Bootstrap`.
- **Docker bundles.** Bundles created with `com.vectisops.*` labels are still recognised and managed. New bundles get `com.inference.*` labels and `inference-{bundle_id}-*` resource names.
- **Backups.** Old `.vbackup` archives (`VECTISOPS-BACKUP` marker) still restore. Existing local scheduled archives stay downloadable. New archives use `.ibackup`.
- **Two-factor authentication.** Existing authenticator app entries keep the "VectisOps" label; their codes still work.

**Compose service and volume.** The Compose service is now `inference`. Replace the old `vectis-ops` container, otherwise port 3000 conflicts:

```text
docker compose up -d --remove-orphans
```

The data volume is declared as `inference-data` with `name: ${INFERENCE_VOLUME:-inference-data}`. Before upgrading, find your existing volume's real name with `docker volume ls` (it usually looks like `<project>_vectis-data`) and set `INFERENCE_VOLUME` to it, for example in `.env`. Otherwise Compose creates a new empty volume and your data will appear missing. Take an encrypted application backup first.
