# System settings and secret templates

These endpoints require an administrator session. Mutations require a permitted Origin and session CSRF token. The settings and secrets screens are still pending frontend work.

## Application settings

`GET /api/admin/settings` retains the existing deployment information (`secure_cookies`, effective `session_hours`, `allowed_origins`, `allow_cidrs`, modules) and adds:

- `default_preferences`: defaults used when a user has not saved preferences.
- `saved`: the complete editable policy, including its saved session length.
- `revision`: the current policy hash for stale-write protection.
- `environment_locked_fields`: `session_hours` when `INFERENCE_SESSION_HOURS` controls the deployment.

`PUT /api/admin/settings` replaces the editable policy. Read the current revision first:

```json
{
  "expected_revision": "<revision from GET>",
  "session_hours": 12,
  "default_preferences": {
    "theme": "dark",
    "page_size": 25,
    "error_notifications": true
  }
}
```

Session length is an integer from 1 to 168 hours; page size is 10–100, and theme is `dark`, `light` or `n8n`. Unknown fields and invalid types return 422. A stale revision returns `409 settings_changed`. This is full replacement: omitted preference fields receive their documented defaults.

The validated policy and audit event commit together in SQLite documents. Runtime publication happens after commit without an intervening await; cancellation waits for commit/publication to settle. Failure before commit leaves the existing policy intact. Invalid persisted policy fails startup rather than silently resetting it. Backup/restore includes the saved policy automatically.

Changes affect newly issued sessions. Existing sessions retain their expiry until logout, revocation or expiration. Default preferences do not overwrite users' saved preferences. The frontend consumes `/api/admin/preferences`; Settings and the account-bar theme picker save personal theme choices.

When `INFERENCE_SESSION_HOURS` is set at startup, it controls the effective session length. GET distinguishes the effective value from `saved.session_hours`; trying to change the saved session length while locked returns `409 setting_environment_locked`. Other editable defaults can still change while retaining the saved session value. Session policy must use the supported 1–168 hour range.

Cookie security, origins, egress allowlists, filesystem locations, Docker socket and bootstrap credentials remain deployment configuration. They cannot be changed through this policy endpoint. Telemetry retention, polling, assistant providers and backup schedules retain their own existing APIs.

## Credential templates

`GET /api/admin/secret-templates` returns safe field metadata under `data`:

| Template ID | Required fields | Optional fields |
|---|---|---|
| `api_key` | `api_key` | None |
| `basic_auth` | `username`, `password` | None |
| `s3` | `access_key_id`, `secret_access_key` | `session_token` |

`POST /api/admin/secret-templates/{template_id}/apply` validates and creates a compound encrypted vault secret:

```json
{
  "name": "BACKUP_S3",
  "values": {
    "access_key_id": "<access key>",
    "secret_access_key": "<secret key>"
  }
}
```

Success returns only `name` and `template_id`. Resolve compound values server-side with `$BACKUP_S3` or a field reference such as `$SERVICE.api_key`. Templates validate required/unknown fields, UTF-8 text and the existing 64 KiB secret limit; they do not contact a provider or prove credentials are valid. Blank optional session tokens are omitted. Nonblank values are preserved exactly.

Template application is create-only. An existing name returns `409 secret_exists`, including concurrent attempts; it is never overwritten. The existing explicit secret-edit API remains available for intentional updates. Values never appear in template metadata, responses or audit events. Permission checks happen inside the serialized write transaction, and cancellation settles the threaded vault write and audit before releasing it.

Vault files and SQLite audit records are not one transaction. If a file write succeeds but audit persistence fails, the response is `503 secret_outcome_unknown`; check the secret list before retrying. A repeated create safely refuses an existing name.

The current catalog is built-in. Generic compound secrets remain available through `/api/admin/secrets`; there is no custom template schema editor in this unit.
