---
target: Inference frontend
total_score: 22
max_score: 40
na_heuristics: 
p0_count: 1
p1_count: 3
target_identity: "file:D:\\n8n debugging\\frontend\\index.html"
target_fingerprint: "sha256:ac9be88161a4fae3238ef522ef2ae823a2d9e95cdcd376f7f33480961d9b88bd"
target_path: "D:\\n8n debugging\\frontend\\index.html"
timestamp: 2026-09-15T16-00-09Z
slug: frontend-index-html
---
Method: dual-agent (A: design review · B: detector + browser)

## Design health score

| # | Heuristic | Score | Key issue |
|---|---|---|---|
| 1 | Visibility of System Status | 3 | Live status/coverage good; findings lack timestamps; live refresh silently rebuilds view |
| 2 | Match System / Real World | 2 | Raw JSON, internal hashes, raw error_type values |
| 3 | User Control and Freedom | 2 | No undo on Mark resolved; auto-refresh replaces page mid-read |
| 4 | Consistency and Standards | 2 | "Silent failure"/"Silent Failure"; Observability/Observe; Workflows State column holds a button |
| 5 | Error Prevention | 3 | Strong preflight/idempotency/PURGE/approvals; destructive buttons look safe; confirms omit target |
| 6 | Recognition Rather Than Recall | 2 | Typed workflow IDs, conversation IDs, JSON mappings |
| 7 | Flexibility and Efficiency | 1 | No shortcuts, bulk resolve, sorting, filters |
| 8 | Aesthetic and Minimalist Design | 2 | Calm overview; JSON walls and repeated headings elsewhere |
| 9 | Error Recovery | 3 | Excellent recovery copy; coverage banner lists three causes without naming which |
| 10 | Help and Documentation | 2 | Inline hints; no doc links or term definitions |
| Total | | 22/40 | Acceptable |

## Design specificity verdict
Specific in words, interchangeable in form. Honest n8n-aware copy; stock metric strip + white cards; no time ordering, severity, blast radius or execution links; Errors view (core) is the thinnest view. Detector: 2 overused-font (Inter) findings, false positive (brief mandates Inter). Browser overlay: 9-10px subtitle and 11px shell text on all routes, all-caps metric labels, ~114ch lines and 0px heading spacing on AI workspace, one unconfirmed mobile text-occlusion, three 18px mobile links. No JS errors except favicon 404; no overflow; no unnamed controls; focus visible on first 10 tab stops.

## What's working
1. Honest data states (Unavailable vs fabricated 0; No completed runs).
2. Promotion as staged state machine with invalidation and idempotency.
3. Shared theme tokens, tabular numerals, visible focus, focusable table regions, skip link.

## Priority issues
- [P0] Incomplete telemetry styled as confirmed failure (dashboard.js:54 uses .error). Fix: neutral/amber partial treatment with info icon; muted smaller Unavailable; explain the two confirmed-error counts. clarify/colorize.
- [P1] Triage queue lacks triage data (errors.js:15 unclassed badge, raw occurred_at; dashboard findings lack time/execution). Fix: severity pill icon+color+text, relative time with absolute title, workflow/node, instance, right-aligned count, execution id, consistent labels, type filters. layout/typeset.
- [P1] Live updates destroy place (app.js:38-39 navigate()); toasts cover header, no dedupe/dismiss. Fix: "New updates - Refresh" bar; relocate/dedupe/dismissible toasts. harden.
- [P1] Raw JSON at high-stakes moments (promote.js:28/31, assistant.js:65, fleet-health.js:11, instances.js:32). Fix: readable change summary, sentence-form approvals, raw payload disclosure. clarify/distill.
- [P2] Flat action hierarchy (.danger unused, Workflows state hidden, generic window.confirm). Fix: danger styling, Active/Inactive badge, target-naming confirms. polish.

## Persona red flags
- Alex: no shortcuts, no bulk resolve, no sorting, no Ctrl+Enter, conversation resume by pasted ID.
- Sam: metric values are h2 with p labels; aria-live toast floods; focus lost on refresh; 9-11px text; trace details only in title tooltip.
- On-call operator: red outage-looking banner; findings lack time/execution link; only action Explain with AI even with no model.

## Minor observations
Mobile select truncation; numeric columns left-aligned; audit outcome vocabulary mixed; unused Signal Blue .hero; filler slogans; favicon 404.

## Questions to consider
- Why open with four counters instead of the single most urgent anomaly?
- Should "unknown" have its own visual language?
