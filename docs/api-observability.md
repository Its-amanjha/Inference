# Telemetry, errors and costs

## Exporter setup

An administrator creates a token with `POST /api/otel/tokens`, JSON `{"instance_id":"saved-instance-id","name":"n8n exporter"}`. The plaintext `token` is returned once; storage retains its hash. `GET /api/otel/tokens?instance_id=...` lists metadata; `DELETE /api/otel/tokens/{id}` revokes it. These admin routes use session authentication and CSRF.

Send traces to `POST /api/otel/v1/traces` with `Authorization: Bearer <token>`. This exact endpoint uses machine authentication, not browser cookies or CSRF. It accepts OTLP JSON (`application/json`) or protobuf (`application/x-protobuf`), with identity or gzip encoding. Responses use the request format and report rejected span counts through OTLP partial success.

The token determines the instance. Resource attributes cannot override it. Revocation and instance deletion are checked again inside the insertion transaction. Limits: 2 MiB request body, 8 MiB decoded body, 2,000 spans, 16 KiB safe attributes per span, four concurrent readers/parsers, 120 requests/minute per token and a 15-second body-read deadline. Invalid individual spans are rejected; duplicate instance/trace/span IDs are ignored. Partial-success retries must follow the [OTLP specification](https://opentelemetry.io/docs/specs/otlp/).

Prompt, input/output content, secret attributes and raw exception text are removed by default. Numeric token/item counts and exact model/provider identifiers are retained. Timestamps are stored as signed 64-bit nanoseconds and returned as strings so browsers preserve precision.

## Trace and error APIs

| Route | Role and behavior |
|---|---|
| `GET /api/otel/traces` | Viewer; optional `instance_id`, `workflow_id` (pair it with `instance_id`), `limit` 1–100, `before` cursor; groups by instance and trace; each row carries `workflow_id`, `workflow_name` and `execution_id` when a span reported them, else null |
| `GET /api/otel/traces/{trace_id}` | Viewer; required `instance_id`, optional `limit` up to 2,000 and `after` cursor; spans with attributes, exact costs and provenance |
| `GET /api/otel/summary` | Viewer; optional `instance_id`; actual span/trace/error counts |
| `POST /api/otel/classify` | Operator + CSRF; JSON `instance_id`, `execution_id`, optional `zero_output_nodes` IDs |
| `GET /api/errors` | Viewer; optional `instance_id`, `status=open/resolved`, `limit`, `before` |
| `POST /api/errors/{id}/resolve` | Operator + CSRF; marks an error resolved without deleting telemetry |
| `POST /api/errors/{id}/reopen` | Operator + CSRF; returns a resolved error to the open queue (the Undo after Mark resolved) |
| `GET/PUT /api/otel/retention` | Admin; PUT JSON `{"days":7}`, allowed 1–365 |

Trace detail costs cover the returned page (`cost_scope=returned_page`). `known_cost_usd` sums known decimal costs; `cost_usd` is null if any returned span has unknown cost. Pagination never silently claims a complete trace total.

Confirmed span errors enter triage at ingestion. Classification fetches execution evidence and caches a redacted terminal result for subsequent checks. It waits at least 30 seconds after terminal execution before inferring continuation errors or incomplete telemetry. Missing branches/spans do not prove failure. Opted-in zero-output warnings additionally require executed-node evidence, positive input count and at least ten successful historical executions for that workflow/node with no zero-output history. These are warnings (`health_status=low`), not confirmed errors.

Classification runs through the explicit API or an enabled persistent polling grant. Unsupported execution evidence stays unknown rather than producing a fabricated diagnosis.

## Automatic execution polling

Admin `GET/PUT /api/otel/polling/{instance_id}` reads or replaces persistent polling settings:

```json
{"enabled":true,"interval_seconds":60,"zero_output_nodes":{"workflow-id":["node-id"]}}
```

The interval is 30–3,600 seconds. The workflow/node map persists explicit zero-output preferences and is also used by manual classification when it does not supply a transient override. PUT replaces the complete configuration; omitted fields take their defaults.

The grant survives logout and application restart. It authorizes only execution reads on the configured instance. The granting administrator must remain enabled with admin role; configuration changes replace the grant version. Changing the instance connection requires re-enabling polling. Grants are rechecked before remote reads and classification commits, so disabling a grant during a read prevents its result from being stored.

Admin `POST /api/otel/polling/{instance_id}/run` requests a cycle immediately. Normal cycles use four bounded workers, at most 100 discovered execution records and 20 eligible local executions per instance, with a 120-second deadline. Up to ten cursor pages are swept before returning to the newest page. Telemetry drives classification candidates; execution-list discovery alone does not invent node evidence. A run with no telemetry remains incomplete.

Terminal execution details are cached. Classification runs again when the arrival grace expires, late spans arrive, or preference generation changes. Completed cache entries are filtered before applying batch limits; a deleted/malformed execution gets individual backoff rather than blocking healthy work. GET settings reports discovery/classification counts, cursor progress and safe failures. Deadline/backoff state persists. Code and execution writes, credential reads and switching instances are outside the poller's grant.

Retention runs at startup and hourly, using server receipt time for spans. It removes expired spans, errors and cached execution evidence. Client-supplied future timestamps cannot extend retention.

## Price book

`GET /api/otel/prices` returns selected exact-model rates and refresh status. Admin `PUT /api/otel/prices/{model}` accepts decimal USD-per-million values:

```json
{"input_per_million":"2","output_per_million":"8","cached_input_per_million":"0.5"}
```

Optional `reasoning_output_per_million` prices the reported reasoning subset. Cached and reasoning token categories are not counted twice. Unknown model/usage returns null; explicit Ollama usage records zero provider charge. Costs retain exact decimal text, source, timestamp and estimate status. An override affects new ingested spans; previous stored estimates retain their original provenance.

Admin `POST /api/otel/prices/sync` enables persistent refresh and attempts a bounded fetch from the fixed public OpenRouter model catalog. It sends no instance credentials or client cookies. Refresh repeats every six hours with bounded failure backoff; failures retain the last successful cache. Offline defaults are seeded without network access. Priority is override, then live cache, then documented bundled defaults. Exact model IDs are preserved; no fuzzy aliases or provider-prefix stripping.

Catalog units follow the [OpenRouter model API](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties). The two bundled snapshot rates include their OpenAI model-page source URLs and verification date; provider billing may differ from stored estimates.

## Verified boundary

Tests exercise parser limits/redaction, real database/auth, machine-token attribution and revocation, exact pricing, classification evidence and local real-server ingestion across restart. Live n8n exporter compatibility and production telemetry load have not been verified. The full remaining product scope stays in AGENTS.md and IMPLEMENTATION.md.
