# Encrypted backup export and offline restore

## Create an export

`POST /api/backups/export` requires an administrator session, permitted Origin and `X-AGD-CSRF`. Send JSON with `passphrase` (14–256 characters). The passphrase is used only for this export and is not saved in the vault, database or audit log. Keep it separately from the downloaded archive.

The response is an `application/octet-stream` attachment named `inference.ibackup`. Save the response bytes unchanged. A successful export contains an online SQLite backup snapshot, `.secret_key`, encrypted `secrets.json`, optional existing `config.json`/`users.json`, and all validated Markdown notes. Settings and assistant configuration stored in SQLite documents are included. Process environment variables, external n8n databases/volumes, models and host Docker resources are not part of this application-data archive.

Exports use a versioned envelope, random 16-byte salt, fixed Scrypt parameters (N=32768, r=8, p=1, 32-byte key) and authenticated Fernet encryption. Inner ZIP entries are stored without compression. Limits: 128 MiB combined plaintext, 1100 files and the existing per-note/scan limits. Encryption works in memory, with multiple bounded copies; deployment memory must accommodate more than the archive size. One export runs at a time; concurrent requests receive `409 backup_busy`.

The SQLite snapshot is consistent. Notes mutations are locked during snapshot/capture. Vault/config files are captured individually using regular-file checks; this does not promise one distributed instant across externally edited files and SQLite. The vault key is stable for the application lifetime. Private temporary SQLite snapshots are removed after processing, including settled cancellation; a host/process crash can leave `.backup-*` temporary directories requiring local inspection. Live administrator authorization is checked before capture and before releasing encrypted bytes. `backups.exported` audit records archive creation, not proof of download delivery.

Invalid input returns 422 without echoing passphrases. Storage, unsafe paths and size failures return redacted `503 backup_failed`. Revoked sessions/roles return 401/403. No automatic retry is performed.

## Restore offline

Stop Inference and choose an empty destination. From the project environment run:

```powershell
.venv\Scripts\python.exe -m backend.modules.backups.restore backup.ibackup D:\inference-restored
```

The command prompts for the export passphrase without echoing it. It does not accept a passphrase in command-line arguments. It authenticates the complete archive, rejects duplicate/colliding/traversal/device paths and nonregular entries, validates the vault key against its encrypted contents, checks SQLite integrity/references and validates the current application schema in private staging. Schema versions 5 through 7 are accepted. Restore migrates older snapshots in staging, validates them against the current schema version (v7), then installs the migrated database; newer unsupported schemas are rejected.

Validation failures do not populate the destination. Existing files are never overwritten. Files are installed with private permissions and fsync; supported POSIX directory fsync is also attempted. Installation is not one filesystem transaction: an I/O failure or crash may leave a partial destination. Do not start the application in a partial restore. Windows permission guarantees depend on local ACLs, and concurrent directory replacement by a local administrator is outside the filesystem trust boundary.

After success, set `INFERENCE_DATA_DIR` to the destination and configure required process environment settings before starting. The archive restores saved accounts, sessions and application configuration; it does not provision external services. Preserve your original deployment until the restored deployment is verified.

## Verification and remaining work

Tests exercise an export/restore/application-start round trip with encrypted fleet credentials, vault values and notes; wrong passphrases, tampering, unsafe ZIP entries, corrupt/incompatible databases, nonempty destinations, role revocation, cancellation, concurrency, size limits and redacted validation errors.

Schedules, persisted run history, local downloads/deletion and optional S3-compatible upload are implemented. The backup frontend is implemented in frontend/js/views/backups.js and POSIX filesystem verification passed in the locked Linux container (886 passed). Live S3 and real multi-instance disaster-recovery checks remain. Fixtures include an accelerated background loop, real verified loopback S3-shaped PUT and the AWS published signing vector; they do not establish live provider compatibility.


## Automatic backups and run history

All routes below require a live administrator session; mutations also require CSRF and a permitted Origin.

| Route | Operation |
|---|---|
| `GET /api/backups/schedules` | List configurations and next-run timestamps; never resolved secrets |
| `POST /api/backups/schedules` | Create a schedule |
| `PUT /api/backups/schedules/{id}` | Replace configuration, rebind its granting admin and reset the next run |
| `DELETE /api/backups/schedules/{id}` | Revoke scheduling; retain existing archives and run history |
| `POST /api/backups/schedules/{id}/run` | Run an enabled schedule now and return its recorded outcome |
| `GET /api/backups/runs?limit=50&before=ID` | Newest-first history, up to 100 rows, with `next_cursor` |
| `GET /api/backups/runs/{id}/download` | Download a local archive after verifying its size/hash |
| `POST /api/backups/runs/{id}/reconcile` | Verify an uncertain local file against its recorded hash; never retries S3 |
| `DELETE /api/backups/runs/{id}/file` | Delete the matching local file, retaining history and any remote object |

Example schedule body:

```json
{
  "name": "Daily recovery copy",
  "enabled": true,
  "interval_minutes": 1440,
  "passphrase_ref": "$BACKUP_PASSWORD",
  "s3": null
}
```

`passphrase_ref` is a whole environment/vault reference, optionally with compound fields. Store the password separately so it remains available after losing the original server. The resolved value must contain 14–256 characters. The schedule persists the reference inside encrypted configuration, not a copied plaintext password. A password separately stored in the vault is naturally included in that vault's encrypted backup. Missing/invalid references create a failed run with a safe diagnostic code.

Intervals range from 15 minutes to 365 days; this is interval scheduling, not calendar/cron scheduling. The lifespan worker checks every 30 seconds while Inference is running. It starts at most one overdue run per schedule instead of replaying every missed interval. Claims advance `next_run_at` (Unix seconds) inside the same transaction that creates a run. At most 100 schedules are supported. Manual exports and scheduled runs share one processing lock.

The granting admin must remain enabled with the admin role; logout alone does not revoke a schedule. Editing, disabling or deleting the schedule invalidates an in-flight grant before its next effect. Role, version and export-password changes are checked before local storage; authorization is checked again before upload. Effects already sent to external storage cannot be recalled by a subsequent revocation.

Local archives live under `INFERENCE_DATA_DIR/backups/{run_id}.ibackup`, with private files, atomic non-overwriting installation and recorded SHA-256/size. Storage is capped at 1 GiB and 1000 directory entries. Capacity rejection fails the new run; it does not silently delete old archives. Use explicit local deletion to free space. Hardlinks, symlinks, reparse points and unexpected nonregular entries fail closed. Interrupted temporary files may need administrator inspection after a crash.

Runs record creation/completion times, status, safe error code and remote outcome. `succeeded` means the local file was installed and any requested upload returned success; `partial` retains a local copy when remote completion failed or is unknown. `uncertain` requires checking an interrupted local effect; `failed` records a known failure and `deleted` marks local removal. Startup settles `running`, `storing`, `uploading` and `deleting` records without retrying those effects. Cancellation waits for bounded in-flight work and bookkeeping before releasing the lock. A crash between filesystem and database writes can still require reconciliation.

Archives include schedule/run metadata in SQLite, but do not recursively include previous backup files. Restored history may therefore reference files absent on the new host. Restored enabled schedules can run once the service starts and their secret references resolve; disable or replace schedules before running a duplicate deployment.

## Optional S3-compatible upload

Supply `s3` in the schedule body:

```json
{
  "endpoint": "https://storage.example.com",
  "bucket": "inference-backups",
  "region": "us-east-1",
  "prefix": "daily",
  "credentials_ref": "$BACKUP_S3"
}
```

The referenced compound secret contains `access_key_id`, `secret_access_key` and optional `session_token`. The endpoint is an HTTPS origin without path, credentials, query or fragment. Plain HTTP is accepted only for loopback fixtures, which still require an explicit egress CIDR allowlist. Internal HTTPS endpoints also use the application's egress policy.

Uploads use a single path-style PUT to `/{bucket}/{prefix}/{run_id}.ibackup`, signed using [AWS Signature Version 4](https://docs.aws.amazon.com/AmazonS3/latest/developerguide/sig-v4-header-based-auth.html). The shared verified HTTP transport enforces egress/TLS; request construction excludes ambient cookies, default auth and unrelated headers. The total request deadline is 30 seconds. Redirects and write retries are disabled; response bodies are not consumed or exposed. Timeouts and ambiguous server failures record `remote_status=unknown` while keeping the local file. S3 reconciliation/retry, remote deletion and multipart uploads are not exposed; inspect remote objects independently after an unknown outcome. No real cloud bucket was modified during development.
