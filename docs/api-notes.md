# Notes vault API

Notes are ordinary UTF-8 Markdown files beneath `INFERENCE_DATA_DIR/notes`. Existing compatible `.md` files appear in scans; no database import or search index is required. The vault is shared: viewers read, operators/admins save and delete. All routes require a session, and browser mutations require the usual CSRF header and Origin check.

| Route | Input / response |
|---|---|
| `GET /api/notes` | `{data:[{path,revision}]}`, sorted by path |
| `GET /api/notes/note?path=Operations.md` | `{path,content,revision}` |
| `PUT /api/notes/note` | JSON `{path,content,expected_revision?}`; returns the saved note |
| `DELETE /api/notes/note?path=Operations.md&expected_revision=...` | `{path,deleted:true}` |
| `GET /api/notes/search?q=...&case_sensitive=false` | Matching paths/revisions and text excerpts |
| `GET /api/notes/backlinks?path=Operations.md` | Unique referring note paths/revisions |
| `GET /api/notes/render?path=Operations.md` | `{path,revision,html}` with escaped text in a `<pre>` |

## Save and edit

Create a note:

```json
{"path":"Runbooks/Deploy.md","content":"# Deployment\nSee [[Operations]].","expected_revision":null}
```

An omitted/null revision means **create only**, not overwrite. File creation installs the completed temporary file atomically without replacing an existing destination. To edit, read the note and submit its returned SHA256 `revision` as `expected_revision`; a changed or missing file returns `409 note_conflict`. Deletion also requires the current revision.

Saves write a private exclusive temporary file in the same directory, flush/fsync it, check the expected revision and install it atomically. Temporary files are cleaned up. POSIX operations also fsync the parent directory when supported. Existing-note replacement is atomic, but external editors can still race the final revision check because ordinary filesystems do not expose a content-based compare-and-swap. Reload after a conflict.

Requests serialize through the application notes lock. Authorization is checked inside the database transaction immediately before the bounded local mutation, so queued requests cannot retain revoked permissions. Cancellation waits for file/audit completion before releasing the lock. File contents are omitted from audit records and WebSocket notifications.

The filesystem and SQLite are separate stores. If an I/O result cannot be confirmed or audit persistence fails after the file changes, the API returns `503 notes_outcome_unknown`; it does not claim rollback. Reload the note before retrying.

## Paths and bounds

Use canonical relative paths with forward slashes and a lowercase `.md` suffix. Nested directories are created as needed. Absolute paths, `.`/`..`, backslashes, percent-encoded path forms, drive/stream syntax, Windows device names, trailing dots/spaces and control characters are rejected. Paths have at most 32 components and 512 characters; individual components must fit 255 UTF-8 bytes.

The vault rejects symlinks, Windows reparse points, hard-linked notes and non-regular note files. POSIX operations open ancestors through no-follow directory descriptors. Windows checks existing entries for reparse points; a host administrator concurrently replacing directories remains outside the application's protection boundary.

Limits are explicit: 256 KiB per note, 1,000 Markdown files, 32 MiB of note contents and 10,000 filesystem entries per scan. Backlinks examine at most 100,000 references per request. Exceeding a bound returns an error rather than silently hiding notes. Invalid UTF-8 and unsafe entries produce storage errors; scans do not follow them elsewhere.

## Search, links and preview

Search matches literal text in paths or contents. Characters such as `.*`, `%` and `_` have no wildcard meaning. Matching is case-insensitive by default; `case_sensitive=true` selects exact case. Results include a bounded text excerpt, not rendered HTML.

Backlinks recognize textual `[[Note]]`, `[[folder/Note]]`, `[[Note|label]]` and `[[Note#heading]]` references. Resolution checks the source note's directory, then the vault root, then an unambiguous basename. Ambiguous basenames are not guessed. Self-links are omitted and repeated references produce one backlink. Windows uses case-insensitive filename matching. This is textual wikilink scanning, not a full Markdown parser.

The preview endpoint preserves the text and escapes HTML; it does not execute tags, load embedded resources or turn user URLs into active links. It currently returns a plain text preview, not a styled Markdown renderer. The future frontend should also insert excerpts and filenames as text.

## Shared service and verification

`app.state.notes` is the lifespan-owned `NotesStore`; synchronous filesystem work runs in a worker thread. `app.state.notes_lock` serializes API operations. Future backups that coordinate notes and the database must acquire the notes lock before taking the database snapshot, matching the mutation lock order.

Tests cover CRUD, replay/stale/concurrent saves, create-only races, search, ambiguous backlinks, HTML escaping, role/CSRF checks, revocation during reads, cancellation, audit failure, path traversal, device names, hard links, reparse points, invalid UTF-8 and I/O failure cleanup. Actual symlink creation and POSIX directory-fsync checks are skipped on this Windows environment, but verified in the locked Linux test image (886 passed, 0 skipped). The notes frontend is implemented and verified in frontend/js/views/notes.js.

### Browser Markdown preview

The Notes view formats headings, bold/italic, inline and fenced code, flat lists, quotes, horizontal rules and explicit HTTP/HTTPS/mailto links. It is a small Markdown dialect; unsupported syntax stays text. Raw HTML and image syntax are inert, and no remote images load. Large/dense documents use a plain-text fallback. The server preview endpoint continues returning escaped plaintext for compatibility.
