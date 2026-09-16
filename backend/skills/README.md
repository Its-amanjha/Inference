# n8n skill library

This folder holds the n8n guides (skills) that Inference ships. Each subfolder is one skill, and the folder name is the skill ID. The assistant reads a skill with the `get_skill` tool before it writes or debugs n8n code, and signed-in users can read them through `GET /api/skills`.

Full reference: [Skill library](../../docs/skills.md).

## How Inference uses this folder

- Skills are read-only application content. There is no upload, editing or custom-skill feature.
- At every start, the loader (`backend/modules/skills/loader.py`) syncs this folder into the `skills` table in one transaction. Disk is the source of truth: new and changed skills are written, and rows whose folder is gone are deleted.
- Only `SKILL.md` in each folder is read. Reference files beside it and the files under `n8n-self-hosting/assets/` are never loaded, served or executed.
- Files at the top level of this folder, including this README and the license files, are ignored, because the loader reads only folders.

To add, refresh or remove a skill, replace, add or delete folders here and restart the application. For a container deployment, rebuild the image and restart, because the skills are baked into the image and its root filesystem is read-only. Check the result with `GET /api/skills`.

Some guides were written for an n8n-mcp MCP server and name tools such as `search_nodes`. Inference does not provide that server.

## The skills

| Skill | Use when |
|---|---|
| [using-n8n-mcp-skills](using-n8n-mcp-skills/SKILL.md) | Routing any n8n-mcp workflow task to the right specialist skill. |
| [n8n-mcp-tools-expert](n8n-mcp-tools-expert/SKILL.md) | Using the n8n-mcp tools: searching nodes, validating configs, templates, managing workflows and credentials, auditing an instance. |
| [n8n-workflow-patterns](n8n-workflow-patterns/SKILL.md) | Designing workflow structure: webhook, API, database, AI, batch and scheduled architectures. |
| [n8n-node-configuration](n8n-node-configuration/SKILL.md) | Configuring nodes: property dependencies, required fields, operation-aware patterns by node type. |
| [n8n-validation-expert](n8n-validation-expert/SKILL.md) | Interpreting validation errors and warnings, telling false positives from real fixes, the validation loop. |
| [n8n-expression-syntax](n8n-expression-syntax/SKILL.md) | Writing and fixing `{{ }}` expressions, `$json`/`$node` access, mapping data between nodes. |
| [n8n-code-javascript](n8n-code-javascript/SKILL.md) | Writing JavaScript in Code nodes: `$input`/`$json`, `$helpers`, DateTime, batching, per-item versus all-items. |
| [n8n-code-python](n8n-code-python/SKILL.md) | Writing Python in Code nodes: `_input`/`_json`, the standard library and Python's limits in n8n. |
| [n8n-code-tool](n8n-code-tool/SKILL.md) | The AI-agent-callable Custom Code Tool (`@n8n/n8n-nodes-langchain.toolCode`), whose contract differs from the Code node. |
| [n8n-error-handling](n8n-error-handling/SKILL.md) | Making failures loud, structured and recoverable: error workflows, node error outputs, API and webhook response shapes. |
| [n8n-agents](n8n-agents/SKILL.md) | Building AI nodes (`@n8n/n8n-nodes-langchain.*`): agents, tools, memory, RAG, structured output, sub-workflow as tool. |
| [n8n-binary-and-data](n8n-binary-and-data/SKILL.md) | Files and binary data: images, PDFs, uploads and downloads, base64, multimodal input, files as tool input. |
| [n8n-subworkflows](n8n-subworkflows/SKILL.md) | Reusable sub-workflows: extracting shared logic, Execute Workflow, typed inputs. |
| [n8n-multi-instance](n8n-multi-instance/SKILL.md) | Choosing the target instance when n8n-mcp reaches more than one n8n instance. |
| [n8n-self-hosting](n8n-self-hosting/SKILL.md) | Deploying self-hosted n8n on a Linux VM: Docker Compose behind Caddy, single versus queue mode, day-2 operations. |

## Attribution

The skills are vendored from [czlonkowski/n8n-skills](https://github.com/czlonkowski/n8n-skills), licensed under the MIT License. The upstream `LICENSE` and `NOTICES` files are copied verbatim as [LICENSE-n8n-skills.txt](LICENSE-n8n-skills.txt) and [NOTICES-n8n-skills.txt](NOTICES-n8n-skills.txt).

The upstream NOTICES file lists hook scripts adapted from [n8n-io/skills](https://github.com/n8n-io/skills) under the Apache License 2.0, and refers to an Apache license copy in `NOTICES-APACHE-2.0.txt`. Those hook scripts are not part of this folder, and that license copy is not included here.
