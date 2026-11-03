# Inference

Inference is a self-hosted dashboard for operating one or more n8n instances: fleet health, execution error triage, workflow promotion between instances, trace ingestion and an AI assistant that proposes workflow repairs for a human to approve. It is a FastAPI backend with SQLite storage and an encrypted secret vault, serving a native ES-module frontend that needs no build step.

## What it does

**Fleet.** Register n8n instances by base URL and API key (the key is encrypted at rest and never returned). Probe read access, view health across the fleet, list and search workflows and executions, activate or deactivate workflows and delete executions. Secret-looking parameter values are masked before workflow documents reach the browser.

**Errors and observability.** n8n exports OpenTelemetry traces to a token-authenticated OTLP endpoint (JSON or protobuf, optionally gzip). Inference stores spans per instance, classifies executions, flags nodes that returned no output, prices model calls from an editable price book and applies retention. Instances can also be polled for executions. The error triage view lists failures and lets an operator resolve them.

**Promotion.** Map credentials between instances, run a preflight that shows a structural diff, deploy the workflow to the target as an inactive create or update, activate it as a separate step, reconcile uncertain writes and restore the previous version.

**Assistant and AI repair.** Connect OpenAI, OpenRouter, Groq, Anthropic, a local Ollama server or any OpenAI-compatible endpoint. Chat can call read-only tools over fleet metadata. Any change the model proposes (activation, node code update, workflow patch, restore) becomes a pending approval that a user with the right role must confirm; approvals expire after 15 minutes. Repair conversations send the model masked workflow and error context, and the server computes the preview of every proposed patch. MCP servers (Streamable HTTP) can be attached, with their tool calls going through the same approval gate.

**Skill library.** Fifteen n8n guides are shipped in `backend/skills/`. The model reads them with the `get_skill` tool before writing or debugging n8n code, and signed-in users can read them through `/api/skills`. See [Skill library](docs/skills.md).

**CodeLab.** Read and replace the code of JavaScript and Python Code nodes in inactive workflows, with stale-write rejection and readback verification.

**Containers.** Optionally provision n8n and task-runner bundles on the host Docker engine through a mounted Unix socket. Inference only manages bundles it created and labelled.

**Notes, backups and administration.** A Markdown notes vault with search and wikilink backlinks. Encrypted backup export, scheduled local backups with run history, optional S3-compatible upload and an offline restore command. Users with `viewer`, `operator` and `admin` roles, TOTP two-factor authentication with recovery codes, an audit feed, an encrypted secrets vault with credential templates and editable system settings.

The frontend has 14 views: Overview, Workflows, Promote, Errors, Observability, Fleet health, CodeLab, AI workspace, Containers, Notes, Backups, Secrets, Instances and Settings (which includes user management). It has first-run onboarding and Light, Dark and n8n-style themes.

## Architecture

```
Browser (frontend/, ES modules)        n8n OTLP exporter
        |  session cookie + X-AGD-CSRF          |  Authorization: Bearer <token>
        v                                       v
+---------------------------------------------------------------+
| FastAPI app (backend/main.py), one process, one worker        |
|  SecurityMiddleware: security headers, session lookup,        |
|  Origin check, signed CSRF, body size and read deadline        |
|  Role checks per route: viewer < operator < admin              |
|  Modules discovered from backend/modules/*/manifest.json       |
+---------------------------------------------------------------+
        |              |               |                |
        v              v               v                v
  SQLite + vault   n8n public API   Docker socket   Model providers,
  (INFERENCE_      (via egress      (optional,      MCP servers
   DATA_DIR)        guard)           Unix only)      (via egress guard)
```

Every `/api` request except `/api/auth/status`, `/api/auth/login`, `/api/auth/setup` and the OTLP endpoint `POST /api/otel/v1/traces` requires a session. Mutating requests (POST, PUT, PATCH, DELETE) must pass the Origin check and send the readable `agd_csrf` cookie value in the `X-AGD-CSRF` header. Write paths re-check the caller's role and session inside the database transaction, so a demoted or disabled user cannot finish a write that started before the change. `/ws` delivers authenticated notifications to the browser, and `/healthz` reports database-backed health.

### Modules

Each module is a folder under `backend/modules/` with a `manifest.json` and a `router.py`. The loader rejects a manifest whose prefix does not match its folder name.

| Module | API prefix | Purpose |
|---|---|---|
| `auth` | `/api/auth` | First-administrator setup, login, logout, TOTP enrolment |
| `admin` | `/api/admin` | Users, secrets vault, audit feed, system settings, preferences, credential templates |
| `n8n_proxy` | `/api/n8n_proxy` | Instances, probes, fleet health, workflows, executions, dashboard aggregates, CodeLab node editing |
| `n8n_credentials` | `/api/n8n_credentials` | Credential metadata, schemas and cross-instance mappings (admin) |
| `n8n_promote` | `/api/n8n_promote` | Promotion preflight, deploy, run history, activation, reconciliation, restore |
| `observability` | `/api/otel` | OTLP ingestion, ingestion tokens, traces, summary, classification, retention, price book, execution polling |
| `errors` | `/api/errors` | Error triage feed and resolve |
| `assistant` | `/api/assistant` | Providers, chat, tools, approvals, AI repair, MCP |
| `skills` | `/api/skills` | Read-only n8n skill guides |
| `docker_mgr` | `/api/docker_mgr` | Owned n8n and runner bundles on the host Docker engine (admin) |
| `notes` | `/api/notes` | Markdown notes vault |
| `backups` | `/api/backups` | Encrypted export, backup schedules and run history (admin) |

### Available API

| Prefix | Operations |
|---|---|
| `/api/auth` | `GET /status`; `POST /setup`, `/login`, `/logout`, `/totp/enroll`, `/totp/confirm`, `/totp/disable` |
| `/api/admin` | `GET/POST /users`; `PATCH /users/{username}`; `GET/POST /secrets`; `DELETE /secrets/{name}`; `GET /audit`; `GET/PUT /settings`, `/preferences`; `GET /secret-templates`; `POST /secret-templates/{id}/apply` |
| `/api/n8n_proxy` | Instance CRUD and probes, fleet health, workflows, executions, dashboard, CodeLab node read and write; see [fleet API](docs/api-fleet.md) |
| `/api/n8n_credentials` | Credential metadata, schemas and mappings; see [promotion API](docs/api-promotions.md) |
| `/api/n8n_promote` | Preflight, deploy, runs, activation, reconciliation and restore; see [promotion API](docs/api-promotions.md) |
| `/api/otel`, `/api/errors` | OTLP ingestion, tokens, traces, classification, pricing, polling, retention and error triage; see [observability API](docs/api-observability.md) |
| `/api/assistant` | Providers, chat, tools, approvals, repair and MCP; see [assistant API](docs/api-assistant.md) |
| `/api/skills` | `GET /` lists guides; `GET /{id}` returns one guide's markdown; see [skill library](docs/skills.md) |
| `/api/docker_mgr` | Docker status, bundles and containers; see [Docker API](docs/api-docker.md) |
| `/api/notes` | Notes list, read, save, delete, search, backlinks and render; see [notes API](docs/api-notes.md) |
| `/api/backups` | `POST /export`; schedules and runs; see [backups API](docs/api-backups.md) |
| `/ws` | Authenticated notifications; Origin required, session checked before delivery and during heartbeat |

Administration requires `admin`. Every signed-in user can manage their own preferences and two-factor authentication. Disabling an account or changing its role or password revokes its sessions, and the last enabled administrator cannot be disabled or demoted.

Secrets are stored as `{"name":"SERVICE","value":{"api_key":"..."}}`; listings return names only. A reference such as `$SERVICE.api_key` resolves from the process environment first, then from the vault. TOTP enrolment requires the current password; confirmation returns recovery codes once, and each code is single-use. Login accepts an optional `code` for TOTP or a recovery code.

On a `csrf_failed` rejection nothing was changed: refresh `/api/auth/status` and retry once. Do not blindly retry other write failures.

### Data storage

Everything the application owns lives in `INFERENCE_DATA_DIR` (default `data`, `/data` in the container):

| Path | Contents |
|---|---|
| `dashboard.db` (with `-wal` and `-shm` files) | SQLite database: users, sessions, instances, traces, approvals, promotion runs, settings, skills index, audit events |
| `.secret_key` | Fernet key for the vault and for encrypted database columns |
| `secrets.json` | Encrypted vault of named secrets |
| `notes/` | Markdown notes |
| `backups/` | Archives written by scheduled backups |

The schema is versioned with `PRAGMA user_version` and migrated automatically at startup; the current version is 7. An older application refuses to open a newer database. Keep `.secret_key` together with the database and vault: if the key is missing while encrypted data exists, startup fails rather than creating a new key. On Unix the files are created with private permissions; on Windows, protect the directory with ACLs.

### Safety model

- **Approval gate.** Model output and MCP tool annotations never authorize a change. A proposed mutation is stored as an approval bound to the user, session, tool, arguments and a fingerprint of the target resource. It expires after 15 minutes and is rejected as stale if the resource changed.
- **Inactive-only writes.** Promotion, CodeLab and AI repair write only to inactive workflows. If a repair targets an active workflow, a linked deactivation proposal is created first. After each write Inference reads the workflow back and checks the content hash and the inactive state. A timeout or mismatch is reported as an uncertain outcome and is never retried automatically. Repair keeps an encrypted before-snapshot so the change can be undone through restore, and it blocks patches that would make a workflow call its own trigger route.
- **Egress guard.** All outbound HTTP goes through `backend/net.py`. Hostnames are resolved, each answer is validated and the connection is pinned to it, TLS hostname checks stay on and redirects are not followed. Public addresses are allowed. Loopback, private and other non-global addresses are refused unless a CIDR in `INFERENCE_EGRESS_ALLOW_CIDRS` covers them. Cloud metadata addresses, link-local, multicast, unspecified, reserved and documentation ranges are refused regardless of the allowlist.
- **Secret handling.** Instance API keys, TOTP secrets, approval state and backup schedule configuration are encrypted in the database. Workflow documents and repair context mask secret-like values, validation errors never echo request input, and OTLP ingestion drops prompt and content attributes by default.

## Requirements

- Python 3.10 or newer (`requires-python = ">=3.10"`). Development and testing have used Python 3.13 on Windows.
- Optional: Docker with a Linux engine, for the container deployment, the Containers view and the live checks. The Docker manager needs a Unix socket, so on Windows it works only when Inference itself runs in a Linux container.
- Optional: [Ollama](https://ollama.com) or another model provider for the assistant.
- Optional, tests only: the `test` extra (pytest, pytest-asyncio, Playwright) and Microsoft Edge installed for the browser smoke suite.

Run a single process with one worker. SQLite transactions and several subsystem locks are process-local.

## Quick start (local)

1. Create a virtual environment and install the package with its test extra.

   Windows (PowerShell):

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -e '.[test]'
   copy .env.example .env
   ```

   Linux or macOS:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install -e '.[test]'
   cp .env.example .env
   ```

   `pip install -e .` without `[test]` is enough to run the application.

2. Edit `.env` for plain-HTTP local use:

   ```ini
   INFERENCE_SECURE_COOKIES=false
   # Only if n8n, Ollama or an MCP server runs on this machine or your LAN:
   INFERENCE_EGRESS_ALLOW_CIDRS=127.0.0.1/32,192.168.1.0/24
   ```

   With Secure cookies left on, the browser drops the session cookie over HTTP and sign-in fails. Leave `INFERENCE_BOOTSTRAP_TOKEN` empty for a local first run (see step 4).

3. Start the server:

   ```sh
   python scripts/run_local.py
   ```

   Use the virtual environment's interpreter (`.\.venv\Scripts\python.exe` or `.venv/bin/python`). The launcher reads `.env`, lets variables already set in your shell take precedence, and runs `uvicorn backend.main:create_app --factory --no-proxy-headers` on `INFERENCE_HOST`:`INFERENCE_PORT` (default `127.0.0.1:3000`). The application itself reads only the process environment and never loads `.env`, so if you start uvicorn directly, set the variables yourself.

4. Open http://127.0.0.1:3000 and create the first administrator. The password must be at least 14 characters. No default account exists. Setup rules, from `backend/modules/auth/router.py`:
   - With `INFERENCE_BOOTSTRAP_TOKEN` empty, setup is accepted only when both the client address and the request hostname are loopback (`127.0.0.1`, `::1` or `localhost`).
   - With a token set, every setup request must carry it, including local ones. In the browser, expand **Have a setup token?**; API clients send it as `X-Inference-Bootstrap`.
   - Setup works once. After an administrator exists it returns `409 already_initialized`.

   The same can be done without the browser: `POST /api/auth/setup` with `{"username":"owner","password":"..."}`.

5. Connect an n8n instance in **Instances**: give it a name, the base URL (without `/api/v1`) and an n8n API key, then run the probe. An n8n on `localhost` or a private address needs a matching entry in `INFERENCE_EGRESS_ALLOW_CIDRS`, and the variable is read only at startup.

To send traces, create an ingestion token in Observability and point the n8n OTLP exporter at `/api/otel/v1/traces`; see [Exporter setup](docs/api-observability.md#exporter-setup).

## Configuration

All settings are environment variables. `.env.example` lists and explains the ones meant to be changed.

| Variable  | Default  | Meaning  |
|---|---|---|
| `INFERENCE_HOST` | `127.0.0.1` | Bind address. Used by `scripts/run_local.py` only. |
| `INFERENCE_PORT` | `3000` | Listen port for `scripts/run_local.py`; Compose uses it as the host port. |
| `INFERENCE_DATA_DIR` | `data` | Database, vault, key, notes and backups. |
| `INFERENCE_SECURE_COOKIES` | `true` | Mark cookies `Secure`. Set `false` only for plain-HTTP local use. |
| `INFERENCE_SESSION_HOURS` | `12` | Lifetime of new sessions, 1 to 168. When the variable is set (including through `.env`), the Settings page cannot change it. |
| `INFERENCE_MAX_REQUEST_BYTES` | `2097152` | Largest accepted API request body. |
| `INFERENCE_ALLOWED_ORIGINS` | empty | Comma-separated exact browser origins. Empty means the request's own origin. Set it behind a reverse proxy. |
| `INFERENCE_EGRESS_ALLOW_CIDRS` | empty | Comma-separated CIDRs for private, loopback and LAN destinations. |
| `INFERENCE_BOOTSTRAP_TOKEN` | empty | When set, required to create the first administrator. Required by Compose. |
| `INFERENCE_DOCKER_SOCKET` | `/var/run/docker.sock` | Absolute Unix socket path for the Docker manager. Named pipes are not used. |
| `INFERENCE_FRONTEND_DIR` | `frontend/` in the repository | Directory served at `/`. Set by the Dockerfile to `/app/frontend`; not in `.env.example`. |

Compose-only variables: `INFERENCE_VOLUME` (name of the data volume, default `inference-data`) and `INFERENCE_DOCKER_GID` (group of the Docker socket, for `docker-compose.docker.yml`).

## Local AI model (Ollama)

Inference talks to an existing Ollama server through its OpenAI-compatible `/v1` API. It does not install Ollama or download models.

1. Pull a model that supports tool calls. `qwen2.5:3b` has been verified for chat, tool calling and `get_skill`:

   ```sh
   ollama pull qwen2.5:3b
   ```

2. Set `OLLAMA_CONTEXT_LENGTH=8192` in the environment of the Ollama process and restart Ollama. With Ollama's default context window, a prompt carrying a skill guide gets truncated and the model loses the question. This variable belongs to Ollama, not to Inference.

3. Permit loopback egress in `.env` and restart Inference:

   ```ini
   INFERENCE_EGRESS_ALLOW_CIDRS=127.0.0.1/32
   ```

   Without it, the connection fails with `provider_configuration_invalid`.

4. As an administrator, open **Connect a model**, choose **Local Ollama** (base URL `http://127.0.0.1:11434/v1`, no API key), enter `qwen2.5:3b`, save and run **Test connection**. Enable tool calls and run the tool-calling check before relying on skills or repair.

Expect answers to take minutes on CPU; a follow-up turn that uses a skill guide took several minutes in testing, and one chat can make up to three provider calls. Local providers therefore get a 600-second read timeout and a 630-second total limit per call, compared with 45 and 60 seconds for hosted providers.

In a container, `127.0.0.1` is the container itself. Use an address of the Ollama host that the container can reach, permit that address, and make Ollama listen on that interface. Details: [Ollama setup](docs/api-assistant.md#ollama-setup) and [Local model servers](docs/deployment.md#local-model-servers).

## Docker deployment

The image is built from `python:3.13-slim` pinned by digest and installs dependencies from `requirements-linux-amd64.lock` with hash checking. It runs as UID 10001 with one uvicorn worker and a health check on `/healthz`.

`docker-compose.yml` publishes the service on host loopback only (`127.0.0.1:${INFERENCE_PORT:-3000}`), runs it with a read-only root filesystem, a small `/tmp` tmpfs, all capabilities dropped and `no-new-privileges`, and keeps `/data` in the named volume `inference-data`. `INFERENCE_BOOTSTRAP_TOKEN` must be set or Compose refuses to start. Compose defaults `INFERENCE_SECURE_COOKIES` to `false` for loopback HTTP.

PowerShell:

```powershell
$env:INFERENCE_BOOTSTRAP_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
docker compose up --build -d
docker compose ps
```

Open http://localhost:3000 and supply the token under **Have a setup token?**. Requests from the host reach the container over the bridge network, so they do not count as loopback and the token is always needed. `docker compose down` keeps the volume; deleting the volume deletes the data.

For HTTPS behind a reverse proxy, forward the original Host header, allow WebSocket upgrades, and set `INFERENCE_SECURE_COOKIES=true` and `INFERENCE_ALLOWED_ORIGINS=https://your-host`. To let Inference manage Docker bundles, add the override file, which mounts the host socket (this grants control of the host Docker daemon):

```sh
docker compose -f docker-compose.yml -f docker-compose.docker.yml up --build -d
```

Skills are baked into the image, so changing `backend/skills/` requires a rebuild. The full guide, including schema upgrades and release lock updates, is [docs/deployment.md](docs/deployment.md).

## Testing

Backend tests (pytest with `asyncio_mode = "auto"`, configured in `pyproject.toml`):

```sh
python -m pytest -q
```

Some POSIX permission and fsync tests are skipped on Windows.

Foundation smoke test. It starts a real uvicorn process on a temporary loopback port with temporary data, exercises authentication, vault storage and OTLP ingestion, and contacts nothing external:

```sh
python scripts/smoke_foundation.py
```

Browser smoke suite. It starts an isolated server, drives the frontend with Playwright through the installed Microsoft Edge (`channel='msedge'`) and writes logs and screenshots to `test-results/` (ignored by git). It needs the `test` extra and Edge; no Node build is involved.

```sh
python scripts/smoke_frontend.py
```

Live checks are manual and opt-in. They create disposable containers, pull images or call real providers, and are never run by pytest:

| Script | Checks | Guide |
|---|---|---|
| `scripts/smoke_container.py` | Built image in a disposable container and volume | [deployment](docs/deployment.md) |
| `scripts/smoke_live_n8n.py` | Docker manager, real n8n and runners, execution visibility, OTLP arrival | [live n8n](docs/live-n8n-verification.md) |
| `scripts/smoke_live_repair.py` | AI repair end to end against real n8n and a tool-calling provider | [live assistant](docs/live-assistant-verification.md) |
| `scripts/smoke_assistant_integrations.py` | MCP interoperability with the official Python SDK | [live assistant](docs/live-assistant-verification.md) |
| `scripts/smoke_local_ollama.py` | Assistant against a disposable Ollama container | [live assistant](docs/live-assistant-verification.md) |

## Project layout

```
backend/
  main.py            app factory, security middleware, lifespan, /healthz, /ws
  config.py          settings from environment, Fernet vault
  auth_gate.py       sessions, roles, CSRF and Origin checks
  database.py        SQLite access and numbered schema migrations
  net.py             outbound HTTP transport with egress guard
  modules/           one folder per API module (manifest.json + router.py)
  skills/            vendored n8n skill guides and their license files
frontend/
  index.html         single page shell
  js/app.js          router for the 14 views
  js/views/          one module per view (assistant and CodeLab share studio.js)
  js/lib/            shared UI helpers
  css/               stylesheets by concern (base, components, forms, shell, ...)
  themes/            light, dark and n8n theme tokens
scripts/             local launcher, smoke suites, live checks, lock tooling
tests/               pytest suite; tests/docker/ holds the Linux test image
docs/                API guides, deployment, contracts, verification notes
Dockerfile, docker-compose.yml, docker-compose.docker.yml
requirements-linux-amd64.lock   hash-pinned runtime wheels for the image
```

## Documentation

| Document | Covers |
|---|---|
| [docs/status.md](docs/status.md) | What works, verification evidence and remaining work |
| [docs/deployment.md](docs/deployment.md) | Docker Compose, HTTPS, egress allowlists, local model servers, skills in the image, schema upgrades, release lock |
| [docs/api.md](docs/api.md) | Index of the API guides |
| [docs/api-admin-settings.md](docs/api-admin-settings.md) | Editable system settings and credential templates |
| [docs/api-fleet.md](docs/api-fleet.md) | Instances, probes, workflows, executions, CodeLab |
| [docs/api-promotions.md](docs/api-promotions.md) | Credential mapping, promotion, activation and restore |
| [docs/api-observability.md](docs/api-observability.md) | OTLP ingestion, traces, classification, pricing, polling, error triage |
| [docs/api-dashboard.md](docs/api-dashboard.md) | Dashboard aggregates |
| [docs/api-assistant.md](docs/api-assistant.md) | Providers, local Ollama, chat, tools, approvals, AI repair, MCP |
| [docs/skills.md](docs/skills.md) | The skill library, its API and the `get_skill` tool |
| [docs/api-docker.md](docs/api-docker.md) | Owned Docker n8n and runner bundles |
| [docs/api-notes.md](docs/api-notes.md) | Markdown notes vault |
| [docs/api-backups.md](docs/api-backups.md) | Encrypted export, schedules, S3 upload and offline restore |
| [docs/foundation-contract.md](docs/foundation-contract.md) | Contract for config, database, network and auth |
| [docs/fleet-contract.md](docs/fleet-contract.md) | Contract for fleet connectivity |
| [docs/promotion-contract.md](docs/promotion-contract.md) | Contract for credentials and promotion |
| [docs/observability-contract.md](docs/observability-contract.md) | Contract for ingestion, classification and pricing |
| [docs/live-n8n-verification.md](docs/live-n8n-verification.md) | Manual live check against real n8n and runners |
| [docs/live-assistant-verification.md](docs/live-assistant-verification.md) | Manual live checks for MCP, Ollama and AI repair |
| [docs/conformance-review.md](docs/conformance-review.md) | Requirement gaps still open |
| [DESIGN.md](DESIGN.md) | Frontend visual direction and design decisions |
| [PRODUCT.md](PRODUCT.md) | Product purpose, users and constraints |
| [AGENTS.md](AGENTS.md) | Product specification, protected requirements and instructions for coding agents |
| [IMPLEMENTATION.md](IMPLEMENTATION.md) | Dated implementation checkpoints and verification results |
| [docs/original-brief.md](docs/original-brief.md) | The original build brief |
| [docs/superpowers/specs/2026-09-07-inference-design.md](docs/superpowers/specs/2026-09-07-inference-design.md) | Approved implementation specification |
| [docs/superpowers/plans/2026-09-07-inference.md](docs/superpowers/plans/2026-09-07-inference.md) | Implementation plan and session checkpoints |

## Security notes

- Never commit `.env` or the data directory. Both are in `.gitignore`. The data directory holds the vault key; anyone with `.secret_key` and the database can decrypt stored API keys and secrets.
- Back up with the encrypted export (Backups view or `POST /api/backups/export`) rather than copying files from a running instance. Restore offline into an empty directory with `python -m backend.modules.backups.restore ARCHIVE DESTINATION`; see [backups](docs/api-backups.md).
- Keep `INFERENCE_SECURE_COOKIES=true` whenever the dashboard is reached over HTTPS, and set `INFERENCE_ALLOWED_ORIGINS` behind a proxy. The launch commands pass `--no-proxy-headers`, so forwarded client addresses are not trusted.
- Keep `INFERENCE_EGRESS_ALLOW_CIDRS` as narrow as possible, for example a `/32` per local service. Entries widen what the server may connect to on behalf of any user who can save an instance, provider or MCP URL. Metadata and reserved addresses stay blocked whatever you list.
- Mounting the Docker socket gives the container control of the host Docker daemon. Leave `docker-compose.docker.yml` out unless you need the Containers view.
- The audit log is append-only from the application's point of view; it is not tamper-proof against someone with host access.

## Third-party content

The guides in `backend/skills/` are vendored from [czlonkowski/n8n-skills](https://github.com/czlonkowski/n8n-skills), copyright Romuald Członkowski, under the MIT License. The upstream license and notices are kept verbatim in [backend/skills/LICENSE-n8n-skills.txt](backend/skills/LICENSE-n8n-skills.txt) and [backend/skills/NOTICES-n8n-skills.txt](backend/skills/NOTICES-n8n-skills.txt), and [backend/skills/README.md](backend/skills/README.md) lists the skills with their attribution.
