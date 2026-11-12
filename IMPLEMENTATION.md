# Inference — implementation progress

Updated: 2026-09-17. Product: **Inference** (Execution Anomaly System). Checkpoint entries below are chronological.

## Status at a glance — 2026-09-15

**Done and verified**
- All backend modules: auth/admin, fleet, credentials, promotion, CodeLab, OTLP observability/classification/pricing, assistant (providers, MCP, approvals), Docker bundles, notes, encrypted backups with schedules and S3 upload.
- All 14 frontend views, three themes (Light, Dark, n8n-inspired), AI workspace, guided model setup, dashboard aggregates, fleet-wide workflow search.
- Inference deployment identifiers: 673 backend tests passed (Windows), foundation, browser and container smoke passed; `inference:local` image rebuilt; branch `feat/inference`.
- Live checks: managed n8n 2.19.0 + runners (JavaScript/Python, proxy history, OTLP arrival); official MCP SDK 1.30.0; hosted custom OpenAI-compatible provider non-streaming chat.
- Frontend critique (22/40) and fix A: incomplete telemetry uses a dashed amber partial-data note, never failure styling (browser verified).
- Frontend critique groups B–F (browser verified 2026-09-15): B dense error triage table with severity pills, relative times, execution IDs, type filters and in-place resolve, plus dashboard finding evidence and non-heading metric values; C live-update bar instead of view rebuilds, dismissible de-duplicated bottom toasts, nav group labels no longer headings, theme picker sync; D plain-language promotion preview/run results, approval sentences, fleet-health and connection checklists with raw payload disclosures; E crimson destructive actions, target-naming confirmation dialog with typed PURGE, More actions menus, workflow Active/Inactive badges, progressive two-factor setup, audit outcome badges; F 12px text floor, sentence-case metric labels, 24/44px touch targets, unclipped selects, right-aligned numeric columns, accessible failed-span trace bars, neutral n8n notices; SVG favicon.

**In progress**
- AI repair workspace round 1 (scope A: diagnose, patch, undo, linked deactivate/activate, "Supports repair" check, error-text consent) — **verified locally 2026-09-16** with the full backend suite and the complete browser smoke against the loopback n8n simulator and a scripted provider; live n8n 2.19 + hosted-model verification and round 2 (create, retry) remain. Original plan notes (user-approved 2026-09-15): one AI workspace window where a human asks to repair or create a workflow; the AI diagnoses and proposes, the exact change appears inline as a server-computed diff, and the human clicks Apply once (creation: preview, then create switched off; activation stays a separate confirmation). No autonomous writes: every mutation keeps the actor/session/argument-bound approval gate. Today's assistant can only list workflows, read execution metadata and error metadata (no messages), propose activate/deactivate/delete execution, and replace Code node code in inactive workflows; it cannot read error details, patch general nodes/connections, create workflows or test fixes. Plan ready 2026-09-15, awaiting user decisions:
  - Backend: new `assistant/repair_contract.py` (strict operation schemas: set/remove parameter, set code, add/remove node, enable/disable, connect/disconnect, allow-listed settings; create, restore, retry arguments), `repair_context.py` (masked workflow and scrubbed error context with size caps, marked untrusted remote text), `repair_diff.py` (server-computed before/after changes, code line diffs, risk flags), `repair_actions.py` (confirm-time re-fetch, content-hash binding that excludes `active`, write only to inactive workflows, before-snapshot, single PUT/POST, readback verification, uncertain on mismatch, read-only reconcile, never retried). Tools: `get_workflow_context`, `get_error_context`, `propose_workflow_patch`, `propose_create_workflow`, `propose_restore_workflow` (snapshot or version history), `propose_retry_execution` (`loadWorkflow: true`, typed RETRY). Allow-list adds version-history GET and execution retry POST only. Chat gains a pinned repair/create target (`target_mismatch` otherwise); MCP hidden in repair mode; no database migration.
  - Frontend: AI workspace keeps a manual Code editor tab; toolbar target chip; "What the AI sees" disclosure; inline proposal cards with the server diff, risks, preconditions (linked deactivate), Apply/Cancel, stale/uncertain states, Undo/Retry/Activate follow-ups; "Repair with AI" entry points on errors, dashboard findings and workflows; new `lib/proposals.js`, `lib/repair-target.js`, `css/repair.css`. Added by the lead: per-connection "Supports repair" tool-calling check.
  - Tests: pure schema/masking/diff tests, approval-gate tests (no write before confirm, stale with zero writes, single-use, uncertain without second write, create/restore/retry, roles), simulator extensions with secret-bearing error fixtures, a browser repair scenario and a live n8n 2.19 check.
  - Phases: contract 0.5 day; four parallel agents with exclusive files 2–3 days; integration 1 day; hardening 1–2 days; live verification 1 day.
  - User decisions (2026-09-15): repairs write only to inactive workflows; an active target gets a linked Deactivate proposal first and a separate Activate afterwards. The scrubbed error summary (message, description, HTTP code, node; secrets masked; no items, stack, context or run data) may be sent to the model provider only in repair conversations, with a per-conversation toggle and an admin off switch; other conversations keep the default of sending no execution payloads.
  - Scope decided 2026-09-15: A, repair first. Round 1 builds context reads, workflow patch, restore (undo), linked deactivate/activate, the per-connection "Supports repair" tool-calling check and the error-text consent controls. Create and retry follow in round 2. Build started: the lead froze contract v1 in `backend/modules/assistant/repair_contract.py` with `tests/fixtures/repair_proposal_v1.json`; four parallel agents implement against it with exclusive file ownership. Additions adopted from plan revision 2 after the build started (sent to the owning agents): one model request per human action in repair mode with turn status `completed | awaiting_continue | interrupted`, ordered steps and explicit Continue / Try again; provider 429 → `provider_rate_limited` and truncated output → `provider_incomplete`, never retried; tool declarations flattened (no `$ref`/`discriminator`) for every provider kind; tool-calling check results reset when the connection changes and a rate-limited check never overwrites an earlier pass.
  - Build progress (2026-09-16, unverified end to end until the full suite and browser smoke pass): agent A finished `repair_context.py`/`repair_diff.py` (86 focused tests passed); agent B finished tools, approvals, router, providers, `repair_actions.py` and the client allow-list (256 assistant/client/CodeLab/promotion tests passed; only a viewer `GET /workflows/{id}/{uuid}` history route was added); agent D finished simulator extensions, `scripts/frontend_repair_checks.py` (wired into the browser smoke) and a guarded `smoke_live_repair.py` skeleton; agent C finished the frontend (one AI workspace window with "AI workspace" and "Code editor (manual)" tabs, pinned repair target, "What the AI sees", inline proposal cards with server diff, preconditions, stale/uncertain/succeeded states, Undo and Activate follow-ups, repair turn steps with Continue and Try again, "Supports repair" badge and admin check, admin error-text setting, "Repair with AI" links on errors, dashboard findings and workflows; `lib/proposals.js`, `lib/repair-target.js`, `css/repair.css`). Security fix during integration: the repair UI no longer reads raw workflow documents; target names come from the masked repair context and name search/approval-card names use the `list_workflows` tool (id/name/active only; first 25 workflows per instance searchable). Browser secret scan is strict for everything received on the AI conversation view; pre-existing Workflows/CodeLab raw-workflow reads are reported, not failed. Complete `scripts/smoke_frontend.py` with the repair scenario then **passed** (2026-09-16): tool-calling check (scripted provider "Supports repair: yes"; a rate-limited re-check keeps it without retry); deep link pins the target and "What the AI sees" masks header and error-text secrets; one provider request per human action (text, 429 + Try again, cut-off, read tool + Continue) with no secret sent to the provider; inline server diffs; active workflows blocked in UI and API (409); linked deactivate; stale apply writes nothing; apply writes exactly one PUT with verified readback and replay 409; undo restores with one PUT; readback divergence is uncertain and reconcile writes nothing; admin switch withholds error text; "Repair with AI" links on errors, workflows and dashboard findings; strict secret scan on the AI conversation view (96 responses) with only the pre-existing Workflows view raw proxy read reported; all earlier fleet, approval, admin/backup, design, theme, guided setup and 14-view checks passed with no page errors. Test-harness repairs during integration (no product changes): Playwright listeners must be Python functions or bound methods (not `list.append`); the secret scan reads bodies only for `requestfinished` requests (an aborted request's `body()` never resolved and hung one run, which was stopped); setup check uses the renamed tabs. Desktop and 390px repair screenshots inspected. Follow-ups: on mobile the bounded chat panel leaves little room for proposal diffs when "What the AI sees" is open; linked deactivate proposals also appear in the Action review rail as separate cards with "Confirm exact action", duplicating the card's own Deactivate button. Local preview restarted with the repair backend (`/api/assistant/repair/settings` answers 401 unauthenticated). `docker build -t inference:local .` passed with repair round 1 and `scripts/smoke_container.py` **passed** (UID 10001, read-only root, frontend/auth/notes, restart persistence; no leftover smoke containers, volumes or dangling images).
- Live repair verification (2026-09-16, user-approved, disposable `docker.n8n.io/n8nio/n8n:2.19.0` container labelled `inference.live-repair=true` on 127.0.0.1:5681, local preview restarted with `INFERENCE_EGRESS_ALLOW_CIDRS=127.0.0.1/32`, a hosted OpenAI-compatible model with tool calling enabled on the saved connection). Setup: an order-service webhook workflow plus "Order status sync (live repair test)" whose HTTP Request called the misspelled `/webhook/order-stauts` (404); a real webhook run failed (execution 3). Note: inside the container n8n answers unknown non-API paths with its editor HTML (200), so a typo in a `/healthz` path does not fail a workflow; webhook-path typos do. Results: tool-calling check `supported`; turn 1 read context (1,744 bytes, delivered as a tool result) and the provider returned 429 → `interrupted`; the continued turn 63 s later produced a diagnosis and a `propose_workflow_patch`; linked deactivate confirmed; apply wrote one PUT and **readback verified on real n8n 2.19** (four-field PUT accepted; content hash unchanged by n8n normalization); activate succeeded; undo via `propose_restore_workflow` restored the pre-repair URL with verified readback. **Safety finding:** the model diagnosed the typo correctly but, seeing only the target workflow, pointed the HTTP Request at that workflow's own trigger webhook (`/webhook/order-status-7fc0ea3c`) instead of the order service. The server preview reported no risk, the change was applied and activated, and each run called itself: 3,816 recursive executions ran in a few minutes until the workflow was deactivated directly in the disposable n8n (running executions then errored out; CPU returned to idle). Nothing outside the disposable container was affected. Required follow-ups: a server-side risk (and apply guard) for URLs that call the same workflow's own webhook path (`/webhook/` or `/webhook-test/` on any host), making repair diagnosis aware of other workflows (e.g. offer `list_workflows` in repair scope), and keeping activation of a repaired workflow a deliberate separate step. The disposable n8n, its workflows and 3,816 executions remain for the user to try Repair with AI; the saved connection now has tool calling enabled.
- Self-call guard (2026-09-16, from the live finding): `repair_diff.py` finds the workflow's own enabled trigger routes (`/webhook/`, `/webhook-test/`, `/webhook-waiting/`, `/form/`, `/form-test/`, `/form-waiting/` by trigger path or webhookId, host-independent, including expression URLs and sub-paths) and reports newly introduced self-calls as risk `self_webhook_call`; `repair_actions.prepare` rejects such patches with 422 `self_call_blocked` before any approval exists, telling the model to use the intended service. The repair context now carries `webhook_directory` (other active workflows' trigger routes: id, name, method and masked route only; no parameters or credentials; up to 100 endpoints; optional, skipped if the listing fails) so the model can find the real service. Contract updated (`RiskCode`, `ERROR_CODES`, `WebhookEndpoint`, `RepairContextResponse.webhook_directory`); frontend risk text "Calls this workflow’s own trigger (can loop forever)". Privacy note: other workflows' webhook routes are shared with the model provider in repair conversations; obvious tokens are masked and the directory is visible in "What the AI sees". Administrator switch (user decision 2026-09-16): Assistant settings → Repair conversations → "Share other workflows’ trigger addresses with the AI" (`webhook_directory_enabled` in `assistant:settings:repair`, default on, including settings saved before the switch existed). Off: repair context omits `webhook_directory` (no workflow listing is fetched) and continuing a turn that already sent it returns `409 repair_webhook_directory_disabled`. `PUT /repair/settings` is now a partial update that always stores both switches; a corrupt stored setting withholds both error text and the directory. Also fixed: "Try again" after either administrator 409 starts a new turn with the same message (the server drops the paused turn, so continuing it failed with `turn_not_pending`). Verification: focused repair tests 138 passed; full Windows backend suite **838 passed, 11 skipped** (394 s, run alongside the Docker build); `docker build -t inference:local .` passed and `scripts/smoke_container.py` **passed**; local preview restarted with the switch; complete `scripts/smoke_frontend.py` **passed** (new check: the trigger-address sharing switch saves and restores without changing the error-text switch; all repair, fleet, approval, admin/backup, design, theme, guided setup and 14-view checks; strict repair secret scan clean with only the pre-existing Workflows raw proxy read reported; no page errors). Verification: focused repair/assistant tests 174 passed; full Windows backend suite **836 passed, 11 skipped**. Live hosted-model rerun against the same disposable n8n, with the guard and directory (2026-09-16): **passed end to end.** Tool-calling check `supported`; one model request (no rate limit) with 1,903 bytes of context including the directory; the model cited "the only registered webhook on the instance is `/webhook/order-status` (registered by the Order service workflow)" and proposed the correct single change `/webhook/order-stauts` → `/webhook/order-status` with no risks; apply wrote one PUT with verified readback; activation succeeded; a real webhook run returned HTTP 200 with execution status `success` (no loop); undo (deactivate, `propose_restore_workflow`, confirm) restored the broken URL with verified readback. The live harness also refuses to activate if an applied URL still targets the workflow's own webhook. Complete `scripts/smoke_frontend.py` then **passed** again with the guard (all repair, fleet, approval, admin/backup, design, theme, guided setup and 14-view checks; strict repair secret scan clean; no page errors). `docker build -t inference:local .` passed with the guard and `scripts/smoke_container.py` **passed** (UID 10001, read-only root, frontend/auth/notes, restart persistence; no leftovers). Lead checks: a 40-node oversized workflow context summarizes to 14 KiB, keeps the failing node and neighbours in full and blocks edits to unseen code; setup check updated for the renamed AI workspace tabs. Full Windows backend suite after agents A and B: **824 passed, 11 skipped** (was 673 before repair), one installed Starlette/httpx warning, 243 seconds. Open integration items: the repair target chip must resolve workflow names from masked data (it fetched raw workflow JSON, exposing parameter secrets to the browser — fix requested from agent C; the browser secret scan then becomes strict for repair traffic while pre-existing Workflows/CodeLab/approval raw-workflow reads stay reported). Deferred to round 2: a `notes` field on `RepairContextResponse` (admin-withheld error text is already signalled by the `X-Inference-Context-Notes` header, `context_log.notes` and the step code; a body field needs `build_context` byte accounting changes). Security follow-up, fixed 2026-09-16: `/api/n8n_proxy/workflows` (list and detail) masks secret parameter values before they reach a browser.
- Verified n8n 2.19.0 public API facts for that work (from the image's `public-api/v1/openapi.yml`): no endpoint runs a workflow on demand; `POST /executions/{id}/retry` accepts `{"loadWorkflow": true}` to retry a failed execution with the currently saved workflow (the only API way to test a repair on the original input; repeats real side effects, so it must be a separate danger-styled, approval-gated action and never automatic); `GET /workflows/{id}/{versionId}` returns workflow history versions (rollback and pre-repair diff).
- User decisions (2026-09-15): keep the per-workflow name lookups on assistant approval cards; use the existing custom hosted OpenAI-compatible provider for live repair verification, after first probing whether it supports tool calling (its verified scope so far is plain non-streaming chat).
- Hosted provider tool-calling probe (2026-09-15, dummy `get_time` tool, no workflow data sent, key never printed): OpenAI-style tool calling works. Round 1 returned `finish_reason: tool_calls` with valid JSON arguments (266 tokens); a follow-up request sent immediately returned HTTP 429, and the same follow-up sent 60 seconds later returned 200 with an answer that used the tool result (386 tokens). The provider sends no rate-limit headers, so its limits are unknown. The repair workspace must therefore surface provider 429s as a clear, user-retried state (no automatic retries) and keep tool rounds few.
- Repair requirement: provider-agnostic. The hosted provider is only the verification model; any saved connection (openai, anthropic, openrouter, groq, ollama or custom OpenAI-compatible) can drive repair if its model supports tool calling. Planned: an optional tool-calling check per connection (dummy tool, no workflow data, no history write, admin/CSRF/rate-limited, no automatic retry) shown as "Supports repair: yes / no / untested", and repair entry points that say when the selected model has not passed it. Tool-call parsing is normalized across the OpenAI-compatible and Anthropic adapters; approvals and server-computed diffs are identical for every provider.
- Forms and navigation redesign (2026-09-16). The user found the forms and nav bar generic. The replacement draws on Primer, Carbon, Geist, Grafana Saga, Linear, Supabase, Stripe, GitHub and Vercel; see DESIGN.md "Forms and navigation".
  - Navigation:
    - quiet sidebar and 56px top bar on the page canvas, with no tagline, slogan or page footer;
    - 32px nav items with a neutral selection;
    - an instance switcher breadcrumb (still the native select labelled "Selected instance") and a live-updates dot;
    - an "Account menu" holding name and role, the Theme segmented control, Settings and Sign out;
    - a "Menu" disclosure on mobile.
  - Forms:
    - new `css/forms.css` and `css/shell.css`;
    - `field(label, input, {hint})` adds help text, inline checkboxes and a show/hide toggle on every password or API-key input;
    - `formCard()` renders a heading, a 720px body and a footer holding a note, Cancel and the primary action;
    - applied to Instances, Secrets, Connect a model, Settings (preferences, 2FA, system defaults, users, audit), Backups and sign-in/setup;
    - a quiet 2px focus border replaces the 3px ring with a 4px offset.
  - Browser checks updated for the account menu (`choose_theme`), the "Menu" label and the top bar.
  - Verification:
    - before/after screenshots in all three themes and at 390px;
    - complete `scripts/smoke_frontend.py` run 1 passed every check, including all repair, fleet, admin/backup, design, theme, guided setup and 14-view checks, with no page errors;
    - run 1 still exited 1 while deleting its temporary data folder, because the killed server still held `dashboard.db` on Windows; the harness now waits for the handle before removing the folder;
    - rerun **passed** with exit code 0: every check passed, the temporary data folder was removed and there were no page errors. The only note was the known raw-workflow proxy read on the Workflows view.
    - `docker build -t inference:local .` passed with the redesign, and `scripts/smoke_container.py` **passed** (UID 10001, read-only root, frontend/auth/notes, restart persistence; no leftover containers).
- Control alignment pass (2026-09-16, from the user: "refresh buttons etc are not aligned at many places"). A scratchpad script measured every toolbar and heading row across all fourteen views at 1440px and 390px.
  - Cause: buttons were 42px against 36px fields, and a button beside a labelled field centred against label plus control.
  - Fix: buttons are 36px (32px in tables, rails and filter chips; 44px on phones), a toolbar holding a labelled field bottom-aligns, toolbar headings lose their block margin, and phone tables scroll inside their focusable region instead of crushing cells (the triage table still stacks into cards).
  - Re-measured: desktop control heights are only 32 and 36, phone heights are 44. The one remaining flagged row is the Notes button row wrapping onto two lines, which the Notes conversion will take.
  - Complete `scripts/smoke_frontend.py` **passed** again (exit 0, every check, no page errors).
- Form conversions and native-dialog removal (2026-09-16, step 3 of the user's step-by-step pass).
  - Containers provision, the Notes editor, the MCP server form and Promote's source/target step now use `formCard` with field hints and a footer holding the actions. The Notes action row no longer wraps, because Preview Markdown moved beside the preview heading.
  - CodeLab deploy, Promote deploy/activate/restore and the Instances Remove action confirm in the app dialog naming the target (Remove also takes the danger treatment). Promote's reconcile uses the new `askText()` dialog in `lib/confirm.js` instead of `window.prompt`. A repository grep confirms no view reaches for a native confirm or prompt.
  - `scripts/frontend_fleet_fixture.py` confirms the CodeLab and Promote dialogs instead of auto-accepting native ones.
  - Verification: the alignment measurement across all fourteen views reports 0 rows needing attention and no page errors; complete `scripts/smoke_frontend.py` **passed** (exit 0, every check). Two defects were caught before that run: a `mapping` name collision that broke `promote.js`, and a leftover `confirm()` in `instances.js`.
- Workflow proxy masking (2026-09-16, step 4; closes the standing security follow-up).
  - `/api/n8n_proxy/workflows` (list) and `/api/n8n_proxy/workflows/{id}` (detail) now return documents whose secret values are replaced with `[redacted:secret]`, reusing `mask_value` from `repair_context` through a function-level import so the fleet module keeps no import-time dependency on the assistant. Names, URLs, structure and Code node code stay readable.
  - CodeLab is unaffected: `codelab.py` reads and writes through its own node endpoint, which still returns the real code.
  - `scripts/frontend_repair_checks.py` dropped its raw-proxy exemption, so a secret in any API response now fails the browser run.
  - Verification: `tests/test_fleet.py::test_workflow_proxy_masks_secret_parameters_for_browsers` proves both endpoints hide the key while CodeLab still reads `return items;`; full Windows backend suite **839 passed, 11 skipped**; complete `scripts/smoke_frontend.py` **passed** (exit 0) with the strict scan and no exemption note.

**Remains**
- AI repair: self-call guard and webhook directory are done and verified live (2026-09-16); the directory's administrator switch is done. Remaining: verify version-history restore and retrying inactive workflows live; round 2 create workflow and retry execution; a `notes` field on the repair context response; more room for proposal diffs on mobile; show linked deactivate proposals inside their repair card instead of as separate Action review cards.
- Frontend: keyboard shortcuts, sortable columns, saved views, bulk resolve, ID pickers instead of typed IDs (CodeLab workflow, conversation resume, promotion mappings); readable MCP tool results and catalog; Users table status badges; mobile Errors toolbar Refresh button stretches tall; mobile toasts can cover content until dismissed; a critique re-run to measure the score.
- Live integrations: local Ollama inference; hosted streaming and model-driven tool calls; MCP SDK 2.x/2026 protocol and authenticated third-party servers; real S3 upload and disaster-recovery restore; exhaustive OTLP span/node correlation and classifier cases on real data; n8n version matrix and live promotion.
- Release: load/performance tests; Python 3.10 and non-amd64 runs; locked Linux full suite rerun on the current code; final platform conformance/release review; configure a git remote (commits are local only).

---

Earlier baseline, 2026-09-09: **Foundation, fleet, promotion, observability, assistant, Docker bundle, notes and encrypted backup export/restore/scheduling implemented.**

The backend serves authentication/admin, fleet, credential mapping, workflow promotion, CodeLab node updates, OTLP ingestion, trace/error queries, automatic and explicit classification, pricing, assistant chat/providers/MCP/approval APIs and owned Docker bundle provisioning/lifecycle APIs and a local notes vault plus encrypted export/offline restore, scheduled local archives/run history and optional S3 upload. Secret templates and writable session/default-preference policy are implemented. All 14 API-backed frontend views, onboarding, navigation and light/dark themes are implemented; browser verification scope is recorded below. Full product scope remains unchanged in `AGENTS.md`.

## Implemented

- [x] Validated environment settings; encrypted Fernet vault, atomic writes, key-loss/corruption checks, compound secret resolution and instance context isolation.
- [x] SQLite lifecycle, original tables plus auth/audit/preferences storage, numbered migrations, WAL, transaction isolation/cancellation cleanup and non-overwriting online snapshots.
- [x] Database/WAL/SHM permission hardening before initialization, regular-file checks and descriptor-based updates.
- [x] HTTPX/httpcore egress: URL/IP validation, DNS pinning, TLS Host/SNI preservation, private-network allowlists, bounded pooling/timeouts and fallback across validated addresses.
- [x] FastAPI factory/lifespan, trusted local module discovery, health endpoint, bounded request reads, security headers and redacted errors.
- [x] First-admin bootstrap, password hashing, login/logout, sessions, signed CSRF, origin validation, RBAC, rate limiting and TOTP/recovery codes.
- [x] Admin user/secret APIs, preferences, audit feed and settings inspection; session revocation and last-admin protection.
- [x] Transaction-time authorization checks prevent in-flight writes after disablement, demotion or logout.
- [x] Authenticated WebSocket notifications with bounded queues, session revalidation and disconnect cleanup.
- [x] README, environment reference and real-server smoke script. Original spec/design/plan retained.
- [x] Instance CRUD with encrypted API keys, URL validation, explicit private-network allowlists and local-only connection removal.
- [x] Independent workflow/execution read-capability probes, persisted results, stale-configuration rejection and bounded fleet health checks with worker cleanup.
- [x] Explicit-instance workflow/execution lists and details, opaque pagination, activation/deactivation and execution deletion with audit intent/outcomes and cancellation-safe final audit writes.
- [x] Guarded n8n client: live authorization, per-instance serialization, isolated request credentials, no cookie forwarding, secret-echo redaction, bounded responses/timeouts and no uncertain-write retries.
- [x] Fleet API documentation and real loopback HTTP integration through the production egress transport.
- [x] Schema v3: persistent credential/workflow mappings and encrypted promotion journals; interrupted writes become uncertain on restart without retries.
- [x] Admin credential discovery/schema/mapping APIs; explicit unsupported-discovery diagnostics, local JSON Schema validation and secret-hash checks before provisioning.
- [x] Encrypted, actor-bound, expiring promotion previews with structural diffs and source/target/config/mapping/secret/subworkflow fingerprints.
- [x] Precise credential/subworkflow/error-workflow remapping; malformed, dynamic and ambiguous references block deployment. Arbitrary equal strings remain unchanged.
- [x] Idempotent inactive create/update, transaction-time authorization, durable stages and outcomes, partial credential results and no uncertain-write retries.
- [x] Separate activation, read-only reconciliation, previous snapshot retrieval and guarded inactive restoration; no claims of undoing external effects.
- [x] Real loopback promotion test verifies unchanged source, inactive target, correct keys, persisted mapping and one write across repeated requests. API guides in `docs/api.md`.
- [x] Schema v4: hashed per-instance telemetry tokens, price layers, execution evidence cache, exact cost/provenance columns and deduplicated error triage.
- [x] Bounded JSON/protobuf/gzip OTLP, four concurrent parsing slots, strict IDs/timestamps, partial rejection, secret-content redaction and transaction-time machine-token revalidation.
- [x] Instance-separated trace lists/details, precise timestamp strings, exact page cost totals with unknown counts, real summary metrics and operator error resolution.
- [x] Explicit execution classification with 30-second terminal grace, redacted terminal evidence cache, continuation error confirmation and incomplete-telemetry distinction.
- [x] Opt-in zero-output warnings use executed-node evidence and persisted successful workflow/node history; missing branches do not produce failure alarms.
- [x] Exact model/Decimal pricing, cached/reasoning subsets, overrides/live/bundled precedence, explicit Ollama zero provider charge, opt-in bounded OpenRouter refresh/backoff and last-good cache.
- [x] Configurable 1–365 day retention, startup/hourly cleanup, lifecycle task cancellation and real Uvicorn machine protobuf/gzip ingestion across restart.
- [x] Persistent admin-owned polling grants and workflow/node zero-output preferences; grants survive logout, restrict reads to one instance's executions and recheck owner/version/connection before reads and classification commits.
- [x] Bounded automatic discovery/classification, terminal cache reuse, grace/late-span rechecks, per-execution failure backoff, completed-record filtering and aggregated history queries.
- [x] CodeLab read/hash/update endpoints for inactive JavaScript, legacy/native Python Code nodes; exact node edits, stale-write rejection, readback verification and cancellation-safe audit outcomes with no automatic retry.

- [x] Schema v5: encrypted durable assistant approval actions, atomic single-use claims, bounded pending cache, actor/session/argument/resource binding, expiry/cancel/stale/uncertain outcomes and startup expiration with audit.
- [x] OpenAI-compatible OpenAI/OpenRouter/Groq/Ollama/custom adapters and Anthropic, exact model IDs, keyless local support, server-resolved references, isolated headers, bounded requests/responses and no retries.
- [x] Admin provider/MCP configuration, encrypted user-owned chat history, bounded tool rounds, metadata-only built-in reads and approval-only mutations; live authorization and approved CodeLab baseline retained across awaited work.
- [x] MCP 2025-11-25 Streamable HTTP initialization, JSON/SSE replies, bounded catalogs, session cleanup, admin discovery and approval-only invocation regardless of annotations. Generic remote resource state is not available; proposals explicitly warn that binding covers arguments/configuration/credentials/tool definition.
- [x] Approval cancellation/commit settlement, role downgrade during MCP handshake, expiry during resource recheck, tool-schema changes, key/response redaction, and real loopback keyless model-shaped HTTP through the production transport. No actual inference server was used.

- [x] Lifespan-owned aiodocker Unix-socket client, persistent installation ownership ID, admin-only bundle APIs and clear unavailable-service 503; no ambient Docker endpoint discovery or host-resource pruning.
- [x] Strict matching-version n8n/runner templates, Kahn ordering, shared generated authentication, isolated bridge/named volume, resource limits, separate readiness deadlines and native n8n OTLP token/header/path injection.
- [x] Encrypted staged deployment journals and actor/request idempotency; partial/uncertain outcomes retained without retries or automatic rollback, restart recovery, ownership-safe inspection/reconciliation, start/stop and explicit teardown/purge with default volume preservation.
- [x] Recheck network/volume ownership before container creation, canonical IDs before effects, live administrator role during awaited operations and token revocation; incomplete bundles cannot report ready or restart with missing resources.
- [x] Key-loss guard now recognizes encrypted JSON documents and approval/promotion journals even when both vault files are absent, preventing replacement-key generation over existing ciphertext.
- [x] Docker SDK fixtures plus a real loopback simulated Engine verify creation/inspection/teardown through installed aiodocker; no real images or host resources changed. Docker Desktop's Linux engine remains unreachable.

- [x] Notes vault: bounded UTF-8 markdown storage, atomic create/save, revision-checked edits/deletes, traversal/device/reparse/symlink/hardlink protection and bounded scans.
- [x] Notes list/read, literal search, contextual wikilink backlinks and escaped plaintext previews; operator writes, live authorization, CSRF, content-free audit and cancellation settlement.
- [x] Notes tests cover stale/concurrent edits, create races, ambiguous links, revoked sessions, cancellation and honest unknown outcomes when file writes succeed but audit persistence fails. The notes frontend provides an escaped text preview; styled Markdown rendering remains pending.

- [x] Admin-only encrypted backup download: SQLite online snapshot, original vault key/secrets, optional config/users files and bounded notes capture; separate nonpersisted export passphrase, fixed Scrypt/Fernet format and one concurrent export.
- [x] Offline restore authenticates before parsing, validates allowed paths/vault/SQLite integrity/current schema in staging, rejects nonempty destinations and installs without overwriting. Cancellation settles capture/encryption/cleanup; role revocation prevents archive release.
- [x] Backup-focused verification: 38 passed, including vault/credential/notes round trip and restored application startup, malformed/tampered archives, schema corruption, role changes, cancellation and bounds. Schedules/history/S3 were completed in the following scheduling unit.

- [x] Schema v6: backup_schedules and backup_runs, encrypted schedule references, revocable administrator grants, persisted due times, single processing lock and lifespan scheduling with graceful cancellation.
- [x] Local archive storage with hashes/downloads, explicit file deletion, retained paginated history, capacity limits, startup failure/uncertain outcomes and local reconciliation; no retries of uncertain writes.
- [x] Optional single-attempt S3 SigV4 PUT through verified application egress, isolated headers, total timeout and redacted outcomes; local backup retained on upload failure. Real loopback transport and AWS signing-vector checks pass; no live cloud bucket was used.
- [x] Previous v5 backup archives migrate to v6 in restore staging. Key-loss detection includes encrypted schedule configuration. Schedule/S3 focused tests: 40 passed, including accelerated background scheduling, role/version revocation, cancellation, storage bounds, restore migration and upload uncertainty.

- [x] Admin system-settings overlay stored in documents: stale-revision protection, deployment session-hours precedence, new-session policy, default preferences without overwriting personal preferences, live role checks and cancellation-safe commit/publication.
- [x] Built-in API key, basic-auth and S3 secret templates: structural field/UTF-8/size validation, create-only vault locking, encrypted compound values and content-free audit, with unknown outcomes for file/audit persistence mismatch.
- [x] Admin-focused checks: 26 passed, covering restart persistence, environment precedence, stale/concurrent saves, revocation, cancellation, audit rollback, secret collisions and blank optional S3 token handling. Template implementation and settings review completed with a parallel agent; root verified final edits.

## Latest verification

| Check | Result |
|---|---|
| `.venv/Scripts/python.exe -m pytest -q --tb=short` | **627 passed, 11 skipped**, one installed Starlette/httpx deprecation warning (214.89 seconds; before the final manual-run actor correction) |
| Final `.venv/Scripts/python.exe -m pytest tests/test_backup_schedules.py -q --tb=short` | **16 passed** after correcting manual-run audit attribution and adding its regression test |
| `.venv/Scripts/python.exe -m compileall -q backend tests scripts` | Passed |
| Application OpenAPI generation | Passed; eight backup route paths registered; existing modules retained |
| `.venv/Scripts/python.exe scripts/smoke_foundation.py` | Passed separately after final changes: real Uvicorn auth/CSRF, scheduled backup/download/history, encrypted export/restore, restored-server login/vault/notes/telemetry/schedules, audit and cleanup |

Skips cover POSIX permissions/directory fsync and symlink creation unavailable under this Windows environment. Permission ordering is covered by portable tests. Network checks use deterministic boundaries plus real loopback fleet/promotion/model-shaped and Docker Engine-shaped fixtures; no live n8n/TLS interoperability, actual Docker bundle execution, real model/provider/MCP compatibility or production rollout has been verified. Python 3.13 was exercised; Python 3.10 compatibility has not been run in CI. Promotion checks cover replay, changed snapshots/secrets, cross-actor access, partial provisioning, cancellation, restart, reconciliation races and recovery. External n8n edits can race its non-conditional update API; local locks cannot provide distributed compare-and-swap.

## Remaining full-product work

| Area | Status / next requirements |
|---|---|
| Fleet / credentials / promotion | Implemented; populated browser mutations (activation, CodeLab, promotion replay, fleet search) verified against a loopback n8n simulator; managed live n8n 2.19.0 verified for execution and proxy history (2026-09-14). Remaining: n8n version matrix and live cross-instance promotion |
| Observability / errors | Ingestion/query/triage, classification, preferences, prices and retention implemented; native n8n OTLP arrival verified live. Remaining: exhaustive span field/node correlation, classifier cases on real data, production-load checks |
| Assistant | Implemented; browser approval cards verified; official MCP SDK 1.30.0 interoperability and hosted custom OpenAI-compatible provider (non-streaming) verified. Remaining: local Ollama inference run, streaming and model-driven tool calls, MCP SDK 2.x/2026 protocol and authenticated third-party servers. MCP resource-state fingerprints remain unavailable generically |
| Docker manager | Implemented; live managed n8n + runners bundle provisioning, readiness, OTLP injection and owned cleanup verified on Docker Desktop Linux (2026-09-14). Remaining: other n8n versions and hosts. Native Windows named-pipe control is deliberately unsupported |
| Notes | Implemented with safe Markdown dialect preview and backlinks; POSIX filesystem tests passed in the locked Linux container. Remaining: full CommonMark is not claimed. External editor replacement after the final revision check cannot be made conditional across processes |
| Backups | Export/restore, schedules, run history and optional S3 upload implemented; browser download/restore verified. Remaining: real S3 bucket upload and disaster-recovery restore. Local reconciliation does not retry or confirm remote outcomes |
| Admin | Implemented: users/2FA/secrets, credential templates, session/default-preference policy, personal preferences. Remaining: complete-platform conformance review |
| Frontend | All 14 views, three themes, AI workspace and populated browser scenarios verified; critique 22/40 followed by fixes A–F and a favicon, browser verified 2026-09-15. Remaining: shortcuts, sorting, saved views, bulk resolve, ID pickers, CodeLab/Promote native dialogs, MCP result readability, two small mobile polish items and a critique re-run |
| Delivery | Dockerfile/Compose, hashed Linux lock, locked Linux full suite (671 passed, 2026-09-11), container smoke and `inference:local` rebuild verified. Remaining: locked Linux suite rerun on the current code, load tests, Python 3.10/other architectures, final release review, commit and remote setup |

## Environment and resume point

- `.venv` uses Python 3.13.14 with system site packages; foundation dependencies, jsonschema 4.26, opentelemetry-proto 1.44, protobuf 7.36, aiodocker 0.27.0 and aiohttp 3.14.3 are installed. The earlier complete editable install was not completed. Do not repeat environment setup blindly.
- Docker CLI exists; this session's check could not reach Docker Desktop's Linux engine named pipe. No live n8n instances or provider credentials configured. Tests use local fixtures and clean up their servers. No real Docker resources were changed.
- Git branch: `feat/inference` (since 2026-09-15); 25 commits, no remote configured; identifier change, fix A and in-progress frontend groups are uncommitted.
- Next unit: populated frontend operation scenarios, complete-platform conformance and delivery checks. Admin guide: docs/api-admin-settings.md. Schema is v6; scheduled backup guides are in docs/api-backups.md. Notes API: docs/api-notes.md; export/restore: docs/api-backups.md. For consistent backup locking, acquire notes_lock before the database snapshot; filesystem and database writes are not one atomic transaction. Docker API/setup: docs/api-docker.md; assistant/local-model setup: docs/api-assistant.md. Live Docker, inference and exporter checks remain distinct from fixture results.
- Smoke-check note: one concurrent run timed out during the focused tests; the unchanged smoke deadlines passed on a separate rerun. Run server smoke separately from the full suite.

Update this ledger with actual code and check results after each unit. Do not count plans, mocks or unrun tests as working features.

## Frontend checkpoint — 2026-09-10

- User-directed visual override: ash-mist canvas, white surfaces, signal-blue pill actions, editorial serif headings and floating navigation. See DESIGN.md. Original protected product specification is unchanged.
- Implemented onboarding/login, all 14 views, role-aware navigation, instance selection, WebSocket notices, same-origin CSRF injection with one explicit pre-execution rejection retry, safe text helpers, responsive layout, and persisted theme switching. Error triage links prepare an assistant draft; nothing is sent automatically.
- Full automated regression: **654 passed, 11 skipped**, one installed Starlette/httpx deprecation warning, 327.15 seconds.
- `scripts/smoke_frontend.py`: **passed** against real Uvicorn and temporary SQLite/vault with Edge. Covers first-admin setup, all 14 empty-fleet views, note save/reload and escaped content, persisted theme reload, 390px navigation and sign-out/sign-in; no JavaScript page errors. Desktop/mobile screenshots inspected in test-results/.
- These checks do not prove live workflow promotion, real model inference, actual Docker deployment or S3 interoperability. Dashboard presents retained telemetry counts rather than inventing unavailable 24-hour fleet metrics. Notes currently preview escaped text. Monaco and Google fonts use optional external CDNs with local textarea/system-font fallbacks.
- Final separate verification: scripts/smoke_foundation.py passed real Uvicorn auth/CSRF, admin settings/templates, backup/download/history, encrypted export/restore and restored settings/vault/notes/telemetry/schedules; Python compilation passed.

## Populated fleet browser verification — 2026-09-10

- Extended scripts/smoke_frontend.py with scripts/frontend_fleet_fixture.py: actual Edge browser, production FastAPI/CSRF/session/egress transport, temporary database and a loopback n8n-shaped HTTP service.
- Passed workflow activate/deactivate on the selected instance, CodeLab read/save, stale CodeLab rejection with no remote write, stale promotion preview rejection with no target write, successful cross-instance inactive promotion and idempotent replay with exactly one target creation. Existing setup, all 14 empty-fleet views, notes, persisted themes, mobile navigation and login/logout checks also passed with no JavaScript page errors. Python script compilation passed.
- No production code changes were needed for these scenarios. This verifies the local browser-to-backend integration; the n8n peer is simulated, so live version compatibility remains unverified. The earlier 654-pass suite was not rerun for test-script/documentation-only changes.
- Next bounded unit: assistant approval-card scenarios with populated data, including confirm/cancel and rejected/replayed actions. Remaining cross-module UI coverage, styled Markdown, delivery files and live integration checks stay open.

## Assistant approval browser verification — 2026-09-10

- Extended the real local frontend smoke to create durable proposals through /api/assistant/tools/run and operate their actual approval cards. Passed pending-card persistence across page reload, Cancel with zero remote writes, stale-resource confirmation rejection, successful confirmation and replay rejection with exactly one remote mutation. No JavaScript page errors.
- The full frontend smoke also reran and passed its setup, 14-view navigation, notes, themes, mobile/login and populated fleet/promotion scenarios. Script compilation passed. No application code changes were needed; the earlier 654-test backend result remains the last full-suite run.
- n8n is a loopback simulator and proposals originate from the tool API. Real model inference and model-generated tool-call compatibility are not established by this check.
- Next bounded unit: populated administration/backup browser operations. Styled Markdown, full conformance, Docker packaging and live integrations remain open.

## Administration and backup browser verification — 2026-09-10

- Passed actual browser user creation and role editing, encrypted secret saving, paused schedule creation, enabling the schedule, manual backup run and browser download. Restored the downloaded encrypted archive into a fresh temporary destination and verified the edited user and saved note.
- Fixed backup view: paused schedules now explain that enabling is required, rather than offering a Run now action rejected by the backend. Regression check verifies the paused action is unavailable and enabling permits the run.
- Complete scripts/smoke_frontend.py passed, including earlier setup/navigation/notes/themes/mobile, fleet promotion and assistant approval scenarios; no JavaScript page errors. Script compilation passed. Full backend suite was not repeated for this frontend/test-only change; last full result remains 654 passed, 11 skipped.
- Test restore runs separately from Playwright's event loop and explicitly closes SQLite before Windows cleanup. No existing user data, real n8n or S3 bucket was changed.
- Next bounded unit: safe styled Markdown preview for Notes. Remaining final conformance, Docker packaging and live integration checks are still open.

## Notes Markdown preview — 2026-09-10

- Implemented frontend/js/lib/markdown.js and connected open/save/Preview Markdown in Notes. Supports headings, emphasis, inline/fenced code, flat ordered/unordered lists, blockquotes, horizontal rules and explicit HTTP/HTTPS/mailto links. Raw HTML and image syntax remain literal; links open with noopener/noreferrer. This is a documented small dialect, not a complete CommonMark implementation. Existing wikilink backlink discovery remains unchanged.
- Rendering uses DOM/text nodes rather than innerHTML. Large or densely marked documents fall back to plain text to bound generated DOM work. Added typography, quote and code styling using existing theme tokens. The existing server plaintext-preview API remains backward compatible.
- Complete real browser smoke passed, including saved-note heading rendering, inline/block formatting, inert script/image markup, rejected javascript links and large-document fallback. Earlier fleet/approval/admin/downloaded-backup-restore checks also passed; no JavaScript page errors. Script compilation passed. No backend code changed; last full backend suite remains 654 passed, 11 skipped.
- Next: final requirement conformance review and delivery packaging. Live n8n/runner/OTLP, models/providers/MCP, S3 and POSIX compatibility remain unverified. Earlier statements that styled Notes preview is pending are superseded by this checkpoint.

## Docker delivery and conformance review — 2026-09-10

- Added Dockerfile, loopback-only docker-compose.yml, optional docker-compose.docker.yml socket override, allowlisted .dockerignore and docs/deployment.md. Image uses Python 3.13, UID/GID 10001, one worker, explicit frontend assets and HTTP health check. Compose adds persistent data, read-only root, bounded tmpfs, dropped capabilities and rotated logs.
- Both Compose configurations validated. Docker Engine 29.7.2 / Compose v5.5.1 were reachable this session. `docker build -t inference:local .` passed. `scripts/smoke_container.py` passed real Linux-container startup, UID, frontend, bootstrap, notes and persisted login/note access after restart. Disposable labeled container and volume were removed; built image remains locally. No existing containers were changed.
- Smoke harness now re-reads Docker's dynamically assigned host port after restart. This was a harness issue, not application data loss.
- Source conformance review recorded in docs/conformance-review.md: row-count/notification preferences are saved but not applied; fleet-wide workflow search, specified dashboard aggregations, JSON theme assets and release reproducibility remain open. Earlier all-views-complete statements do not mean every requirement is complete.
- Next bounded unit: wire saved page_size and error_notifications preferences to actual lists and notices, with browser checks. Later: dashboard/fleet aggregation, theme assets and release lock. Live n8n/runner/OTLP, model/MCP and S3 interoperability remain unverified; running Inference itself in Docker does not establish these.
- User typography adjustment: only the login headline phrase A little clarity. now uses Geist 400; remaining headings retain DM Serif Display and body text retains Inter. Google Fonts import updated; system sans-serif fallback retained. Source checked; browser font rendering not reverified for this small change.
- Persistent loopback preview started at http://127.0.0.1:3000 using data/ and HTTP-only local development cookies. Earlier temporary smoke servers were stopped, explaining the unreachable preview. User must create their own first admin; test accounts were not copied. Latest Geist CSS served. Logs: test-results/preview.log and preview-error.log.
- User typography adjustment: the setup heading Make yourself at home. now also uses the existing Geist Regular clarity-title class.

## Applied UI preferences — 2026-09-10

- Shared session preference state now loads saved values on sign-in, applies changes after Save preferences and resets on sign-out. Row counts drive workflow/error/trace/span/backup/audit pagination and dashboard execution-feed requests. Promotion's bounded workflow discovery remains separate from display pagination.
- Error notification preference suppresses error/silent-failure toasts while preserving error records, refreshes, operation-failure feedback and other notices. Re-enabling takes effect immediately.
- Complete frontend smoke passed after changes. New browser assertions save 10 rows/disabled error notifications through Settings, reload, verify persisted controls and actual error/trace request limits, suppress an injected error packet, preserve a backup notice and re-enable error notices. Notification packets are injected into the browser's real WebSocket message handler; this does not verify external event delivery. All previous fleet/approval/admin/backup-restore scenarios also passed without JavaScript page errors. Script compilation passed; no backend changes.
- Next bounded unit: JSON theme assets and loader, then dashboard/fleet-wide aggregation and release/live checks. Local source preview serves current files; the previously built Docker image must be rebuilt to include these frontend changes. Last full backend regression remains 654 passed, 11 skipped.

## JSON theme assets and loader — 2026-09-10

- Added frontend/themes/light.json and dark.json, preserving current palette/surfaces and Geist typography. themes.js now loads same-origin JSON, validates known color/shadow tokens, caches successful requests and limits fetch duration. Latest theme choice wins across asynchronous loads. Failed loads show the default CSS light appearance with a notice; failed cache entries can retry.
- Complete frontend smoke passed, including actual dark computed color, persisted switching, overlapping theme choices, 503 fallback and successful retry, plus previous workflow/approval/preferences/admin/backup-restore checks. No JavaScript page errors. Backend code unchanged; last full backend regression remains 654 passed, 11 skipped.
- Next bounded unit: fleet-wide workflow search with explicit mutation targets; then missing dashboard aggregates and release/live checks. Rebuild Docker image to include latest frontend changes.

## Fleet-wide workflow search — 2026-09-11

- Workflows now displays all connected instances when All instances is selected, with shared name search, independent per-instance cursors/page limits and refresh/recovery. Initial/search requests use at most three workers. Unavailable instances display their own error without discarding healthy results.
- Every inspect/activate/deactivate/promote/CodeLab action carries the row's explicit instance ID. CodeLab accepts that route ID and identifies its target in the heading description. Instance-list refresh preserves All instances instead of implicitly picking the first instance.
- Complete real browser smoke passed: fleet search, separate pagination, identical workflow IDs on two instances, activation reaching only the intended target, CodeLab target navigation, partial failure and instance recovery. Existing promotion/approval/admin/backup restore/preferences/theme checks also passed with no JavaScript page errors. n8n remains a local HTTP simulator in these checks.
- Next bounded unit: required dashboard workflow totals and 24-hour execution/success/silent-failure aggregation with honest coverage. Release reproducibility and live integration checks remain. Docker image must be rebuilt for latest frontend changes; protected specification unchanged.

## Dashboard aggregation — 2026-09-11

- Added GET /api/n8n_proxy/dashboard and dashboard cards for workflow totals, saved executions started in the last 24 hours, completed-run success rate and confirmed received silent-error spans. Retained telemetry counts remain separately labeled. Details: docs/api-dashboard.md.
- Explicit instance scope, up to three workers, ten 250-row pages per resource, 30-second instance deadline and four requests/minute/user. Partial, malformed, duplicate/repeated-cursor or capped scans produce null affected totals and coverage notices. Unknown statuses cannot invent a success rate; running/waiting executions are excluded from its denominator. Saved-history limitations and non-atomic remote snapshots are documented.
- Full regression: 659 passed, 11 skipped, one installed dependency warning in 209.61 seconds (before the final additional silent-span test). Final dashboard-focused suite: 6 passed, including scoped recent confirmed silent-span counting. Python compilation passed. Complete frontend browser smoke passed, including two-instance aggregate totals and actual zero executions plus previous fleet/approval/preferences/admin/backup-restore/theme checks; no JavaScript page errors.
- Local source preview restarted with the same data directory to serve the new route; local account data preserved. Docker image needs rebuilding for current code.
- Next bounded unit: release reproducibility (dependency lock/base-image digest), then updated image verification and remaining live integration/version checks. Metrics depend on available saved n8n history; pruned/unsaved runs are outside coverage, and large scans explicitly report incomplete totals.

## Release dependency/base pinning — 2026-09-11

- Pinned Docker base manifest digest from the previously verified build. Added requirements-linux-amd64.lock with exact versions and SHA-256 hashes for all 43 runtime wheels. Compose targets Linux amd64; lock scope is CPython 3.13. Docker installs only hashed binary wheels, runs pip check, and serves copied source without an unpinned application build backend.
- Added scripts/hash_release_wheels.py to verify manifest/wheel agreement and regenerate the lock; update procedure and platform limitations are documented in docs/deployment.md. Development pyproject ranges remain separate. Dependency inputs are pinned; byte-identical image layers and offline builds are not claimed.
- Both Compose variants validate. Locked image build passed, pip check passed, all 43 installed runtime versions matched the lock exactly. Real container smoke passed UID 10001/read-only root/frontend/auth/notes and restart persistence. Disposable test container/volume cleaned; inference:local now includes current dashboard/frontend/theme updates. Script compilation passed. No live n8n/provider/S3 resources were changed.
- Next bounded unit: run the backend regression suite against the locked Linux environment, addressing POSIX-specific failures if present. Then actual managed n8n/runners/OTLP and model/MCP/S3 compatibility checks. Last full Windows backend result remains 659 passed/11 skipped, plus final six dashboard tests.

## Locked Linux backend regression — 2026-09-11

- Full backend suite in the locked Linux amd64/CPython 3.13 image: **671 passed, zero skipped**, two installed-library deprecation warnings, 163.40 seconds. Includes the final dashboard test and POSIX filesystem/permission/symlink cases unavailable on Windows. No product fixes were needed.
- Added tests/docker/Dockerfile and its context allowlist. The test-only image installs six pinned pytest/tool dependencies with --no-deps; pip check passed. Verified build input matches the saved Dockerfile. Runtime lock remains unchanged.
- Tests ran as UID 10001 with a read-only root, temporary tmpfs, dropped capabilities, no host data mount and no Docker socket. Disposable test container exited successfully and was removed; local test image remains for repeat runs. docs/deployment.md records commands.
- Next bounded unit: actual n8n + matching-version runner bundle verification through the Docker manager, including JavaScript/Python execution and exporter compatibility where supported. Existing host containers must remain untouched. Model/provider/MCP and S3 compatibility/disaster recovery remain separate; local fixtures and Linux tests do not establish them. Python 3.10/other architectures remain outside this verified runtime matrix.


## Inference dashboard redesign — 2026-09-14

- User selected **Inference**, tagline **Execution Anomaly System**, and supplied the Flowmapp white/blue reference. Used Impeccable Operate guidance and researched Carbon/Grafana dashboard hierarchy and drill-down patterns; sources and palette conflict resolution are in DESIGN.md. Added PRODUCT.md and a current-brand override in AGENTS.md without changing the protected baseline or feature scope.
- Replaced the floating capsule/editorial styling with a grouped Monitor/Orchestrate/Manage sidebar, persistent instance/account bar, Inter typography, white/cool-gray surfaces, black primary pill actions, blue selection accents, consistent SVG icons, responsive Explore navigation and retained dark theme. All fourteen views inherit the shared styles. UI, app title and assistant display/client identity now say Inference. Existing environment keys, cookies, headers, Docker ownership identifiers, package names and backup formats remain compatible; no identifier/data migration was performed.
- Rebuilt the overview around the real open-errors API: bounded investigation queue, literal escaped messages, explicit-instance assistant draft links, connected-instance coverage, existing four execution aggregates, recent executions and separately labelled retained telemetry. Partial metrics remain unavailable; no empty state claims the fleet is healthy. Counting details are expandable. Serialized refresh and navigation abort cleanup retained. Live-update connection text now reflects actual WebSocket state. Fixed skip-link focus and desktop/mobile navigation disclosure behavior.
- Final full scripts/smoke_frontend.py **passed**: setup, all fourteen routes, actual browser/API/simulated-n8n activation, CodeLab stale protection, promotion/replay, assistant approvals, preferences, notes, account/secret/schedule edits, downloaded encrypted backup restore, and JSON-theme race/failure/retry behavior. Added frontend_design_checks.py for UI-only fixture states: silent finding, inert injected HTML, explicit assistant target, partial metrics, failed findings API, desktop/mobile navigation and dark theme. The fixture values appear only in test responses, never production assets.
- Focused backend verification: **51 passed**, one pre-existing Starlette/httpx deprecation warning (test_app, test_assistant_mcp, test_assistant_providers). Python compileall passed. Full backend suite was not repeated for the visual changes; last locked Linux full result remains 671 passed.
- Inspected desktop/mobile/setup and populated light/dark captures. Initial screenshot revealed hidden desktop navigation; corrected native details state and added visible-navigation assertions. One intermediate smoke exceeded the existing dashboard scan rate limit after an unnecessary extra test navigation; removed that extra scan, retained visible-link checks, and the complete final smoke passed. Final mechanical badge selector correction prevents alert padding from expanding error badges. Impeccable detector reported only the brief-pinned Inter warning; its optional image-comparison workflow remained open because this delivery was built directly from the supplied reference, not a generated comp. No claim of comp-workflow certification.
- Local preview started at http://127.0.0.1:3000 with the existing data directory. Verified /healthz 200, new title and sidebar assets served. Runtime files remain out of git. Screenshots: test-results/frontend-dashboard.png, frontend-mobile.png, frontend-mobile-nav.png, inference-populated-desktop.png, inference-populated-dark.png and inference-populated-mobile.png. Populated captures are explicitly test-fixture evidence.
- Next original product unit remains actual managed n8n + matching-version runners, JavaScript/Python execution and OTLP compatibility. Model/provider/MCP and S3/disaster-recovery compatibility remain separate. Rebuild the Docker image before expecting it to contain this redesign; current verification and preview use source files.


## Authentication copy correction — 2026-09-14

- Replaced vague welcome/fleet marketing copy with the approved Execution Anomaly System headline and a concise description of monitoring, investigation and workflow management. Removed n8n references from the authentication screen. Sign-in and setup headings now directly name the task; authentication behavior is unchanged.
- Verified the running local preview serves the new wizard source and contains none of the removed phrases. Copy-only change; no additional backend tests run.

- Authentication copy follow-up: replaced the supporting description with "A little clarity. Across your fleet." at the user's request; retained Execution Anomaly System as the main heading. Verified updated source served by the local preview.

- User-authorized local admin password reset: regenerated scrypt hash, revoked prior admin sessions and recorded a content-free audit event. Password kept out of project files. Real HTTP login verification returned 200.


## Assistant workspace layout — 2026-09-14

- Replaced the right-aligned 720px chat drawer with a responsive main chat column and action-review rail. Bounded independently scrolling transcript and bottom composer fit the desktop viewport; model selector and New conversation form a compact toolbar. Mobile stacks the action rail below chat.
- Moved conversation-ID controls and context/privacy explanations into disclosures. Assistant settings and Connect model open/focus the existing admin configuration form. No-provider composer stays disabled; loading failures display a distinct error state. All provider/MCP configuration, conversation resume, proposals and confirm/cancel actions remain connected to existing routes. No backend/provider changes.
- Full scripts/smoke_frontend.py passed, including all fourteen views and existing promotion, CodeLab, assistant approval/cancel/stale/replay, notes, preferences and backup/restore checks. New assertions verify no-model disabled controls, setup focus, desktop chat width and composer position at 1520x900, and mobile overflow at 390px. Inspected test-results/assistant-desktop.png and assistant-mobile.png. Detector returned no style findings; previously open generated-comp tooling state is unrelated to this scoped code refinement.
- Restarted the existing-data local preview at http://127.0.0.1:3000 and verified it serves the new assistant module. Live model inference remains unverified. Next original integration work remains as recorded above.

## Theme selection and operational icons — 2026-09-14

- Researched Carbon icon guidance and n8n brand reference (DESIGN.md links). Added optional charcoal/coral n8n-inspired theme alongside Light and Dark. Account-bar native selector and Settings persist personal choices; installation defaults accept the same three values. Explicit theme whitelist and validated action/selection tokens retain safe fallback and latest-choice behavior. No database migration or deployment identifier changes.
- Added shared heading/action SVG icons with visible labels and decorative aria-hidden markup, keyboard-focusable table scroll regions, compact controls and theme-aware dark status colors. Preserved assistant chat/composer layout and all existing actions.
- Verification: tests/test_admin_settings.py **11 passed** (including n8n personal/default persistence and invalid choice rejection). Full scripts/smoke_frontend.py **passed**: all fourteen views, actual browser/API/local simulated-n8n flows, promotion/CodeLab/approvals, preferences, notes, encrypted backup restore and prior design/theme checks. New frontend_theme_checks.py verifies Settings save/reload/header sync, text contrast on all three themes, semantic icons, 390px overflow and malformed theme fallback. Inspected desktop/mobile n8n screenshots. Full backend suite was not rerun; latest full locked Linux result remains 671 passed.
- Restarted the existing-data source preview at http://127.0.0.1:3000; auth status, n8n theme and theme loader returned HTTP 200. Docker image still needs rebuilding for current source.
- Remaining: real managed n8n + matching runners with JavaScript/Python execution and OTLP compatibility; actual local/hosted model and remote MCP checks; real S3 upload and disaster recovery; current image rebuild and final integration/load/release review. Python 3.10/other architectures remain outside the verified runtime matrix. Local fixture tests do not prove these integrations. Protected product specification unchanged.

## Stale frontend/theme recovery and visible dashboard refinement — 2026-09-14

- User screenshot showed the old moon toggle, missing new heading icons and theme fallback. Static assets lacked explicit cache headers, allowing old module/theme combinations; theme token schemas differ between versions. Added no-store response policy for the raw frontend, explicit no-store theme fetch, and revised entry asset URLs. Existing open tabs require one hard refresh. Verified running preview serves new picker/icon modules and all tested assets return 200/no-store.
- Dashboard now places compact execution metrics above the investigation/fleet workspace, with SVG icons for all four metrics, three section headings and connected instances. Preserved real data, incomplete-coverage reporting and all actions. Settings preferences use a compact responsive grid; save buttons have icons and operation messages replace prior notices instead of stacking.
- Complete frontend smoke passed: all fourteen views and prior integration fixtures, plus asset cache headers, dashboard icon counts/order, theme persistence/reload, settings save icon and single confirmation after repeated save, theme contrast/fallback and mobile overflow. First new cache assertion used a relative URL unsupported by the test request client; corrected to absolute URL and reran successfully. Inspected desktop/mobile dashboard captures. Live preview restarted at http://127.0.0.1:3000 with existing data. Full backend suite not rerun.
- Live n8n/runners/telemetry, model/MCP, S3/disaster recovery and current-image release checks remain unchanged.

## Real managed n8n and runners — 2026-09-14

- Started Docker Desktop Linux engine and rebuilt inference:local from current source. Existing host containers were not modified. Added scripts/smoke_live_n8n.py and docs/live-n8n-verification.md: isolated application data, actual Docker-manager API provisioning, same-version n8n/runners 2.19.0, generated temporary owner/key, webhook JavaScript plus native Python execution, proxy history and native telemetry checks.
- Final real integration run **passed**: both managed containers healthy; idempotent replay returned the same bundle; JavaScript and Python each returned 42; execution appeared through Inference's instance-scoped proxy; native OTLP spans arrived for that instance. All disposable managed containers, network and volume were removed through the ownership-checked API; the temporary app container was removed. Confirmed no running test containers or managed test volumes remained. Images remain cached.
- Environment findings: Windows source preview does not support the Unix Docker socket, so testing used the Linux app image. Local test bootstrap requires a token because the app sees a bridge client IP. n8n keeps Secure cookies enabled; the HTTP-only harness explicitly forwards its freshly issued setup cookie. Host-address proxy access returned 502; joining the temporary app to its owned bundle network and using http://n8n:5678 passed. OTLP export used Docker Desktop host forwarding. These harness changes do not relax production security.
- Current image also passed scripts/smoke_container.py: UID 10001, read-only root, frontend/auth/notes and data persistence after restart. No product backend behavior changed in this unit; full backend suite not rerun. Telemetry arrival is verified, but exhaustive real span field/node correlation, classifier cases, sustained load, Python 3.10 and other architectures are not.
- Next bounded unit: actual local Ollama/provider and remote MCP integration. Then real S3/disaster recovery and final release/integration/load checks. Current Docker image now includes the latest frontend fixes. Protected specification unchanged.

## Assistant connection checks and setup feedback — 2026-09-14

- Added independent official MCP SDK 1.30.0 integration scripts, running in an isolated test environment with real HTTP. SSE and JSON discovery, approval-before-call, result 42, single-use replay rejection and cancellation all passed. Corrected Windows test process-tree cleanup after a first run left a transient SQLite handle. This verifies supported 2025-11-25 protocol interoperability, not SDK 2.x/2026 protocol or arbitrary hosted MCP servers.
- Local Ollama 0.34.0 image download started; scripts/smoke_local_ollama.py prepares a disposable CPU service and dedicated labeled model cache. Inference through Ollama remains unverified until that script completes. Hosted provider selected by user: a custom OpenAI-compatible endpoint and model. No hosted request has been sent yet. Never record its key in project files.
- Screenshot confirmed provider save rejection was caused by a raw key in the reference-only field. Added field-specific, redacted backend validation guidance and frontend secret-reference help/link; trims surrounding form whitespace. Six assistant chat tests passed, including raw-key redaction and successful custom provider configuration using a vault reference. JS syntax check passed. Preview restarted with existing data.
- Remaining active unit: finish local inference and user-configured hosted provider testing. Follow with real S3/disaster recovery and release/load/telemetry-depth checks.

## Hosted provider connection verified — 2026-09-14

- Inspected saved configuration without exposing key values. The base URL incorrectly included /chat/completions, and the saved API-key-template secret resolved to a compound object. Corrected the saved connection through the authenticated API to the provider base URL ending in /v1, its model and a vault secret reference, tools disabled for the connection check. Existing encrypted secret unchanged.
- Actual hosted chat through Inference returned HTTP 200 and Hello. Provider reported 64 prompt / 36 completion / 100 total tokens. This verifies this endpoint/model/key with non-streaming text, not gpt-5.2 availability, streaming or model-driven tools. Test session logged out afterward; user's other sessions preserved.
- Form now explains the base URL and compound template reference; successful save keeps the entered reference visible instead of clearing it. Reference text contains no secret value. JavaScript syntax check passed.

## Guided model setup and shared AI workspace — 2026-09-14

- Replaced provider configuration jargon with provider presets, connection name, base URL/model examples, direct password-type API-key entry and automatic encrypted vault storage. Saved references and tool access are Advanced options; tools default off. Keys clear after secure saving and references survive failed configuration saves. Completion endpoint suffixes normalize to a base URL. Custom provider URL starts empty rather than saving an example endpoint; keyless custom services remain an explicit advanced option.
- Added admin/CSRF/rate-limited POST /api/assistant/providers/{id}/test. Uses a fixed short prompt, no tools, no chat history write, and rechecks authorization after response. Saving configuration does not claim connectivity. The existing saved connection returned HTTP 200 on the new real hosted test endpoint.
- One AI workspace sidebar entry contains Chat and Code panels. Existing assistant/codelab deep links still work, including explicit instance/workflow targets. Panels load lazily and remain mounted while switching, preserving drafts/editor state; navigation disposes both. No feature was removed from the protected specification.
- Seven assistant backend tests passed, including no-tool/no-history connection tests. Complete frontend smoke passed: all fourteen route implementations (thirteen sidebar entries), workflow/CodeLab/approval/backup flows, guided encrypted key saving, completion-URL normalization, connection-test UI fixture, tab draft retention and mobile overflow. Inspected setup capture. Final custom URL/keyless option refinement passed JS syntax check. Preview restarted with existing user data; current Docker image needs rebuilding for this unit.

## Deployment identifiers — 2026-09-15

- User authorized Inference deployment identifiers: `INFERENCE_*` environment, `inference_session` cookie, `X-Inference-Bootstrap`, `com.inference.*` Docker labels and `inference-{bundle}-*` resources, `INFERENCE-BACKUP` marker and `.ibackup` files, default S3 prefix `inference`, Compose service/image/volume `inference`, container user `inference` (UID/GID 10001 unchanged), Python distribution `inference`, TOTP issuer and logger names.
- Verification: full Windows backend suite **673 passed, 11 skipped**, one installed Starlette/httpx warning (221.22 seconds). Python compileall and edited-JS syntax checks passed. Both Compose files validated with the new keys and missing bootstrap token rejection. Browser/container smoke scripts were updated but not rerun; The Docker image must be rebuilt as `inference:local`. The running local preview still serves the previous code until restarted.
- Follow-up verification, same day: `scripts/smoke_foundation.py` **passed** and complete `scripts/smoke_frontend.py` **passed** (14 views, promotion/CodeLab, approvals, fleet search, dashboard, admin/backup restore, themes, provider setup; no page errors). Two earlier foundation runs timed out at the post-restore login under the script's 2-second client limit while Docker Desktop was starting; a diagnostic run with longer limits measured that login at 0.35 seconds and passed, and the unmodified rerun passed. No product change was needed. `docker build -t inference:local .` passed and `scripts/smoke_container.py` **passed** (UID 10001, read-only root, frontend/auth/notes, restart persistence; disposable container/volume removed). User-approved removal of the superseded local and test images; Ollama, n8n 2.19.0 test images, the user's own unrelated n8n container and data volume and build cache kept. Local branch is `feat/inference` (no remote configured). Local preview restarted at http://127.0.0.1:3000 with existing data; `/healthz` 200 and the new bootstrap header served. Changes remain uncommitted.
- Frontend critique (2026-09-15): dual-agent Impeccable review scored the UI 22/40 (1 P0, 3 P1); snapshot in `.impeccable/critique/`. User chose to fix issues one group at a time.
- Fix A, honest partial data: incomplete coverage now renders as a dashed amber `.coverage-partial` status note (light/dark/n8n) naming each instance and whether its workflow list and/or execution history is incomplete, with possible causes stated as possibilities. Uncountable metrics show muted "Unavailable" plus "Coverage incomplete"; the silent-error metric and retained "Error spans" each explain what they count. Red error surfaces remain for confirmed failures only. Design checks now assert the status note, per-instance text, two unknown metrics and no error styling inside the coverage area. Complete `scripts/smoke_frontend.py` passed; light, dark and 390px screenshots inspected. Detector clean on the changed view. Rule recorded in DESIGN.md.
- Fixes B–F, implemented by five parallel agents with strict file ownership and verified together (2026-09-15). New modules `lib/triage.js`, `lib/payload.js`, `lib/confirm.js`, `lib/icons.js` `registerIcons()`, per-group stylesheets `triage.css`, `live.css`, `decisions.css`, `actions.css` loaded after components.css (asset version 20260915-1), and an SVG favicon served at `/favicon.svg`. No backend or `tests/` changes. JavaScript and Python script syntax checks passed; the detector reported only the brief-mandated Inter warning.
- Browser verification repairs, all test-harness only: activation now confirms in an in-page dialog, so the fleet fixture clicks its confirm button (4 places); two dashboard checks read `.metric-number` through the metric's accessible label instead of expecting heading values; the live-update and theme-reload checks route the dashboard metrics endpoint to an empty-fleet fixture so they no longer exhaust its 4-requests-per-minute limit before the real fleet aggregate check (that rate-limit rejection was correct product behavior). Complete `scripts/smoke_frontend.py` then **passed**: setup, 14 views, fleet activation/CodeLab/promotion replay, approval cards, fleet search, dashboard aggregates, admin and backup flows including named delete-schedule dialog, design fixtures (partial coverage, severity pills, relative time, execution IDs, Errors table filters and in-place resolve, 390px), three-theme contrast with neutral notices and failed-span bars, guided provider setup, theme loading, logout/login, no page errors. Desktop dashboard, mobile Errors, dark mobile dashboard, assistant and n8n-theme approval screenshots inspected. Remaining small issues are listed under Status at a glance. `docker build -t inference:local .` then passed with these frontend changes and `scripts/smoke_container.py` **passed** (UID 10001, read-only root, frontend/auth/notes, restart persistence; disposable container and volume removed; no dangling image left).
- User-authorized local admin password reset (2026-09-15): regenerated scrypt hash, revoked both existing admin sessions and recorded a content-free `auth.local_password_reset` audit event. Real HTTP login returned 200 and the verification session was signed out. Password kept out of project files.

## Phone polish and linked deactivation card — 2026-09-16

- `99f64b9` toasts on phones: at 390px a stack of three toasts (190px) covered "Repair with AI", "Mark resolved" and "Older errors", and scrolling could not clear them (gap between scroll bottom and viewport measured 0). `measureToasts()` writes `--toast-reserve` from `toast()` and `removeToast()`; under 700px `body:has(#toasts .toast) .page` turns it into bottom padding. Measured at 390px with three toasts: reserve and padding both 214px, no control covered; dismissing all three returns both to 0px. Desktop unchanged. Known limit: rotation mid-toast leaves the reserve stale for at most one toast lifetime (8 s).
- `e1d923a` proposal code diffs on phones: at 390px a diff line showed 232px of code, 30 characters, so an 82-character Code-node line showed 37% of itself behind 396px of sideways scroll inside a vertically scrolling transcript. Under 700px the code soft wraps with the +/- marker in its own column. Measured with a real card from `renderProposal()`: overflow 396px to 0, the 82- and 69-character lines wrap to 4 and 3 rows, code starts at 86px against the marker at 68px on every row; desktop at 1440px unchanged (`white-space:pre`, 88-character column, no overflow).
- `0d49db1` action review rail: a succeeded linked `deactivate_workflow` row (it has `linked_to` the patch) kept returning from `GET /approvals` and rendered a permanent "Deactivate ... Succeeded" card beside its proposal. The rail now skips rows with `linked_to` that are no longer pending; a pending linked row stays visible. Rendering only: the row stays in the response because precondition logic reads it. Verified with the two rows the repair fixture really returns: resolved linked row 1 rail item and 0 generic cards (previously 1 and 1); pending linked row 1 rail item and 1 generic card. No page errors; browser suite passed.

## Skill library, local Ollama and repair scoping — 2026-09-17

- `6e423fe` skill library (99 files changed). `backend/skills/` holds 15 vendored guides extracted from a supplied `built-in-n8n-skills` archive (87 files, 1.2 MB). New module `backend/modules/skills/` (manifest, `loader.py`, `router.py`) mounted at `/api/skills` by module discovery. Schema v7 adds the `_SKILLS` group (`skills`: id, name, description, content, `updated_at` TEXT). `backend/main.py` runs the loader at boot after the database opens. Loader: disk is the source of truth; upserts present folders, deletes rows for missing ones, one transaction; skips non-directories, symlinks, names not matching `^[a-z0-9][a-z0-9-]{0,63}$`, missing or symlinked `SKILL.md`, files over 256 KiB, unreadable or non-UTF-8 files and empty bodies; reads only `SKILL.md` (the `.sh`, `.yml` and Caddyfile assets under `n8n-self-hosting/assets` are never read or executed); frontmatter parsed by two known keys, no YAML dependency. Routes (viewer, read-only): `GET /api/skills` returns `{data: [{id, name, description, updated_at}]}` without bodies; `GET /api/skills/{id}` returns the markdown or 404 `skill_not_found`. Tool `get_skill` (`SkillArguments`), viewer read, never an approval, dispatched in `execute()` before `instance_id` is read; `MAX_SKILL_RESULT` 24 KiB with `truncated`. System prompt: consult skills before n8n code work; skill text is reference material that never authorises action. Four tests that asserted exact tool sets or schema version 6 updated; the backup restore test now asserts `SCHEMA_VERSION`. Verified: 845 passed, 11 skipped; skills end to end on a fresh database 9/9 (15 loaded, frontmatter stripped, 15/15 descriptions, unknown and path-like IDs refused, guide returned untruncated, malformed name 422).
- User decision: no user-added or custom skills and no skill editing UI. Skills change by replacing folders in `backend/skills/` and restarting; the container root filesystem is read-only, so containers need an image rebuild.
- `22324f9` local endpoint hint. Found: the "Local Ollama" preset prefills `http://127.0.0.1:11434/v1`, but loopback is not a global address, so saving succeeded and the connection failed with `provider_configuration_invalid` with no explanation. The base URL field now shows a hint for loopback or private endpoints; for administrators it reads `allow_cidrs` from `/api/admin/settings` and says either that loopback egress is already permitted or to restart with `INFERENCE_EGRESS_ALLOW_CIDRS=127.0.0.1/32`; a denied or failed lookup falls back to the static instruction; hosted providers never see it; Advanced opens for local endpoints. Ollama placeholder `qwen3:0.6b` → `qwen2.5:3b`. `docs/api-assistant.md` now teaches `INFERENCE_EGRESS_ALLOW_CIDRS`. Verified in a real browser against two deployments: 8/8 (no allowlist names the variable, 127.0.0.1/32 confirms reachability, hidden for hosted providers, no page errors); browser suite passed.
- Local Ollama measurements (native Ollama, CPU, `qwen2.5:3b`, 1.9 GB), with `INFERENCE_EGRESS_ALLOW_CIDRS=127.0.0.1/32`: 5/5 — provider saved without a key, connection test passed, tool-calling check `supported`, chat answered, and the model called `get_skill` on its own. Without the allowlist entry: `provider_configuration_invalid`. A follow-up request carrying a guide took more than 47 s, over the 45 s provider read timeout. Timed measurements (CPU, about 4 to 5.5 tokens/s): follow-up turn with the full 17 KB guide injected 175.6 s, and 123.4 s with the prompt prefix cached; with an 8 KB excerpt 164.3 s; with a 4 KB excerpt 32.1 s; inside a live chat the first turn 14.0 s and the guide follow-up 228 s.
- `c5a3760` two bugs found with the real model. (1) `get_skill` was in `REPAIR_SCOPE` but always rejected: `target_problem` required the pinned `instance_id` on every repair call, so each call ended as `target_mismatch`. `INSTANCE_FREE_TOOLS = {'get_skill'}` skips the instance check; everything that reaches an instance still must match instance and workflow, and a test pins both refusals. (2) `qwen2.5:3b` asked for `Code` and `n8n-code-js`; the strict ID pattern raised before the tool ran and `standard_chat` did not catch it, ending the chat. `skill_name` now accepts `^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$` (no slashes, leading alphanumeric), is trimmed, case-folded, spaces and underscores to hyphens; an unknown name returned `{skill, error: "not_installed", available: [ids]}` (changed later the same day, see below). Tests: `tests/test_skills.py` 14 tests (parsing, boot sync add/update/delete, both routes, unknown and path-like IDs, tool result, `Code` regression, normalisation); repair suite gains a skill-guided correction across Continue and the scoping guard.
- Verification at this point: backend suite 859 passed, 11 skipped (before the in-progress edits below); targeted assistant and skills tests 69 passed after them; browser smoke suite 18 PASS; skills end to end 9/9; Ollama checks 5/5; egress hint 8/8.
- `087191f` standard chats return a failing read tool to the model as `{"error": {"code", "detail"}}` and continue the turn; `authentication_required`, `session_revoked`, `session_expired` and `permission_denied` still end it, and failures while preparing a proposal (mutations, `mcp_call`) still end the request with no proposal from that round stored. Only the error's code and detail are sent, never argument values. Repair conversations already behaved this way. Docs markers for it removed after checking `standard_chat` in `backend/modules/assistant/router.py`.
- Local provider timeouts (`8ad3fac`, verified 2026-09-17: backend 880 passed / 11 skipped, browser smoke 18/18). `backend/modules/assistant/providers.py`: hosted `READ_TIMEOUT` 45 s / `TOTAL_TIMEOUT` 60 s unchanged; `LOCAL_READ_TIMEOUT` 600 s / `LOCAL_TOTAL_TIMEOUT` 630 s; connect 5 s, write 10 s, pool 5 s unchanged. `is_local()`: kind `ollama` on any host, or kind `custom` whose base URL host is `localhost`, `*.localhost`, or a loopback or private IP literal; no DNS lookup, so a public hostname never gets the long budget; `openai`, `openrouter`, `groq`, `anthropic` never local, even on 127.0.0.1. `_budget()` picks the pair; `tests/test_assistant_providers.py` pins both the httpx read timeout and the `asyncio.wait_for` total for each kind. Reason: the measurements above. Before the change the skill-guided live chat failed at 47 s with 502 `provider_unavailable`; after it, it returned 200 in 243 s and the model called `get_skill` on its own. A standard chat can make up to three provider calls, so a local chat can take several minutes; neither the browser client nor the server imposes a shorter response timeout (only the 15 s request-body arrival limit in `backend/main.py`).
- Egress hint fix (`a859277`, verified 2026-09-17: backend 880 passed / 11 skipped, browser smoke 18/18). The `22324f9` hint always named `127.0.0.1/32` and treated only entries starting with `127.`, `::1/128` or `0.0.0.0/0` as permitting. `frontend/js/lib/provider-setup.js` now (`ipv4`, `egressTarget`, `permits`, `paintEgress`): for literal loopback (127.x, `::1`, `localhost` treated as 127.0.0.1) or a private IPv4 literal (10/8, 172.16/12, 192.168/16) the hint names that exact address as `/32` (`::1/128` for `::1`) and decides "already permits" by CIDR containment against `allow_cidrs`, mirroring `backend/net.py`; `::1` counts only `::1/128` or `::/0`. Wording: "does not permit egress to loopback" / "the private address <ip>". Other hostnames show no hint.
- `get_skill` not-installed shape (`8806866`, verified 2026-09-17: backend 880 passed / 11 skipped, browser smoke 18/18). `backend/modules/assistant/tools.py` now returns `{"skill": <name as sent>, "error": {"code": "skill_not_installed", "detail": "No installed skill has that name. Call get_skill again with one of the available ids."}, "available": [up to 50 ids]}`, the same error shape as other failed tool calls.
- Finding: Ollama context truncation. With the full guide injected, the Ollama log showed `truncating input prompt limit=2050 prompt=5989/6144 keep=4`: only the first few tokens and the end were kept, so the system prompt and the user's question were dropped and the model summarised the guide instead of answering the Code-node question. Fix, verified the same day: `OLLAMA_CONTEXT_LENGTH=8192` set as a Windows user variable and Ollama restarted (`ollama ps` CONTEXT 8192; server log `n_ctx = 8192`); the same live question then produced a 6,440-token prompt with `truncated = 0`, a `get_skill` round (33.0 s) and a guided answer (4m22s) that fixed the error with `const items = $input.all();`, HTTP 200 in 295.2 s. Alternative: or a Modelfile with a larger `num_ctx`; Inference uses Ollama's OpenAI-compatible `/v1` endpoint, which does not accept `num_ctx` per request. Also noted: standard chat history (`GET /api/assistant/chats/{id}`) stores only user messages and final answers, so `get_skill` calls are not visible there; the Ollama log shows them as separate requests.
- Documentation: new `docs/skills.md` and `docs/status.md`; `docs/api-assistant.md` gains Ollama setup, tool errors, tools by scope and Skills; `docs/deployment.md` gains local model servers, skills in the image and the schema v7 note; `README.md` gains skills, a local model quickstart and a documentation index; `docs/api.md` links the skill library.
- Docker cleanup done 2026-09-17: deleted `ollama/ollama:0.34.0` (9.18 GB) and all build cache (3.59 GB); Docker disk use 17.97 GB to 5.2 GB. Kept `inference:local`, `n8nio/runners:2.19.0` and `docker.n8n.io/n8nio/n8n:2.19.0` (used by the app's container feature and live checks) and the user's own `n8nio/n8n:latest`. Two stopped containers remain: `inference-live-repair`, `n8n`.
- Identifier fallbacks removed before release (2026-09-17): the application reads only `INFERENCE_*` environment variables and accepts only the `inference_session` cookie, the `X-Inference-Bootstrap` header, `com.inference.*` Docker labels and `INFERENCE-BACKUP`/`.ibackup` archives; Compose files use plain `INFERENCE_*` substitutions. Tests now check that other cookie, header and label names are ignored. Backend suite 875 passed, 11 skipped. The architecture and planning documents (`AGENTS.md`, `IMPLEMENTATION.md`, `docs/original-brief.md`, `docs/superpowers/`) are tracked in git.
- Remaining. Release blockers: load tests; Python 3.10 compatibility check; Linux rerun of the suites; final review. Product queue: live version-history restore and retry-inactive against real n8n (~1 h); readable MCP results and Users badges (~2 h); design critique re-run (~30 min); ID pickers (~half day); repair round 2 (~1 day); keyboard shortcuts, sortable columns, saved views, bulk resolve (~1 day). Known limits: small local models may still choose near-miss skill names (handled); skill-guided answers on CPU are slow. Correction from 2026-09-18 review: the 8192-token Ollama context workaround and all three fixes were already verified and committed, as recorded above; they are not unfinished work. Doc follow-ups resolved: `backend/skills/README.md` rewritten for Inference (read-only, boot sync, restart or rebuild to change); `docs/live-assistant-verification.md` no longer claims a default provider (`--provider-base-url` and `--provider-model` are required with `--confirm-live`).
- License finding: the supplied archive had no license or notice files although the vendored README named `LICENSE-n8n-skills.txt` and `NOTICES-n8n-skills.txt`. Fetched upstream `LICENSE` (MIT, Copyright (c) 2025 Romuald Członkowski) and `NOTICES` verbatim from https://raw.githubusercontent.com/czlonkowski/n8n-skills/main/ (identical on `master`) into `backend/skills/LICENSE-n8n-skills.txt` and `backend/skills/NOTICES-n8n-skills.txt`. The loader reads only directories, so these root files are ignored. Upstream NOTICES lists hook scripts adapted from n8n-io/skills (Apache 2.0) and refers to `NOTICES-APACHE-2.0.txt`; those hooks are not vendored and the Apache copy was not added. Confirm no Apache-licensed content is in `backend/skills/`.

## Codebase reanalysis — 2026-09-18

- Reviewed current source, commit history, verification scripts and status documents. Starting worktree clean; HEAD b8696da, 114 commits. Latest code change is eeab7c1 (removal of legacy identifier fallbacks), not 8806866 as the status header said. No application code changed in this review.
- Confirmed new work since the earlier guided-setup checkpoint: repair contract v1 with guarded proposals/undo/self-call protection, 15 boot-synced skills and get_skill, native Ollama measurements and larger local timeouts, guided setup/form and phone refinements, and INFERENCE-only deployment identifiers. Existing code still exposes raw MCP result JSON and plain Users status strings. AI create-workflow/retry-execution round 2 is not in the assistant tool catalog.
- Fresh focused verification: tests/test_assistant_providers.py and tests/test_skills.py **68 passed in 10.54s**. Full backend, browser and live integrations were not rerun in this review. Latest recorded full Windows result after identifier fallback removal is 875 passed/11 skipped; old locked Linux result remains 671 passed on the September 11 revision.
- True release gaps: load/performance tests, Python 3.10 compatibility, current Linux backend/browser runs, current-image verification and final conformance/security/license review. Live integration gaps: S3 upload plus disaster recovery, promotion/version-history restore/retry paths against real n8n, broader real span correlation/version matrix, authenticated external MCP/newer protocol and broader provider capabilities. Distinguish these from verified hosted text chat, native Ollama, SDK 1.30 MCP and first live n8n/repair checks.
- Product queue: repair round 2 (create/retry/context notes); ID selectors; readable MCP output and Users badges; follow-up design review; keyboard shortcuts, sorting, saved views and bulk resolve. Skill reference files are not served to the model; standard chat history omits tool-call activity. Local CPU latency remains a known usability limit even after context/timeout fixes.
- Recommended next bounded unit: run the full current backend suite in the locked Linux image before adding further product features.

## Explicit remaining review checks — 2026-09-18

- [ ] Re-run the design review after the implemented fixes (estimated 30 minutes). The previous 22/40 score predates those fixes; no new score is claimed.
- [ ] Audit vendored skills against upstream license notices (estimated 10 minutes). Confirm whether any Apache-licensed content is present, including assets; record evidence and add required notices if applicable. The presence of MIT LICENSE/NOTICES files alone does not establish the result.
- Both reviews remain pending. The next implementation/verification priority remains the current locked Linux backend suite.

## Current locked Linux backend verification — 2026-09-18

- Started Docker Desktop Linux engine, rebuilt inference:local from current source and inference:tests with the existing pinned test dependencies. Both builds succeeded; runtime/test pip dependency checks passed.
- Full suite in Linux amd64 / CPython 3.13: **886 passed, zero failures, zero skipped**, 2 dependency deprecation warnings, 182.98 seconds. Ran as the image's UID 10001 with read-only root, temporary /tmp, dropped capabilities and no Docker socket or host data mount. The disposable test container used --rm and exited successfully.
- No product fixes were required. Warnings concern Starlette/httpx TestClient and the AnyIO BlockingPortal alias. Existing unrelated containers/data were not modified.
- The current Linux backend rerun is complete. Linux browser verification, Python 3.10, performance/load tests, final review, design review rerun and skills licensing/provenance review remain separate pending work.

## Skills provenance check — 2026-09-19 (incomplete)

- Compared all 86 files under the vendored skill directories, including assets, against upstream commit `19cd793f4789e3ef9c657ccf26e097f641a77df0`. With line endings normalized, 43 match their upstream paths and 43 differ. This is not a completed provenance verification.
- The Apache-derived hooks named in the retained upstream notice are absent, but the included router skill is also mentioned as borrowing document structure. Added this qualification to `backend/skills/README.md`; do not claim that no Apache-derived material is included.
- Remaining: identify the imported upstream revision and review differences before closing the license check. Design review rerun is still pending; no frontend changes were made during this check.

## 2026-09-22 — Dashboard alignment and semantic icons

Moved investigation refresh feedback and its timestamp into the panel content
padding, aligned the two summary panels on desktop, and kept their mobile stack.
Added locally embedded Lucide paths for the queue, servers, retrieved/partial
coverage and empty inbox, with text labels and the upstream license retained.
Navigation links use the existing arrow glyph. All styling is scoped in the new
frontend/css/dashboard.css; existing shared CSS and icon helpers are untouched.

Verification: headless Edge on a temporary loopback static server with simulated
API results. Light, Dark and n8n themes passed at 1440 and 390 px; desktop summary
panel bottoms match, mobile has no horizontal overflow, coverage icons render,
and a 429 retains findings/instance evidence with status notices and Retry.
Before/after screenshots are in OS temp/inference-dashboard-layout. JavaScript
passed node --check as a temporary .mjs copy. No live VPS calls or full backend
suite were needed for this visual-only change.


## 2026-09-22 — Shared UI consistency and honest loading

Completed the all-page icon, native-dropdown motion, Notes layout, optional Docker
state and dashboard loading pass. See docs/ui-audit-2026-09-22.md for scope,
remaining limits and verification. Backend: 897 passed, 11 skipped, 1 warning.
Browser: 18 PASS, exit 0. All 30 modules parse. Delayed API checks show independent
dashboard rendering and retained successful content after transient errors.


## 2026-09-22 — Archived repair rejection

Read-only VPS checks found the failed target archived (last updated before both failures). Both saved desired documents validate against that server's published PUT schema. Block repair proposal creation and confirmation when archived, with an explicit unarchive instruction; never unarchive automatically. Two regressions prove zero remote writes, including archiving after proposal. Full verification: 899 passed, 11 skipped, 1 warning; 18 browser PASS. Backend activation awaits the user's restart choice.


## 2026-09-22 — Proposal display consistency

Omit absent DOM children instead of rendering literal null after failed/cancelled proposals. Stack each action-review title above its status so long titles keep the full column. Edge reproduced one null text node before and zero after; 390px rail titles both measure 320px with badges below. Full browser suite includes the null regression and passes 18 checks.


## 2026-09-22 — Workflow suggestions

Add debounced name suggestions from each selected instance, bounded to three concurrent reads and ten displayed options. Cancel stale requests on typing, blur and navigation. Names include instance/ID; keyboard arrows/Enter/Escape and reduced motion are verified at 1440/390px. The VPS returned partial-name matches for AI and GATE in read-only checks. Clear search now actually clears the query. Browser suite: 18 PASS; changed modules parse.


## 2026-09-22 — Answer questions before proposing changes

Repair prompt previously directed the model to propose a complete change even when the message was a question. Clarify in both general and repair prompts that a pinned target is context, and edits require a request for a change/fix. Approval controls remain unchanged. Full suite: 899 passed, 11 skipped, 1 warning in 307.78s; provider tests are scripted, so no claim of live model compliance. Backend activation awaits restart.


## 2026-09-22 — Verified local activation

User explicitly approved restarting Inference. Verified the old port-3000 PID command line, stopped only that process and restarted scripts/run_local.py hidden. New server PID 4648 completed startup, /healthz returned 200 and the live app serves the updated workflow suggestions, proposal errors and CSS. Backend archive guards and question-oriented prompts are now loaded. No n8n workflow was changed. All work remains committed on work/ui-consistency; nothing pushed.


## 2026-09-22 — Workspace spacing and partial fleet visibility

The saved local n8n connection at 127.0.0.1:5681 does not respond to a read-only
socket probe. Its incomplete history correctly prevents claiming complete fleet
totals. Show counts separately for each resource whose instance scan completed,
including true zero, while keeping fleet totals unavailable when incomplete.
No connection was deleted and no remote workflow was changed.

Notes now uses one library/editor workspace with aligned panes, a bounded editor
and a shared preview section; mobile stacks the panes. Closed assistant details
explicitly remove their non-summary descendants from layout. Before hardening,
closed descendants still had off-page bounding boxes, although the reported large
blank gap was not reproduced in the clean isolated session. After hardening,
Edge and Chrome both measure a 40px desktop / 31.94px mobile end gap after chat and
repeated disclosure toggles. Mobile notification space remains intentional.
Focused browser checks and changed-module syntax checks pass. Full-suite outcome
is recorded below after the final run. No backend code changed in this unit.

Final browser verification: 18 PASS, exit 0; notes persistence, disclosure toggles, mobile layouts, partial counts and existing repair flows pass. All changed frontend files deployed to the running repository after checking for concurrent edits. No restart required.


## 2026-09-24 — Real AI step costs from n8n executions

n8n's own spans carry no token usage, so real n8n AI steps showed "Unknown" cost. The server now reads
usage from the execution the way n8n's telemetry does (`backend/modules/observability/usage.py`):
`tokenUsage`/`tokenUsageEstimate` on language-model sub-nodes and `metadata.tokenUsage` on vendor nodes,
model from node parameters (expressions skipped), provider from the node type (an OpenAI node with a custom
base URL is `openai-compatible`). A sub-node's usage goes to the step it feeds; counts that arrived with
telemetry are never overwritten; only counts, model, provider and node names are kept.

- `POST /api/otel/traces/{trace_id}/usage` (viewer + CSRF, 30/min per user) reads once per finished run;
  a run n8n did not keep (pruned, or successful runs not saved) returns `missing: true` and is not asked
  for again; other failures are reported and retried on the next open. The classifier applies usage from
  its existing execution fetch. Markers expire with telemetry retention.
- Prices are provider-aware (`find_price`): OpenRouter catalog rates apply only through OpenRouter or the
  model's own vendor (`gpt-4o-mini` through OpenAI matches `openai/gpt-4o-mini`); other hosts (Groq, Azure,
  Bedrock, custom base URLs) need an override or bundled price for the exact ID.
- A new override or catalog refresh prices stored AI steps that were waiting for a price (`repriced`);
  priced steps keep their stored cost and provenance. New admin routes: `DELETE /prices/{model}` removes an
  override, `DELETE /prices/sync` stops the six-hourly refresh. The refresh loop survives a failed round.
- UI: the trace panel reads usage when opened and says when n8n did not keep the execution; admins get
  Settings › AI prices (refresh on/off, update now, search, add/remove your own price).
- Also this day: removed boto3 (never imported; S3 is SigV4 over httpx) and 71 unused skill reference files.
  The Docker image was not rebuilt with the smaller lock (Docker Desktop not started).

Verification: full Windows backend suite **923 passed, 11 skipped** (7 new usage/pricing tests: n8n usage
parsing, malformed and absurd counts, host-specific catalog pricing, end-to-end read and price, a run n8n
did not keep, re-pricing on a new override and removal, stopping the refresh); complete
`scripts/smoke_frontend.py` **18/18 PASS** with a new check that adds a price, rejects a malformed number
and removes it; desktop and 390px screenshots of Settings › AI prices and the trace panel inspected. Not
verified: usage read against a live n8n AI execution, and the Docker image with the smaller lock.


## 2026-09-27 — AI cost from n8n runs, without telemetry

The user's VPS n8n sends no telemetry (it cannot reach Inference on 127.0.0.1) and calls OpenAI through an HTTP
Request node, which n8n counts nothing for, so no AI cost appeared. Checked read-only against the two real runs
(executions 2027 and 2028, `gpt-4.1-2025-04-14` via the Responses API): $0.078726 and $0.08887 (the second had
28,416 cached input tokens).

- Parser (`usage.py`): one list of AI calls per execution for both paths (trace panel and run costs). HTTP
  Request answers are read from their usage block (OpenAI Chat Completions/Responses and compatible services,
  Anthropic with cache reads/writes as input, Gemini with thinking as output, Ollama); the service comes from the
  URL's literal host, anything else is `openai-compatible`. Code nodes are not read (no double counting).
- Pricing: explicit OpenAI snapshot names priced as their model (`SNAPSHOTS`, recorded as `priced_as`) and nine
  more bundled snapshot prices, each checked on its OpenAI model page 2026-09-27; `gpt-4o-2024-05-13` excluded.
- Storage and routes: schema v8 table `run_costs` (per run: counts, models, prices; FK cascade with its
  instance, telemetry retention by start time); `POST /api/otel/runs/scan`, `GET /api/otel/runs`,
  `GET /api/otel/runs/summary`. Finished runs are read once; missing or oversized ones are recorded as
  unreadable; other failures stop the check and are reported; new prices re-price calls that had none.
- UI: Observability has "AI cost" (default) and "Traces" tabs on one bar with the tab's action on the right.
  AI cost shows 7-day totals, per-workflow totals, runs with AI calls and a panel with each call; partial data
  (unreachable instance, unreadable or running runs) uses the dashed partial-data note.

Verification: full Windows backend suite **934 passed, 11 skipped** (11 new: response shapes, non-AI JSON,
HTTP Request calls by host and model with Code nodes ignored, snapshot pricing limits, scan/list/summary with
running and missing runs, a failed read that stops and resumes, retention, re-pricing, instance deletion,
session/CSRF, the trace panel counting HTTP Request calls, and the v7 to v8 migration); complete
`scripts/smoke_frontend.py` **19/19 PASS** with the new `scripts/frontend_cost_checks.py` (simulated AI run:
"At least 1.92 ¢", the per-call panel, tabs, phone layout, no payload text in the page or API responses).
Desktop and 390px screenshots of both tabs and the run panel inspected. Not verified: a scan through the live
app against the VPS n8n (the parser and prices were checked read-only against its two real runs).


## 2026-09-28 — Errors grouped by problem, and read from n8n without telemetry

The user found Errors indistinguishable from Traces: every failed run was its own error row (the `count`
column was never above 1), and errors came only from telemetry, which their VPS n8n does not send. Also found:
nothing on the server ever sent the `errors.changed` event the frontend listens for.

- Grouping (`backend/modules/errors/grouping.py`, schema v9 `errors.group_key` with an index): same instance,
  workflow, step, error type and message (run-specific values masked; three-digit codes kept) are one error.
  `GET /api/errors` returns one row per group (count in retention, first and last seen, latest execution,
  `returned` when it came back after a resolve); resolve closes every open occurrence; reopen restores exactly
  what the latest resolve closed. A late root span that fills in a workflow ID re-groups the record.
- n8n as a source: the run scan records failed and crashed runs (step and masked message only; description,
  stack and run data never kept). One record per run whichever source reports first (`run_error_key`).
- `errors.changed` is now broadcast after telemetry, classification and scans record new failures.
- UI: Overview, Errors and Observability check n8n for new runs as they open (shared `lib/run-scan.js`, at most
  once a minute per instance per tab); the Errors details panel shows first seen, latest execution and "came
  back"; rows that came back carry a "Back after resolve" badge.
- Background checks while no page is open still need the poller switched on; it has no UI switch yet.

Verification: full Windows backend suite **940 passed, 11 skipped** (6 new in `tests/test_error_groups.py`:
message masking, one error across runs with resolve/undo/come-back, failures read from n8n with the secret in
message, description and stack never stored, one record per run whichever source is first, the live event,
and the v8 to v9 migration with regrouping; the identity test now expects one error seen twice). Complete
`scripts/smoke_frontend.py` **19/19 PASS**; the AI cost check now also finds two simulated failed runs as one
error with 2 occurrences, first and last seen and the latest execution, with no secret in the page or API
responses. First smoke run failed because the AI cost check reused a check made under a minute earlier, before
the simulated runs existed; the check now asks for a new one.


## 2026-11-13 — CodeLab read limited to operators (security)

Reported: a viewer could read unmasked Code node source through `GET /api/n8n_proxy/workflows/{id}/nodes/{node}`
(CodeLab's editor read), while the Workflows endpoints mask secrets for the same node. Keys written into code
(`const apiKey = "sk-live-..."`) leaked to read-only accounts. Fix: the editor read now requires the operator
role, the same as saving; CodeLab tells viewers to read masked code in Workflows; "Open in CodeLab" is offered
to operators only. New test `test_a_viewer_never_reads_a_key_written_into_code` fails on the old code (200)
and passes with the fix (403, no request reaches n8n); the old CodeLab viewer test now expects 403.
Verification: full backend suite 941 passed, 11 skipped; browser smoke 19/19.
