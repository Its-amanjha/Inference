# Project status

Last updated 2026-09-17. Branch `feat/inference`, 114 commits (first 2026-08-05, latest code commit `eeab7c1`). Pushed to a private GitHub repository.

This report covers what Inference does today, the work done so far and how it was verified, and what remains before release. Detailed dated entries are kept in [IMPLEMENTATION.md](../IMPLEMENTATION.md).

## What Inference does today

Inference is a self-hosted operations dashboard for one or more n8n instances. It is a FastAPI backend with SQLite storage (schema version 7) and a zero-build native JavaScript frontend with 14 API-backed views. It runs as one process, locally or in the provided container.

| Module | What works |
|---|---|
| Foundation (`backend/`) | Environment settings with `INFERENCE_*` names; encrypted Fernet vault; numbered SQLite migrations with schema validation on every open; egress transport with DNS pinning, SSRF protection and CIDR allowlists; security headers; health endpoint. |
| Authentication and admin (`auth`, `admin`) | First-admin bootstrap, scrypt passwords, session cookies, signed CSRF, origin checks, viewer/operator/admin roles rechecked inside transactions, TOTP with recovery codes, users, secrets, audit feed, editable session and default-preference settings, credential templates. |
| Fleet (`n8n_proxy`) | Instance CRUD with encrypted API keys, read-capability probes, fleet health, workflow and execution lists and details, activation, execution deletion, fleet-wide workflow search, dashboard aggregates. Secret parameter values are masked before workflow documents reach a browser. |
| Credentials and promotion (`n8n_credentials`, `n8n_promote`) | Credential schema inspection and explicit mappings; promotion preflight with structural diffs and fingerprints; inactive create or update; separate activation; reconciliation; previous-version restore. Uncertain writes are never retried. |
| CodeLab | Read and replace code in inactive JavaScript and Python Code nodes, with stale-write rejection and readback verification. |
| Observability and errors (`observability`, `errors`) | Token-scoped OTLP ingestion (JSON, protobuf, gzip), trace queries, execution classification, zero-output warnings, exact model pricing with a price book, retention, error triage feed with in-place resolve. |
| Assistant (`assistant`) | OpenAI, OpenRouter, Groq, Anthropic, Ollama and custom OpenAI-compatible providers; guided provider setup that asks a known service only for key and model; connection test and tool-calling check; chat with metadata read tools; approval-only mutations bound to user, session, arguments and resource fingerprint; MCP 2025-11-25 Streamable HTTP client with approval-only calls. |
| AI repair (`assistant`, contract v1) | Pinned repair conversations with masked workflow and error context, one provider request per human action, server-computed previews and risks, writes only to inactive workflows (with a linked deactivation), readback verification, undo through restore, self-call guard, webhook directory with an administrator switch, error-text consent switch. |
| Skills (`skills`) | 15 bundled n8n guides synced from `backend/skills/` at boot, a read-only `/api/skills` API and the `get_skill` assistant tool in standard and repair conversations. See [Skill library](skills.md). |
| Docker manager (`docker_mgr`) | Optional provisioning of owned n8n and runner bundles through a mounted Unix socket, with ownership labels, staged journals, reconciliation and explicit teardown. |
| Notes | Markdown vault with revision-checked edits, wikilink backlinks, search and path-traversal protection. |
| Backups | Encrypted export, offline restore with schema migration, local schedules with run history, optional single-attempt S3 upload. |
| Frontend | 14 views, first-run onboarding, Light, Dark and n8n-inspired themes, AI workspace (chat, repair, manual code editor), action review rail, shared form system, phone layouts. |
| Delivery | Dockerfile pinned by digest, hashed Linux amd64 dependency lock, Compose file with read-only root, dropped capabilities and loopback-only port. |

## Work done

### Size

- 114 commits between 2026-08-05 and 2026-09-17, 30 of them on 2026-09-16 and 2026-09-17.
- Backend: 65 Python files (about 590 KB). Tests: 49 files (about 470 KB) with 518 test functions; parametrised cases bring the suite to 870 collected tests. Scripts: 15 verification and smoke scripts. Frontend: 29 JavaScript files and 9 stylesheets.

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

**Frontend and design**
- `227c2ac` zero-build SPA shell; `8b6f442`, `2b978d4` operational and specialised views; `8b7fdfc` design system and themes.
- `7ff5643` forms and navigation rebuilt; `8e17e72` one control scale; `848e29b` last form conversions, native browser dialogs replaced.
- Phone fixes: `f08eb7e` error type filters scroll on one line; `99f64b9` toasts reserve space so the last controls stay reachable; `e1d923a` proposal code diffs wrap (previously 30 of 82 characters visible); `0d49db1` no duplicate rail card for a completed linked deactivation.

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
| Backend suite (Windows, Python 3.13) | 859 passed, 11 skipped before the latest assistant edits; targeted assistant and skills tests 69 passed after them |
| Browser smoke suite (`scripts/smoke_frontend.py`, Playwright with Edge) | 18 PASS |
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

## What remains

### Release blockers

| Item | Notes |
|---|---|
| Load and performance tests | Completed 2026-09-19 (`scripts/smoke_load.py`). Verified under 10 concurrent workers with 25k seeded errors/spans. |
| Python 3.10 compatibility check | Completed 2026-09-19 in Linux `python:3.10-slim`. 886 passed, zero failures. `requires-python = ">=3.10"` confirmed. |
| Linux browser verification | Completed 2026-09-19 in Linux container (`inference:browser` via `tests/docker/Dockerfile.browser` with Playwright bundled Chromium). 18/18 PASS, exit code 0. |
| Final review | Complete-platform conformance and release review. |

### Open items

| Item | Notes |
|---|---|
| Ollama context window | Raise it with `OLLAMA_CONTEXT_LENGTH=8192` for the Ollama process, or a Modelfile with a larger `num_ctx`; the `/v1` endpoint Inference uses does not accept `num_ctx` per request. Verified 2026-09-17: set to 8192 on this machine, no truncation, on-topic answer in 295 s. |
| Vendored skill license & provenance | Resolved 2026-09-19. Pinned exact upstream revision `29d3c31a6e5acc13ada4565e0d07b297bcc03f34` (`v1.21.0`, 86/86 files matching 100%). Explained 43/43 diff against v1.35.0 across 14 subsequent upstream releases. Added verbatim `backend/skills/NOTICES-APACHE-2.0.txt` in compliance with Apache License 2.0 Section 4 for borrowed structure in `using-n8n-mcp-skills`. |

### Product queue

Estimates are for the implementer.

| Item | Estimate |
|---|---|
| Live version-history restore and retry-inactive against real n8n | about 1 hour |
| Readable MCP tool results and Users table status badges | about 2 hours |
| Design critique re-run (last score 22/40 before fixes A to F) | about 30 minutes |
| ID pickers instead of typed IDs (CodeLab workflow, conversation resume, promotion mappings) | about half a day |
| AI repair round 2: create workflow, retry execution, `notes` field on repair context | about 1 day |
| Keyboard shortcuts, sortable columns, saved views, bulk resolve | about 1 day |

### Known limits

- Small local models may still pick near-miss skill names. This is handled: the model gets `skill_not_installed` and the installed IDs, at the cost of one tool round.
- Skill-guided answers on CPU inference are slow: 123 to 228 seconds per follow-up turn with the full guide on `qwen2.5:3b`, and a chat can make up to three provider calls.
- Ollama's default context window can truncate a prompt that carries a guide, dropping the system prompt and the question.
- Standard chat history stores only user messages and final answers, so `get_skill` and other tool calls are not visible in `GET /api/assistant/chats/{id}`.
- Only `SKILL.md` is served; reference files the guides mention are not available to the model. Some guides assume an n8n-mcp server Inference does not provide.
- There are no user-added skills or skill editing, by decision. Skills change only by replacing `backend/skills/` and restarting (rebuilding the image for containers).
- Live integrations not yet verified: hosted streaming, MCP SDK 2.x and authenticated third-party MCP servers, real S3 upload and disaster-recovery restore, exhaustive OTLP span correlation on real data, n8n version matrix and live cross-instance promotion.
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

