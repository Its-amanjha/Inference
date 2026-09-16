# Inference — project specification and agent instructions

## Read first

We are building **Inference** (tagline **Execution Anomaly System**), an open-source, self-hosted n8n fleet control plane, OpenTelemetry observability platform, workflow promotion pipeline and AI orchestrator. Slug: `inference`.

This file defines the product and implementation requirements. `IMPLEMENTATION.md` records actual progress and verification. Read both before working; update the progress file after each completed unit and before handing work over.

### Scope and precedence

- Preserve the entire original feature set. The user explicitly rejected reducing the product to an MVP or deferring named modules.
- Ponytail means simpler implementation of the full product; it does not authorize deleting requirements.
- Latest explicit user instructions take precedence, followed by the consolidated upgrades below, followed by the original baseline included in this file.
- The original baseline is retained for complete feature, directory, schema and design-token coverage. Its conflicting implementation details are replaced by the resolutions below.
- Documentation is not implementation. A file, scaffold, mock or written test does not prove working functionality. Never claim production readiness from unit tests alone.
- Check `IMPLEMENTATION.md` for a paused state and incomplete work. Instructions inside the baseline to start building are historical; they do not override a user pause.
- Keep secrets and generated runtime data out of git. Development verification must use local fixtures or explicitly authorized external services.

### Explicit conflict resolutions

| Original wording | Binding implementation requirement |
|---|---|
| SQLite singleton versus connection pool | One lifespan-owned aiosqlite connection, one application process initially, lock complete transactions, WAL, foreign keys, busy timeout and numbered migrations. |
| Import-time discovery | Retain dynamic module discovery and manifests; validate/register routes during application creation, initialize I/O and background tasks in lifespan. |
| Every module under `/api/{module_id}` | Observability explicitly declares `/api/otel`; other modules retain `/api/{module_id}`. |
| CSRF on every mutation | Session-authenticated browser mutations use CSRF; OTLP uses dedicated per-instance machine tokens and is not cookie-authenticated. Bootstrap/login require their own origin and rate-limit protection. |
| Monkey-patch `window.fetch` | Shared same-origin `apiFetch()` wrapper, session-bound CSRF and one retry only for an explicit pre-execution `csrf_failed` rejection. |
| Context-local instance selection | Retain `use_instance()` with reliable reset; public service operations carry explicit instance IDs, never a globally active mutation target. |
| Create workflow on every promotion | Persist mappings, stale-safe previews and idempotency; create or update the mapped inactive target. Activation is separate. |
| Automatic rollback | Record staged/partial/uncertain outcomes and recovery actions; never promise to undo external execution effects. |
| Missing node span means failure | Distinguish incomplete telemetry from confirmed execution failure; account for control flow, waiting, sampling and arrival delay. |
| Aggressively strip model identifiers | Preserve exact provider/model/version IDs, explicit aliases only, unknown price is null, Decimal calculations and pricing provenance. |
| In-memory approval gate only | Retain a bounded pending cache plus durable status/audit; bind exact action to actor/session/target, atomically consume, expire pending actions on restart. |
| Immutable local audit log | Application-level append-only audit; do not claim resistance to an administrator modifying local storage. |
| Original schema is complete | Preserve its fields and add the persistent state required by all promised subsystems. |

## Consolidated architecture and upgrade specifications

The following requirements are normative and take precedence over conflicting wording in the baseline.

## Architecture

FastAPI, Python >=3.10, httpx, aiosqlite, Pydantic v2, Fernet. Raw HTML/CSS/ES modules, no Node frontend toolchain. One application process and one SQLite connection initially; serialize complete transactions with an asyncio lock, enable WAL, foreign keys, busy timeout, numbered transactional migrations. Persistent data lives beneath INFERENCE_DATA_DIR (default data). Startup creates resources in lifespan; module manifests are discovered at app creation with explicitly validated IDs and route prefixes. Observability declares /api/otel; all other modules use /api/{module_id}.

Roles: viewer reads ordinary fleet data, operator may mutate workflows/promote and run approved assistant tools, admin manages instances, credentials, secrets, users, Docker and backups. Auth, authorization and CSRF are enforced centrally plus service-level checks for AI tools. Session cookies are HttpOnly, SameSite strict, Secure under HTTPS. A session-bound signed csrf token is returned in agd_csrf; mutating browser requests must submit X-AGD-CSRF and a permitted Origin. Machine OTLP uses dedicated hashed tokens scoped to one instance, never session cookies. WebSockets validate session, Origin and expiry and send redacted notifications only.

## Shared backend interfaces

- `request.app.state.db`: Database with async `fetchone(sql, params=()) -> dict|None`, `fetchall(sql, params=()) -> list[dict]`, `execute(sql, params=()) -> int` (lastrowid), `transaction()` yielding Transaction with the same operations, `backup(path)`.
- `request.app.state.settings`: Settings with data_dir Path, secure_cookies bool, allow_cidrs tuple[str], frontend_dir Path.
- `request.app.state.vault`: Vault encrypt(str)->str, decrypt(str)->str, resolve(str)->Any, list()->list[str], set(name,value), delete(name). Writes atomic. Fernet key retained across restarts and missing key with existing ciphertext fails closed.
- `request.app.state.http`: one lifespan-owned `httpx.AsyncClient`, verified egress transport, timeout, connection limits, no redirects, trust_env=False.
- `request.app.state.hub`: async broadcast(dict) to authenticated sockets, bounded client queues.
- `request.state.user`: dict with username, role, session_id; set by central middleware on protected routes.
- `backend.auth_gate.require_role(request, role)` raises 403 unless viewer<operator<admin threshold met.
- `backend.auth_gate.require_current_role(request, role, tx)` rechecks live session, account and role inside a write transaction after awaited work; mutations must not rely on a cached authorization decision.
- `backend.audit.record(request, action, target, details=None, outcome='success')` writes redacted application-append-only audit records.
- Module router paths are relative to manifest prefix. Each module `router.py` exports router; manifest fields id, version='1.0.0', api_version=1, prefix, title.
- API errors: JSON `{"detail": string, "code": string}`. JSON identifiers are strings. List endpoints return `{"data": [...]}` and optional next_cursor. All external errors are redacted.
- `backend.modules.n8n_proxy.client.N8nClient(request)` exposes async `request(instance_id, method, path, **kwargs)->Any`, with fixed `/api/v1/` prefix and explicit instance. `use_instance` retained only as a ContextVar convenience that resets in finally. Never globally select mutation target.

## Persistence

Keep original tables and add users (scrypt password hashes, encrypted TOTP seed), audit_events, credential_mappings, workflow_mappings, promotion_runs, approval_actions, backup_schedules, backup_runs, documents, price_book, telemetry_tokens, execution_cursors. `documents` stores validated application settings/preferences/chat records as keyed JSON; secrets stay encrypted. Spans uniquely keyed by instance_id/trace_id/span_id, with exact costs as decimal text and estimate/provenance metadata. Preserve original cost_usd projection for consumers.

## Promotion contract

GET /api/n8n_proxy/workflows and /executions require instance_id; workflow read/update/activate/deactivate and execution detail/delete use explicit instance_id. Validate request bodies and forward only allowed paths.

POST /api/n8n_promote/preflight: source_instance_id, target_instance_id, workflow_id, credential_map (source id->target id), secret_map (source id->vault reference). Returns persisted preview_id, workflow diff, mapped/missing/collisions, source/target hashes and warnings; never secret plaintext. Credential mirrors are explicitly persisted, names never establish identity. Verify public API capabilities; unsupported credential discovery returns actionable diagnostics and accepts explicit mapping. Remap credentials and mapped subworkflow IDs only in known node fields, never arbitrary equal strings. Unknown source-specific references block or require explicit mapping.

POST /api/n8n_promote/deploy: preview_id, idempotency_key. Bind preview to actor and exact source/target snapshot. Re-fetch and reject stale data. Create or update mapped workflow, inactive. Persist stages before external effects. Timeouts after remote writes yield uncertain status and require reconciliation; no blind retry. Preserve previous target JSON for recovery. Activation is separate and checks mapped credential bindings. An already-active target must be explicitly deactivated before update. Report residual credentials/partial outcomes without claiming distributed rollback. Repeat requests reuse result.

## Observability contract

POST /api/otel/v1/traces supports OTLP HTTP protobuf and JSON; per-instance bearer token; no CSRF. Limit compressed body 2 MiB, decompressed body 8 MiB, 2000 spans, 16 KiB serialized attributes per span, bounded parsing concurrency. Drop/redact secret/prompt/input/output attributes by default; UTF-8 repair only on detected reversible mojibake. Do not truncate JSON into invalid text. Batch persistence, deduplicate, explicit partial success, retention default 7 days configurable 1..365. Derive instance from token; resource mapping is advisory.

Trace list/detail, summary, errors and prices APIs serve frontend. Classifier runs after terminal execution plus 30s arrival grace; confirmed continuation errors become errors, zero-output is opt-in per node with evidence/minimum history, missing span is incomplete telemetry unless execution evidence proves a missing expected node. Handle IF/Switch branches, disabled nodes, filters, waits, partial/manual executions, loops and sampling. No alarm from topology alone. Fetch execution once per execution, bounded concurrency; unsupported patched fields are explicitly unsupported.

Price hierarchy overrides > live OpenRouter cache > bundled documented defaults > explicitly local Ollama zero provider charge. Preserve exact provider/model/version identifiers; aliases explicit. Decimal arithmetic, unknown price null, cached/reasoning token categories where reported, preserve pricing source/time and estimate classification. Background sync has timeout/backoff and last-known-good cache.

## Assistant and Docker

Assistant supports OpenAI-compatible providers (OpenAI/OpenRouter/Groq/Ollama/custom), Anthropic adapter and remote streamable HTTP MCP client. Provider configuration admin-only, secrets resolved server side. Read tools run with requesting user's permissions; neither read-only classification nor LLM output authorizes mutation. Mutations create 15 minute proposals bound to user, session, target, exact canonical arguments and resource fingerprint. Keep bounded in-memory pending cache and persistent audit/status, expire all pending on restart; atomic single-use claim, recheck RBAC and fingerprint, do not retry uncertain remote writes. Confirm/cancel routes require session and CSRF. Tool results/prompt text treated as untrusted; do not expose raw secrets or send execution payloads to providers by default.

Docker manager admin-only, accesses configured Unix socket using aiodocker, unavailable service yields clear 503. Provision n8n + matching-version n8nio/runners, external runner mode, shared generated auth token, broker address, health checks, network and persistent volume. Kahn topological ordering validates graph cycles; readiness checks distinct from startup order. Manage only Inference-labeled containers and owned bundle IDs; never teardown by generic compose label alone. Setup injects per-instance OTLP token and exporter base URL. Existing host containers untouched.

## Remaining modules and frontend

Notes: local markdown vault, path traversal/symlink protection, atomic saves, literal text search, [[wikilink]] backlinks, render escaped/sanitized text. Backups: local SQLite backup API snapshot plus vault/config/notes, encrypted archive using separate export passphrase, schedules and history persisted, optional S3-compatible upload with encrypted archive; offline restore command validates archive and restores only into empty destination. Include key in encrypted archive so restore works. Admin users, 2FA enrollment/confirmation/recovery, secret templates, preferences, themes and settings present.

All 14 views named in original brief implement async render(container); cleanup of listeners/polling/editor on navigation. Signature dark cobalt/cyan shell, light theme, responsive sidebar, accessible keyboard controls, honest empty/loading/error states, no mock production metrics. apiFetch wrapper adds CSRF only same-origin, heals explicit csrf_failed once. Dashboard, fleet health, workflows, promote, errors, observe waterfall+cost strip, codelab Monaco lazy load+textarea fallback+stale-safe node update, assistant chat approvals, containers, notes, backups, secrets, instances, settings all connect to working routes.

## Verification and external prerequisites

Tests cover session/CSRF/RBAC, cross-instance isolation, replay/stale promotion, partial failure, approval replay/expiry/wrong user/stale resource, OTLP malformed/oversize/duplicate/wrong attribution, false-positive classifiers, exact/unknown prices, notes traversal, key loss, migration reruns and encrypted backup restore. Docker image builds and compose validates. Integration suite uses a local simulated n8n server for deterministic failures; live supported n8n and runner verification is separately reported and requires reachable Docker. No production-readiness claim from mocks alone.

## Complete original product baseline

This baseline retains every requested feature, exact directory layout, initial SQL schema and design token. Apply the explicit conflict resolutions and consolidated upgrades above.

# TASK: BUILD INFERENCE — OPEN-SOURCE n8n FLEET CONTROL PLANE & OBSERVABILITY PLATFORM
You are a Principal Distributed Systems Engineer and Security Architect. Scaffold and implement the complete, production-ready codebase for **Inference** (slug: `inference`), an open-source, self-hosted central command center, OpenTelemetry observability platform, CI/CD promotion pipeline, and AI orchestrator for multi-instance n8n automation deployments.
---
### 1. CORE ARCHITECTURAL NON-NEGOTIABLES
1. **Zero-Build Frontend:**
   - Native HTML5, modern CSS Custom Properties, and Vanilla JavaScript (ES Modules).
   - **STRICTLY NO Node.js frontend tooling:** No React, Vue, Svelte, Vite, Webpack, Babel, npm build steps, or `node_modules`.
   - The frontend is served as raw static assets directly by FastAPI (`frontend/`).
   - Monaco Editor is loaded asynchronously via CDN (`https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.45.0/min/vs/loader.js`).
   - Every UI view lives in `frontend/js/views/{view_name}.js` and exports `export async function render(container)`.
2. **Asynchronous High-Throughput Backend:**
   - **Python 3.10+** using **FastAPI**, **uvicorn**, **httpx** (async client with connection pooling), and **aiosqlite**.
   - Strict type hints on all public functions, Pydantic v2 schemas, and standard error handling.
3. **Serverless Embedded Persistence & Encryption:**
   - **Database:** Single-file SQLite at `data/dashboard.db` via an `aiosqlite` singleton with automatic idempotent schema migrations (`PRAGMA table_info` checks).
   - **Secrets Vault:** Symmetric Fernet encryption (`cryptography.fernet`) saved at `data/secrets.json`.
   - **Secret Resolution:** Hierarchical interpolation where host environment variables (`$VAR_NAME`) take precedence over the encrypted vault. Supports compound multi-field secrets (`$SECRET.field`).
   - **Permission Hardening:** Best-effort POSIX `0o600` permissions enforced on `.secret_key`, `secrets.json`, `config.json`, `users.json`, and `dashboard.db`.
4. **Dynamic Auto-Discovery Module Architecture:**
   - Modules reside in `backend/modules/{module_id}/`.
   - Each module contains a `manifest.json` and exposes a FastAPI `router = APIRouter(...)`.
   - The backend dynamically discovers, validates compatibility, and mounts all module routers at `/api/{module_id}/` during import time.
5. **Security Defense-in-Depth:**
   - **Double-Submit CSRF:** Mutating requests (`POST`, `PUT`, `DELETE`, `PATCH`) must validate the `X-AGD-CSRF` header against the `agd_csrf` cookie, with frontend self-healing retry logic.
   - **SSRF Protection (`backend/net.py`):** Resolve hostnames before connection; reject cloud metadata (`169.254.169.254`), IPv6 link-local (`fe80::/10`), multicast, and reserved addresses. Support optional `INFERENCE_EGRESS_ALLOW_CIDRS`.
   - **Prompt Injection Defense:** State-changing AI tool executions must halt, queue into an in-memory 15-minute TTL holding structure, and require human UI confirmation.
   - **Memory Exhaustion Defense:** Ingested OTLP span attributes must be capped at 16KB per span.
---
### 2. COMPLETE DIRECTORY STRUCTURE TO IMPLEMENT
```text
inference/
├── backend/
│   ├── __init__.py
│   ├── main.py                      # FastAPI app, lifespan boot, middleware stack, WS /ws
│   ├── config.py                    # Settings, Fernet crypto, $VAR resolution, use_instance()
│   ├── database.py                  # aiosqlite connection pool, schema init, migrations
│   ├── websocket.py                 # WebSocket ConnectionManager & live broadcast hub
│   ├── net.py                       # SSRF validation, DNS resolution, TLS verification
│   ├── audit.py                     # Immutable audit logging for mutating operations
│   ├── auth_gate.py                 # Session tokens, RBAC roles, CSRF validation, TOTP 2FA
│   ├── ratelimit.py                 # In-memory sliding-window rate limiter
│   └── modules/
│       ├── __init__.py              # Dynamic module loader & manifest validator
│       ├── n8n_proxy/               # Multi-instance proxy (workflows, executions, health)
│       │   ├── __init__.py
│       │   ├── manifest.json
│       │   ├── client.py            # Context-local instance httpx client (use_instance)
│       │   └── router.py
│       ├── n8n_promote/             # CI/CD promotion engine & credential remapping
│       │   ├── __init__.py
│       │   ├── manifest.json
│       │   ├── promote.py           # Preflight checks, credential resolver, AST JSON rewriter
│       │   └── router.py
│       ├── n8n_credentials/         # Credential schemas, mirror detection, mapping
│       │   ├── __init__.py
│       │   ├── manifest.json
│       │   └── router.py
│       ├── observability/           # OTLP trace receiver, pricing engine, silent failure
│       │   ├── __init__.py
│       │   ├── manifest.json
│       │   ├── ingest.py            # OTLP Protobuf/JSON span parser & 16KB sanitization
│       │   ├── health.py            # Silent failure classifier (modes 1, 2, dead-man)
│       │   ├── pricing.py           # 4-layer price book & OpenRouter background sync
│       │   ├── cost.py              # Token usage to USD cents calculator
│       │   ├── storage.py           # Trace and span persistence queries
│       │   ├── instance_map.py      # O(1) instance hash attribution
│       │   └── router.py
│       ├── errors/                  # Error feed, deduplication, grouping, preferences
│       │   ├── __init__.py
│       │   ├── manifest.json
│       │   └── router.py
│       ├── assistant/               # LLM Chat, MCP client, human approval gate
│       │   ├── __init__.py
│       │   ├── manifest.json
│       │   ├── approvals.py         # Human-in-the-loop 15-min TTL gate
│       │   ├── providers.py         # Multi-provider LLM connector (OpenRouter, Anthropic, Ollama)
│       │   ├── tools.py             # Read-only vs destructive tool declarations
│       │   └── router.py
│       ├── docker_mgr/              # Docker socket controller & Python sidecar runner
│       │   ├── __init__.py
│       │   ├── manifest.json
│       │   ├── client.py            # /var/run/docker.sock aiodocker client
│       │   ├── bundle.py            # Kahn's topological sort for multi-container bundles
│       │   ├── deployer.py          # 2-container bundle deployer (n8n + python runner)
│       │   └── router.py
│       ├── notes/                   # Obsidian-compatible markdown vault, backlinks, search
│       │   ├── __init__.py
│       │   ├── manifest.json
│       │   └── router.py
│       ├── backups/                 # Scheduled backups to local disk or S3/R2/MinIO
│       │   ├── __init__.py
│       │   ├── manifest.json
│       │   └── router.py
│       ├── admin/                   # User accounts, secret templates, settings overlay
│       │   ├── __init__.py
│       │   ├── manifest.json
│       │   └── router.py
│       └── auth/                    # Local authentication & session management
│           ├── __init__.py
│           ├── manifest.json
│           ├── service.py
│           └── router.py
├── frontend/
│   ├── index.html                   # Cyberpunk dark command-center shell & navigation
│   ├── css/
│   │   ├── base.css                 # CSS variables, typography reset, layout
│   │   └── components.css           # Cards, tables, modals, badges, waterfalls, toasts
│   ├── js/
│   │   ├── app.js                   # SPA router, dynamic view loader, WebSocket listener
│   │   ├── api.js                   # HTTP client with double-submit CSRF injection
│   │   ├── wizard.js                # First-run onboarding setup wizard
│   │   ├── themes.js                # Theme loading and CSS variable applicator
│   │   └── views/                   # View controllers (export async render(container))
│   │       ├── dashboard.js         # Overview, active executions, real-time error feed
│   │       ├── workflows.js         # Fleet workflow explorer & state toggles
│   │       ├── promote.js           # Multi-instance promotion wizard with preflight diff
│   │       ├── errors.js            # Aggregated error triage & silent failure feed
│   │       ├── observe.js           # OpenTelemetry trace waterfalls & token cost strip
│   │       ├── fleet-health.js      # Rollup health metrics across all connected instances
│   │       ├── codelab.js           # Monaco IDE for n8n Code Nodes with deploy button
│   │       ├── assistant.js         # AI assistant chat dock with interactive approval cards
│   │       ├── containers.js        # Docker container manager & template deployer
│   │       ├── notes.js             # Markdown notes vault with backlinks and search
│   │       ├── backup.js            # Scheduled local & offsite S3 backup manager
│   │       ├── secrets.js           # Encrypted credentials & secret vault UI
│   │       ├── instances.js         # Instance connection manager (URL + API Key)
│   │       └── settings.js          # System settings, users, audit logs, themes
│   └── themes/
│       ├── dark.json                # Signature Cobalt/Cyan Dark theme
│       └── light.json               # Clean minimal light theme
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```

### 3. DATABASE SCHEMA SPECIFICATION (backend/database.py)
Using aiosqlite, initialize and migrate the following schema:

```sql
CREATE TABLE IF NOT EXISTS instances (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    api_key_enc TEXT NOT NULL,
    is_active INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL DEFAULT '',
    workflow_id TEXT NOT NULL,
    workflow_name TEXT NOT NULL DEFAULT 'Unknown Workflow',
    execution_id TEXT DEFAULT '',
    node_name TEXT DEFAULT '',
    error_type TEXT DEFAULT 'Error',
    error_message TEXT NOT NULL,
    count INTEGER DEFAULT 1,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_errors_occurred ON errors(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_errors_instance ON errors(instance_id, occurred_at DESC);
CREATE TABLE IF NOT EXISTS otel_spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_id TEXT NOT NULL DEFAULT '',
    instance_id TEXT NOT NULL DEFAULT '',
    workflow_id TEXT NOT NULL DEFAULT '',
    workflow_name TEXT NOT NULL DEFAULT '',
    execution_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    kind INTEGER NOT NULL DEFAULT 0,
    start_ns INTEGER NOT NULL DEFAULT 0,
    end_ns INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '',
    attributes_json TEXT NOT NULL DEFAULT '{}',
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    model TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL,
    health_status TEXT,
    error_type TEXT,
    error_summary TEXT,
    output_items INTEGER,
    input_items INTEGER,
    node_id TEXT,
    silent INTEGER DEFAULT 0,
    checked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_otel_spans_trace ON otel_spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_otel_spans_instance ON otel_spans(instance_id, start_ns DESC);
CREATE INDEX IF NOT EXISTS idx_otel_spans_node ON otel_spans(node_id, start_ns DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_otel_spans_unique ON otel_spans(trace_id, span_id);
CREATE TABLE IF NOT EXISTS otel_instance_map (
    resource_hash TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'learned',
    learned_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    id_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    user_agent TEXT,
    ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(username);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT DEFAULT '',
    body TEXT DEFAULT '',
    level TEXT NOT NULL DEFAULT 'info',
    source TEXT DEFAULT '',
    occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### 4. CORE SUBSYSTEM IMPLEMENTATION SPECIFICATIONS
A. Multi-Instance Proxy & Context Switching (backend/modules/n8n_proxy/)
Maintain stored instance records with Fernet-encrypted API keys and URLs.
Implement use_instance(instance_dict) using Python's contextvars.ContextVar. When an async operation wraps its calls in with use_instance(target):, all outbound n8n_client calls route to that instance automatically.
Provide routes: GET /workflows, GET /executions, GET /executions/{id}, POST /workflows/{id}/activate, POST /workflows/{id}/deactivate.
B. CI/CD Workflow Promotion Engine (backend/modules/n8n_promote/)
Preflight Stage (promote.py):
Fetch workflow JSON from the source instance via /api/v1/workflows/{id}.
Recursively parse the workflow graph for node.credentials blocks ({credType: {id, name}}).
Query target instance schemas via /api/v1/credentials/schema/{type} and check existing mirrors.
Return validation report: {mapped: [...], missing: [...], collisions: [...]}.
Auto-Provisioning & AST Rewriting:
If a target credential is missing, resolve secrets from data/secrets.json or host $ENV. Auto-provision on the target via POST /api/v1/credentials.
Traverse the workflow JSON AST in memory, substituting source credential IDs with target credential IDs.
Deploy rewritten workflow via POST /api/v1/workflows.
Guarded activation: verify node-credential bindings on the target instance before activating; rollback/abort if n8n rejects bindings.
C. Observability, Telemetry Ingestion & Cost Engine (backend/modules/observability/)
OTLP Ingestion (ingest.py):
Provide endpoint POST /api/otel/v1/traces.
Decode Protobuf/JSON payloads, normalize span attribute keys, and repair double-encoded UTF-8 mojibake.
Strictly cap serialized attribute JSON at 16KB per span to prevent memory exhaustion attacks.
Silent-Failure Classifier (health.py):
Asynchronously fetch execution runData from n8n.
Mode 1 (Demoted Errors): Check taskData.continuation (from patched n8n instances) or runData[node].executionStatus == 'error'. If present while root span status is OK, classify node as ERROR and trace as silent = 1.
Mode 2 (Item-Drop Anomalies): Compare input_items vs output_items. If input_items > 0 and a historically reliable node outputs 0 items, classify as LOW and silent = 1.
Mode 3 (Dead-Man's Switch): Diff declared nodes from workflow JSON against received spans. If an upstream node produced output but a declared downstream node emitted no span, flag as a missing node failure.
Record silent failures in the errors table and broadcast an alert packet over /ws.
AI Cost Calculation Engine (cost.py & pricing.py):
Extract prompt_tokens and completion_tokens from runData[node].data.ai_languageModel[0][0].json.tokenUsage.
Normalize model names via _normalize(model) (strip vendor prefixes, date suffixes, and variant tags).
Match models against a 4-tier price book: Operator Overrides 
→
→ Live OpenRouter Cache 
→
→ Bundled Defaults 
→
→ Local Ollama ($0.00).
Calculate USD cost: (prompt_tokens / 1e6 * in_rate) + (completion_tokens / 1e6 * out_rate). Store in otel_spans.cost_usd.
D. AI Assistant with Human-in-the-Loop Security Gate (backend/modules/assistant/)
Support LLM providers: OpenRouter, Anthropic, OpenAI, Groq, Ollama, and Custom OpenAI-compatible endpoints.
Anti-Prompt-Injection Gate (approvals.py):
Read-only tools (list_workflows, get_execution, list_errors) execute immediately.
State-changing tools (activate_workflow, delete_execution, update_node):
Immediately suspend execution.
Generate a single-use proposal in _pending with a 15-minute TTL.
Return a pending_action payload to the UI.
UI renders an interactive Approval Card with "Confirm" and "Cancel" buttons.
Execute mutation only upon receiving POST /api/assistant/tools/confirm with a valid session cookie and CSRF token.
E. Docker Manager & Sidecar Bundler (backend/modules/docker_mgr/)
Connect to /var/run/docker.sock via aiodocker.
Provision n8n as a two-container bundle:
Primary container: n8n with auto-injected environment variables: N8N_OTEL_ENABLED=true, N8N_OTEL_EXPORTER_OTLP_ENDPOINT=http://inference:3000/api/otel.
Sidecar container: n8n-task-runners (Python worker) so Python Code Nodes execute out of the box.
Use Kahn's topological sort algorithm (bundle.py) to enforce container startup order and apply com.docker.compose.project labels for cascade teardowns.
5. FRONTEND SPA ARCHITECTURE (frontend/)
Design Tokens (frontend/css/base.css)
```css
:root {
  --brand-name: "Inference";
  --bg-void: #0A0F1D;
  --bg-panel: rgba(16, 24, 44, 0.85);
  --bg-input: rgba(22, 33, 62, 0.7);
  --border-dim: rgba(99, 102, 241, 0.12);
  --border-mid: rgba(99, 102, 241, 0.25);
  --text-primary: #E2E8F0;
  --text-secondary: #94A3B8;
  --accent-primary: #6366F1;     /* Cobalt */
  --accent-teal: #00F0FF;        /* Cyan */
  --accent-success: #10B981;     /* Emerald */
  --accent-danger: #F43F5E;      /* Rose */
  --font-heading: 'Space Grotesk', sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
}
```

Client Interceptor (frontend/js/api.js)
Monkey-patch window.fetch to automatically inject X-AGD-CSRF: readCookie('agd_csrf') on all non-GET requests.
Implement CSRF self-healing: on 403 CSRF check failed, query /api/auth/status once to obtain a fresh cookie and retry the mutation.
Connect to /ws on boot. On event: render animated toast notifications, increment the error badge, and update active views.
6. PHASED EXECUTION SEQUENCE
Scaffold the codebase following this strict sequence:

Phase 1: Core foundational layer (pyproject.toml, config.py, database.py, net.py, main.py).
Phase 2: Authentication, CSRF gate, session store, and module auto-discovery registry.
Phase 3: n8n_proxy client and n8n_promote CI/CD promotion engine with JSON rewriter.
Phase 4: observability subsystem: OTLP ingest, 16KB sanitization, silent-failure classifier, price book, and cost calculator.
Phase 5: assistant approvals gate (15-min TTL) and docker_mgr bundle deployer.
Phase 6: Zero-build frontend SPA shell (index.html, base.css, app.js, api.js) and modular views (dashboard.js, promote.js, observe.js, codelab.js).
Delivery requirement: complete, tested files with no placeholders. The current user instruction and IMPLEMENTATION.md determine when execution resumes.

## Mutable session handoff — 2026-09-10

Progress only; protected specification above is unchanged. Read IMPLEMENTATION.md and the implementation plan before continuing.

- Backend modules and all 14 native frontend views are implemented. The latest user visual specification supersedes the original dark visual defaults: see DESIGN.md. Full product scope is preserved.
- Latest full regression: 654 passed, 11 platform skips, one dependency deprecation warning. Real Edge/local-Uvicorn browser smoke passed: setup, all 14 empty-fleet views, notes save/reload/escaped content, persisted themes, mobile navigation and login/logout. Artifacts are in ignored test-results/.
- Next: populated browser workflows and mutation/error scenarios, final conformance review, Dockerfile/Compose and delivery checks. Live n8n/runner/OTLP, real model/provider/MCP, S3/disaster recovery and POSIX checks remain. Notes preview is escaped text; styled Markdown remains. Do not infer production readiness from local fixtures.
- Preserve explicit-instance operations, stale/replay checks, actor/session-bound approvals and uncertain-write handling. Do not automatically retry unknown external outcomes. Notes locking precedes database backup snapshots.
- Workspace: the repository root, branch feat/inference, files uncommitted. Reuse .venv, Python 3.13.14, schema v6. Python Playwright and Microsoft Edge are used for browser checks; no Node build. Docker engine was unavailable and no real model/provider was configured. Run backend server smoke separately from the full test suite.
- Update this mutable handoff and IMPLEMENTATION.md after verified units; preserve the core specification bytes.

## Mutable session handoff — 2026-09-15

Supersedes the workspace/branch notes above. The "Status at a glance" section at the top of IMPLEMENTATION.md is the authoritative done / in progress / remains list.
- Workspace: the repository root, branch `feat/inference`, no git remote (commits are local only). Python 3.13.14 `.venv`, schema v6, Python Playwright + Microsoft Edge for browser checks, no Node build. Docker Desktop Linux engine reachable; `inference:local` rebuilt 2026-09-16 with frontend fixes A–F, AI repair round 1, the self-call guard, the webhook-directory switch and the forms and navigation redesign; container smoke passed (rebuild again after later frontend or backend changes).
- Naming: product is Inference; identifiers are `INFERENCE_*`, `inference_session`, `X-Inference-Bootstrap`, `com.inference.*`, `inference-*` bundle resources, `INFERENCE-BACKUP`/`.ibackup`. No other environment prefix, cookie, header, label namespace or backup marker is accepted.
- Local preview: http://127.0.0.1:3000 serving source with existing `data/`; a local admin account exists (its password is not stored in project files; it was reset on 2026-09-16 and exists only in that session's conversation, so reset it again with the app's own `hash_password` if it is lost).
- Frontend quality work follows the Impeccable critique in `.impeccable/critique/` (22/40). Rules added to DESIGN.md: incomplete data uses the dashed amber partial-data treatment and never failure styling. Groups B–F were implemented in parallel with strict file ownership (B triage: errors.js/dashboard.js/lib/triage.js/css/triage.css; C live updates: app.js/css/live.css; D decisions: promote.js/assistant.js/fleet-health.js/instances.js/css/decisions.css; E actions: containers/backup/workflows/secrets/notes/settings/lib/confirm.js/css/actions.css; F readability: components.css/base.css/html.js/observe.js/themes). Verified 2026-09-15: complete `scripts/smoke_frontend.py` passed with updated fixtures (see IMPLEMENTATION.md). New shared frontend modules: `lib/triage.js` (severity labels/pills, relative time), `lib/payload.js` (raw-payload disclosures, instance names, capability checklists), `lib/confirm.js` (`confirmAction` dialog with optional typed confirmation, `actionMenu`, `stateBadge`). Reuse them instead of new one-off JSON dumps, `window.confirm` or unlabeled status text.
- Browser smoke constraint: the dashboard metrics endpoint allows 4 requests per minute per user. Only the fleet aggregate check should spend real requests; UI-only checks that rebuild the overview route `/api/n8n_proxy/dashboard` to fixtures. Workflow activation and destructive actions now confirm through an in-page dialog, so tests click the dialog's confirm button; CodeLab and Promote still use native dialogs.
- Approved next feature (2026-09-15): AI repair workspace — a single AI workspace window where the AI diagnoses a failing or requested workflow, proposes a patch or a new workflow, and the human applies it with one click. Hard limits: the AI never writes without an approval bound to the exact action and target; diffs shown to humans are computed by the server, never authored by the model; writes follow promotion's inactive-first, fingerprint/stale-check and readback rules; uncertain writes are never retried; credentials and secret values never reach the model; remote text (error messages, workflow content) is untrusted input. n8n's public API has no run-workflow endpoint: testing a fix means `POST /executions/{id}/retry` with `loadWorkflow: true`, which repeats real side effects and must be its own danger-styled confirmation. Workflow history (`GET /workflows/{id}/{versionId}`) supports undo. See IMPLEMENTATION.md "In progress" for the current plan state.
- Repair build, round 1 (scope A: repair first). Contract v1 is frozen in `backend/modules/assistant/repair_contract.py` with samples in `tests/fixtures/repair_proposal_v1.json`; change it only deliberately and update the fixture and both sides together. File ownership for the parallel build: A pure core (`repair_context.py`, `repair_diff.py`, `tests/test_repair_context.py`, `tests/test_repair_diff.py`); B gate and n8n calls (`tools.py`, `approvals.py`, `router.py`, `providers.py`, new `repair_actions.py`, `n8n_proxy/client.py`, assistant/client tests, `docs/api-assistant.md`); C frontend (`assistant.js`, `studio.js`, `errors.js`, `dashboard.js`, `workflows.js`, `lib/triage.js`, new `lib/proposals.js`, `lib/repair-target.js`, `css/repair.css`, `index.html`, `DESIGN.md`); D simulator and browser checks (`scripts/frontend_fleet_fixture.py`, new `scripts/frontend_repair_checks.py`, `scripts/frontend_design_checks.py`, `scripts/smoke_frontend.py`, `docs/live-assistant-verification.md`). The lead owns `AGENTS.md`, `IMPLEMENTATION.md` and integration.
- Repair round 1 verified locally 2026-09-16: full backend suite 824 passed / 11 skipped; complete browser smoke passed including `scripts/frontend_repair_checks.py` (loopback n8n simulator with secret-bearing fixtures and a scripted provider). Not yet verified: real n8n 2.19 repair writes and proposals driven by a hosted model (`scripts/smoke_live_repair.py` is a guarded skeleton), round 2 (create workflow, retry execution). Browser harness rules learned: Playwright event listeners must be Python functions or bound methods, never builtins like `list.append`; read `response.body()` only for requests that fired `requestfinished` (aborted requests hang); the repair secret scan is strict for every view, because `/api/n8n_proxy/workflows` masks secret parameter values before they reach a browser (CodeLab keeps its own node endpoint for the code it edits). The repair UI must never read raw workflow documents; use the masked repair context or the `list_workflows` tool.
- Live repair check 2026-09-16 (disposable n8n 2.19 + a hosted OpenAI-compatible model): mechanics verified on real n8n (interrupted-then-continued turn, linked deactivate, single PUT with verified readback, activate, undo with verified readback). Safety finding: the model, seeing only the target workflow, proposed pointing an HTTP Request at the workflow's own webhook; no risk was flagged and activation created a 3,816-execution self-call loop until deactivated. Before further live repair use: add a server-side risk plus apply guard for URLs targeting the same workflow's webhook path, and give repair diagnosis awareness of other workflows. Never treat "verified readback" as "fix is correct"; a successful write only proves n8n stored the proposal. `scripts/smoke_live_repair.py` now carries that harness: disposable container and bundle, the order-service plus misspelled-path fixture, context masking, tool-calling status, paced turns, apply with linked deactivate, replay 409, undo and a secret search, each step reporting PASS or NOT VERIFIED. It needs `--confirm-live` and a provider key in `INFERENCE_LIVE_PROVIDER_KEY`. A provider failure reports NOT VERIFIED with the server's detail instead of aborting the run; the first hosted provider stopped the first attempt by refusing both plain chat and tool-calling requests because the account had no credit. That connection and its vault secret were deleted from this installation on 2026-09-16 at the user's request, and replaced on the same day with a connection to a second hosted OpenAI-compatible provider (key stored as a vault secret reference), which is the model this installation uses now. The live script takes `--provider-base-url` and `--provider-model` instead of defaulting to any vendor. **Passed end to end 2026-09-16** against the second hosted provider's OpenAI-compatible model: all ten steps PASS, including context masking, a supported tool-calling check, a proposal after one model request that corrected `/webhook/order-stauts` to `/webhook/order-status` with no risks, one verified write after the linked deactivate, undo back to the pre-repair URL, the planted fake secret absent from transcript/approvals/context, and ownership-checked cleanup leaving no containers or volumes. The script is provider-agnostic: pass `--provider-base-url` and `--provider-model` for any OpenAI-compatible connection that returns tool calls.
- Self-call guard is mandatory: repair patches that make a workflow call its own trigger route are rejected with `self_call_blocked` (risk `self_webhook_call` in previews), and repair context includes a masked `webhook_directory` of other active workflows. Keep both when changing repair diffing, context or the contract; add a test for any new trigger route prefix n8n introduces. An administrator switch (`webhook_directory_enabled` in `assistant:settings:repair`, default on) withholds the directory from repair context and blocks continuing a turn that already sent it; every settings write must store both switches. Verified live 2026-09-16: with the guard and directory, the first hosted model proposed the correct fix for the practice workflow, the real run succeeded after activation and undo restored the original.
- New shared frontend helpers: `registerIcons()` in lib/icons.js lets a view add glyphs without editing the shared map; per-group stylesheets load after components.css in index.html (bump the `?v=` query when assets change).
- Forms and navigation redesign (2026-09-16; see DESIGN.md "Forms and navigation"). Build forms with `field(label, input, {hint})` and `formCard({title, description, body, note, actions})` from lib/html.js. Password inputs get the show/hide toggle automatically, and checkboxes render beside their label. Control appearance lives in `css/forms.css` (low-specificity `:where()` rules that views may override); the sidebar, top bar and account menu live in `css/shell.css`. The theme choice and Sign out are inside the "Account menu" button. Browser checks open it first, and `choose_theme(page, label)` in `scripts/frontend_theme_checks.py` picks a theme. The mobile navigation disclosure is labelled "Menu", and the instance switcher is still the native select labelled "Selected instance".
- Run the browser smoke alone: concurrent smoke runs, Docker Desktop start-up or parallel agents cause client timeouts that are harness flakes, not product failures.

- New verified unit: populated fleet browser smoke passes activation/deactivation, CodeLab save/stale rejection, stale promotion rejection, inactive target deployment and replay with exactly one target creation via real local HTTP. The n8n server is simulated. Next bounded unit: populated assistant approval-card browser scenarios. No production code changed in this unit.

- Assistant approval browser unit passed: pending card survives page reload; Cancel and stale confirmation make no remote writes; Confirm executes once and replay returns 409. Full frontend smoke and script compilation passed. No real inference was used. Next: populated administration/backup browser operations.

- Administration/backup browser unit passed: create/edit user, save secret, pause/enable schedule, manual backup/download and offline restore verifying user/note. Fixed paused schedules exposing an unusable Run now button. Complete frontend smoke passed. Next bounded unit: safe styled Markdown preview, followed by remaining conformance/delivery/live checks.

- Notes Markdown preview implemented and browser-verified: headings/emphasis/code/flat lists/quotes/safe links, inert HTML/images and large-document fallback. Existing backlink discovery unchanged. Complete browser smoke passed, including backup restore. Next: final requirement conformance and delivery packaging; real integrations remain unverified.

- Docker delivery verified: image built, both Compose configs validated, real non-root/read-only Linux container auth/frontend/notes and restart persistence passed. Disposable container/volume cleaned; inference:local image retained. Docker is now reachable (29.7.2, Compose 5.5.1), superseding prior unavailable-engine notes. See docs/deployment.md.
- Source conformance gaps are explicit in docs/conformance-review.md. Next: apply saved row-count/notification preferences; then dashboard/fleet aggregation, JSON theme assets and release reproducibility. Live managed n8n/runners/model/MCP/S3 checks are still separate and unverified.

- Saved UI preferences now applied: display pagination/feed limits and error-toast settings; session reset prevents preference carry-over. Browser persistence/request-limit/notification-toggle checks and complete frontend smoke passed. Next: JSON theme assets/loader, then dashboard/fleet aggregation and release/live checks. Rebuild the Docker image for latest frontend changes.

- JSON light/dark theme assets and validated cached loader implemented. Browser checks pass computed appearance, persistence, latest-choice races, unavailable-file fallback and retry; full frontend smoke passed. Next: fleet-wide workflow search with explicit targets, then dashboard aggregates and release/live checks.

- Fleet-wide workflow search completed 2026-09-11: bounded concurrent per-instance loading, independent pagination, explicit action/CodeLab targets, partial failure recovery and retained All instances selection. Complete browser smoke passed, including duplicate workflow ID isolation. Next: dashboard workflow totals and honest 24-hour execution/success/silent-failure aggregation.

- Dashboard aggregation implemented 2026-09-11 with bounded explicit-instance scans, null incomplete totals and labeled saved-history/telemetry scope. Full regression 659 passed/11 skipped; final dashboard-focused suite 6 passed; full browser smoke and compilation passed. See docs/api-dashboard.md. Next: release dependency/base-image pinning and refreshed image verification; live integrations remain unverified.

- Release inputs pinned 2026-09-11: base digest and 43 hashed Linux amd64/CPython 3.13 wheels. Locked build, pip check, exact installed-version comparison, Compose validation and real container restart smoke passed. Updated local image includes latest code; disposable resources cleaned. Next: backend regression in locked Linux environment, then remaining live integrations.

- Locked Linux backend suite passed 2026-09-11: 671 passed, zero skipped, two library warnings (163.40s), including POSIX cases. Reusable test Dockerfile/context and commands saved. Read-only/non-root disposable container removed; runtime image/data unchanged. Next: owned live n8n/runners bundle checks, then remaining model/MCP/S3 compatibility. Do not touch existing host containers.
