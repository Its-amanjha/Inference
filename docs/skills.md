# Skill library

Inference ships a library of written n8n guides, called skills. The assistant reads them through the `get_skill` tool before it writes or debugs n8n code, and any signed-in user can read them through `/api/skills`.

## Why skills exist

Models recall n8n syntax unreliably: Code node return shapes, `$input` versus `$json`, expression delimiters and error-output wiring change between n8n versions and are easy to confuse. A model that reads a guide first makes fewer of those mistakes than one working from memory. Skills are reference text only. They add no permissions and no tools, and they never cause an action.

## Where skills live

Each skill is a folder under `backend/skills/`. The folder name is the skill ID. The loader reads one file per folder, `SKILL.md`: YAML-style frontmatter with `name` and `description`, followed by the markdown body.

The 15 folders are vendored, not written in this repository. They come from a supplied `built-in-n8n-skills` archive. Only each folder's `SKILL.md` ships: the archive's reference files (`EXAMPLES.md` and the like) were removed on 2026-09-24 because the loader never read them; they remain in git history and upstream. The upstream library is [czlonkowski/n8n-skills](https://github.com/czlonkowski/n8n-skills) (MIT). Its `LICENSE` and `NOTICES` files, which were not in the archive, are copied verbatim from the upstream repository as `backend/skills/LICENSE-n8n-skills.txt` and `backend/skills/NOTICES-n8n-skills.txt`. `backend/skills/README.md` lists the skills and the attribution. The loader ignores these root files because only folders are read.

Skills live beside the backend package, not in the data directory. They are application content, versioned with the code and copied into the container image with `backend/`.

## Installed skills

| ID | Use when |
|---|---|
| `n8n-agents` | Building AI Agent, LLM chain, Text Classifier or Information Extractor nodes: model, memory, tools, output parsers, RAG, human review. |
| `n8n-binary-and-data` | Working with files and binary data: `$binary` versus `$json`, uploads, PDFs, images, base64, files as agent tool input. |
| `n8n-code-javascript` | Writing JavaScript in Code nodes: `$input`/`$json`, `$helpers`, DateTime, execution modes, batching, `pairedItem`. |
| `n8n-code-python` | Writing Python in Code nodes: `_input`/`_json`, the standard library and Python's limits in n8n. |
| `n8n-code-tool` | Writing the AI-agent-callable Custom Code Tool (`toolCode`), whose input and return contract differs from the Code node. |
| `n8n-error-handling` | Making failures loud and recoverable: error outputs, retries, Error Trigger workflows, 4xx/5xx webhook responses. |
| `n8n-expression-syntax` | Writing and fixing `{{ }}` expressions, `$json`/`$node` access and data mapping between nodes. |
| `n8n-mcp-tools-expert` | Using n8n-mcp MCP tools: node search, configuration validation, templates, workflow and credential management. |
| `n8n-multi-instance` | Choosing and verifying the target instance when an n8n-mcp account reaches more than one n8n instance. |
| `n8n-node-configuration` | Configuring node parameters: required fields per operation, property dependencies, `displayOptions`. |
| `n8n-self-hosting` | Deploying and operating self-hosted n8n on a Linux VM with Docker Compose and Caddy, single or queue mode. |
| `n8n-subworkflows` | Building reusable sub-workflows: typed inputs, Execute Workflow, each versus all modes, naming. |
| `n8n-validation-expert` | Interpreting validation errors and warnings and separating false positives from real fixes. |
| `n8n-workflow-patterns` | Choosing a workflow architecture: webhook, API, database, AI, batch and scheduled patterns. |
| `using-n8n-mcp-skills` | Routing any n8n-mcp workflow task to the right specialist skill. |

The descriptions above are shortened from each `SKILL.md` frontmatter. `GET /api/skills` returns the full text.

## Boot sync

Every application start runs `backend/modules/skills/loader.py` after the database is open and before the application serves requests. It replaces the `skills` table's contents with what is on disk, in one transaction:

- A folder on disk with a usable `SKILL.md` is inserted, or updated if its ID already exists (`name`, `description`, `content` and `updated_at` are replaced).
- A row whose folder is no longer on disk is deleted.
- Disk is the source of truth. The table is a cache so the API and the tool read one place. Nothing writes skills through the API.

The loader skips an entry, without failing startup, when:

- the entry is not a directory, or is a symlink;
- the folder name does not match `^[a-z0-9][a-z0-9-]{0,63}$` (lowercase letters, digits and hyphens, starting with a letter or digit, at most 64 characters);
- `SKILL.md` is missing, is a symlink, is larger than 256 KiB, cannot be read or is not valid UTF-8;
- the body after the frontmatter is empty.

Only `SKILL.md` is read, and it is the only file a skill folder ships. Links inside a guide to its upstream reference files are not served.

Frontmatter is parsed by reading the two top-level keys `name` and `description`; there is no YAML dependency. A file without frontmatter still loads, with the folder name as its name and an empty description. The body is stored with the frontmatter removed.

## Schema

The `skills` table was added in database schema version 7:

| Column | Type |
|---|---|
| `id` | `TEXT PRIMARY KEY` |
| `name` | `TEXT NOT NULL` |
| `description` | `TEXT NOT NULL DEFAULT ''` |
| `content` | `TEXT NOT NULL` |
| `updated_at` | `TEXT DEFAULT CURRENT_TIMESTAMP` |

A version 6 database migrates to 7 automatically at startup. A restored backup archive from schema 5 or 6 is migrated the same way. The table holds only data derived from disk, so it needs no backup of its own: the next start rebuilds it.

## API

Both routes require a signed-in session with at least the `viewer` role. They are read-only, so no CSRF header is needed. The role is rechecked inside the database transaction.

### List skills

`GET /api/skills` returns every installed skill without bodies, ordered by ID:

```json
{
  "data": [
    {
      "id": "n8n-agents",
      "name": "n8n-agents",
      "description": "Design n8n AI agents the right way. Use when building or editing any @n8n/n8n-nodes-langchain.* AI node ...",
      "updated_at": "2026-09-17 00:42:10"
    }
  ]
}
```

### Read one skill

`GET /api/skills/{id}` returns one skill with its markdown body:

```json
{
  "id": "n8n-code-javascript",
  "name": "n8n-code-javascript",
  "description": "Write JavaScript code in n8n Code nodes. ...",
  "content": "# JavaScript Code Node\n\nExpert guidance for writing JavaScript code in n8n Code nodes.\n...",
  "updated_at": "2026-09-17 00:42:10"
}
```

Errors:

| Status | Code | Cause |
|---|---|---|
| 401 | `authentication_required` | No session. |
| 403 | `permission_denied` | The session's role is below `viewer`. |
| 404 | `skill_not_found` | The ID is well formed but not installed. |
| 422 | `validation_failed` | The ID does not match the skill ID pattern (for example uppercase or a space). |

The API matches IDs exactly. Name normalisation applies only to the assistant tool.

## The `get_skill` tool

`get_skill` is an assistant tool that returns one skill to the model.

**Arguments.** One field, `skill_name`: a string matching `^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$`. It must start with a letter or digit and cannot contain a slash, so path-like values such as `../../etc/passwd` or `/etc/passwd` are rejected as `invalid_tool_arguments`. No other fields are accepted.

**Normalisation.** The name is trimmed, case-folded, and spaces and underscores become hyphens before the lookup. `N8N-Code-JavaScript`, `n8n code javascript` and `n8n_code_javascript` all resolve to `n8n-code-javascript`. There is no fuzzy matching: `Code` or `n8n-code-js` are not installed IDs.

**Result.** A found skill:

```json
{
  "skill": "n8n-code-javascript",
  "name": "n8n-code-javascript",
  "description": "Write JavaScript code in n8n Code nodes. ...",
  "content": "# JavaScript Code Node\n...",
  "truncated": false
}
```

`content` is capped at 24 KiB (24,576 characters). When the body is longer, `truncated` is `true`, so the model does not treat a cut guide as complete. The largest shipped `SKILL.md` is 23,337 bytes including frontmatter, so no shipped skill is currently truncated. The cap exists because a turn allows eight tool calls, and eight uncapped guides could put about 180 KB into one provider request.

An unknown name is an answer, not an error:

```json
{
  "skill": "Code",
  "error": {
    "code": "skill_not_installed",
    "detail": "No installed skill has that name. Call get_skill again with one of the available ids."
  },
  "available": ["n8n-agents", "n8n-binary-and-data", "n8n-code-javascript", "..."]
}
```

`skill` is the name exactly as the model sent it, before normalisation. `error` has the same `code` and `detail` shape as any failed tool call (see [Assistant API](api-assistant.md#tool-errors-in-conversations)). `available` lists up to 50 installed IDs in ID order, so the model can call again with a correct one. The conversation continues. This case exists because a real `qwen2.5:3b` asked for `Code` and `n8n-code-js`, and before the fix a near miss ended the chat.

**Permissions and approvals.** `get_skill` is a viewer-level read. It never creates a proposal or approval record, and it is never a mutation. It is also available through `POST /api/assistant/tools/run` with `{"name": "get_skill", "arguments": {"skill_name": "..."}}`.

**Scopes.** It is declared in both standard and repair conversations. Repair is where n8n code guidance matters most.

**Why it is instance-free.** Every other built-in tool names an n8n instance, and repair conversations reject any call whose `instance_id` differs from the pinned target. Skills are local application content and reach no instance, so `get_skill` has no `instance_id` and is listed in `INSTANCE_FREE_TOOLS`, which skips that check. Before this exemption every `get_skill` call in a repair conversation was rejected as `target_mismatch`. Tools that can reach an instance still have to match the pinned instance and workflow.

In a repair conversation, `get_skill` counts as a read. The turn stops at `awaiting_continue` with the guide stored, and the guide reaches the model when the user presses Continue, as with other repair reads.

**System prompt.** The assistant's system prompt tells the model to call `get_skill` for the relevant skill before writing or debugging n8n JavaScript, Python, expressions, error handling or node configuration. It also states that skill text is reference material, not instructions from the user. Skill text never grants permission to act, and mutations still need the usual proposal and confirmation.

## Adding, refreshing and removing skills

Skills are part of the release. There is no upload, editing or custom-skill feature, and none is planned. To change the library:

1. Replace, add or delete folders under `backend/skills/`. Each folder needs a valid ID as its name and a `SKILL.md` with frontmatter and a non-empty body.
2. Restart the application. For a source deployment, restart the Uvicorn process. For a container deployment, rebuild the image (`docker compose up --build -d`). The container runs with a read-only root filesystem, and the skills are baked into the image, so editing files inside a running container is neither possible nor intended.
3. Confirm with `GET /api/skills`.

To refresh from a new archive, replace the whole `backend/skills/` folder. Rows for folders that are no longer present are deleted on the next start.

## Limits

- 256 KiB per `SKILL.md`. Larger files are skipped.
- 24 KiB of `content` per tool result, with `truncated` reported.
- Only `SKILL.md` is served. Guides that point to their own reference files (for example "see EXAMPLES.md") point to text the model cannot fetch.
- Several skills (`using-n8n-mcp-skills`, `n8n-mcp-tools-expert`, `n8n-multi-instance`) assume an n8n-mcp MCP server with tools such as `search_nodes`. Inference does not provide that server. An administrator can configure MCP servers, but MCP tools are available only in standard chats and always need approval.
- A standard chat has at most three provider rounds, and tools are declared on the first two. A model that guesses a wrong name spends one of those rounds on the `skill_not_installed` answer.
- Small local models may still choose near-miss names. The miss is reported to the model, which usually retries with a listed ID.
- A guide adds up to about 24 KB to the prompt. On CPU inference this makes skill-guided answers much slower: with `qwen2.5:3b`, a follow-up turn with the full 17 KB guide took 123 to 176 seconds. Local providers get a 600-second read timeout for this reason (see [Assistant API: Ollama setup](api-assistant.md#ollama-setup)).
- A guide can exceed a local model server's context window. With the full guide injected, Ollama truncated the prompt (`truncating input prompt limit=2050 prompt=5989/6144 keep=4`), keeping only the first few tokens and the end. The system prompt and the user's question were dropped, and the model summarised the guide instead of answering.

## Security notes

- Skill bodies are sent to the configured model provider when the model calls `get_skill`. They contain no instance data or secrets.
- The model chooses the skill name, and the name is only a lookup parameter. It never reaches a file path. The tool reads from the database, and the name pattern rejects slashes and leading dots.
- The API route pattern rejects malformed IDs before any lookup.
- The loader skips symlinked folders and files, so a link cannot pull outside content into the table.
- No bundled script or configuration asset is executed or served.
- The system prompt marks skill text as reference material that cannot authorise actions. Approval, role and binding checks apply regardless of what a guide says.
- Treat a replacement archive as code review input: its text enters model context and shapes proposals, even though it cannot bypass approvals.

## Troubleshooting

**A skill is missing after restart.** Check that the folder name is lowercase letters, digits and hyphens only; that `SKILL.md` exists, is UTF-8, is not a symlink and is under 256 KiB; and that its body is not empty. Skipped entries are not logged. For a container, confirm the image was rebuilt after the change; restarting the old image restores the old library.

**A removed skill still appears.** The table is rebuilt only at startup. Restart the application, or rebuild and restart the container.

**The model asks for the wrong name.** The model receives `skill_not_installed` with the installed IDs and normally retries. If it keeps guessing, name the skill ID in your message (for example "use n8n-code-javascript"), or use a model that passes the tool-calling check reliably.

**The model never calls `get_skill`.** Tools must be enabled on the provider connection (`tools_enabled: true`), and the model must support tool calls; run the tool-calling check. Models may still answer from memory; the prompt instructs them to consult skills but cannot force a call.

**The model summarises the guide instead of answering (Ollama).** The prompt was probably truncated to fit Ollama's context window. Look for `truncating input prompt` in the Ollama server log. Inference uses Ollama's OpenAI-compatible `/v1` endpoint, which does not accept `num_ctx` per request, so raise the context on the Ollama side: set `OLLAMA_CONTEXT_LENGTH=8192` in the environment of the Ollama process and restart Ollama, or build a model from a Modelfile with a larger `num_ctx`. Verified on 2026-09-17 with `OLLAMA_CONTEXT_LENGTH=8192` on `qwen2.5:3b` (CPU): the same Code node question produced a 6,440-token prompt with no truncation (`truncated = 0` in the Ollama log), the model called `get_skill`, and the answer addressed the error with `const items = $input.all();`. The chat returned in 295 seconds (33 s for the tool call, 4 min 22 s for the guided answer). `ollama ps` shows the active context in its CONTEXT column.

**Checking whether the model called `get_skill`.** Standard chat history (`GET /api/assistant/chats/{id}`) stores only user messages and final answers, so tool calls do not appear there. With Ollama, the server log shows each provider call, including the follow-up that carries the guide, as a separate request.

**A repair conversation stops after `get_skill`.** This is expected. Skill reads in repair turns end the step at `awaiting_continue`; press Continue to send the guide to the model.

**`invalid_tool_arguments` for `get_skill`.** The name contained a slash, started with a non-alphanumeric character, was empty or was longer than 64 characters.
