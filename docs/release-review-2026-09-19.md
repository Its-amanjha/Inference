# Inference Conformance and Release Review

**Date:** 2026-09-19  
**Review Target:** `origin/main` commit `2e6aead`  
**Reviewer:** Systems Engineering & Security Reviewer  
**Scope:** Complete-platform conformance, security boundaries, verification audit, and release readiness.

---

## 1. Verdict

**Ship with caveats**: Inference is recommended for release as a self-hosted n8n operations dashboard, OpenTelemetry observability collector, workflow promotion pipeline, and AI repair orchestrator, subject to operational constraints regarding local model inference throughput, provider context limits, and required deployment configuration parameters (specifically `INFERENCE_BOOTSTRAP_TOKEN` and CIDR allowlists).

---

## 2. Blocking Issues

**Zero blocking code defects.** All 886 collected test cases pass cleanly in the locked Linux image (Python 3.10 and 3.13), 875 pass on Windows (11 platform-specific POSIX filesystem tests appropriately skipped), zero broken dependencies exist (`pip check`), and both Linux and Windows browser smoke suites pass 18/18 with zero errors.

### Deployment Prerequisites (Operational Readiness Requirement)

While not a defect in code, the following operational requirement must be observed during deployment:

1. **Headless Bootstrap Token Configuration**
   - **What:** On first boot with an empty database, an administrator account must be established via `POST /api/auth/bootstrap`. The bootstrap endpoint requires the auto-generated bootstrap token printed to server logs unless pre-configured via `INFERENCE_BOOTSTRAP_TOKEN`.
   - **Evidence:** `backend/modules/auth/router.py:48-95`
   - **Impact:** In headless, restricted container environments (such as Kubernetes pods or cloud container services) where stdout logs are delayed or access to loopback terminal is restricted, failure to provide `INFERENCE_BOOTSTRAP_TOKEN` in the container environment will prevent the operator from completing first-run setup.
   - **Suggested Fix / Mitigation:** Include `INFERENCE_BOOTSTRAP_TOKEN` explicitly in production Compose / environment manifests.
   - **Estimated Effort:** 5 minutes (documentation and example `.env` update).

---

## 3. Non-Blocking Issues (Ranked)

1. **Ollama Default Context Window Truncation with Skill Guides**
   - **Evidence:** `docs/status.md:72,121`, `backend/modules/assistant/providers.py:46-120`
   - **Impact:** Ollama's default context window is 2048 tokens (`num_ctx=2048`). When the assistant injects an n8n skill guide (up to 17 KB / ~5,000 tokens) to answer a workflow question, Ollama truncates the prompt prefix, dropping the system prompt and the user question. The model responds with a generic summary of the guide instead of answering the query.
   - **Workaround / Fix:** The operator must run the host Ollama server with `OLLAMA_CONTEXT_LENGTH=8192` or create a custom Modelfile with `PARAMETER num_ctx 8192`. The standard OpenAI-compatible `/v1/chat/completions` API implemented by Ollama does not accept per-request `num_ctx`.
   - **Priority:** High (Operational documentation caveat).

2. **Local CPU Model Latency**
   - **Evidence:** `docs/status.md:71,140`, `backend/modules/assistant/providers.py:46`
   - **Impact:** On CPU-only hosts running quantized models (e.g. `qwen2.5:3b` at 4–5.5 tokens/sec), generating an answer with a full injected guide requires 120–240 seconds per turn. Although backend timeouts have been extended to 600 seconds for local providers (`backend/modules/assistant/providers.py:46`), operators on CPU hardware must be aware of multi-minute turn latency.
   - **Priority:** High (User experience expectation).

3. **Multi-Cloud Hosted S3 Provider Matrix**
   - **Evidence:** `docs/status.md:145`, `backend/modules/backups/s3.py:45-130`, `scripts/smoke_s3_backup.py`
   - **Impact:** The backup subsystem implements AWS SigV4 request signing and single-attempt uploads with verified SHA-256 payload checksums. Local S3-compatible service round-trip (MinIO `RELEASE.2024-01-18T22-51-28Z`), object persistence, S3 download, offline disaster-recovery restore, and 5 failure paths are verified (`scripts/smoke_s3_backup.py --confirm`). However, continuous production verification across third-party hosted cloud providers (AWS S3, Cloudflare R2, Backblaze B2) remains an operational testing item.
   - **Priority:** Medium.

4. **Workflow and Credential ID Entry Ergonomics (Typed IDs vs Pickers)**
   - **Evidence:** `docs/status.md:133`, `frontend/js/views/codelab.js`, `frontend/js/views/promote.js`
   - **Impact:** In CodeLab and the promotion pipeline mapping screens, operators must enter or paste workflow and credential IDs manually rather than selecting them from an autocomplete picker dropdown.
   - **Priority:** Medium (Usability enhancement for v1.1).

5. **Upstream n8n Public API Non-Conditional Update Concurrency**
   - **Evidence:** `docs/status.md:146`, `backend/modules/n8n_proxy/client.py:80-115`, `backend/modules/n8n_promote/promote.py:210-260`
   - **Impact:** Upstream n8n does not support optimistic locking (`If-Match` or ETag headers) on its `PUT /api/v1/workflows/{id}` endpoint. Inference mitigates this by calculating full SHA-256 hashes of the workflow before mutation, rejecting stale operations (`409 workflow_changed`), and performing readback verification. However, an external user concurrently modifying the workflow directly in the n8n UI immediately after Inference's check could theoretically race the write.
   - **Priority:** Low (Inherent upstream API limitation).

6. **Assistant Chat Intermediate Tool Call Persistence**
   - **Evidence:** `docs/status.md:142`, `backend/modules/assistant/router.py:120-175`
   - **Impact:** In standard assistant chat (`GET /api/assistant/chats/{id}`), SQLite documents persist the user messages and final assistant answers, but intermediate tool calls (such as `get_skill` lookups or `list_workflows` inspections) are held in memory during the turn and not recorded in persistent chat history.
   - **Priority:** Low (Intentional privacy/storage design choice, but worth noting for audit continuity).

---

## 4. Conformance Table

| Capability | Status | Evidence (Files, Routes, and Tests) |
|---|---|---|
| **FastAPI & Async Architecture** | Conforming | Single application process, `aiosqlite` with WAL and foreign keys enabled; numbered migrations up to `SCHEMA_VERSION = 7`. `backend/database.py:35-140`, `backend/main.py:45-120`. Tested in `tests/test_database.py`. |
| **Encrypted Secrets Vault** | Conforming | Fernet encryption with 0o600 permissions on `.secret_key` and `secrets.json`. Environment variable precedence (`$VAR`). Atomic file writes. `backend/config.py:130-185`. Tested in `tests/test_config.py`. |
| **SSRF & Egress Security** | Conforming | Pinned DNS resolution (`PinnedAsyncHTTPTransport`), strict RFC1918 / link-local / cloud-metadata blocking; CIDR allowlist support. `backend/net.py:45-95`. Tested in `tests/test_net.py`. |
| **Authentication & RBAC** | Conforming | Scrypt password hashing, session cookies (`HttpOnly`, `SameSite=Strict`), double-submit signed CSRF (`X-AGD-CSRF`), 3-tier RBAC (`viewer` < `operator` < `admin`), live role re-checks in transactions. `backend/auth_gate.py:90-215`. Tested in `tests/test_auth_gate.py`, `tests/test_auth_service.py`. |
| **TOTP Two-Factor Auth** | Conforming | Encrypted TOTP secret generation, 6-digit verification, single-use recovery codes, confirmation enforcement. `backend/modules/auth/service.py:120-190`. Tested in `tests/test_auth_service.py`. |
| **Multi-Instance Fleet Proxy** | Conforming | Multi-instance CRUD with encrypted API keys, capability probing, workflow/execution proxying, execution deletion, sensitive parameter masking. `backend/modules/n8n_proxy/client.py`, `router.py`. Tested in `tests/test_n8n_proxy.py`. |
| **CodeLab Node Editor** | Conforming | Read and update JavaScript/Python Code nodes in inactive workflows; SHA-256 workflow hash verification (`expected_hash`); readback validation; Monaco CDN loader with textarea fallback. `backend/modules/n8n_proxy/router.py:280-350`, `frontend/js/views/codelab.js`. Tested in `tests/test_n8n_proxy.py`. |
| **Workflow Promotion Pipeline** | Conforming | AST diffs, credential and secret mapping, preview generation (`/preflight`), inactive deployment (`/deploy`), idempotency keys, separate activation, undo restore. `backend/modules/n8n_promote/promote.py`, `router.py`. Tested in `tests/test_n8n_promote.py`. |
| **OTLP Trace Collector** | Conforming | Protobuf and JSON ingestion at `POST /api/otel/v1/traces`, per-instance bearer token, parser concurrency limit, 16 KiB span attribute cap, gzip support. `backend/modules/observability/ingest.py`, `router.py`. Tested in `tests/test_observability.py`. |
| **Silent Error Classification** | Conforming | 30s arrival grace, branch/filter/loop awareness, false-positive prevention, zero-output detection. `backend/modules/observability/health.py`. Tested in `tests/test_health_classifier.py`. |
| **Model Cost Engine** | Conforming | 4-layer price book (Overrides > OpenRouter cache > Bundled defaults > Local Ollama $0), exact decimal math, cached/reasoning token tracking. `backend/modules/observability/pricing.py`, `cost.py`. Tested in `tests/test_pricing.py`. |
| **AI Assistant & Human Approvals** | Conforming | Multi-provider LLM connector (OpenAI, OpenRouter, Groq, Anthropic, Ollama), 15-minute TTL pending action queue, atomic single-use confirmation, resource fingerprinting. `backend/modules/assistant/approvals.py`, `providers.py`, `tools.py`. Tested in `tests/test_assistant.py`. |
| **MCP Streamable HTTP Client** | Conforming | MCP 2025-11-25 Streamable HTTP client implementation, tool discovery, approval gate enforcement for tool calls. `backend/modules/assistant/mcp.py`. Tested in `tests/test_assistant_mcp.py`. |
| **AI Workflow Repair (Contract v1)** | Conforming | Pinned repair conversations, masked error context, single-action turns, server-computed diffs/risks, inactive-only writes, readback verification, recursive self-call blocker (`422 self_call_blocked`). `backend/modules/assistant/repair.py`. Tested in `tests/test_assistant_repair.py`. |
| **Bundled Skill Library** | Conforming | 15 bundled n8n guides synced from `backend/skills/` to SQLite, read-only `/api/skills` API, `get_skill` assistant tool, Apache 2.0 notice compliance. `backend/modules/skills/router.py`, `backend/skills/`. Tested in `tests/test_skills.py`. |
| **Docker Manager** | Conforming | aiodocker Unix socket client, Kahn's topological sort for container dependencies, 2-container bundle provisioning (n8n + runners), ownership label enforcement (`inference.managed=true`). `backend/modules/docker_mgr/deployer.py`, `bundle.py`, `client.py`. Tested in `tests/test_docker_mgr.py`. |
| **Markdown Notes Vault** | Conforming | Local notes filesystem vault, strict path traversal blocking, wikilink backlink scanner (`[[Note]]`), literal text search, atomic saves, escaped HTML preview. `backend/modules/notes/storage.py`, `router.py`. Tested in `tests/test_notes.py`. |
| **Encrypted Backups & Offline Restore**| Conforming | Scrypt + Fernet encrypted `.ibackup` archive, offline CLI restore with staging and integrity verification, schema v5-v7 migration, scheduled local retention and AWS SigV4 upload. Live MinIO round-trip and 5 failure paths verified. `backend/modules/backups/archive.py`, `restore.py`, `s3.py`. Tested in `tests/test_backups.py`, `scripts/smoke_s3_backup.py`. |
| **Zero-Build Native Frontend** | Conforming | 14 ES module views, zero build step, dark cobalt/cyan and light themes, responsive mobile layout, action review rail, double-submit CSRF self-healing wrapper (`apiFetch`). `frontend/index.html`, `frontend/js/app.js`, `frontend/css/`. Verified in Playwright test suites. |

---

## 5. Security Findings Table

| Area | Severity | Finding | Evidence | Resolution / Mitigation |
|---|---|---|---|---|
| **Egress / SSRF Protection** | Informational | All outbound HTTP requests (n8n proxy, LLM providers, MCP servers, S3, OTLP) route strictly through `create_http_client` with pinned DNS and IP address filtering. Cloud metadata (`169.254.169.254`), link-local, and loopback are blocked by default. | `backend/net.py:45-95`, `backend/config.py:112` | Design is robust and verified. Operators configure `INFERENCE_EGRESS_ALLOW_CIDRS` if private network endpoints are required. |
| **CSRF & Origin Verification** | Informational | Mutating requests (`POST`, `PUT`, `DELETE`, `PATCH`) enforce double-submit CSRF tokens and validate origin against allowed origins and host headers. Machine OTLP ingestion is isolated with dedicated per-instance tokens. | `backend/auth_gate.py:126-175`, `backend/modules/observability/router.py:40-60` | Design verified. Frontend `apiFetch` handles CSRF token refresh automatically on `csrf_failed`. |
| **Authorization & RBAC** | Informational | Session-based role checks (`viewer`, `operator`, `admin`) are enforced at router entry and re-verified via `require_current_role` inside write transactions to prevent time-of-check to time-of-use (TOCTOU) elevation. | `backend/auth_gate.py:90-215` | Fully implemented across all mutating endpoints. |
| **Fernet Vault & Key Security** | Informational | Encryption keys are stored in `data/.secret_key` with best-effort POSIX `0o600` permissions. Missing key with existing ciphertext causes startup to fail closed. | `backend/config.py:130-185` | Robust fail-closed design verified in unit tests. |
| **Human Approval Gate for AI** | Informational | AI Assistant tools cannot execute destructive actions autonomously. Any mutating tool queues a 15-minute pending action bound to the specific user, session, target, and arguments. Replay attacks are blocked via atomic single-use claims. | `backend/modules/assistant/approvals.py:35-120` | Robust defense against prompt injection and autonomous hallucinated mutations. |
| **Docker Socket Isolation** | Informational | Docker management commands strictly filter by label `inference.managed=true` and bundle ID. Host containers without this label cannot be stopped, inspected, or deleted. | `backend/modules/docker_mgr/client.py:40-95`, `backend/modules/docker_mgr/deployer.py:50-80` | Host workloads are isolated from managed automation instances. |
| **Backup Archive Path Traversal** | Informational | Offline restore staging enforces canonical path checks, rejecting `..`, absolute paths, symlinks, and non-regular entries. Staged SQLite database is checked for foreign key and schema integrity before placement. | `backend/modules/backups/archive.py:45-110`, `backend/modules/backups/restore.py:35-105` | Defense against malicious ZIP archive injection. |
| **Notes Vault Path Traversal** | Informational | File paths are validated against Windows reserved device names (`CON`, `PRN`, etc.), null bytes, traversal segments, and symlinks. Synchronous filesystem operations run in worker threads with locks. | `backend/modules/notes/storage.py:45-90` | Verified against path traversal attacks. |

---

## 6. Verification Method: Executed vs Read-Only

### Verified by Execution (Direct Test and Benchmark Runs)

1. **Full Backend Pytest Suite (Windows, Python 3.13)**:
   - Command: `pytest -q` in `D:\n8n-final-review`
   - Result: 875 passed, 11 skipped, 1 warning (11 skips are platform-dependent POSIX symlink and fsync checks).
2. **Full Backend Pytest Suite (Locked Linux Container, Python 3.13)**:
   - Command: Rebuilt container image `inference:local` on Linux amd64
   - Result: 886 passed, 0 skipped, 182.98 seconds execution time.
3. **CPython 3.10 Compatibility Suite (Linux Container, Python 3.10-slim)**:
   - Command: Containerized pytest in `python:3.10-slim`
   - Result: 886 passed, 0 skipped, 2 deprecation warnings (`requires-python = ">=3.10"` confirmed).
4. **Load and Performance Benchmark**:
   - Command: `python scripts/smoke_load.py --confirm`
   - Result: 25,000 errors and 25,000 spans loaded. Tested across 10 concurrent workers. Error retrieval p95 < 60ms; authentication checks > 500 req/s; rate limiting verified (status 429 triggered as configured).
5. **Headless Browser Smoke Suite (Linux Container, Playwright Chromium)**:
   - Command: `tests/docker/Dockerfile.browser`
   - Result: 18/18 PASS, exit code 0.
6. **Headless Browser Smoke Suite (Windows, Playwright Edge)**:
   - Command: `python scripts/smoke_frontend.py`
   - Result: 18/18 PASS.
7. **Python Package Consistency**:
   - Command: `python -m pip check`
   - Result: 0 broken requirements.
8. **Live AI Repair End-to-End**:
   - Command: `python scripts/smoke_live_repair.py` against real n8n 2.19.0 container
   - Result: 10/10 steps passed (including self-call blocker verification and undo restore).
9. **MCP SDK Interoperability**:
   - Result: Official MCP SDK 1.30.0 Streamable HTTP verified.
10. **Native Ollama Integration**:
    - Result: `qwen2.5:3b` tool-calling, connection verification, and prompt truncation limits verified.
11. **Live MinIO S3 Backup and Disaster Recovery**:
    - Command: `python scripts/smoke_s3_backup.py --confirm` against disposable MinIO container (`quay.io/minio/minio:RELEASE.2024-01-18T22-51-28Z`)
    - Result: 19/19 PASS (exit code 0). Complete round-trip verified: AWS SigV4 upload, S3 SigV4 download, offline restore into clean directory, database row validation, vault secret decryption, and restored application server boot. Five failure paths verified: unreachable endpoint (`partial`/`s3_upload_failed`/`unknown`), bad credentials 403 (`partial`/`s3_upload_failed`/`rejected`), nonexistent bucket 404 (`partial`/`s3_upload_failed`/`rejected`), corrupted archive bit flip (`ArchiveError`), and wrong restore passphrase (`ArchiveError`). Local archives preserved on all upload failures.

### Verified by Static Code Analysis and Reading

1. **Security & Cryptographic Boundaries**:
   - Audited `backend/auth_gate.py`, `backend/net.py`, and `backend/config.py` for token verification, timing safety, and header inspection.
2. **Promotion Pipeline Remapping AST**:
   - Audited `backend/modules/n8n_promote/promote.py` to verify credential and subworkflow remapping occurs strictly in known node parameter fields rather than global string replacement.
3. **Database Concurrency & Transaction Serialization**:
   - Audited `backend/database.py` connection locking and migration integrity (`PRAGMA user_version` tracking).
4. **Docker Client Command Generation**:
   - Audited `backend/modules/docker_mgr/deployer.py` and `bundle.py` to confirm network creation, environment variable injection, and teardown constraints.
5. **Notes Sanitization & Search Algorithms**:
   - Audited `backend/modules/notes/storage.py` path normalization and backlink regex scanning.
6. **Frontend View DOM Lifecycles**:
   - Audited dynamic module unloading, listener detachment, and cleanups in `frontend/js/app.js` and view components.

---

## 7. Open Questions for the Maintainer

1. **Ollama Context Length Documentation and Defaults**:
   Should the quickstart guide and container documentation explicitly advise setting `OLLAMA_CONTEXT_LENGTH=8192` when pairing Inference with a local Ollama instance, or should Inference detect an Ollama provider and surface an advisory badge if the response indicates truncation?
2. **Upstream n8n Real-Time Collaboration**:
   Since n8n's public API lacks `If-Match` ETags, Inference validates SHA-256 hashes prior to writes. Would connecting an optional WebSocket listener to n8n's internal execution bus to detect external edits in real time be desirable for v1.1?
3. **Multi-Cloud S3 Disaster Recovery Test Matrix**:
   AWS SigV4 is tested and verified locally, but should an automated CI matrix be added to continuously test compatibility against MinIO, Cloudflare R2, and Backblaze B2 endpoints?
4. **Standard Chat Intermediate Tool History**:
   Should intermediate read tool calls (e.g. `get_skill`, `list_workflows`) be preserved in the persistent chat database, or is retaining only the user prompt and final assistant answer the desired permanent privacy/storage design?
5. **ID Selection UX vs Direct Typed Entry**:
   Currently, operators input workflow IDs and credential IDs directly. Would a searchable modal picker for workflows and credentials improve operator ergonomics in v1.1?
