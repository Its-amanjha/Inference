# Implementation conformance checkpoint

Reviewed against the protected AGENTS.md requirements and the later user-supplied light design. This is a source/verification checkpoint, not a production-readiness claim.

## Verified locally

All 14 native views exist and use application APIs. Browser checks cover onboarding, themes, notes, administration, downloaded backup restore, selected-instance workflow mutations, stale CodeLab edits, promotion replay and durable assistant approval cards. Backend tests cover the major authorization, persistence, telemetry and uncertain-write boundaries; see IMPLEMENTATION.md for exact results.

## Concrete gaps still open

| Requirement | Current evidence and remaining work |
|---|---|
| User preferences affect the UI | Resolved 2026-09-10: saved page size drives display requests; error notification setting filters error toasts. Browser persistence, request-limit and message-handler checks pass. |
| Complete dashboard metrics | Implemented 2026-09-11: workflow totals, saved 24-hour executions/completed success rate and confirmed silent-error spans. Bounded scans expose null incomplete totals. Backend and browser checks pass; live history/telemetry coverage limitations are explicit in docs/api-dashboard.md. |
| Fleet-wide workflow search | Resolved 2026-09-11: All instances mode searches across instance sections with bounded concurrency and independent cursors. Browser tests verify identical workflow ID target isolation and partial failure recovery. |
| Original theme assets | Resolved 2026-09-10: light.json/dark.json and validated cached loader implemented; appearance/persistence/race/failure/retry browser checks passed. |
| Notes Markdown coverage | Safe small dialect is implemented, with backlinks. Full CommonMark parsing and nested formatting are not claimed. |
| Delivery reproducibility | Base digest and 43 hashed runtime wheels pinned for Linux amd64/CPython 3.13. Locked build/version-match/runtime checks pass. Locked Linux backend regression passes all 671 tests, including POSIX cases. Other Python/architecture combinations and final live release validation remain. Python wheel alone does not contain a standalone frontend distribution. |
| External compatibility | Actual n8n/runners/OTLP, local/hosted model inference, MCP servers, S3 provider/disaster recovery still need independent live checks. POSIX backend filesystem tests passed in the locked Linux container. Container application smoke is separate from managed n8n bundle validation. |

The presence of every view does not mean every original feature or integration is complete. Preserve the requirements while closing these gaps. Preferences, dashboard/fleet aggregation and theme assets are implemented. Next verify real managed n8n/runners and OTLP, then live model/MCP and S3/disaster recovery integrations before final release review. Rebuild the Docker image for the latest frontend and theme preference changes.

2026-09-14 checkpoint: managed n8n/runners 2.19.0, JS/Python execution, proxy history, native OTLP arrival and owned cleanup passed on Docker Desktop Linux with direct bundle networking. Current source image rebuilt and container persistence smoke passed. Next: actual models/MCP, S3/disaster recovery, then remaining telemetry-depth and load/release checks. See IMPLEMENTATION.md for precise limits.
