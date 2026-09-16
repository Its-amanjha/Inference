# Live assistant interoperability checks

The official MCP Python SDK runs in a separate environment, not in the application runtime:

```powershell
.venv/Scripts/python.exe -m venv test-results/mcp-sdk-env
test-results/mcp-sdk-env/Scripts/python.exe -m pip install mcp==1.30.0
.venv/Scripts/python.exe scripts/smoke_assistant_integrations.py --mcp-python test-results/mcp-sdk-env/Scripts/python.exe
```

This launches an independent SDK server over real loopback HTTP in both SSE and JSON response modes. Inference uses its real configuration, discovery, durable proposal, confirm/cancel and history routes. The test checks that discovery/proposal do not call the tool, confirmation returns 42, replay is rejected and cancellation leaves the invocation count unchanged. Temporary app/server processes and data are removed. Windows cleanup terminates process trees to release SQLite handles.

SDK 1.30.0 tests the application's supported MCP 2025-11-25 protocol. SDK 2.x/2026 protocol compatibility and arbitrary authenticated third-party deployments are separate, unverified coverage. The server's arithmetic tool is test content; protocol handling comes from the actual SDK, not hand-written response mocks.

For local CPU inference:

```powershell
docker pull ollama/ollama:0.34.0
.venv/Scripts/python.exe scripts/smoke_local_ollama.py
```

This starts a loopback-only disposable Ollama container, downloads qwen3:0.6b, warms the model and invokes chat through a temporary Inference installation. A dedicated labeled `inference-ollama-integration-cache` volume retains downloaded model files; the service container is removed afterward. Existing volumes with that name but without the expected label are refused. The small model checks integration, not production triage quality. Hosted inference requires an explicitly configured provider/key and is not established by local Ollama success.

## Repair mode

### Browser scenario

`scripts/smoke_frontend.py` calls `exercise_repair` from `scripts/frontend_repair_checks.py` inside the fleet fixture, after the approval and fleet-search scenarios. Every UI name the check relies on is in the `UI` dict at the top of that file. `scripts/frontend_fleet_fixture.py` simulates both n8n and the model over loopback HTTP. The model is a scripted OpenAI-compatible `/v1/chat/completions` endpoint, saved as the custom connection `scripted-repair` only in the smoke's temporary installation. No real inference, account or data is involved. Run the browser smoke alone.

The simulator serves the repair data only after the scenario enables it, so the earlier workflow and execution counts do not change:

- Workflow `wf-orders` on the source instance, active: Webhook, then HTTP Request with a v1 URL and an `Authorization: Bearer sk-fixture-secret` header parameter, then Code with `return item;`. The secret is fake.
- Failed execution 4821 in the `includeData=true` shape. `resultData.error` reads "Request failed with status code 404". The stack, context, cause, error description, node parameters, run data items and `workflowData` all carry the fake secret.
- n8n-style saves. A PUT carries name, nodes, connections and settings (staticData optional). Read-only fields, other fields and unknown settings get HTTP 400. Each save sets a new `versionId` and a later `updatedAt`, and every version stays readable at `GET /workflows/{id}/{versionId}`.
- Accepted writes are recorded in `state['writes']` and rejected ones in `state['repair']['rejected']`.
- One-shot switches in `state['repair']`: `concurrent_edit` saves a parameter change, as another editor would, before the next request for the workflow; `readback_divergence` accepts the next PUT but stores and returns different Code node content.
- A provider script (`queue_provider`) answers each request with text, a tool call, HTTP 429 or `finish_reason: "length"`. A tool call may use `{{content_hash}}`, which is filled from the latest workflow context in the request. Every request body is kept, so the check can count requests per human action and search them for the secret.

The error row comes from real token-scoped OTLP ingestion. The scenario then checks, in order:

1. An administrator's "Check tool calling" changes "Supports repair: untested" to "yes". A second check answered with 429 sends one request, is not retried and leaves "yes", including after the view re-renders.
2. The deep link pins the workflow and error. "What the AI sees" shows `[redacted` for the header value and the error description, and includes the scrubbed error message by default.
3. Each chat turn sends exactly one provider request. A text answer completes. A 429 ends the turn as interrupted with `provider_rate_limited`, no proposal and no write, and nothing is sent until "Try again". A cut-off answer says nothing was proposed or applied. A read tool waits for "Continue", then the model proposes a patch, which is cancelled without a write. With the conversation toggle off, the error message is absent from the provider request.
4. A proposal made through `tools/run` with the conversation ID renders inline with the server diff (URL and code line) and "AI description (unverified)". While the workflow is active, Apply is disabled and "Deactivate workflow" is offered, and a direct confirm returns 409 `workflow_active` without a write. Screenshots: `repair-desktop.png`, and `repair-mobile.png` at 390 px with no horizontal overflow.
5. The linked deactivate writes one POST. A concurrent edit then makes Apply stale: "Nothing was written", no write, and "Ask AI to re-read" is offered.
6. After the workflow is re-activated in n8n, a new proposal is blocked again. Deactivate and Apply write one POST and one PUT. The readback is verified, the real header value is kept, the workflow stays inactive, "Undo this change" and "Activate workflow" are offered, and a replayed confirm returns 409.
7. "Undo this change" creates a restore proposal. "Restore version" writes one PUT, and the workflow content equals the pre-repair version.
8. With readback divergence, Apply writes one PUT and ends uncertain. "Check current state" writes nothing, and Apply is not offered again.
9. With the administrator switch "Allow error text in repair conversations" off, the context withholds the error text and the provider request does not contain it. The switch is then turned back on.
10. "Repair with AI" links on the Errors table, the Workflows table and dashboard findings carry the instance and workflow IDs, plus the error ID for errors. Dashboard metrics come from a fixture, because the fleet aggregate check before this scenario uses the real budget of 4 requests per minute.

The secret must not appear in the DOM, form values, provider requests or browser API traffic, in any view. There is no exemption: `/api/n8n_proxy/workflows` masks secret parameter values before they reach a browser, so a secret in any API response fails the run.

### Live check

`scripts/smoke_live_repair.py` runs the manual live check against a disposable container, its managed n8n 2.19 bundle and a real tool-calling connection (`hy3` on api.b.ai by default). It is not part of any suite. Without `--confirm-live` it prints NOT RUN, and `--plan` lists its ten steps. The run provisions the container and bundle the way `scripts/smoke_live_n8n.py` does, then builds the fixture: an order-service workflow owning `/webhook/order-status`, and a failing workflow whose HTTP Request calls the misspelled `/webhook/order-stauts` with a per-run fake secret (`sk-live-<random>`) in an Authorization header. It checks context masking, the tool-calling status, one model request per human action, the linked deactivate plus a single verified write, a replayed confirm returning 409, a new n8n version, undo back to the pre-repair URL, and the absence of the fake secret from the transcript, approvals and context. Each step prints PASS or NOT VERIFIED, provider requests are paced at least 60 seconds apart, a 429 stops the run without retrying, and the bundle and container are removed through the ownership-checked API. The provider key comes from an environment variable (default `INFERENCE_LIVE_PROVIDER_KEY`) and is stored as a vault secret inside the disposable container; it is never passed as an argument.

hy3 answered a request sent right after a tool call with HTTP 429, and the same request 60 seconds later with 200. It sends no rate-limit headers. The live check therefore keeps at least 60 seconds between provider requests and never retries: a 429 stops the run, and the affected steps are reported as not verified.

Sources: [official MCP SDK](https://github.com/modelcontextprotocol/python-sdk), [Ollama Docker](https://docs.ollama.com/docker), [Qwen3 model](https://ollama.com/library/qwen3:0.6b). Actual run results are in IMPLEMENTATION.md.
