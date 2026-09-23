# Credentials and workflow promotion

These APIs require a signed-in session. Mutations require the session CSRF header. All IDs identify a particular saved instance; names never establish identity.

## Credentials

Every `/api/n8n_credentials` route requires admin:

| Method and path | Input / behavior |
|---|---|
| `GET /` | `instance_id`; bounded, paginated metadata discovery; returns `supported`, `data`, `diagnostics` |
| `GET /schema/{type}` | `instance_id`; reads and checks the credential schema; external schema references rejected |
| `GET /mappings` | `source_instance_id`, `target_instance_id`; saved explicit mappings |
| `PUT /mappings` | JSON below; verifies IDs/types when discovery is supported |
| `DELETE /mappings/{key}` | Removes only the saved mapping |

```json
{
  "source_instance_id": "source-instance",
  "target_instance_id": "target-instance",
  "source_credential_id": "source-credential",
  "target_credential_id": "target-credential",
  "credential_type": "httpHeaderAuth",
  "target_name": "Production service"
}
```

Discovery returns only ID, name and type. Forbidden/unsupported discovery returns an explicit diagnostic and accepts administrator-supplied mappings. Authentication, malformed responses, transport errors and incomplete pagination fail instead of pretending there are no credentials. No secret export endpoint is used.

## Preview

`POST /api/n8n_promote/preflight`:

```json
{
  "source_instance_id": "source-instance",
  "target_instance_id": "target-instance",
  "workflow_id": "source-workflow",
  "credential_map": {"source-credential": "target-credential"},
  "secret_map": {},
  "subworkflow_map": {"source-child-workflow": "target-child-workflow"}
}
```

Operator can promote with administrator-established credential mappings. New explicit credential mappings and secret provisioning require admin. To provision an unmapped credential, put its source ID in `secret_map` with a vault/environment reference such as `$SERVICE`; it must resolve to a JSON object matching the target credential schema. Do not provide both a credential ID mapping and a secret reference for the same source ID.

Preflight is read-only against n8n. It returns a 15-minute `preview_id`, source/target hashes, `can_deploy`, mapped/missing/collision diagnostics and a structural diff. The diff identifies added/removed nodes and changed fields without returning parameter values. Source/target workflow snapshots are encrypted locally. Secrets are schema-validated server-side and only their hashes are bound to the preview.

Credential IDs change only inside node credential blocks. Explicit subworkflow mappings change supported workflow-reference fields. Dynamic/unsupported references block deployment. Arbitrary matching strings in code or parameters are preserved. Source workflow metadata, pin data and static data are excluded from the target write.

## Deploy and inspect

`POST /api/n8n_promote/deploy`:

```json
{"preview_id":"32-character-preview-id","idempotency_key":"a-unique-key-for-this-attempt"}
```

The preview belongs to its creator and may be consumed once. Repeating the same actor/key/preview returns the existing run without repeating remote effects. A key reused for another preview, or a second key for an already-consumed preview, is rejected.

Deployment re-fetches source, mapped target, target subworkflows, connection settings, mappings and secrets. Changes reject the stale preview. Target updates require an inactive workflow. New workflows are created inactive; successful writes are read back before recording deployment and mappings. Every remote effect has a persisted stage before it is sent; terminal outcomes are audited. Partial results retain known created credential IDs.

| Route | Behavior |
|---|---|
| `GET /api/n8n_promote/runs?limit=50` | Current user's runs, at most 100 |
| `GET /api/n8n_promote/runs/{id}` | Safe status, created time, source instance and workflow, target ID, residual credentials, error code, recovery actions |
| `GET /api/n8n_promote/runs/{id}/previous` | Creator's previous target snapshot, when an existing workflow was updated |
| `POST /api/n8n_promote/runs/{id}/activate` | Separate activation after target snapshot and credential binding checks |
| `POST /api/n8n_promote/runs/{id}/restore` | Restores previous writable workflow JSON only if the deployed target is unchanged and inactive |

Credential-bearing activation requires administrator credential discovery access to verify current bindings. Missing discovery never silently counts as verification. Basic fleet activation remains a separate operator control.

## Uncertain and partial outcomes

`uncertain` means n8n may have accepted a write. Do not retry it with another preview merely to clear the status. Interrupted in-flight runs become uncertain at application startup; startup sends no remote writes.

`POST /api/n8n_promote/runs/{id}/reconcile` reads actual state and updates local records only. Supply `{}` for known target IDs. For a create whose response was lost, identify the remote workflow explicitly:

```json
{"target_workflow_id":"the-created-workflow-id"}
```

The selected workflow must match the expected payload and activation state. A mismatched workflow or a competing changed mapping blocks reconciliation. An interrupted activation or restore is checked against that action's expected state.

For uncertain credential provisioning, an administrator can supply `{"credential_map":{"pending-source-id":"verified-target-id"}}`; discovery verifies the ID/type and records it among residual credentials. Then create a new preview with explicit mappings for retained credentials. Reconciliation does not retry provisioning, deploy a workflow, or delete residual resources. If discovery is unsupported, inspect n8n directly and use the explicit mapping API to establish identities before making a new preview.

Restore does not delete newly created workflows/credentials or undo external executions. Workflow snapshots can contain business data; the previous-version endpoint is restricted to the run's creator.

## Verification boundary

The implementation follows the published [n8n 2.19.0 credential API](https://github.com/n8n-io/n8n/blob/n8n%402.19.0/packages/cli/src/public-api/v1/handlers/credentials/spec/paths/credentials.yml) and [workflow update API](https://github.com/n8n-io/n8n/blob/n8n%402.19.0/packages/cli/src/public-api/v1/handlers/workflows/spec/paths/workflows.id.yml). n8n updates can republish an already-active workflow, which is why this module checks inactivity and separates activation.

Tests use local HTTP fixtures and deterministic failures. Live n8n compatibility remains unverified. Inference serializes its own instance changes and operations; the n8n API does not provide an atomic compare-and-swap for these writes. An external n8n editor can race the final read and write. Coordinate external editing during promotion; post-write checks detect divergence but cannot undo execution effects.
