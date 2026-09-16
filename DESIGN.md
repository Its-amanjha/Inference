# Inference visual direction

User-approved replacement reference, 2026-09-14: Flowmapp — white blueprint desk with one blue pen. Product name: **Inference**. Tagline: **Execution Anomaly System**.

## Operational adaptation

Anomaly-first dashboard for operators, with Monitor / Orchestrate / Manage navigation groups. White surfaces, pale cool-gray canvas (#f5f7f9), black and Carbon (#222222) primary text/actions, Graphite (#636f7b) secondary text. Signal Blue (#0080ff) marks the brand; a darker blue (#0050c7) provides readable interactive text. Sky Wash (#c5e0fb) supports selection. Semantic green/rose/amber retain status meaning and text labels.

Inter 400/500/600/700 is the single UI family, with JetBrains Mono only for code. Compact 14px body, 30px page titles, 16px panel headings, tabular data numerals. Preserve native fields, visible keyboard focus and reduced motion. No marketing-scale headlines, decorative charts, invented metrics or floating mockups.

232px sidebar, a 56px top bar with the instance switcher and account menu, flexible two-column investigation/fleet area, four-metric strip, execution table, and separate retained-telemetry summary. 20px panel radii and pill buttons adapt the supplied shapes. Under 700px the sidebar becomes an in-flow Menu disclosure; instance selection remains visible. Dark theme retains the same hierarchy.

All fourteen views share the components. Investigation previews use the real errors API; empty results do not claim healthy telemetry. Partial coverage remains visible, while counting explanations use progressive disclosure. Refresh is serialized, abortable and paused in hidden tabs. The existing metrics endpoint bounds/rate limits remain authoritative.

## Research applied

- [Carbon dashboards](https://carbondesignsystem.com/data-visualization/dashboards/): prioritized data, limited top-level metrics, consistent colors and exploration links.
- [Grafana dashboard best practices](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/best-practices/): explicit monitoring questions, scope selection, overview-to-investigation paths and honest aggregation.

The supplied token table explicitly reserves Signal Blue from primary CTAs, despite a conflicting blue hero example. Use the reference's Carbon/Ink action treatment for operational primary buttons. Inter is explicitly supplied and takes precedence over the design detector's generic overused-font warning.

## Compatibility

The product is Inference, formerly VectisOps. Deployment identifiers now use Inference names (INFERENCE_* environment keys, `inference_session` cookie, `X-Inference-Bootstrap` header, `com.inference.*` Docker labels, `inference` package and `.ibackup` backup format). Legacy VectisOps identifiers remain accepted as fallbacks; see [Upgrading from VectisOps](docs/deployment.md#upgrading-from-vectisops). The `agd_csrf` cookie and `X-AGD-CSRF` header are unchanged. Original protected feature requirements are not rewritten.


## Assistant workspace

Use the available content width for chat with an adjacent action-review rail. Keep the composer at the bottom of a bounded chat panel and scroll messages independently. Place model selection in the toolbar, conversation IDs and privacy details in disclosures, and provider administration behind Assistant settings. Stack the rail below chat on narrow screens. Missing models disable sending and offer the admin a direct configuration action.

## Theme and icon refinement — 2026-09-14

Light remains the default; Dark and n8n inspired are selectable in the account menu and Settings, with persisted personal and installation defaults. The optional n8n-inspired theme uses charcoal surfaces, coral accents, compact 12px panels and 8px buttons. This is an original adaptation, not a claim of exact official n8n brand colors. Theme changes preserve operational status meanings and visible focus.

Shared page headings and common actions use small inline SVG icons with visible text labels; decorative SVGs are hidden from assistive technology. Wide tables are keyboard-focusable scroll regions. Theme tokens cover selection, primary actions, focus and status surfaces.

Research: [Carbon icon usage](https://carbondesignsystem.com/elements/icons/usage/) informs consistent sizing and accompanying labels; [n8n brand guidelines](https://n8n.io/brandguidelines/) provide the optional theme reference.

Dashboard refinement: compact metric strip precedes the main investigation workspace; consistent SVG icons identify metric types, section headers and instances. Findings remain the primary detailed content. Settings groups preference fields in a responsive two-column form with one current operation message.

## Partial data treatment — 2026-09-15

Missing or incomplete data has its own visual language and never borrows the failure style. Incomplete coverage renders as a dashed amber status note with an info icon, names each affected instance and which resource is incomplete (workflow list, execution history or both), and lists possible causes without claiming which one applies. Metrics that could not be counted show a muted "Unavailable" with a "Coverage incomplete" hint instead of a full-size value. Red error surfaces stay reserved for confirmed failures and failed requests. Counts that measure different things carry a one-line explanation where they appear (24-hour silent-error spans versus all retained error spans).

## Operational refinements — 2026-09-15

From the Impeccable critique (22/40) and research on operational dashboards and enterprise data tables ([Pencil & Paper](https://www.pencilandpaper.io/articles/ux-pattern-analysis-enterprise-data-tables), [Grafana](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/best-practices/)):

- **Severity** always combines icon, text and status color in a pill; color never carries meaning alone. Labels are shared: Silent failure, Zero-output warning, Execution error.
- **Triage** uses a dense table (13px cells, ~44px rows, sticky header, right-aligned counts, relative time with the absolute time available, execution ID or "Unavailable"). Under 700px each row stacks into a labelled card. Type filters state that they apply to loaded rows only.
- **Live updates** never rebuild the view under the reader: a single "New activity since this view loaded" bar offers Refresh view. Toasts sit bottom-right, merge duplicates with a count, pause on hover/focus and can be dismissed.
- **Decisions** read as sentences built from real response fields (promotion headline with blocking findings first, approval summaries with instance and workflow names, capability checklists). Raw JSON stays one click away in a "Raw payload" disclosure.
- **Actions** carry their risk: one primary per form; destructive actions use the crimson danger treatment (kept distinct from the n8n coral accent), live in More actions menus when secondary, and confirm in a dialog that names the target and consequence; purge requires typing PURGE. State (Active/Inactive, Enabled/Paused, audit outcomes) is shown as badges, not implied by a button.
- **Readability floor:** no UI text below 12px, sentence-case labels, touch targets ≥24px on desktop and ≥44px on mobile, numeric table columns right-aligned with tabular numerals, headings with more space above than below, accessible names for trace bars with a visible Failed marker.

## Forms and navigation — 2026-09-16

The user rejected the previous forms and navigation bar as generic. The replacements follow shipping products and design systems: GitHub Primer, IBM Carbon, Vercel Geist, Grafana Saga, Linear's 2026 refresh, Supabase and Stripe. Rules live in `css/forms.css` and `css/shell.css`.

- **Quiet chrome.** The sidebar and top bar sit on the page canvas; content sits on cards. The sidebar head and top bar share one 56px line. No taglines, slogans or badges in the chrome. Nav items are 32px (44px on touch) with 16px icons; the current view gets a neutral fill and semibold text, with the accent only on its icon. Page headings keep their icon without a tinted tile.
- **Top bar.** The instance switcher leads as a breadcrumb: a native select styled as a quiet button with an up/down chevron, then "/" and the current view. On the right, a live-updates dot with its text (the text is visually hidden below 1180px and stays available to assistive technology), then the account menu.
- **Account menu.** An initials avatar opens a disclosure with the signed-in name and role, a Theme segmented control (Light, Dark, n8n inspired), Settings and Sign out. It closes on Escape, outside clicks and link clicks. Theme and sign out are never top-level header controls. The page footer was removed.
- **Fields.** Label above the control (13px, weight 500), help text below it and linked with `aria-describedby`. Controls are 36px (44px and 16px text under 700px), 8px radius (6px in n8n inspired), with a 1px border of about 2.2:1 that darkens on hover. Focus changes the border to the focus colour plus a 1px ring, with no glow or offset. Invalid styling appears only after interaction (`:user-invalid`). Checkboxes sit beside their label and hint. Selects draw their chevron with gradients because the CSP blocks `data:` images.
- **Secrets.** Password and API-key inputs are masked, monospaced and carry one show/hide toggle (constant accessible name, `aria-pressed` state) that replaces the browser's own reveal button. A saved key is never shown again: edit forms leave the field blank and say that a key is already saved.
- **One control scale.** Buttons match the controls beside them: 36px by default, 32px for tables, rails and filter chips, and 44px on phones, where wide tables scroll inside their focusable region rather than crushing their cells. A toolbar holding a labelled field aligns to the control's bottom edge, not to the middle of label plus control. Measured across all fourteen views at 1440px and 390px.
- **Settings cards.** `formCard()` renders a 16px heading with a one-line description, a body capped at 720px, and a tinted footer: an optional note on the left and the actions on the right (Cancel, then the primary action). Inline editors, such as a user being edited, use the same field and action rules without a card.

## Guided connections and AI workspace

Assistant and CodeLab share one AI workspace entry with "AI workspace" and "Code editor (manual)" tabs; switching tabs keeps local state. Existing deep links remain supported. Model setup accepts provider details and a masked API key directly, saves encrypted credentials automatically and offers a separate connection test. Advanced references and tool access remain available. An empty custom URL forces users to enter their actual provider address. Never label a saved configuration connected until a test succeeds.

## AI repair workspace — 2026-09-16

One window, two tabs: **AI workspace** (the conversation, headed AI copilot) and **Code editor (manual)** (CodeLab). `#assistant` and `#codelab` deep links still open the matching tab.

- **People pin the target.** Operators get "Repair with AI" on Errors rows, overview findings and workflow rows; it opens `#assistant?mode=repair&instance_id=…&workflow_id=…&error_id=…`. A "Repair target" chip under the model toolbar names the instance, workflow name and ID, and the error or execution, with Change (inline workflow search) and Clear. The model never sets it; the address updates in place without rebuilding the view.
- **Context is visible.** "What the AI sees" shows the redaction count, the "Send scrubbed error text to the AI" choice (disabled with a sentence when an administrator turns error text off) and the exact context JSON. Replies carry a "Context sent" disclosure, and repair turns list their steps with status badges. Continue and Try again run only on a click.
- **Proposals sit in the conversation**, after the message that created them. The heading and change list come only from the server preview: Before/After values (secrets read "Masked"), code as monospaced lines with +/- markers in a horizontally scrolling block, and node, connection and setting changes as sentences. Risks are amber warning items with icon and text. The model's words are set apart as "AI description (unverified)"; the raw server preview is one click away.
- **One primary action.** The button uses the server's apply label (Apply change, Restore version) and stays disabled until every "Before you apply" item is done and for viewers. Deactivate workflow confirms in a dialog naming the workflow and instance; danger labels use the crimson treatment. Cancel is secondary.
- **Outcomes stay honest.** Stale reads "Nothing was written" with Ask AI to re-read. Uncertain uses the dashed amber unknown-outcome note with Check current state, a read-only comparison that never re-applies. Applied offers Undo this change (a restore proposal reviewed the same way) and Activate workflow (confirmed).
- **Rail and model support.** The action rail lists repair proposals as compact rows with Show in conversation; other approvals keep their full cards. A "Supports repair: yes / no / untested" badge sits beside the model selector, and administrators can run Check tool calling. With a target pinned, an untested or failed model gets a plain notice while diagnosis stays available.
- Diff tints (green added, rose removed) always pair with +/- markers. Amber means unknown or needs review; red stays reserved for confirmed failures.
- **Loops never reach Apply.** A change that makes a workflow call its own trigger address is refused by the server before a proposal card exists, and the conversation explains why; restore previews that reintroduce one show the risk "Calls this workflow’s own trigger (can loop forever)". "What the AI sees" includes the directory of other active workflows' trigger addresses (names, methods and masked routes only) so reviewers can see what the model could choose from. Added after a live test in which an unguarded self-call ran 3,816 times. Administrators can turn the directory off in Assistant settings under Repair conversations ("Share other workflows’ trigger addresses with the AI").
