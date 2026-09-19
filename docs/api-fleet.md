# Fleet connection API

All routes use `/api/n8n_proxy`, the existing session cookie, and CSRF protection for browser mutations. No route chooses a globally active instance.

## Instances

`POST /instances` (admin):

```json
{"name":"Production","url":"https://n8n.example.com","api_key":"your-n8n-api-key"}
```

Use the instance base URL, optionally with a reverse-proxy subpath, without `/api/v1`, credentials, query strings or fragments. Literal private/loopback destinations require `INFERENCE_EGRESS_ALLOW_CIDRS`; hostnames are resolved, validated and pinned when connecting. API keys are Fernet-encrypted in storage and are never returned.

`GET /instances` lists connections; `GET /instances/{id}` reads one. Viewers may read connection names and URLs. `PATCH /instances/{id}` accepts any nonempty subset of `name`, `url`, `api_key`; changes invalidate the saved probe. Admin-only `DELETE /instances/{id}` removes local connection configuration without deleting remote workflows or executions.

`POST /instances/{id}/probe` (admin) checks workflow and execution read access separately. A successful HTTP response does not prove permission to activate workflows or create credentials. The returned `capabilities` contain `workflow_read` and `execution_read`, each `supported`, `forbidden`, or `unavailable`; `diagnostics` contain safe error codes. No product version is guessed. Safe probe results are persisted for later UI use. If the URL or API key changes during the probe, `409 instance_changed` prevents saving stale results.

`GET /health` returns per-instance connectivity with bounded fan-out and polling limits. A failed instance does not discard healthy results. `reachable` means an API response established connectivity; it is not workflow success or general server health.

## Workflows and executions

| Route | Role | Query |
|---|---|---|
| `GET /workflows` | viewer | Required `instance_id`; optional `limit` (1–250), `cursor`, `active`, `name` |
| `GET /workflows/{id}` | viewer | Required `instance_id` |
| `POST /workflows/{id}/activate` | operator | Required `instance_id` |
| `POST /workflows/{id}/deactivate` | operator | Required `instance_id` |
| `GET /executions` | viewer | Required `instance_id`; optional `limit`, `cursor`, `workflow_id`, `status` |
| `GET /executions/{id}` | viewer | Required `instance_id`; includes execution data for inspection |
| `DELETE /executions/{id}` | operator | Required `instance_id`; irreversible remote execution deletion |

Lists return `{"data": [...], "next_cursor": null, "instance_id": "..."}`. Pass a non-null `next_cursor` unchanged into the next call's `cursor`. Each instance has its own cursor; do not reuse one across instances. Detail and successful mutation responses include the selected `instance_id`.

Execution status filters: `canceled`, `crashed`, `error`, `new`, `running`, `success`, `unknown`, `waiting`. List calls omit large execution payloads and pinned workflow data; detail calls provide the selected record. Workflow/execution JSON can itself contain sensitive business data, so viewer access is a trusted fleet-wide role.

Every remote mutation records audit intent and outcome. Request cancellation during the upstream call records an unknown outcome; cancellation during final audit persistence preserves the confirmed outcome before exiting. Process termination or storage failure can still leave an intent without a final outcome. Current role/session and the instance snapshot are rechecked before sending. Config edits and requests for the same instance are serialized; different instances can proceed concurrently. Revocation cannot retract a request already accepted by n8n.

## Errors and compatibility

Errors use `{"detail":"safe explanation","code":"machine_code"}`. Upstream API-key rejection returns a gateway error, not a local-login failure. Redirects and upstream cookies are not forwarded. Responses are bounded; malformed or oversized data is rejected.

**`upstream_outcome_unknown` means a write may have taken effect. Inspect the workflow/execution before retrying.** Requests are never automatically retried, including activation/deactivation. These controls affect real n8n instances when configured; tests use fixtures only.

The endpoint shapes were checked against the [n8n 2.19.0 workflow API contract](https://github.com/n8n-io/n8n/blob/n8n%402.19.0/packages/cli/src/public-api/v1/handlers/workflows/spec/paths/workflows.yml) and [execution API contract](https://github.com/n8n-io/n8n/blob/n8n%402.19.0/packages/cli/src/public-api/v1/handlers/executions/spec/paths/executions.yml). n8n 2 calls activation publication. Newer n8n versions may deprecate the activation endpoint; Inference reports incompatibility rather than guessing and retrying a mutation on a different endpoint.

Live n8n interoperability is not yet certified. Verification uses mocked HTTP boundaries plus a real loopback HTTP fixture through the production pinned transport.

## CodeLab node editing

`GET /workflows/{workflow_id}/nodes/{node_id}?instance_id=...` returns a Code node's `workflow_hash`, language and code. `PUT` on the same route requires operator role, CSRF and JSON `{"expected_hash":"hash-from-GET","code":"new code"}`. JavaScript, legacy Python and native Python Code nodes are supported; code is limited to 256 KiB of UTF-8.

The workflow must be explicitly inactive and its complete snapshot hash must still match. The write changes only the selected Code node's code parameter, then reads back the result. Concurrent Inference saves serialize; the second save with an old hash fails. Audit records retain intent and confirmed/failed/unknown outcomes without storing code. Timeouts and unverifiable writes are not retried.

The language fields follow the [n8n 2.19.0 Code-node source](https://github.com/n8n-io/n8n/blob/n8n%402.19.0/packages/nodes-base/nodes/Code/Code.node.ts). External editors can still race n8n's nonconditional workflow update API. This endpoint saves code; it does not run it or publish the workflow. The CodeLab frontend editor is implemented and verified in frontend/js/views/codelab.js.
