---
target: Inference frontend
total_score: 33
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 2
target_identity: "file:frontend/index.html"
target_fingerprint: "sha256:bc032be59fc5a02f5871099a7ca9c4beceb0c0995333e04079ae69869e207dc2"
target_path: "frontend/index.html"
timestamp: 2026-09-20T08-40-16Z
slug: frontend-review
---
Method: dual-agent (A: e3df9700-0ef2-48d4-a8e2-e8840763af56 · B: c5a699be-9d61-461d-b7d5-abc9acc14907)

Evidence: live app at `http://127.0.0.1:3000` signed in as `demo` (`admin`), evaluated via Playwright with Microsoft Edge (`channel='msedge'`, `headless=True`). Code reviewed at commit `a0f4767` ("merge: readable audit history, consistent wizard steps and honest disabled controls") in dedicated worktree `D:\n8n-review-2` on branch `work/design-review-2` while concurrent work proceeds on `frontend/js/lib/**` and `settings.js`. All 14 views inspected at desktop (1440x900) and mobile (390x844) viewports across `light`, `dark`, and `n8n` themes. Screenshots saved under the OS temporary directory (`inference_critique_2`). Dynamic and degraded states verified by route interception: loading skeletons on slow proxy responses, transient 429 rate limit with data retention and operable Retry, 500 error alerts, all 21 empty states, and multi-row populated tables testing sorting, selection, and bulk resolve.

## Design health score

| # | Heuristic | Score (0–4) | Key Issue / Observation |
|---|-----------|:-----------:|-------------------------|
| 1 | Visibility of System Status | **4** | **Excellent (was 3).** Live WebSocket indicator dot, real-time pending updates bar, "as-of" timestamps, honest partial-coverage banners, de-duplicated toasts with hover-pause, and loading skeleton rows with `role="status"`. Failed refreshes retain existing data and display an actionable Retry banner. |
| 2 | Match System / Real World | **4** | **Excellent (was 3).** Plain-language audit action labels (`settings.js:15`), single standardized outcome vocabulary, relative elapsed times with localized absolute timestamps (`triage.js:64`), and exact cost projections without hidden rounding (`observe.js:8`). |
| 3 | User Control and Freedom | **3** | **Good (was 3).** Universal modal dismissal via Escape/Cancel, clear recovery pathways on promotions, and proposal undo (`assistant.js:201`). Lacks multi-step undo in CodeLab and Notes editors. |
| 4 | Consistency and Standards | **3** | **Good (was 3).** Strict token inheritance across all 3 themes, standardized dialogs, and reusable table helpers with mobile row-stacking. Slight visual tension remains between 50px pill buttons (`components.css:1`) and 8px squarish inputs (`forms.css:6`). |
| 5 | Error Prevention | **4** | **Excellent (was 3).** Exceptional guardrails: Preconditions enforced before workflow patch application (`proposals.js:167`), target workflow status checks before deployment, and typed confirmation (`PURGE`) for destructive container operations (`containers.js:58`). |
| 6 | Recognition Rather Than Recall | **3** | **Good (was 3).** High-utility ID pickers with live search (`repair-target.js:84`) and saved triage views. Downgraded because Promote (`promote.js:93-95`) exposes raw JSON textareas where users must manually formulate ID mappings. |
| 7 | Flexibility and Efficiency | **3** | **Good (was 2).** Superb error triage accelerators (`?`, `/`, `r`, `j`/`k`, `x`, `e` in `errors.js:570`), sortable columns, and bulk resolution. Downgraded from 4 because keyboard shortcuts are trapped exclusively in Errors and do not exist across the other 13 views. |
| 8 | Aesthetic and Minimalist Design | **3** | **Good (was 3).** Restrained palette and clean typography, but severe toolbar stacking in `errors.js` (5 stacked control tiers) smothers the viewport before any errors are visible. |
| 9 | Help Recognize, Diagnose & Recover | **4** | **Excellent (was 3).** Best-in-class recovery guidance: Stale proposals provide "Ask AI to re-read" (`proposals.js:192`), uncertain writes provide "Check current state", and network/rate-limit errors offer inline retries while retaining valid metrics. |
| 10 | Help and Documentation | **2** | **Acceptable (was 3).** Good field hints and offline restore instructions, but no global search, onboarding flow, guided tour, or documentation on configuring n8n OpenTelemetry collectors. |
| **Total** | | **33 / 40** | **Good** (Rating band: 28–35, up from 29/40 on 2026-09-19 and 22/40 baseline) |

**Why +4.** The eight rounds of rapid fixes resolved every single P0 and P1 from the 2026-09-19 critique: loading states are explicit skeletons, 429 rate limits preserve cached metrics with an inline Retry button, control borders hit >= 3:1 in all 3 themes, mobile table card stacking is universal across all tables, and triage tools (sortable columns, saved views, bulk resolve, keyboard shortcuts) are fully implemented. The score is held at 33 because those powerful accelerators remain trapped inside the Errors view rather than being shared across the platform shell, and five stacked toolbars now compress the triage viewport.

---

## Design specificity verdict

**Verdict: Domain-Grounded Core Encrusted with Category-Interchangeable Admin Scaffolding.**

Inference commits to the Flowmapp "white blueprint desk with one blue pen" visual identity (`DESIGN.md:3`). When evaluating whether the composition, interaction, and visual language are authored specifically for this product or category-interchangeable, the interface splits into two distinct personalities:

1. **The Domain-Grounded Core (Highly Specific & Authoritative)**:
   - **Partial Coverage & Telemetry Honesty**: Unlike generic APM tools that treat absent telemetry as a server failure, Inference explicitly separates missing telemetry from confirmed execution crashes (`frontend/js/views/dashboard.js:82-87`, `frontend/css/components.css:226`). The dashed amber status note (`.coverage-partial`) and muted "Unavailable" metrics prevent false alarms in distributed n8n environments where webhooks or subworkflows experience arrival delays.
   - **AST-Aware Promotion Engine**: The promotion wizard (`frontend/js/views/promote.js:41-73`) does not merely push files; it speaks the language of n8n node topologies—mapping credentials, remapping subworkflow triggers, and highlighting connection and setting diffs.
   - **Grounded AI Repair Workspace**: In `frontend/js/views/assistant.js` and `frontend/js/lib/proposals.js`, the AI cannot execute arbitrary mutations. Proposals are rendered from server previews with line-by-line diff markers (`+`/`-`), active workflow preconditions, and execution loop safeguards (e.g. preventing self-calling webhooks).

2. **The Category-Interchangeable Shell (Generic SaaS Admin)**:
   - The supporting management views—Notes (`frontend/js/views/notes.js`), Secrets (`frontend/js/views/secrets.js`), Backups (`frontend/js/views/backup.js`), and Containers (`frontend/js/views/containers.js`)—are boilerplate CRUD modules that could be lifted into any generic web service without alteration. Notes, in particular, feels like a disconnected markdown scratchpad inside an industrial fleet control plane.
   - The navigation structure ("Monitor", "Orchestrate", "Manage" in `frontend/js/app.js:128`) and card-based dashboard layout (`dashboard-columns` in `components.css:65`) default to standard cloud dashboard layouts rather than expressing a distinctive workflow-orchestration spatial metaphor.

**Deterministic Scan.** `impeccable detect --json frontend` returned **2 findings, both `overused-font` (Inter) at `css/base.css:1` and `index.html:0` — both opinion flags / false positives**, since DESIGN.md mandates Inter as the corporate sans-serif font stack. Zero code-slop anti-patterns fired: zero gradient slop, zero purple-glow AI cliches, zero missing `alt` attributes, zero unlabelled icon buttons.

**Browser Measurement Sweeps.** Measured across all 14 views x 2 viewports (1440x900, 390x844) in `light`, `dark`, and `n8n` themes:
- **Zero WCAG text-contrast failures** in any theme across headings, body, muted text, placeholders, badges, and links.
- **Form control and ghost button borders hit >= 3:1 in all 3 themes**: Light measures **3.50:1** (card) and **3.28:1** (page); Dark measures **3.93:1**; n8n measures **3.44:1**. Disabled controls appropriately drop to `var(--border)` (WCAG inactive exemption). Topbar scope switcher is borderless (`border-color: transparent`) by design as a flush breadcrumb element.
- **Universal mobile table stacking at 390px**: Triage table (358px wide), Settings users & audit tables (324px wide) have **0px horizontal overflow** and **0px clipping**. All cells stack into key-value cards with `td::before` labels.
- **44px mobile touch scale holds**: Assistant action buttons hit 44px, filter chips hit 44px min-height, and checkbox containers enforce 44px tap boundaries.
- **Global `window.fetch` cleanly unpatched**: `api.js` binds `nativeFetch = window.fetch.bind(window)` and avoids monkey-patching global prototype.

---

## Accounting of previous findings (2026-09-19 Review)

| Previous Finding | Claimed Status | Verified Status | Measured Evidence & Location |
|---|:---:|:---:|---|
| **[P0]** Workflows renders nothing at all while loading | Fixed | **Fixed** | `payload.js:10-15` & `workflows.js:52`: Skeleton rows render with `role="status"` and `aria-live="polite"` while loading. On error, renders `loadFailure` card with an actionable Retry button. |
| **[P1]** Transient rate limit deletes dashboard & renders as confirmed failure | Fixed | **Fixed** | `dashboard.js:41-65`: 4/4 metrics retained after simulated 429 rate limit. Renders amber `.coverage-partial` banner with exact throttle reason and active Retry button (`payload.js:28-34`). |
| **[P1]** Mobile row-stacking was built for only one table; others crush & clip | Fixed | **Fixed** | `triage.css:110-135` & `html.js:133-140`: Stacking rules apply to all `.table-wrap:not(.table-scroll)`. Measured at 390px: 0px overflow on Settings and Backups tables; `overflow-wrap: break-word; word-break: normal;` prevents mid-word breaks. |
| **[P1]** Fleet health never resolves, and button is disabled while it hangs | Fixed | **Fixed** | `fleet-health.js:19-35`: Per-instance abortable probes with 10s timeout (`payload.js:68-97`); skeleton cards render immediately; Check Fleet button stays active as Cancel while probing. |
| **[P1]** No border token reaches 3:1 in any theme | Fixed | **Fixed** | `forms.css:4-13`: Light `--control-border` `#7e8a9c` (3.50:1), Dark `#7e8086` (3.93:1), n8n `#75757f` (3.44:1). Ghost buttons match control border. |
| **[P1]** Instances page shows no health at all | Fixed | **Fixed** | `instances.js:100-145`: Cached reachability badge and last-checked relative timestamp rendered on every instance card at initial page load. |
| **[P2]** Every empty state is a dead end | Fixed | **Fixed** | All 21 empty states across all views carry operable buttons or links (e.g. "Open instances", "Start a promotion", "Add your first schedule"). |
| **[P2]** Assistant view opts out of 44px mobile scale | Fixed | **Fixed** | `components.css:273-274` & `forms.css:155-171`: Primary "Send", toolbar buttons, proposal actions, and checkbox containers hit 44px touch targets at 390px. |
| **[P2]** Single-control toolbars stretch to full width | Fixed | **Fixed** | `components.css:197` & `forms.css:108`: `.toolbar>.field` and `.toolbar>.picker` capped at `max-width: 360px`. |
| **[P2]** Promote wizard numbering and heading scale contradict each other | Fixed | **Fixed** | `promote.js:86-230` & `decisions.css:12`: All 3 steps render as identical 16px `h2.wizard-step` headings; Step 2 renders unconditionally before preflight. |
| **[P2]** Audit history is raw machine data with duplicate headers & 6 outcome vocabularies | Fixed | **Fixed** | `settings.js:7-22, 78`: Standardized outcome vocabulary; 48 action tokens humanized; pagination appends to existing `tbody` without duplicate headers. |
| **[P2]** Failure banners carry no icon and no recovery action | Fixed | **Fixed** | `payload.js:28-34`: Warning glyph rendered, retry callback wired as button, specific target context included. |
| **[P2]** Backups renders orphan table header above empty state | Fixed | **Fixed** | `backup.js:40`: `runs()` verifies `result.data.length > 0` before constructing `table(...)`. |
| **[P2]** Controls that cannot succeed stay enabled, validation pattern throws | Fixed | **Fixed** | `notes.js:33`, `promote.js:184`: Disabled buttons explain prerequisites in inline notices; `settings.js:66` escaped regex `\-` compiles without error. |
| **[P2]** Two form systems coexist in CSS + 3 off-scale control sizes | Fixed | **Fixed** | Superseded `.field` and `.danger` removed; `.control-reveal` aligned to scale; `:disabled` styled with explicit tokens. |
| *minor:* numeric columns left-aligned | Partly fixed | **Fixed** | `html.js:75-90`: `numeric` option wired in table helper and applied across views with tabular numerals (`font-variant-numeric: tabular-nums`). |
| *persona:* Alex — no shortcuts / bulk / sorting | Still open | **Partly fixed** | Fixed in Errors (`errors.js`: 7 shortcuts, `?` overlay, bulk resolve, 6 sortable columns), but accelerators remain trapped in Errors view. |

---

## New findings

### [P1] Five-Tier Toolbar Stacking Smothers Error Triage Viewport
- **What**: In `frontend/js/views/errors.js:171-177` and `frontend/css/triage.css:16-43`, five distinct horizontal control rows are stacked vertically: (1) Main toolbar (Status select, Refresh button, Shortcuts `?` button); (2) Saved Views bar (Select view, Save view, Delete view, Active view badge); (3) Bulk action bar (Selection count, Resolve selected, Select all visible, Clear); (4) Filter chips bar with error count summary; (5) Screen reader live announcer. On desktop this consumes ~280px of vertical height; on mobile it consumes over 340px before a single error row appears.
- **Why it matters**: During operational triage incidents, operators need immediate visual access to incident rows. Pushing the data table below the fold increases cognitive load and forces constant vertical scrolling past dozens of competing controls.
- **Fix**: Consolidate triage controls into a two-tier command strip in `frontend/js/views/errors.js:171-177`. Merge the status selector and type filter chips into a single unified segmented control on the top left. Render Saved Views as a compact dropdown on the right. Replace the filter chip row conditionally with the bulk actions bar only when rows are selected (`selectedIds.size > 0`).
- **Evidence**: `errors.js:171-177`, `triage.css:16-43`.
- **Suggested command**: `/impeccable layout frontend/js/views/errors.js`

### [P1] Siloed Accelerators: Keyboard Navigation Trapped Exclusively in Errors View
- **What**: While `frontend/js/views/errors.js:570-642` introduces robust keyboard shortcuts (`?`, `/`, `r`, `j`/`k`, `x`, `e`) and an accessible modal dialog, none of these accelerators exist anywhere else in the application. Pressing `/` in `frontend/js/views/workflows.js` does not focus search; pressing `r` in `frontend/js/views/observe.js` does not refresh traces; and global navigation shortcuts (e.g. jumping between views) do not exist.
- **Why it matters**: Power users (Alex) form muscle memory rapidly. Finding that shortcuts work in Errors but fail completely in Workflows, Observability, and Dashboard creates disorientation and broken expectations across the platform.
- **Fix**: Elevate the keyboard shortcut listener and help dialog into `frontend/js/app.js:174`. Register application-wide shortcuts: `/` to focus the primary filter/search on the active view, `r` to trigger view refresh, and `g d` / `g e` / `g w` / `g o` for instant view switching. Keep row navigation (`j`/`k`, `x`, `e`) scoped to data tables.
- **Evidence**: `errors.js:570-642`, `app.js:116`.
- **Suggested command**: `/impeccable harden frontend/js/app.js`

### [P2] Raw JSON Textareas as Primary Workflow and Credential Mapping Interface
- **What**: In `frontend/js/views/promote.js:93-95, 192-196`, the promotion preflight form presents three raw multiline `<textarea>` elements for Credential IDs, Secrets, and Subworkflow IDs (`jsonBox()`). While dynamic picker rows appear in `mappingBox` below after an initial run, the primary configuration asks users to manually edit JSON maps (`{"source_id": "target_id"}`).
- **Why it matters**: Non-developer operators and first-time users (Jordan) frequently introduce syntax errors (missing quotes, trailing commas). Typing raw JSON in a small textarea violates Nielsen Heuristic #6 (Recognition Rather Than Recall) and creates unnecessary anxiety during workflow promotion.
- **Fix**: In `frontend/js/views/promote.js:189-197`, make the interactive `createIdPicker` mapping rows the primary UI for all credential and subworkflow assignments. Relocate the raw JSON textareas into an advanced disclosure titled "Edit raw mappings JSON (advanced)" at the bottom of the card.
- **Evidence**: `promote.js:93-95, 189-197`.
- **Suggested command**: `/impeccable clarify frontend/js/views/promote.js`

### [P2] Missing Metric Heading Semantics and Incomplete Screen Reader Row Announcements
- **What**: In `frontend/js/views/dashboard.js:77` and `frontend/css/triage.css:10-14`, dashboard metric values render as `<p class="metric-number">` while their labels render as `<p class="muted">` inside an `<article aria-labelledby="...">`. Because neither uses a heading tag (`<h2>` or `<h3>`), screen reader users (Sam) navigating by headings cannot discover or jump between metrics. Furthermore, in `frontend/js/views/errors.js:534-546`, keyboard row navigation (`j`/`k`) moves visual focus (`is-focused`) and sets `tabindex="-1"`, but does not announce the newly focused row's workflow name, node, and severity to polite live regions.
- **Why it matters**: Violates WCAG 1.3.1 (Info and Relationships) and Nielsen Heuristic #1 (Visibility of System Status). Screen reader users cannot efficiently scan high-level fleet metrics or understand which error row is active during keyboard navigation.
- **Fix**: In `frontend/js/views/dashboard.js:77`, structure each metric cell with a semantic heading for the label (`<h3 class="metric-label" id="...">`) and associate the value. In `frontend/js/views/errors.js:544`, update `announcer.textContent` inside `focusRow()` to announce: `Row ${clamped + 1} of ${trList.length}: ${workflow} · ${node}, ${severity.label}`.
- **Evidence**: `dashboard.js:77`, `errors.js:534-546`.
- **Suggested command**: `/impeccable audit frontend/js/views/dashboard.js`

### [P3] Mobile Viewport Height Failure in AI Workspace with On-Screen Keyboard
- **What**: In `frontend/css/components.css:142, 187-188` and `frontend/js/views/assistant.js:43`, the assistant chat panel uses a rigid height (`height: 540px` under 700px). When a mobile user (Casey) taps into the composer `<textarea>`, the mobile virtual keyboard consumes 300px+ of viewport space, pushing the composer and send button completely off-screen.
- **Why it matters**: Casey is prevented from seeing what they are typing or tapping the send button when operating Inference from a mobile device during on-call incidents.
- **Fix**: In `frontend/css/components.css:188` and `frontend/css/repair.css:148`, replace fixed pixel heights with dynamic viewport flex layouts (`height: calc(100dvh - 120px); display: flex; flex-direction: column;`). Allow `.chat-transcript` to flex-shrink while pinning the composer to the bottom above `env(safe-area-inset-bottom)`.
- **Evidence**: `components.css:188`, `repair.css:148`.
- **Suggested command**: `/impeccable adapt frontend/js/views/assistant.js`

---

## Persona red flags

- **Alex (Impatient Power User)**:
  * *Red Flag*: Shortcuts are strictly locked inside `errors.js`. Alex cannot press `/` to find workflows in `workflows.js`, cannot press `r` to reload traces in `observe.js`, and cannot use keyboard chords to switch views without reaching for the mouse.
  * *Red Flag*: In `workflows.js:15`, search requires typing in the input and either pressing Enter or clicking the Search button, which then runs a sequential multi-instance fetch across instances without quick keyboard jumping between rows.
- **Jordan (Confused First-Timer)**:
  * *Red Flag*: Landing on the Overview (`dashboard.js`) with no connected instances presents empty states and "Unavailable" metrics without an onboarding checklist or guided setup wizard explaining how n8n interacts with Inference.
  * *Red Flag*: Promote (`promote.js`) uses dense technical jargon ("Subworkflow mapping", "Idempotency key", "Vault reference") without inline definitions or tooltips explaining what subworkflow triggers are or why error workflows must be explicitly mapped.
- **Sam (Accessibility-Dependent User)**:
  * *Red Flag*: Table row navigation in `errors.js:544` sets DOM focus on `<tr>` with `.is-focused`, but fails to announce row contents or position to screen readers via `aria-live`.
  * *Red Flag*: In `observe.js:31`, the waterfall timeline uses purely visual CSS `div` offset bars (`margin-left: ${offset}%`). While each span has an `aria-label`, the temporal duration and start relationships cannot be perceived non-visually.
  * *Red Flag*: Dashboard metrics (`dashboard.js:77`) lack heading markup, preventing screen reader users from navigating key metrics via heading navigation keys.
- **Casey (Distracted Mobile User)**:
  * *Red Flag*: In `errors.js`, the vertical stack of status dropdown, shortcuts button, saved views bar, bulk actions bar, and filter chips consumes ~320px on an iPhone screen, forcing the actual error table completely below the fold.
  * *Red Flag*: In `assistant.js`, opening the on-screen keyboard inside the chat composer pushes the input field off-screen due to fixed pixel heights (`components.css:188`).

---

## What is genuinely good — do not touch

1. **Honest Incomplete Telemetry Architecture (`frontend/js/views/dashboard.js:68-87`, `frontend/css/components.css:226`)**:
   - Inference’s core philosophical differentiator is refusing to fabricate metrics or equate absent telemetry with system failure. The `.coverage-partial` dashed amber card and the distinct "Unavailable" status pills cleanly communicate missing data without triggering false alarms during deployment lags.
2. **Resilient Data Retention on Failed Refresh (`frontend/js/views/dashboard.js:41-65`, `frontend/js/lib/payload.js:28-34`)**:
   - Rather than wiping dashboard counters on a transient 429 rate limit or network glitch, the UI retains all existing counters, appends an amber status notice naming the throttle reason, and provides an operable Retry button.
3. **High-Stakes Reassurance & Target-Naming Dialogs (`frontend/js/lib/confirm.js:62-97`)**:
   - Every high-risk operation (activating/deactivating workflows, deleting backups, purging Docker containers) presents an accessible modal that explicitly names the target entity and details the downstream consequences. Requiring typed confirmation (`PURGE`) for data destruction provides genuine confidence to operators under pressure.
4. **Evidence-Grounded AI Repair Workspace (`frontend/js/views/assistant.js`, `frontend/js/lib/proposals.js`)**:
   - The strict separation between untrusted LLM output and the server-validated proposal preview is masterclass systems engineering. Proposals cannot execute without meeting preconditions (e.g., deactivating active workflows), code diffs clearly mark added/removed lines, and self-triggering execution loops are blocked before reaching UI buttons.
5. **Universal Mobile Table Card Stacking (`frontend/css/triage.css:110-135`, `frontend/js/lib/html.js:133-140`)**:
   - Every data table in the application now stacks seamlessly into two-column key-value cards with `td::before` labels on small viewports, guaranteeing 0px horizontal overflow and zero clipped columns across all 14 views.

---

## Minor observations

1. **Font Loading Dependency (`frontend/css/base.css:1`)**: `@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');` will stall or fail in air-gapped or offline on-premise deployments. Although system fallbacks are specified, local font bundling is preferred for self-hosted infrastructure.
2. **Backlinks Semantic Structure (`frontend/js/views/notes.js:23`)**: Notes backlinks are appended directly as loose `<button>` tags inside a `<div>` rather than an accessible `<ul>`/`<li>` list.
3. **Cramped Docker Provisioning Form (`frontend/js/views/containers.js:70-84`)**: Six technical fields (bind IP, host ports, version, exporter URL) are packed into a single small card without logical grouping into network and container sections.
4. **Topbar Scope Switcher Borderless by Design (`frontend/css/shell.css:32`)**: The scope switcher select element has `border-color: transparent`, which registers as 1.00:1 border contrast. While intentional for its breadcrumb appearance, keyboard focus outline (2px solid) ensures accessibility.

---

## Synthesis: "Designed" vs "Assembled from Rapid Fixes"

The Inference frontend presents a striking dichotomy: **it has an exceptionally well-designed conceptual foundation that has been incrementally encrusted with localized, additive fixes.**

The underlying architecture—the Flowmapp blueprint palette, the strict refusal to fabricate data, the double-submit CSRF and Fernet crypto discipline, the honest classification of silent failures, and the robust human-in-the-loop AI safeguards—is thoughtful, mature, and deeply grounded in distributed systems engineering.

However, recent feature iterations exhibit the classic signature of reactive patching. Rather than holistically redesigning views to accommodate new capabilities, features were stacked on top of existing layouts:
- In `errors.js`, saved views and bulk actions were added by inserting two additional horizontal toolbar strips on top of the existing status bar, shortcuts button, and filter chips.
- In `promote.js`, step numbers were applied to headings, but the raw JSON mapping textareas remain in place.
- Keyboard shortcuts were built out thoroughly for Error triage, but completely omitted from the remaining 13 views.

The product has crossed the threshold from "fragile scaffold" (22/40) into a "robust operational workstation" (33/40). It is now at the inflection point where further localized micro-fixes will exacerbate visual clutter and cognitive overload. It is ready for an architectural layout pass that consolidates toolbars, unifies accelerators across the application shell, and integrates its powerful anomaly-detection toolset into a truly seamless operational experience.

---

## Questions to consider

1. *Are generic admin tools (Notes, Backups, Secrets) diluting Inference’s focus as an Execution Anomaly System? What would happen if Notes were replaced with contextual workflow incident runbooks tied directly to workflows?*
2. *Why are Dashboard, Fleet Health, and Instances three separate views when they all report on instance connectivity and public API health? Could they be unified into a cohesive Fleet Overview with drill-down capability?*
3. *What if the AI Assistant wasn't confined to a dedicated workspace tab, but available as an ambient, slide-over diagnostic companion directly from any error row or trace waterfall?*

> **Trend for `frontend-review` (last 3 runs): 22 → 29 → 33 (out of 40)**  
> Wrote `.impeccable/critique/2026-09-20T08-40-16Z__frontend-review.md`.
