# Dashboard aggregate metrics

`GET /api/n8n_proxy/dashboard` accepts an optional `instance_id`. Without it, all saved instances contribute. Viewer permission is required; requests are limited to four per minute per user.

- `workflows`: number returned by complete workflow scans, or null if any included instance is incomplete.
- `executions_24h`: saved executions whose timezone-qualified `startedAt` falls within the returned window. Running/waiting executions count here.
- `terminal_executions_24h`: executions in success, error, crashed or canceled state within that same start-time window.
- `success_rate_percent`: successful terminal executions divided by all terminal executions. Null means no terminal executions or incomplete/unsupported data, never an assumed 100%.
- `confirmed_silent_error_spans_24h`: deduplicated received spans with `silent=1`, confirmed error health, and span start within the window. This is a span count, not a count of entire executions. Missing telemetry is not failure evidence.

The response includes exact window boundaries, per-instance observed counts/completeness and scan limits. It never contains execution payloads or credential values. Upstream failures and unsupported/malformed data leave affected aggregate totals null. Workflow and execution completeness are independent.

Each instance scans at most ten pages of 250 records per resource, with a 30-second per-instance deadline and up to three instance workers. Duplicate IDs/repeated cursors mark a scan incomplete. Full available history is scanned because the endpoint does not assume upstream ordering. No cache or background replica is created by this route.

n8n may prune executions or be configured not to save them. These metrics cover available saved history, not every execution that ever ran. Workflow/API changes during a multi-page scan cannot provide a distributed snapshot; live n8n compatibility and large-fleet load checks remain necessary. The dashboard labels this scope and reports incomplete instances.
