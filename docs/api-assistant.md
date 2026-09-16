# Assistant API

All routes are under `/api/assistant`, require a session, and use the usual CSRF header on mutations. Model output and MCP tool annotations never authorize changes.

## Local models

Use an existing **Ollama** server through its OpenAI-compatible `/v1` API. Custom OpenAI-compatible servers also work. Inference does not download models or bundle a model server. Set the exact installed model identifier; select `tools_enabled: false` for a model that cannot emit tool calls. There is no automatic fallback to a hosted provider.

For Ollama running on the same host as the Inference Python process, permit loopback egress with `INFERENCE_EGRESS_ALLOW_CIDRS=127.0.0.1/32`. Without it the saved connection fails with `provider_configuration_invalid`, because loopback is not a global address and needs an explicit allowlist entry. Inside a container, loopback points at that container; use the reachable model server address and explicitly permit its private IP/CIDR instead. Existing metadata/link-local address blocks still apply.

An admin configures a local provider with `PUT /config/provider`:

```json
{
  "id": "local",
  "config": {
    "kind": "ollama",
    "base_url": "http://127.0.0.1:11434/v1",
    "model": "YOUR_INSTALLED_MODEL:EXACT_TAG",
    "tools_enabled": true
  }
}
```

Replace the example model value with an installed model. No API key is required for Ollama. See the [official Ollama compatibility contract](https://docs.ollama.com/api/openai-compatibility). Tool quality and support depend on the selected model; unsupported or malformed calls fail explicitly.

### Ollama setup

1. Install Ollama and pull a model that supports tool calls. `qwen2.5:3b` (1.9 GB) is the verified choice: `ollama pull qwen2.5:3b`.
2. Start Inference with `INFERENCE_EGRESS_ALLOW_CIDRS=127.0.0.1/32` when Ollama listens on the same host's loopback. Add it to any existing value, comma-separated.
3. In **Connect a model**, choose **Local Ollama**. The base URL `http://127.0.0.1:11434/v1` is filled in, **Advanced** opens, and the model field suggests `qwen2.5:3b`. No key is needed.
4. Save, then **Test connection**. Enable tool calls and run the tool-calling check; with tools enabled, the model can call `get_skill` and the read tools.

The form shows a hint under the base URL when the host is a literal loopback address (`127.x.x.x`, `::1`, or `localhost`, which the hint treats as `127.0.0.1`) or a private IPv4 literal (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`). The hint names that exact address as the value to permit: `/32`, or `::1/128` for `::1`. An administrator's form reads `allow_cidrs` from `GET /api/admin/settings` and checks whether any entry's network contains the address, the same containment test `backend/net.py` applies. It then says either that the deployment already permits egress to loopback or to "the private address <ip>", or that it does not permit it yet and names the variable and value to restart with. For `::1`, only the entries `::1/128` and `::/0` count. If the settings lookup is denied or fails, the form shows the static instruction instead. Other hostnames, public addresses and private IPv6 literals show no hint. The hint depends only on the base URL, so the preset URLs of hosted providers never show it.

A literal IP address that egress rules block is refused before any request is sent, as 400 `provider_configuration_invalid`. A hostname such as `localhost` is checked when the connection is made, so it fails as a transport error instead. Use `127.0.0.1` in the base URL.

**Containers.** Inside the Inference container, `127.0.0.1` is the container itself, not the host running Ollama. Use an address of the host that the container can reach, permit that address's CIDR in `INFERENCE_EGRESS_ALLOW_CIDRS`, and make sure Ollama listens on that interface, not only on loopback.

**Verified.** On 2026-09-17, against a native Ollama with `qwen2.5:3b` on CPU, with `INFERENCE_EGRESS_ALLOW_CIDRS=127.0.0.1/32`: the keyless provider saved, the connection test passed, the tool-calling check reported `supported`, chat answered, and the model called `get_skill` on its own. Without the allowlist entry the connection failed with `provider_configuration_invalid`.

**Speed.** CPU inference is slow, and skill-guided answers are slower still, because a guide adds up to 24 KiB to the prompt. Provider calls are non-streaming, so nothing arrives until the whole answer is generated. Measured with `qwen2.5:3b` on CPU (about 4 to 5.5 tokens per second):

| Request | Time |
|---|---|
| Follow-up turn with the full 17 KB guide injected | 175.6 s; 123.4 s on a repeat with the prompt prefix cached |
| Follow-up turn with an 8 KB excerpt | 164.3 s |
| Follow-up turn with a 4 KB excerpt | 32.1 s |
| Live chat, first turn | 14.0 s |
| Live chat, follow-up after the model read a guide | 228 s |

**Timeouts.** Each provider call has a read timeout (the longest wait for response data) and a total limit for the whole call:

| Provider | Read | Total |
|---|---|---|
| Hosted | 45 s | 60 s |
| Local | 600 s | 630 s |

Connect (5 s), write (10 s) and pool (5 s) timeouts are the same for both. A provider counts as local when its kind is `ollama`, on any host, or when its kind is `custom` and the base URL host is `localhost`, a name ending in `.localhost`, or a loopback or private IP literal. The host name is not resolved, so a public hostname never gets the local budget. The kinds `openai`, `openrouter`, `groq` and `anthropic` are never local, even with a base URL on `127.0.0.1`.

The local budget exists because the hosted one cut off real CPU inference: before the change, a skill-guided live chat on `qwen2.5:3b` failed after 47 seconds with 502 `provider_unavailable`. After it, the same chat returned 200 in 243 seconds, and the model called `get_skill` on its own. A standard chat can make up to three provider calls, so a local chat can take several minutes. Neither the browser client nor the server sets a shorter response timeout. The server limits only how long the request body may take to arrive (15 seconds).

**Context window.** With the full guide injected, Ollama truncated the prompt to fit its context window. Its log showed `truncating input prompt limit=2050 prompt=5989/6144 keep=4`: only the first few tokens and the end of the prompt were kept, so the system prompt and the user's question were dropped, and the model summarised the guide instead of answering the Code node question. Inference talks to Ollama's OpenAI-compatible `/v1` endpoint, which does not accept `num_ctx` per request, so the context has to be raised on the Ollama side: set `OLLAMA_CONTEXT_LENGTH=8192` in the environment of the Ollama process and restart Ollama, or build a model from a Modelfile with a larger `num_ctx`. Verified on 2026-09-17 with `OLLAMA_CONTEXT_LENGTH=8192` on `qwen2.5:3b` (CPU): the same Code node question produced a 6,440-token prompt with no truncation (`truncated = 0` in the Ollama log), the model called `get_skill`, and the answer addressed the error with `const items = $input.all();`. The chat returned in 295 seconds (33 s for the tool call, 4 min 22 s for the guided answer). `ollama ps` shows the active context in its CONTEXT column.

**Seeing tool calls.** `GET /chats/{id}` stores only user messages and final answers for standard chats, so tool calls such as `get_skill` do not appear in the history. Ollama's log shows each provider call as a separate request.

## Provider configuration and chat

`PUT /config/provider` accepts `{id,config}`. Config fields: `kind` (`openai`, `openrouter`, `groq`, `ollama`, `custom`, `anthropic`), `base_url`, exact `model`, optional `api_key_ref`, `tools_enabled` (default true). The API base must include the provider's version prefix, such as `/v1`; the adapter appends `/chat/completions`, or `/messages` for Anthropic. OpenRouter's base is `/api/v1`. Keys use whole references such as `$OPENAI_KEY` or `${SERVICE.key}`, resolved server-side. Hosted kinds require keys; Ollama/custom permit keyless endpoints. Store secrets using the existing admin vault API. Configurations and chat records are encrypted in `documents`.

`GET /config/provider` lists configured provider IDs, kinds, model IDs, tool settings and `tool_calling: {status, checked_at}` for viewers (`status` is `supported`, `unsupported` or `untested`). Only admins see endpoints; no response returns keys or their references. Configuration writes are admin-only and audited.

`POST /chat` accepts `{provider_id, message, chat_id?}` and returns `{id, content, pending_actions, usage, context_log: []}`. Omit `chat_id` to begin a conversation; `GET /chats/{id}` retrieves the requesting user's history. The service retains the most recent 20 user/assistant messages; assistant messages that created proposals carry `action_ids`, which are stored locally and never sent to a provider. Standard chats are non-streaming, with at most three provider rounds and eight tool calls per round, four simultaneous chat handlers, and 30 requests per user per minute. A round's proposals are prepared first and stored together, so a failing call leaves no partial proposals. Provider requests are bounded at 256 KiB, responses at 1 MiB, hosted provider calls at 60 seconds (45-second read timeout) and local provider calls at 630 seconds (600-second read timeout; see [Ollama setup](#ollama-setup) for which providers count as local). There are no automatic provider retries. A history too large for the provider request bound returns an explicit error; start a new chat. Adding `context` or `continue_turn` makes the request a repair turn (below).

Only metadata from built-in read tools and skill guides goes to providers in standard chats; repair tools are not offered there. Workflow bodies, execution inputs/outputs and stored secrets are not included automatically. Text typed by the user is sent to their selected provider. Chat accepts user text only; browsers cannot inject system/tool-role messages. Tool results and generated content remain untrusted text for the frontend to escape.

### Tool errors in conversations

The model writes its own tool calls, so a call can name a missing execution, an unreachable instance or invalid arguments.

**Standard chats.** When a read tool fails, for example an n8n 404 or `invalid_tool_arguments`, the failure goes back to the model as that call's result and the turn continues:

```json
{"error": {"code": "invalid_tool_arguments", "detail": "Tool arguments for get_execution do not match the permitted schema. execution_id: Field required"}}
```

Only the error's code and detail are sent, never the call's argument values. Session and permission failures (`authentication_required`, `session_revoked`, `session_expired`, `permission_denied`) still end the request with that error. Failures while preparing a proposal (mutations and `mcp_call`) also end the request, and no proposal from that round is stored. Before this change, any failing read ended the whole request.

**Repair conversations.** All tool calls in a step are validated first. A validation, target or proposal failure rejects the whole round: the error results are stored, the step is `interrupted`, and Continue lets the model correct itself. A read that passes validation but fails when run, such as an unreachable instance, is stored as `{"error": {"code", "detail"}}` for that call, and the step ends at `awaiting_continue`. Session and permission failures end the request. See [Repair turns](#repair-turns).

`get_skill` answers an unknown skill name with a normal tool result in the same error shape, plus the installed IDs: `{"skill", "error": {"code": "skill_not_installed", "detail"}, "available": [...]}`. The conversation continues. See [Skills](#skills).

### Provider adapters

- Tool calls from every kind are normalized to `{id, name, arguments}` with object arguments. OpenAI-compatible arguments may be a JSON string (duplicate keys rejected) or an object; `finish_reason: "stop"` is accepted when complete tool calls are present. Anthropic calls come from `tool_use.input`. Call IDs, names (must be declared), count (≤16) and key redaction are checked as before.
- Tool declarations are flattened for every kind: local `$ref` inlined, no `$defs`, `discriminator` or `title`; optional `X | None` fields become omittable `X`; the repair operation union becomes one object with an `op` enum whose description lists each operation's required fields. The Pydantic contract models remain the only validators.
- Only wire fields (`role`, `content`, `tool_calls`, `tool_call_id`) are forwarded; local annotations never leave the server.
- Errors: provider HTTP 429 → HTTP 429 `provider_rate_limited` with `retry_after_seconds` (numeric `Retry-After` only, else `null`); truncated output (`finish_reason` `length`/`content_filter`, Anthropic `max_tokens`/`pause_turn`) → 502 `provider_incomplete`; other non-200 → 502 `provider_error`; malformed responses → 502 `provider_invalid_response`; transport failures and timeouts → 502 `provider_unavailable`. Exactly one outbound request is made; none is retried.

### Tool-calling check ("Supports repair")

`POST /providers/{id}/tool-test` (admin, CSRF, six checks per admin per minute) sends one request with a dummy `get_time` tool and no workflow data, even when `tools_enabled` is false. It writes no chat history, creates no proposal and never retries. Response: `{id, model, tool_calling: {status, checked_at}, detail}`.

- `supported`: one well-formed `get_time` call whose arguments match its schema.
- `unsupported`: a text answer without a call, a call with mismatching arguments, or a malformed/unexpected call (`provider_invalid_response`).
- Rate limits (`provider_rate_limited`), timeouts, incomplete output and provider errors return the usual error response and leave any earlier result untouched.

The result is stored encrypted, bound to the full provider configuration plus a SHA-256 of the resolved key value; any configuration or key change reports `untested` until checked again. A configuration change during the check returns 409 `provider_changed` without storing. Checks are audited as `assistant.tool_calling_checked`.

## Built-in tools and approvals

`GET /tools` returns role-filtered, flattened tool declarations. `POST /tools/run` accepts `{name, arguments, chat_id?}`; `chat_id` only labels resulting proposals. Built-in tools require explicit `instance_id`, except `get_skill`, which reads local content:

| Tool | Arguments beyond instance_id | Behavior |
|---|---|---|
| `get_skill` | `skill_name` only; no `instance_id` | Viewer; one bundled n8n guide, capped at 24 KiB ([skills](skills.md)) |
| `list_workflows` | optional `limit`, 1–25 | Viewer; IDs, names and active flags |
| `get_execution` | `execution_id` | Viewer; execution status/timestamps only |
| `list_errors` | optional `limit`, 1–25 | Viewer; structural error metadata, no messages/payloads |
| `get_workflow_context` | `workflow_id`, optional `focus_nodes` (≤10) | Viewer; masked workflow context (repair) |
| `get_error_context` | exactly one of `error_id`, `execution_id` | Viewer; scrubbed error context (repair) |
| `activate_workflow`, `deactivate_workflow` | `workflow_id` | Operator; proposal |
| `delete_execution` | `execution_id` | Operator; proposal |
| `update_node` | `workflow_id`, `node_id`, `code` | Operator; proposal for an inactive Code node |
| `propose_workflow_patch` | `workflow_id`, `base_content_hash`, `summary` (≤280), `operations` (1–50) | Operator; repair proposal |
| `propose_restore_workflow` | `workflow_id`, `base_content_hash`, exactly one of `repair_id`, `version_id` | Operator; restore proposal |

Repair argument schemas are the strict models in `backend/modules/assistant/repair_contract.py` (contract v1). Stored repair arguments keep only supplied fields.

### Tools by conversation scope

Declarations are filtered by the user's role; a higher role also sees the lower roles' tools.

| Scope | Viewer | Operator adds | Admin adds |
|---|---|---|---|
| Standard chat | `list_workflows`, `get_execution`, `list_errors`, `get_skill` | `activate_workflow`, `deactivate_workflow`, `delete_execution`, `update_node` | `list_mcp_servers`, `list_mcp_tools`, `mcp_call` |
| Repair turn | `get_workflow_context`, `get_error_context`, `get_skill` | `propose_workflow_patch`, `propose_restore_workflow`, `activate_workflow`, `deactivate_workflow` | none (MCP tools are hidden) |

`GET /tools` without a scope lists every tool the role permits.

A mutation returns `pending_action` with an opaque ID, exact arguments, expiry, status, `warning`, `chat_id` and `preview` (`null` except for repair proposals, which return the contract `PendingRepairAction` with `target` and `preview`). A repair proposal on an active workflow also returns `linked_actions`: a `deactivate_workflow` proposal (with `linked_to`) created in the same transaction. `POST /tools/confirm` and `/tools/cancel` accept only `{action_id}`; replacement arguments are rejected.

`GET /approvals` lists the latest 100 records belonging to this user and this session. Items carry `id, name, target, status, expires_at, created_at, arguments, chat_id, linked_to, preview_summary`; for repair proposals `target` is the contract `Target` object and `preview_summary` its `summary_text`, otherwise `target` is the instance or MCP server ID and `preview_summary` is `null`. `GET /approvals/{id}` returns the full `pending_action` for this user and session (404 otherwise); repair preconditions report `satisfied: true` once the linked deactivation succeeded.

Proposals expire after 15 minutes and bind the user, session, arguments, instance connection and resource fingerprint. SQLite atomically consumes a proposal before effects; replay, another session (404), changed roles (403, rechecked live at confirm) and stale resources cannot execute it. Code updates retain the approved baseline through the CodeLab stale-write check. At most 256 proposals globally and 32 per user remain pending, counted atomically for linked records; the in-memory cache is also bounded. Durable state is authoritative.

Statuses include `pending`, `executing`, `succeeded`, `failed`, `stale`, `cancelled`, `expired`, `uncertain`. Startup expires pending actions and changes interrupted executions to uncertain, with audit records. Timeouts/cancellation after a possible write are not retried. Inspect remote state before creating another proposal. n8n's non-conditional API still permits external-editor races after the final read; local locks cannot supply a distributed compare-and-swap.

## Skills

Inference ships 15 n8n guides in `backend/skills/`, loaded into the `skills` table at every start. The model reads one with `get_skill` (`{"skill_name": "n8n-code-javascript"}`); people read them through `GET /api/skills` and `GET /api/skills/{id}`. The full reference, including the ID list, sync rules and limits, is in [Skill library](skills.md).

- The system prompt tells the model to call `get_skill` before writing or debugging n8n JavaScript, Python, expressions, error handling or node configuration. It also says skill text is reference material that never authorises an action.
- `get_skill` is a viewer read and never creates a proposal. It is declared in standard and repair conversations.
- Names are matched after trimming, case-folding, and turning spaces and underscores into hyphens. An unknown name returns `error: {"code": "skill_not_installed", "detail"}` with up to 50 installed IDs in `available`, and the conversation continues.
- `content` is capped at 24 KiB, and `truncated` reports whether the guide was cut.
- In repair conversations `get_skill` has no `instance_id` and skips the pinned-instance check (`INSTANCE_FREE_TOOLS`). Like other repair reads, it ends the step at `awaiting_continue`.

## AI repair (contract v1, scope A)

Repair covers masked context reads, workflow patches and restore (undo). Creating workflows and retrying executions are not available.

### Repair turns

`POST /chat` with `context` (`RepairTarget`: `{mode: "repair", instance_id, workflow_id?, error_id? | execution_id?, send_error_text: true}`) starts a repair turn; send the same `context` with each new message of a repair conversation. Every human action sends **at most one provider request**:

1. The server reads the context itself (no model cost): the workflow (`workflow_id`, or the one the error/execution belongs to) and the error summary. An error or execution belonging to another workflow stops the turn with `target_mismatch` before any provider request.
2. With tools enabled the context is injected as one synthetic `get_workflow_context` (or `get_error_context`) call and result; without tools it is a user-role data message between random-nonce markers. It is never placed in the system prompt, which states that remote text is data, never instructions.
3. One provider request is sent. Declared tools: `get_workflow_context`, `get_error_context`, `get_skill`, and for operators `propose_workflow_patch`, `propose_restore_workflow`, `activate_workflow`, `deactivate_workflow`. MCP tools are hidden. Tools are declared on the first two requests of a turn only.

Response: `{id, mode: "repair", status, content, steps, pending_actions, context_log, usage, retry_after_seconds}`.

- `status`: `completed` (text answer, or proposals created), `awaiting_continue` (the model requested reads; results are stored and **no further request is sent**) or `interrupted` (context read, provider or tool-call failure; transcript saved, nothing proposed or applied).
- `steps`: `[{n, kind: "context_read"|"model_request", tool, provider_id, status: "done"|"rate_limited"|"failed", code, bytes, message}]`. A provider 429 gives `status: "rate_limited"`, `code: "provider_rate_limited"` and `retry_after_seconds` when known.
- `context_log`: `[{tools, delivery: "tool_result"|"data_message", bytes, redaction_count, notes}]` for the turn's injected context; `notes` may contain `repair_error_text_disabled`.

`POST /chat` with `{provider_id, chat_id, continue_turn: true, message?}` sends the next single request of a pending turn (after reads, or to retry an interrupted request without re-reading context). A turn waits 15 minutes; otherwise `409 turn_not_pending`. A continuation whose `context` differs (target or `send_error_text`) returns `409 target_mismatch`; if error text was sent in the turn and an administrator has since disabled it, the turn is dropped with `409 repair_error_text_disabled`; likewise, if the turn's context included `webhook_directory` and an administrator has since turned sharing off, the turn is dropped with `409 repair_webhook_directory_disabled`.

Tool calls in a turn are all validated first (schema, count ≤8, pinned instance and workflow; `get_skill` names no instance and is exempt from the instance check). Any failure rejects the whole round (`target_mismatch`, `invalid_tool_arguments`, `invalid_patch`, `context_stale`, `history_unavailable`, `approval_limit`): no read runs, no proposal is stored, and the error results are kept so `continue_turn` lets the model correct itself. Proposals and their linked deactivations are stored in one transaction. Only `get_workflow_context` views the model was given in the current turn count as "seen": code truncated in every such view of the same content cannot be replaced.

### Context

`GET /repair/context?instance_id=&workflow_id=&error_id=|execution_id=&send_error_text=` (viewer) returns exactly the bytes a repair turn injects for that target: a `RepairContextResponse` whose `bytes` equals the body length. Header `X-Inference-Context-Notes: repair_error_text_disabled` marks withheld error text. Supply a workflow, an error or an execution (not both error and execution), else 422 `invalid_repair_target`.

Workflow context masks secret-like values as `[redacted:secret]`, lists `redacted_paths`/`truncated_paths`, never includes credential IDs, pinned data or run data, caps strings and summarizes workflows over 48 KiB around failing/focus nodes. Error context contains status, failing node, error name, masked message/description, HTTP code and per-node messages; never items, stacks, context or causes. An execution larger than the n8n client's 4 MiB response cap, pruned or unreachable yields `detail: "unavailable"` with stored error metadata only.

Error text reaches a provider only in repair conversations, only when `send_error_text` is true and only while the administrator switch allows it (`detail: "withheld"` otherwise). Standard chats never receive it.

When a workflow is included, the context may add `webhook_directory`: up to 100 trigger routes of *other active* workflows on the same instance, as `{workflow_id, workflow_name, method, route}` with the route masked like other context strings. It never contains parameters, credentials or node details, and it is omitted when the workflow list cannot be read. It exists so the model can find the service a node should call instead of guessing (a live test showed a model pointing an HTTP Request at its own workflow's webhook). Webhook routes are shared with the provider in repair conversations; review them in "What the AI sees".

`GET /repair/settings` (viewer) returns `{error_text_enabled, webhook_directory_enabled}` (both default `true`). `PUT /repair/settings` (admin, CSRF) accepts either or both booleans as a partial update (at least one, else 422); both effective values are stored in `documents`, returned and audited as `assistant.repair_settings_updated`. With `webhook_directory_enabled` false the context never includes `webhook_directory`; a corrupt stored setting withholds both error text and the directory until an administrator saves again.

### Proposals

Preparing a proposal re-fetches the workflow and never sends POST or PUT:

- `base_content_hash` must equal the current `content_hash` (SHA-256 of name, nodes, connections and allow-listed settings; `active`, `versionId`, IDs, `updatedAt` and pinned data are excluded), else `409 context_stale`.
- Patch: operations apply to a detached copy; unknown nodes, no-op changes, credential edits, placeholders or truncated code give `422 invalid_patch`.
- Patch: a change that makes any non-trigger node address the workflow's own enabled trigger route (`/webhook/`, `/webhook-test/`, `/webhook-waiting/`, `/form/`, `/form-test/`, `/form-waiting/` plus its path or webhookId, on any host, including expressions and sub-paths) gives `422 self_call_blocked` before any approval exists. Restore previews that reintroduce such a call list risk `self_webhook_call`.
- Restore from `repair_id`: the encrypted pre-repair snapshot of the same instance and workflow. Restore from `version_id`: `GET /workflows/{id}/{versionId}` replaces nodes and connections while name and settings keep their current values (stated in `warning`). Missing, foreign, forbidden, unlicensed or pruned sources give `404 history_unavailable`.
- A result identical to the saved workflow gives `422 invalid_patch`; documents over 1 MiB give `422 workflow_too_large`.
- The server computes `preview` (diff summary, masked parameter/code/setting changes, risks, apply label). If the workflow is not inactive, a linked `deactivate_workflow` proposal is created and listed in `preview.preconditions` as `{code: "workflow_active", satisfied: false, tool: "deactivate_workflow", action_id}`.
- The approval binds instance ID, URL, encrypted key, workflow ID, tool and content hash. It excludes `active` and `versionId`, so the linked deactivation does not make it stale.

### Confirmation

`POST /tools/confirm` for a repair proposal (operator, rechecked live) claims it, then:

1. Re-fetches the workflow; binding mismatch → `409 approval_stale` (status `stale`, no write).
2. Requires `active: false`; otherwise `409 workflow_active`, nothing is sent and the claim is released back to `pending` so the linked deactivation can run first.
3. Rechecks expiry and role, stores the encrypted before-snapshot `assistant:repair:{action_id}` (the newest 256 are kept) and an audit intent with hashes only.
4. Sends one `PUT /workflows/{id}` with `{name, nodes, connections, settings}`, reads the workflow back and requires `active: false` and the desired content hash.
5. A timeout, cancellation after sending or readback mismatch returns `upstream_outcome_unknown` (status `uncertain`); a definite upstream rejection returns `upstream_rejected` (status `failed`). Nothing is retried.

Success returns `{id, status: "succeeded", result}` where `result` is the contract `RepairResult` (`repair_id` is the action ID, usable for undo). Audit actions: `assistant.workflow_repaired`, `assistant.workflow_restored`; a `fleet.changed` event is broadcast.

`GET /repair/{id}/reconcile` (same user and session) is read-only and returns `{id, status, state, active, version_id, content_hash}` with `state` `matches_desired`, `matches_before`, `different` or `not_found`. It never changes approval status.

### Repair error codes

`context_stale`, `approval_stale`, `workflow_active`, `upstream_outcome_unknown`, `target_mismatch`, `history_unavailable`, `repair_error_text_disabled`, `repair_webhook_directory_disabled` (409: continuing a turn that already sent the directory after an administrator turned it off), `invalid_patch`, `self_call_blocked` (422: the patch would make the workflow call its own trigger route), plus `turn_not_pending`, `invalid_repair_target`, `workflow_too_large`, `approval_required` (repair proposals cannot run through `/tools/run` execution) and `provider_changed`.

## MCP

Admins configure servers with `PUT /config/mcp`, body `{id,config:{url,api_key_ref?}}`; the URL is the full Streamable HTTP endpoint. MCP references currently use `$NAME` or `$NAME.field`. `GET /config/mcp` lists them; `GET /mcp/{server_id}/tools` discovers the catalog.

The assistant exposes admin-only `list_mcp_servers`, `list_mcp_tools` and `mcp_call` in standard chats (never in repair turns). The latter accepts `{server_id,tool_name,arguments}` and **always creates a proposal**, even if the server advertises `readOnlyHint`. Discovery results supplied to the model are bounded to 64 KiB and report an omitted count; the catalog API exposes the full supported bounded catalog. MCP invocation output is returned to the confirming user and is not automatically sent to a model.

The client implements the explicit stable [MCP 2025-11-25 Streamable HTTP contract](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports): initialization, initialized notification, session/version headers, JSON/SSE POST replies, catalog pagination and best-effort session cleanup. Unsupported negotiated versions, server-originated requests, invalid/oversized responses and schemas fail explicitly. It does not resume or replay streams. Bounds: 45-second operation deadline, 1 MiB responses, 128 tools/eight pages, 64 KiB per tool schema, 128 KiB requests. Approval argument validation rejects schema references and regex keywords rather than retrieving external schemas or executing untrusted regexes.

MCP approvals bind the exact arguments, server configuration, credential and tool definition. **Generic MCP does not provide an application resource fingerprint**, so the proposal explicitly warns that remote resource state cannot be verified. Permissions, expiry and credential/configuration binding are rechecked before invocation. An MCP `isError` result records failure; a transport failure after sending records uncertainty. No claim of distributed rollback is made.

## Verification boundary

Tests cover provider wire formats and normalization, flattened declarations, rate-limit/incomplete mapping, keyless Ollama, strict tool calls, secret redaction, approval replay/session/RBAC/staleness/expiry/cancellation, MCP handshakes/JSON/SSE and schema changes, and repair proposals, confirmation, restore, reconciliation, repair turns and the tool-calling check against a simulated n8n API and model. A loopback model-shaped HTTP server exercises the real egress transport. Live items remain: n8n 2.19 acceptance of the four-field PUT (settings outside the allow-list are neither hashed nor written), version-history response shape, readback hash equality after n8n normalization, and model-driven tool calls with hosted and other providers.

Later live results: repair was verified end to end on real n8n 2.19 with a hosted provider on 2026-09-16 (four-field PUT accepted, readback hash equal after normalization; see [live assistant verification](live-assistant-verification.md)). Local Ollama with `qwen2.5:3b` was verified on 2026-09-17 for keyless save, connection test, tool-calling check `supported`, chat and a model-initiated `get_skill` call. `tests/test_skills.py` covers frontmatter parsing, boot sync add/update/delete, both skill routes, unknown and path-like IDs, the tool result, the `Code` near-miss regression and name normalisation. The repair suite covers a skill-guided correction across Continue and the scoping guard. Live version-history restore remains unverified.
