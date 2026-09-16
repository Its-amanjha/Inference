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
3. DATABASE SCHEMA SPECIFICATION (backend/database.py)
Using aiosqlite, initialize and migrate the following schema:

sql


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
4. CORE SUBSYSTEM IMPLEMENTATION SPECIFICATIONS
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
css


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
Begin scaffolding now, producing complete, production-grade files with no placeholders.