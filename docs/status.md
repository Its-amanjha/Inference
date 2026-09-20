# Project status

Last updated 2026-09-20. Branch `feat/inference`, 239 commits (first 2026-08-05, latest `5bbacf9`). `origin/feat/inference` tracks through `3c50f77`; two local commits on top are unpushed.

This report covers what Inference does today, the work done so far and how it was verified, and what remains before release. Detailed dated entries are kept in [IMPLEMENTATION.md](../IMPLEMENTATION.md).

## What Inference does today

Inference is a self-hosted operations dashboard for one or more n8n instances. It is a FastAPI backend with SQLite storage (schema version 7) and a zero-build native JavaScript frontend with 14 API-backed views. It runs as one process, locally or in the provided container.

| Module | What works |
|---|---|
| Foundation (`backend/`) | Environment settings with `INFERENCE_*` names; encrypted Fernet vault; numbered SQLite migrations with schema validation on every open; egress transport with DNS pinning, SSRF protection and CIDR allowlists; security headers; health endpoint. |
| Authentication and admin (`auth`, `admin`) | First-admin bootstrap, scrypt passwords, session cookies, signed CSRF, origin checks, viewer/operator/admin roles rechecked inside transactions, TOTP with recovery codes, users, secrets, audit feed, editable session and default-preference settings, credential templates. |
| Fleet (`n8n_proxy`) | Instance CRUD with encrypted API keys, read-capability probes, fleet health, workflow and execution lists and details, activation, execution deletion, fleet-wide workflow search, dashboard aggregates. Secret parameter values are masked before workflow documents reach a browser. |
| Credentials and promotion (`n8n_credentials`, `n8n_promote`) | Credential schema inspection and explicit mappings; promotion preflight with structural diffs and fingerprints; inactive create or update; separate activation; reconciliation; previous-version restore. Uncertain writes are never retried. Source workflows are picked from a searchable combobox and credential/subworkflow targets from mapping rows; hand-typed JSON remains as the override path. |
| CodeLab | Read and replace code in inactive JavaScript and Python Code nodes, with stale-write rejection and readback verification. |
| Observability and errors (`observability`, `errors`) | Token-scoped OTLP ingestion (JSON, protobuf, gzip), trace queries, execution classification, zero-output warnings, exact model pricing with a price book, retention, error triage feed with in-place resolve. |
| Assistant (`assistant`) | OpenAI, OpenRouter, Groq, Anthropic, Ollama and custom OpenAI-compatible providers; guided provider setup that asks a known service only for key and model; connection test and tool-calling check; chat with metadata read tools; approval-only mutations bound to user, session, arguments and resource fingerprint; MCP 2025-11-25 Streamable HTTP client with approval-only calls. Repair round 2 is merged: creation and restore proposals with operator notes, execution retry review with side-effect warnings, readable repair events. |
| AI repair (`assistant`, contract v1) | Pinned repair conversations with masked workflow and error context, one provider request per human action, server-computed previews and risks, writes only to inactive workflows (with a linked deactivation), readback verification, undo through restore, self-call guard, webhook directory with an administrator switch, error-text consent switch. |
| Skills (`skills`) | 15 bundled n8n guides synced from `backend/skills/` at boot, a read-only `/api/skills` API and the `get_skill` assistant tool in standard and repair conversations. See [Skill library](skills.md). |
| Docker manager (`docker_mgr`) | Optional provisioning of owned n8n and runner bundles through a mounted Unix socket, with ownership labels, staged journals, reconciliation and explicit teardown. |
| Notes | Markdown vault with revision-checked edits, wikilink backlinks, search and path-traversal protection. |
| Backups | Encrypted export, offline restore with schema migration, local schedules with run history, optional single-attempt S3 upload. |
| Frontend | 14 views, first-run onboarding, Light, Dark and n8n-inspired themes, AI workspace (chat, repair, manual code editor), action review rail, shared form system, phone layouts. Tables sort, filter into saved views and bulk-select with keyboard shortcuts; every per-row control names its row; Settings is grouped into labelled sections; CodeLab, conversation resume and promotion mappings use searchable pickers instead of bare IDs. |
| Delivery | Dockerfile pinned by digest, hashed Linux amd64 dependency lock, Compose file with read-only root, dropped capabilities and loopback-only port. |

## Work done

### Size

- 239 commits between 2026-08-05 and 2026-09-20.
- Backend: 66 Python files (about 610 KB, excluding vendored skill guides). Tests: 50 files (about 490 KB) with 530 test functions; parametrised cases bring the suite to 906 collected tests. Scripts: 21 verification and smoke scripts. Frontend: 29 JavaScript files and 10 stylesheets.

### Milestones by area

**Core platform**
- `f7b535f` database engine with numbered migrations; `276ed30` egress transport with SSRF protection and DNS pinning; `7e4acaa` authentication, sessions, TOTP and RBAC.
- `3aaa911`, `2430ba8`, `277d2ed`, `dc82ed0` test suites for core, modules, observability and infrastructure.
- `5959cde` production Dockerfile, Compose and locked Linux dependencies.

**Fleet, promotion and observability**
- `032fa7a` multi-instance n8n proxy and health probes; `69cf746` credential inspection and mapping; `01c3fa9` promotion pipeline with structural diffs.
- `10fc626` OTLP ingestion; `176d11e` price book and pricing; `2ea4904` execution classifier and error triage.
- `48b1fe2` secret parameter values masked in the workflow proxy.

**Assistant and AI repair**
- `309f8ab` multi-provider connector and 15-minute approval gate.
- Repair contract v1 (server-computed previews, approval gate, inactive-only writes, readback verification, undo through restore) was built and verified locally on 2026-09-16. The branch history groups commits by topic, so the repair backend files first appear in `309f8ab` and the frontend proposal cards in `227c2ac`.
- `51e2b5e` self-call guard after a live incident (see below) and the webhook directory administrator switch.
- `05cd1f4`, `57ba106`, `9d22652` live repair check made runnable end to end and provider-neutral.
- `b24e047` assistant identifies itself as the Inference assistant; `19717ea` tool rejections name the tool and failing fields; `fe24331` provider setup asks a known service only for key and model.
- `6e423fe` skill library and `get_skill` tool; `22324f9` local endpoint egress hint and `qwen2.5:3b` placeholder; `c5a3760` `get_skill` in repair conversations and near-miss names; `087191f` a failing read tool in a standard chat goes back to the model instead of ending the chat.
- Done and verified 2026-09-17: longer timeouts for local providers (`8ad3fac`), the egress hint for private addresses (`a859277`), and the `skill_not_installed` result shape for unknown skill names (`8806866`). Backend suite 880 passed, 11 skipped; browser smoke suite 18 of 18; egress hint 16 of 16 in a real browser.
- Repair round 2, merged 2026-09-19 (`2d988a2` with `3dd4037`, `0aee8b9`, `d71c7d6`, `30dc384`): creation and restore proposals with operator notes, execution retry review with side-effect warnings, readable repair events announced once.

**Frontend and design**
- `227c2ac` zero-build SPA shell; `8b6f442`, `2b978d4` operational and specialised views; `8b7fdfc` design system and themes.
- `7ff5643` forms and navigation rebuilt; `8e17e72` one control scale; `848e29b` last form conversions, native browser dialogs replaced.
- Phone fixes: `f08eb7e` error type filters scroll on one line; `99f64b9` toasts reserve space so the last controls stay reachable; `e1d923a` proposal code diffs wrap (previously 30 of 82 characters visible); `0d49db1` no duplicate rail card for a completed linked deactivation.
- Keyboard, tables and triage, merged 2026-09-19: one shortcut registry for every view (`6ec003b` with the shell dispatcher, typing guard and overlay), sortable columns with saved filter views and bulk resolve, focused-row announcement with chatter suppression (`0113037`), per-row controls named for their row (`3763f27`, suite locators derived from the rendered names in `5f571bc`), two-tier triage toolbar (`27532ef`).
- Metrics and fonts, merged 2026-09-19: navigable metric semantics (`1354ded`, `ded8ec3`, `1c2f462`), Inter and JetBrains Mono bundled offline (`0d66b18`), assistant phone layout (`5dba6c2` with repair-event announcements in `72c7c15`).
- Promotion pickers, merged 2026-09-19 (`fd9a16a` with `60e06be`, `36e1562`, `42cbb7b`, `20ff93e`, `9f79d88`): source-workflow combobox and per-missing-item target pickers writing lossless JSON, plus conversation and CodeLab pickers from the shared helper.
- Design review 2 recorded 2026-09-19 (`c234fa9`): 33/40, up from 22/40.
- Settings sections, merged 2026-09-19 (`3fbf9a5` with `9a7175b`): Account, Security, Display, Deployment, Users and Audit groups with skip navigation; backlinks as a real list (`842a248`); provisioning fieldsets (`90f0153`).
- Two self-audits closed a dozen defects: null markdown sources (`97437a3`), single-node empty actions (`de803c7`), transient-error recognition (`3aec11e`), disconnected dialog focus restore (`5edf551`), table sort state sync (`8f7fabc`), picker clearing and null guards (`a554e11`, `24b2ffb`, `fa5d13b`, `7cf0566`), shortcuts overlay precedence and key rendering (`b511569`, `1670a0a`), stale mapping pickers (`f8b805e`), resolved-row resurrection (`fbce1b2`), and duplicating Settings sections (`9a7175b` with the group-clear follow-up `83a4f3b`); backlinks deduplication landed alongside (`5c29561`).

**Test infrastructure**
- `a87b01e` Playwright browser smoke suites and frontend fixtures; `e6e284f` managed n8n runner integration, MCP SDK server and local verification suites.

### Notable incidents and fixes

- **Recursive self-call (2026-09-16).** In the first live repair run, the model fixed a misspelled webhook URL by pointing the HTTP Request at its own workflow's trigger. The change was applied and activated and ran 3,816 recursive executions in a disposable n8n before it was deactivated. Nothing outside the disposable container was affected. Fix: `422 self_call_blocked` before any approval exists, a `self_webhook_call` risk on restore previews, and a webhook directory so the model can find the real service. The rerun passed end to end.
- **Skills rejected in repair (2026-09-17).** `get_skill` was declared in repair scope, but every call failed `target_mismatch` because it names no instance. Fixed with `INSTANCE_FREE_TOOLS`.
- **Near-miss skill names ended the chat (2026-09-17).** A real `qwen2.5:3b` asked for `Code`. The strict ID pattern rejected it before the tool ran, and the error ended the conversation. The name is now normalised, and unknown names return a `skill_not_installed` error with the installed IDs.
- **Silent local endpoint failure (2026-09-17).** The prefilled Ollama URL failed with `provider_configuration_invalid` because loopback needs an allowlist entry. The provider form now says so, and says whether the deployment already permits it. The first version always named the loopback value and counted only entries starting with `127.`, `::1/128` or `0.0.0.0/0`. It now names the exact loopback or private IPv4 address as a `/32` and checks real CIDR containment against the allowlist (implemented, verified).
- **Read tool failures ended standard chats (2026-09-17).** Any failing read, such as an n8n 404 or invalid arguments, ended the whole request. Since `087191f`, the failure goes back to the model as `{"error": {"code", "detail"}}` and the turn continues. Session and permission errors still end the request.
- **Local model calls cut off at 45 seconds (2026-09-17).** On `qwen2.5:3b` on CPU (about 4 to 5.5 tokens per second), a follow-up turn with the full 17 KB guide took 175.6 s, and 123.4 s with the prompt prefix cached; an 8 KB excerpt took 164.3 s and a 4 KB excerpt 32.1 s. A skill-guided live chat failed at 47 s with 502 `provider_unavailable`. Local providers (kind `ollama`, or kind `custom` on `localhost` or a loopback or private IP literal) now get a 600-second read and 630-second total timeout; hosted providers keep 45 and 60 seconds. With the change the same chat returned 200 in 243 s (first turn 14.0 s, guide follow-up 228 s), and the model called `get_skill` on its own. Implemented, verified.
- **Ollama truncated the guide-carrying prompt (2026-09-17).** With the full guide injected, Ollama logged `truncating input prompt limit=2050 prompt=5989/6144 keep=4`. It kept only the first few tokens and the end, so the system prompt and the question were dropped, and the model summarised the guide instead of answering the Code node question. Not fixed in Inference: the context has to be raised on the Ollama side (see Open items).

### Verification evidence

| Check | Latest result |
|---|---|
| Backend suite (Windows, Python 3.13) | 895 passed, 11 skipped, 1 warning (all collected tests, verified 2026-09-20) |
| Browser smoke suite (`scripts/smoke_frontend.py`, Playwright with Edge) | 18 PASS, exit code 0 (verified 2026-09-20) |
| Skills end to end on a fresh database | 9/9 |
| Local Ollama `qwen2.5:3b` (save without key, connection test, tool-calling check, chat, model-initiated `get_skill`) | 5/5 |
| Egress hint on the provider form, real browser, two deployments | 8/8 |
| Live repair on real n8n 2.19 with a hosted provider (`scripts/smoke_live_repair.py`) | 10/10 steps, 2026-09-16 |
| Managed n8n 2.19.0 and runners, JavaScript and Python execution, OTLP arrival | Passed 2026-09-14 |
| Official MCP SDK 1.30.0 interoperability | Passed 2026-09-14 |
| Container smoke (`scripts/smoke_container.py`: UID 10001, read-only root, restart persistence) | Passed 2026-09-16 |
| Locked Linux backend suite in container | 886 passed, zero skipped, 2026-09-18; current rebuilt image, 182.98 s. |
| Linux CPython 3.10 compatibility suite (`python:3.10-slim`) | 886 passed, zero skipped, 2 warnings, 2026-09-19 (817.79 s). `requires-python = ">=3.10"` verified. |
| Load and performance benchmark (`scripts/smoke_load.py --confirm`) | Passed 2026-09-19 on Windows & Linux (25k errors, 25k spans, 10 workers). Status 200/429/503 bounds verified. |
| Linux browser smoke suite in container (Playwright bundled Chromium) | 18 PASS, zero errors, exit code 0, 2026-09-19 |
| Live MinIO S3 backup and disaster recovery (`scripts/smoke_s3_backup.py --confirm`) | 19/19 PASS, zero errors, exit code 0, 2026-09-19 (MinIO RELEASE.2024-01-18T22-51-28Z). Full round-trip + 5 failure paths verified. |
| Live n8n version matrix and promotion pipeline (`scripts/smoke_live_matrix.py --confirm`) | 13/13 PASS, zero errors, exit code 0, 2026-09-19 (Run 1: 220.28 s, Run 2: 145.86 s, Run 3 under load after OTLP: 152.74 s). Real cross-instance promotion verified: n8n 2.17.7 (`n8nio/n8n:latest`) -> n8n 2.19.0 (`docker.n8n.io/n8nio/n8n:2.19.0`), preflight AST diff, inactive deployment, remapped credentials, explicit activation, idempotent replay, stale preview rejection, and target outage handling. Operational guidance: requires Docker Desktop, ~2.5 to 4 minutes execution, and an unsaturated host; separated 180-240s startup polling and 45s operational timeouts prevent false timeouts during container initialization. |
| Live OpenTelemetry correlation and silent failure classification (`scripts/smoke_live_otlp.py --confirm`) | 14/14 PASS, zero errors, exit code 0, 2026-09-19 (122.32 s). Real n8n 2.19.0 native OTLP protobuf export, 16 KiB attribute cap, 30s arrival grace protection, conservative incomplete telemetry on un-correlated real spans, and continuation error detection. |
| Live Model Context Protocol (MCP) Streamable HTTP & bearer auth (`scripts/smoke_mcp_auth.py --confirm`) | 11/11 PASS, zero errors, exit code 0, 2026-09-19 (7.02 s). Protocol 2025-11-25, Bearer authentication, session ID negotiation, 15-minute human approval gate, replay rejection, expired proposal rejection, fail-closed auth rejection. |

Skips cover POSIX permission, directory fsync and symlink tests that cannot run on Windows.

### Load benchmark evidence (2026-09-19)

Executed via `python scripts/smoke_load.py --confirm` against an isolated server running with 25,000 seeded errors and 25,000 seeded OTLP spans:
- **Error list retrieval (limit=50):** 239.9 req/s | p50: 39.44 ms | p95: 50.53 ms | p99: 59.37 ms | 100% 200 OK
- **Error list retrieval (limit=100):** 194.4 req/s | p50: 49.40 ms | p95: 56.21 ms | p99: 67.55 ms | 100% 200 OK
- **Observability trace summary:** 37.4 req/s | p50: 252.03 ms | p95: 282.50 ms | p99: 367.32 ms | 100% 200 OK
- **Trace aggregation (GROUP BY trace_id):** 25.8 req/s | p50: 353.62 ms | p95: 594.60 ms | p99: 617.03 ms | 100% 200 OK
- **Session authentication check (`/auth/status`):** 524.1 req/s | p50: 18.20 ms | p95: 25.29 ms | p99: 28.91 ms | 100% 200 OK
- **Admin role check (`/api/admin/users`):** 706.8 req/s | p50: 13.46 ms | p95: 19.41 ms | p99: 26.83 ms | 100% 200 OK
- **Dashboard proxy rate limiter:** 174.7 req/s | 4 allowed (200), 16 rate limited (429) enforcing sliding window protection
- **OTLP span ingestion (5-span protobuf batch):** 224.3 req/s | p50: 42.76 ms | p95: 61.93 ms | bounded by parser semaphore

### S3 backup and disaster-recovery evidence (2026-09-19)

Executed via `python scripts/smoke_s3_backup.py --confirm` against a disposable local MinIO container (`quay.io/minio/minio:RELEASE.2024-01-18T22-51-28Z`) with generated credentials on an ephemeral loopback port:
- **Task A (End-to-end round trip):**
  - Sprouted MinIO and Inference on dynamic ports with `INFERENCE_EGRESS_ALLOW_CIDRS="127.0.0.1/32"`.
  - Seeded real data: admin user via setup bootstrap, n8n instance with encrypted API key, Markdown note with runbook content, and Fernet vault secrets (`$BACKUP_PASSWORD`, `$S3_CREDS`, `$APP_TOKEN`).
  - Configured S3 backup schedule pointing to MinIO endpoint via `$S3_CREDS` vault reference.
  - Triggered manual backup run via API: completed with `status='succeeded'` and `remote_status='uploaded'`.
  - Verified object exists on MinIO disk (`/data/<bucket>/fleet-backups/<id>.ibackup`) and downloaded over S3 via AWS SigV4 GET with matching size (662,234 B) and exact SHA-256.
  - Restored offline into an empty directory using `backend.modules.backups.restore.restore_archive`.
  - Verified restored filesystem artifacts (`dashboard.db`, `.secret_key`, `secrets.json`, note runbook), database rows (users, instances, schedules, runs), and decrypted vault secrets and instance API key.
  - Booted second Inference process against restored directory: `/healthz` returned 200 and admin login succeeded.
- **Task B (Five failure paths):**
  - *Unreachable endpoint:* Scheduled backup against unreachable host recorded `status='partial'`, `code='s3_upload_failed'`, `remote_status='unknown'`; local archive preserved intact.
  - *Wrong credentials (HTTP 403):* MinIO rejected PUT with `SignatureDoesNotMatch`; recorded `status='partial'`, `code='s3_upload_failed'`, `remote_status='rejected'`; local archive preserved.
  - *Nonexistent bucket (HTTP 404):* MinIO rejected PUT with `NoSuchBucket`; recorded `status='partial'`, `code='s3_upload_failed'`, `remote_status='rejected'`; local archive preserved.
  - *Corrupted archive (bit flip):* Restore aborted with `ArchiveError: 'Backup authentication failed'`; destination directory remained empty.
  - *Wrong passphrase on restore:* Restore aborted with `ArchiveError: 'Backup authentication failed'`; destination directory remained empty.
- **Repeat command:**
  ```powershell
  .venv\Scripts\python.exe scripts/smoke_s3_backup.py --confirm
  ```

### Live n8n version matrix and promotion evidence (2026-09-19)

Executed via `python scripts/smoke_live_matrix.py --confirm` against real, distinct n8n Docker containers running on ephemeral loopback ports:
- **Discovered matrix:** Source n8n `n8nio/n8n:latest` (version 2.17.7) -> Target n8n `docker.n8n.io/n8nio/n8n:2.19.0` (version 2.19.0).
- **Provisions & bootstrap:** Disposable Inference instance booted with loopback egress allowlist (`127.0.0.1/32`); admin account initialized with double-submit CSRF protection; both n8n instances onboarded via owner setup and registered in Inference's fleet proxy.
- **Workflow & credential deployment:** Seeded realistic multi-node workflow (Webhook, Code, Sticky Note, Header Auth credential) on Source (2.17.7); deployed matching target credential on Target (2.19.0).
- **Promotion preflight:** Verified AST diff generation, node transformations, credential mapping detection (`can_deploy=True`), and preview ID issuance.
- **Inactive deployment enforcement:** Deployed workflow to Target (2.19.0); verified target workflow created strictly in inactive state (`active: false`) with remapped credential ID.
- **Explicit activation:** Verified workflow activation (`POST /api/n8n_proxy/workflows/{id}/activate`) successfully transitions target workflow to active on remote instance.
- **Three edge cases validated:**
  1. *Idempotent replay:* Re-deploying with identical idempotency key returned cached result; zero duplicate workflows created on target instance.
  2. *Stale preview rejection:* Modifying Source workflow after preflight rejected subsequent deploy with `status='rejected'`, `error_code='stale_preview'`.
  3. *Remote outage handling:* Stopping Target container mid-deploy handled cleanly with `status='rejected'` / `uncertain`; zero blind retry loops.
- **Repeat command:**
  ```powershell
  .venv\Scripts\python.exe scripts/smoke_live_matrix.py --confirm
  ```

### Live OpenTelemetry span correlation and silent failure evidence (2026-09-19)

Executed via `python scripts/smoke_live_otlp.py --confirm` against a real n8n 2.19.0 container exporting native OTLP protobuf traces to Inference:
- **Instance token issuance:** Generated dedicated per-instance machine OTLP bearer token; hash persisted in `telemetry_tokens`.
- **Native OTLP export from n8n 2.19.0:** Container booted with `N8N_OTEL_ENABLED=true`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`, and bearer auth; exported 7 spans across two workflow executions directly to `/api/otel/v1/traces`.
- **Span sanitization & limit verification:** Verified ingested spans adhere to the 16 KiB serialized attribute cap.
- **30s arrival grace window protection:** Classification run immediately after execution confirmed continuation errors are not prematurely flagged before arrival grace elapses.
- **Conservative incomplete telemetry protection:** On un-correlated child spans from real n8n (where `n8n.execution.id` is emitted on root span only), classifier conservatively reported `health_status='incomplete'`, `error_type='incomplete_telemetry'` without false failure alarms.
- **Silent failure classification on confirmed node error:** Flagged continuation failure on continued execution with `health_status='error'`, `error_type='continuation_error'`, and `silent=1`.
- **Healthy execution verification:** Verified healthy execution unflagged (`health_status='ok'`, zero false positives).
- **Repeat command:**
  ```powershell
  .venv\Scripts\python.exe scripts/smoke_live_otlp.py --confirm
  ```

### Live Model Context Protocol (MCP) Streamable HTTP evidence (2026-09-19)

Executed via `python scripts/smoke_mcp_auth.py --confirm` against a standalone Streamable HTTP MCP server (protocol 2025-11-25) with Bearer token authentication:
- **Protocol negotiation:** Initialized Streamable HTTP session; verified session ID negotiation (`Mcp-Session-Id`), server capability advertisement, and tool discovery (`tools/list`).
- **Human approval gate enforcement:** Assistant tool call created 15-minute pending proposal bound to user, session, and exact arguments. Zero autonomous execution permitted.
- **Proposal confirmation:** Executed approved proposal with double-submit CSRF; verified tool result returned cleanly.
- **Replay attack rejection:** Replaying the consumed action ID returned HTTP 409 `action_consumed`.
- **Expired proposal rejection:** Action expired past TTL returned HTTP 409 `approval_unavailable`.
- **Fail-closed authentication:** Outbound tool call with invalid Bearer token returned HTTP 502 `mcp_error` without exposing raw authorization credentials.
- **Repeat command:**
  ```powershell
  .venv\Scripts\python.exe scripts/smoke_mcp_auth.py --confirm
  ```

## What remains

### Release blockers

| Item | Notes |
|---|---|
| Load and performance tests | Completed 2026-09-19 (`scripts/smoke_load.py`). Verified under 10 concurrent workers with 25k seeded errors/spans. |
| Python 3.10 compatibility check | Completed 2026-09-19 in Linux `python:3.10-slim`. 886 passed, zero failures. `requires-python = ">=3.10"` confirmed. |
| Linux browser verification | Completed 2026-09-19 in Linux container (`inference:browser` via `tests/docker/Dockerfile.browser` with Playwright bundled Chromium). 18/18 PASS, exit code 0. |
| Final review | Completed 2026-09-19 (see docs/release-review-2026-09-19.md). Complete-platform conformance and release review completed with zero code blockers; release recommended as ship with caveats. |

### Open items

| Item | Notes |
|---|---|
| Ollama context window | Raise it with `OLLAMA_CONTEXT_LENGTH=8192` for the Ollama process, or a Modelfile with a larger `num_ctx`; the `/v1` endpoint Inference uses does not accept `num_ctx` per request. Verified 2026-09-17: set to 8192 on this machine, no truncation, on-topic answer in 295 s. |
| Vendored skill license & provenance | Resolved 2026-09-19. Pinned exact upstream revision `29d3c31a6e5acc13ada4565e0d07b297bcc03f34` (`v1.21.0`, 86/86 files matching 100%). Explained 43/43 diff against v1.35.0 across 14 subsequent upstream releases. Added verbatim `backend/skills/NOTICES-APACHE-2.0.txt` in compliance with Apache License 2.0 Section 4 for borrowed structure in `using-n8n-mcp-skills`. |

### Product queue

Estimates are for the implementer.

| Item | Estimate |
|---|---|
| Live verification of version-history restore and retry-inactive against real n8n (the feature merged in `2d988a2`; only the live check remains) | about 1 hour |

Shipped since the last update and removed from this queue: readable MCP tool results and Users table status badges (merged before this window); design review re-run, recorded 33/40 in `c234fa9`; ID pickers instead of typed IDs (core helper predates this window, promotion comboboxes in `fd9a16a`); AI repair round 2 (`2d988a2`); keyboard shortcuts, sortable columns, saved views and bulk resolve (`6ec003b`).

### Known limits

- Small local models may still pick near-miss skill names. This is handled: the model gets `skill_not_installed` and the installed IDs, at the cost of one tool round.
- Skill-guided answers on CPU inference are slow: 123 to 228 seconds per follow-up turn with the full guide on `qwen2.5:3b`, and a chat can make up to three provider calls.
- Ollama's default context window can truncate a prompt that carries a guide, dropping the system prompt and the question.
- Standard chat history stores only user messages and final answers, so `get_skill` and other tool calls are not visible in `GET /api/assistant/chats/{id}`.
- Live integrations not yet verified: hosted streaming, MCP SDK 2.x, hosted multi-cloud S3 providers (AWS/R2). (Local S3-compatible MinIO upload/download/restore, cross-instance n8n version matrix promotion, live native OTLP span correlation, and authenticated Streamable HTTP MCP client are verified).
- In real n8n 2.x OpenTelemetry, `n8n.execution.id` is emitted on root workflow spans only, so raw child node spans require trace ID selection to correlate automatically (`backend/modules/observability/router.py:281`). Conservative classification safeguards prevent false-positive alarms by flagging un-correlated spans as `incomplete_telemetry`.
- In real n8n 2.x, continue-on-fail node errors place the error inside output payload `data.main[0][0].json.error` with `executionStatus: 'success'`, requiring payload inspection to detect continuation errors without prior node error normalization (`backend/modules/observability/router.py:248`).
- In workflow promotion, target credential display names differing from source credential display names trigger readback mismatch unless display names match (`backend/modules/n8n_promote/transform.py:114`).
- n8n's update API is not conditional, so an external editor can race a confirmed write after the final read.

### Docker cleanup

Done on 2026-09-17. Deleted `ollama/ollama:0.34.0` (9.18 GB; Ollama now runs natively) and all build cache (3.59 GB). Docker disk use went from 17.97 GB to 5.2 GB.

Kept:

| Image | Reason |
|---|---|
| `inference:local` | Current application image |
| `n8nio/runners:2.19.0` | Used by the app's container feature and the live checks |
| `docker.n8n.io/n8nio/n8n:2.19.0` | Used by the app's container feature and the live checks |
| `n8nio/n8n:latest` | The user's own image; never touched |

Two stopped containers remain: `inference-live-repair` and `n8n`.

Review checkpoint 2026-09-18: current worktree was clean at b8696da before documentation updates. Provider/skill tests freshly passed 68 cases; no full or live suites rerun. The 8192-token Ollama context workaround and three September 17 fixes are already verified, not pending. See the latest IMPLEMENTATION.md entry for the current release/product split.

Verification update 2026-09-18: current application/test images rebuilt successfully; full locked Linux backend suite passed all 886 tests without skips. Two dependency deprecation warnings remain. No application code changes were needed.

Verification update 2026-09-19: Linux browser smoke suite containerized and verified (image `inference:browser` derived from `inference:local` with `playwright==1.62.0` and bundled Chromium). All 18 frontend checks passed with zero errors on Linux amd64. Windows Edge smoke suite re-verified with identical 18 PASS results.

