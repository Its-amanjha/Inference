# Inference Implementation Plan

> For agentic workers: use superpowers:subagent-driven-development for independent subsystem implementation and review. Preserve all approved features.

**Goal:** Build the complete Inference brief with the approved correctness and security upgrades.
**Architecture:** Single FastAPI service, SQLite transactional storage, encrypted vault, native ES module frontend, capability-aware n8n connections.
**Tech Stack:** Python >=3.10, FastAPI, httpx, aiosqlite, cryptography, protobuf/OTLP, aiodocker, boto3; zero Node frontend tooling.
**Spec:** [docs/superpowers/specs/2026-09-07-inference-design.md](../specs/2026-09-07-inference-design.md)

## Global constraints

All features in docs/original-brief.md remain in scope. One process, local persistence, no external writes during development checks. Shared interfaces in the spec are binding. Each subsystem owns only its files and tests. Read the existing shared code before adapting integration; do not introduce stubs to mask missing services.

## Task 1: Foundation and authentication
- [ ] Create pyproject, Docker configuration, backend config/database/net/auth_gate/audit/websocket/main, auth/admin modules.
- [x] Write tests for setup/login/session CSRF, roles, vault interpolation/key loss and safe network destinations. Run to show missing behavior, implement, rerun.
- [x] Create lifecycle resources and discover validated module manifests with explicit prefixes; expose static assets and /healthz.
- [x] Supply all Database, Vault, request state and N8nClient interfaces specified in the design.

Progress: backend foundation and pyproject are implemented. Docker configuration remains unchecked; static serving is wired but frontend assets do not yet exist. Admin secret templates and editable session/default-preference settings are implemented; deployment security settings remain environment-controlled.

## Task 2: Fleet, credentials and promotions
- [x] Implement n8n_proxy, n8n_credentials and n8n_promote modules and tests/test_fleet.py.
- [x] Use httpx MockTransport for remote API boundary while exercising real services/database.
- [x] Verify independent concurrent instance calls, cursor pagination, explicit mappings, stale preflight, repeated deployment, partially failed credential creation and uncertain upstream writes.
- [ ] Public interfaces and preview/deploy behavior are exactly those in the spec; document route payloads in docs/api.md.

Progress: API guides exist at docs/api.md, api-fleet.md and api-promotions.md. Inactive deployment, separate activation, reconciliation, previous-version restoration and CodeLab stale-safe updates are locally verified. The broad conformance item remains open for complete-platform review and live n8n compatibility. Credential-bearing promotion activation currently requires admin discovery access; the general fleet activation API remains operator-accessible.

## Task 3: Telemetry, health and costs
- [x] Implement observability and errors modules and tests/test_observability.py.
- [x] Verify actual protobuf/JSON requests, gzip boundaries, per-instance tokens, duplicates, atomic ingestion, retention and unknown-price arithmetic.
- [x] Exercise all classifier modes with branch/wait/sampling fixtures and test that incomplete evidence does not emit confirmed errors.

Progress: telemetry ingestion/query/triage, automatic and explicit classification with cached terminal evidence, durable zero-output preferences, exact pricing, opt-in live price refresh and retention are implemented. Polling uses revocable admin grants, bounded work and per-execution backoff. Live exporter/load verification remains. Task 4 assistant and Docker backends are now implemented; notes backend is also implemented; resume with backups. CodeLab backend updates are implemented.

## Task 4: Assistant, containers, notes and backups
- [x] Implement assistant provider adapters/MCP/tools/approval state, Docker topology/deployer, notes and backup modules.
- [x] Write tests for approval one-time claim, wrong user, expiry, stale state; bundle cycle/ownership; notes traversal; encrypted backup restore into fresh data directory.
- [x] Persist schedules and run background tasks with graceful cancellation; fail closed on unavailable configuration without fake success.
- [x] Assistant backend: provider adapters including keyless Ollama/custom, MCP client, encrypted chat/configuration, metadata tools and durable approval actions.
- [x] Verify approval replay/session/role/stale/expiry/cancellation, provider wire formats, MCP JSON/SSE/handshake/schema changes and real loopback local-model transport.
- [x] Docker backend: owned matching-version n8n/runner bundles, Kahn ordering, readiness, encrypted journals/idempotency, partial outcomes/reconciliation, start/stop/removal and explicit volume purge.
- [x] Verify Docker ownership/replacement, readiness deadline, missing resources, role changes, restart/cancellation, SDK progress errors and simulated Engine transport. Extend key-loss protection to encrypted journals/documents.

- [x] Notes backend: atomic revision-checked markdown storage, path protection, bounded literal search/backlinks, escaped previews, permissions/audit and cancellation tests.

- [x] Backup export/offline restore: separate-passphrase authenticated archive, SQLite/vault/config/notes capture, staging validation, no overwrite, live authorization/cancellation and restored-startup tests.

Progress: assistant, Docker, notes and backup export/restore APIs are implemented; real inference/provider/MCP/Docker compatibility and their frontend views remain unverified. MCP confirmations bind tool definitions and configuration but cannot assert generic remote resource state. Backup schedules, persisted history and S3 are locally verified; live S3/disaster-recovery and backup frontend checks remain; the notes frontend/styled Markdown rendering and POSIX runtime checks remain. Docker delivery configuration and live JavaScript/Python/OTLP checks remain separate from fixture tests.

## Task 5: Full frontend
- [ ] Implement every original frontend view with actual APIs, shared apiFetch and DOM helpers, responsive shell, dark/light themes and lazy Monaco.
- [ ] Document product/design direction from original brief. Use native accessible controls and render untrusted strings safely.
- [ ] Run browser desktop/mobile and navigation checks, collect screenshots and fix functional issues once as a batch.

## Task 6: Integration and delivery
- [ ] Run `python -m pytest -q`, Python compilation and route/OpenAPI checks, `docker compose config --quiet`, image build if daemon available.
- [ ] Run startup/login/fleet/promotion/telemetry/backup browser/API scenarios using local fixtures, inspect final spec coverage, request independent final review and address material findings.
- [ ] Write README, configuration guide, external integration limitations and restore instructions. Report exact tested capabilities and any remaining prerequisites honestly.

## Session checkpoint — 2026-09-09

- Last completed unit: automatic backups, persisted schedules/run history, bounded local files/download/deletion/reconciliation and optional single-attempt S3 upload. Schema v6 and v5 archive restore migration implemented. Combined schedules/S3 checks: 40 passed; final schedule checks after manual-run audit attribution correction: 16 passed. Full regression before that final correction: 627 passed, 11 platform-dependent skips, one Starlette/httpx warning (214.89 seconds). Compilation and eight backup OpenAPI paths passed.
- Separate real Uvicorn smoke passed after final changes: auth/CSRF, scheduled backup/download/history, export/restore, restored-server login/vault/notes/telemetry/schedules, audit and cleanup. Real verified loopback S3-shaped PUT and AWS signing-vector tests pass; no live cloud bucket was used.
- Real loopback fleet/promotion/model tests use production HTTP transport; the simulated Engine uses installed aiodocker. They do not establish live compatibility. This session's Docker Desktop Linux engine check failed because its named pipe was unavailable. No real images/containers/networks/volumes were changed.
- Workspace: the repository root; branch feat/inference; files are saved but uncommitted. Reuse .venv (Python 3.13.14, system site packages, jsonschema, opentelemetry-proto, protobuf, aiodocker 0.27.0 and aiohttp 3.14.3 installed); do not restart dependency setup blindly. Schema is v6; backup schedules/runs now have dedicated tables and Docker journals retain encrypted documents.
- Resume next: native frontend shell/onboarding/apiFetch/themes, all 14 views, then delivery. Acquire notes_lock before database snapshot operations. Remaining checks: live exporter/load behavior; real provider/model/MCP interoperability; actual Docker n8n/runner execution and OTLP delivery; POSIX notes/restore filesystem behavior; live S3/disaster recovery; all 14 frontend views and browser checks; delivery/build/live version matrix. Docker ownership/readiness/recovery, assistant approval security and earlier telemetry/fleet checks are locally covered.
- Read AGENTS.md for unchanged product requirements and IMPLEMENTATION.md for the newest results. Checkbox updates and this checkpoint record progress only; they do not reduce the goal or rewrite the specification.

## Frontend checkpoint — 2026-09-10

- [x] Native shell/onboarding, shared safe API client, themes and all 14 API-backed views implemented with the latest supplied light design.
- [x] Real local browser checks: setup, empty-fleet views, saved/reloaded notes, escaped content, persisted theme, mobile navigation and login/logout. Desktop/mobile screenshots reviewed.
- [x] Full regression: 654 passed, 11 skipped.
- [ ] Populated end-to-end frontend mutations and final cross-module conformance review.
- [ ] Styled Markdown preview and remaining delivery/live integration checks.

This checkpoint supersedes earlier statements that frontend implementation has not started. Live integrations remain unverified. See IMPLEMENTATION.md for current evidence.

- [x] Populated fleet browser scenarios through real local HTTP: selected-instance activation, CodeLab save/stale rejection, stale promotion rejection, inactive cross-instance promotion and idempotent replay. Verified 2026-09-10.
- [x] Populated assistant approval confirm/cancel/stale-rejection/replay browser scenarios, including page reload persistence (2026-09-10).

- [x] Populated administration/backup browser operations, including downloaded archive restore (2026-09-10).

- [x] Safe styled Markdown preview for Notes, browser formatting/security checks passed (2026-09-10).

- [ ] Next: final requirement conformance review and delivery packaging.

- [x] Dockerfile/Compose/socket override, Docker image build and real container auth/frontend/notes/restart persistence (2026-09-10).
- [x] Source conformance checkpoint: docs/conformance-review.md; release conformance is not complete.
- [x] Apply saved row-count and notification preferences; browser behavior verified (2026-09-10).
- [ ] Close dashboard/fleet aggregation, JSON theme assets, reproducibility and remaining live integration gaps.

- [x] JSON theme assets and validated loader; browser switching/race/failure/retry checks passed (2026-09-10).

- [x] Fleet-wide workflow search with explicit mutation targets; browser search/pagination/isolation/partial-failure checks passed (2026-09-11).

- [x] Dashboard totals and 24-hour aggregates with explicit coverage, scoped/backend/browser verification (2026-09-11).

- [x] Release dependency lock/base-image digest; rebuilt and verified locked image (2026-09-11).

- [x] Locked Linux backend regression and POSIX checks: 671 passed, zero skipped (2026-09-11).

- [ ] Next bounded unit: owned live n8n/matching-runner bundle, JavaScript/Python execution and OTLP compatibility checks.
