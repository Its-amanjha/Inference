# Inference

**Execution Anomaly System** — formerly VectisOps. Deployment identifiers (environment variables, cookies, headers, Docker labels, backup format and Compose names) now use Inference names; the legacy VectisOps identifiers are still accepted as fallbacks. See [Upgrading from VectisOps](docs/deployment.md#upgrading-from-vectisops) before updating an existing deployment.

Self-hosted n8n fleet control and observability. The full product specification is in [AGENTS.md](AGENTS.md); verified progress is in [IMPLEMENTATION.md](IMPLEMENTATION.md).

The backend currently serves authentication/admin, fleet, credential mapping, workflow promotion, CodeLab node updates, observability, assistant, owned Docker bundles, Markdown notes and encrypted backup export APIs. Telemetry supports token-scoped OTLP ingestion, trace queries, error triage, automatic classification with persistent preferences and exact pricing. The assistant supports local Ollama/custom endpoints, hosted providers, metadata tools, AI repair with server-computed previews and durable human approvals. It ships a library of 15 n8n guides (skills) that the model reads with the `get_skill` tool before writing or debugging n8n code; see [Skill library](docs/skills.md). Offline restore, scheduled backups, saved run history and optional encrypted S3 upload are implemented. Editable session/default-preference settings and credential templates are implemented. The native frontend is available at `/`, with first-run onboarding, all 14 API-backed views, and light/dark themes. Browser smoke covers empty-fleet navigation and local notes/theme persistence; live integration checks remain separate. The [API guides](docs/api.md) describe configuration and verification limits, including the [notes vault](docs/api-notes.md) and [backup/restore](docs/api-backups.md).

## Run the backend locally

Python 3.10+ is required; current verification uses Python 3.13 on Windows. Run one process/worker.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
# HTTP loopback development only. Keep Secure cookies enabled when serving HTTPS.
$env:INFERENCE_SECURE_COOKIES = 'false'
.\.venv\Scripts\python.exe -m uvicorn backend.main:create_app --factory --host 127.0.0.1 --port 3000 --no-proxy-headers
```

`GET http://127.0.0.1:3000/healthz` reports database-backed health. Configuration comes from process environment variables listed in [.env.example](.env.example); this file is not automatically loaded. No default account or password is created.

The first administrator is created with `POST /api/auth/setup`, JSON `{"username":"owner","password":"your unique password of at least 14 characters"}`. Without a bootstrap token, setup requires a loopback client and loopback request hostname. For remote setup, configure `INFERENCE_BOOTSTRAP_TOKEN` and submit it in `X-Inference-Bootstrap`. Serve HTTPS with Secure cookies for remote use; explicitly configure the public browser origin when behind a reverse proxy. Proxy headers are not trusted by the launch command above.

Login sets the HttpOnly `inference_session` cookie and readable `agd_csrf` cookie. Protected mutations must send the latter as `X-AGD-CSRF`. Browser Origin headers must match the configured origin; cross-site requests are rejected. Nonbrowser clients may omit Origin but must still send their session and signed CSRF token. An explicit `csrf_failed` rejection occurs before mutation; refresh `/api/auth/status` and retry once. Do not blindly retry other write failures.

## Local model (Ollama)

Inference talks to an existing Ollama server through its OpenAI-compatible API. It does not install Ollama or download models.

```powershell
ollama pull qwen2.5:3b
# Loopback is not a global address; permit it explicitly or the connection fails.
$env:INFERENCE_EGRESS_ALLOW_CIDRS = '127.0.0.1/32'
$env:INFERENCE_SECURE_COOKIES = 'false'  # HTTP loopback development only
.\.venv\Scripts\python.exe -m uvicorn backend.main:create_app --factory --host 127.0.0.1 --port 3000 --no-proxy-headers
```

Then, as an administrator, open **Connect a model**, choose **Local Ollama**, enter `qwen2.5:3b`, save and select **Test connection**. No API key is needed. Enable tool calls and run the tool-calling check before relying on skills or repair. `qwen2.5:3b` was verified on CPU for chat, tool calling and `get_skill`. On CPU, an answer that uses a skill guide can take several minutes (228 seconds for one follow-up turn), so local providers get a 600-second read timeout instead of the hosted 45 seconds. Ollama's default context window can truncate a prompt that carries a guide; see [Ollama setup](docs/api-assistant.md#ollama-setup).

In a container, `127.0.0.1` is the container itself: use an address of the Ollama host that the container can reach and permit that address instead. Details: [Ollama setup](docs/api-assistant.md#ollama-setup) and [local model servers](docs/deployment.md#local-model-servers).

## Available API

| Prefix | Operations |
|---|---|
| `/api/auth` | `GET /status`; `POST /setup`, `/login`, `/logout`, `/totp/enroll`, `/totp/confirm`, `/totp/disable` |
| `/api/admin` | `GET/POST /users`; `PATCH /users/{username}`; `GET/POST /secrets`; `DELETE /secrets/{name}`; `GET /audit`; `GET/PUT /settings`, `/preferences`; `GET /secret-templates`; `POST /secret-templates/{id}/apply` |
| `/api/n8n_proxy` | Instance CRUD/probes, fleet health, workflows and executions; see [fleet API](docs/api-fleet.md) for routes and permissions |
| `/api/n8n_credentials` | Credential metadata, schemas and explicit mappings; admin-only |
| `/api/n8n_promote` | Preflight, deploy, run history, activation, reconciliation and previous-version restoration; see [promotion API](docs/api-promotions.md) |
| `/api/assistant` | Providers, chat, AI repair, tools, approvals and MCP; see [assistant API](docs/api-assistant.md) |
| `/api/skills` | `GET /` lists installed n8n guides; `GET /{id}` returns one guide's markdown; viewer, read-only; see [skill library](docs/skills.md) |
| `/api/otel`, `/api/errors` | Machine OTLP ingestion, token administration, trace queries, classification, pricing, retention and error triage; see [observability API](docs/api-observability.md) |
| `/ws` | Authenticated notifications; Origin required, session checked before delivery and during heartbeat |

Users have `viewer`, `operator`, or `admin` roles. Administration requires admin; each signed-in user can manage their own preferences and two-factor authentication. Disabling an account or changing its role/password revokes its sessions. The last enabled administrator cannot be disabled or demoted. User updates accept `role`, `disabled`, and/or `password`.

Secrets accept `{"name":"SERVICE","value":{"api_key":"..."}}`; listings return names only. `$SERVICE.api_key` resolves from the environment first, then the encrypted vault. TOTP enrollment requires the current password and returns a provisioning URI; confirmation requires `code` and returns recovery codes once. Codes are single-use, including accepted TOTP timesteps. Login accepts an optional `code` for TOTP or recovery. TOTP disable requires `password` and `code`.

## Verify

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/smoke_foundation.py
```

The smoke script starts and stops a real Uvicorn process on a temporary loopback port, exercises authentication, encrypted vault storage, token-authenticated protobuf/gzip ingestion and deduplication across restart, and deletes its temporary data. It does not contact n8n or external providers. Run it separately from the test suite to avoid competing for local resources during its short deadlines.

## Storage and deployment boundaries

The application owns `data/dashboard.db`, `.secret_key`, and encrypted `secrets.json`. Preserve the key with the database/vault when moving data; startup fails closed if key loss would make existing ciphertext unreadable. Unix permissions are hardened for database/WAL/SHM and vault files; Windows deployments need appropriate directory ACLs. Local audit records are application-append-only, not immutable against the host administrator.

Database snapshots use `await db.backup(new_path)` and refuse overwriting existing files. Encrypted backup/restore is implemented and locally verified. Container delivery instructions are in [docs/deployment.md](docs/deployment.md); live n8n compatibility and production rollout verification remain.

Outbound HTTP(S) validates and pins DNS answers to connection addresses, retains TLS hostname checks, limits pooled connections/timeouts, and never automatically follows redirects. Private fleet networks require explicit CIDR configuration. Tests cover simulated DNS/network boundaries and a real loopback HTTP fixture using the production transport; live n8n/TLS interoperability has not been verified.

Frontend verification: `.venv/Scripts/python.exe scripts/smoke_frontend.py` uses Python Playwright and installed Microsoft Edge, starts an isolated local server, and writes screenshots under ignored `test-results/`. No Node frontend build is needed.

Container setup: [deployment guide](docs/deployment.md). Remaining requirement gaps: [conformance checkpoint](docs/conformance-review.md).

## Documentation

| Document | Covers |
|---|---|
| [docs/status.md](docs/status.md) | Current project status: what works, verification evidence and remaining work |
| [docs/deployment.md](docs/deployment.md) | Docker Compose, HTTPS, egress allowlists, local model servers, skills in the image, schema upgrades, release lock, upgrading from VectisOps |
| [docs/api.md](docs/api.md) | Index of the backend API guides |
| [docs/api-admin-settings.md](docs/api-admin-settings.md) | Editable system settings and secret templates |
| [docs/api-fleet.md](docs/api-fleet.md) | Instances, health probes, workflows and executions |
| [docs/api-promotions.md](docs/api-promotions.md) | Credential mapping, promotion, activation and restore |
| [docs/api-observability.md](docs/api-observability.md) | OTLP ingestion, traces, classification, pricing and error triage |
| [docs/api-dashboard.md](docs/api-dashboard.md) | Dashboard aggregates |
| [docs/api-assistant.md](docs/api-assistant.md) | Model providers, local Ollama, chat, tools, approvals, AI repair and MCP |
| [docs/skills.md](docs/skills.md) | The bundled n8n skill library, its API and the `get_skill` tool |
| [docs/api-docker.md](docs/api-docker.md) | Owned Docker n8n and runner bundles |
| [docs/api-notes.md](docs/api-notes.md) | Markdown notes vault |
| [docs/api-backups.md](docs/api-backups.md) | Encrypted backup export, schedules, S3 upload and offline restore |
| [docs/foundation-contract.md](docs/foundation-contract.md), [docs/fleet-contract.md](docs/fleet-contract.md), [docs/promotion-contract.md](docs/promotion-contract.md), [docs/observability-contract.md](docs/observability-contract.md) | Module contracts |
| [docs/live-n8n-verification.md](docs/live-n8n-verification.md), [docs/live-assistant-verification.md](docs/live-assistant-verification.md) | Manual live checks against real n8n and model providers |
| [docs/conformance-review.md](docs/conformance-review.md) | Requirement gaps against the product specification |
| [DESIGN.md](DESIGN.md) | Frontend design system |

`IMPLEMENTATION.md` and `AGENTS.md` are local working files and are not tracked in git.
