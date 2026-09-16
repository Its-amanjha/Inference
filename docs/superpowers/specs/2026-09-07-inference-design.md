# Inference implementation specification

The original brief is retained in `docs/original-brief.md`. All named modules and views remain in scope. User approved the upgrades on 2026-09-07; no feature reduction is authorized.

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
