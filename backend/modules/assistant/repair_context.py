"""Masked workflow and scrubbed error context for AI repair (contract v1).

Pure functions: no requests, network or database. Everything built here may reach a model
provider as untrusted remote text, so secret values are masked with ``[redacted:secret]``,
run data, stacks and credential identities are dropped, and sizes are capped.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any, get_args

from backend.modules.n8n_promote.router import writable, workflow_diff
from backend.modules.n8n_promote.transform import fingerprint
from .repair_contract import CONTRACT_VERSION, REDACTED_MARKER, UNTRUSTED, SettingKey

WRITABLE_KEYS = ('name', 'nodes', 'connections', 'settings')
# Contract-editable settings plus the other n8n public API settings a PUT must carry back unchanged.
WRITABLE_SETTINGS = frozenset(get_args(SettingKey)) | {'callerPolicy', 'callerIds', 'timeSavedPerExecution', 'availableInMCP'}
MASK = '[redacted:secret]'
TRUNCATED = '…[truncated]'
CODE_KEYS = ('jsCode', 'pythonCode', 'functionCode')
MAX_STRING_BYTES = 4 * 1024
MAX_CODE_CONTEXT_BYTES = 16 * 1024
MAX_MESSAGE_BYTES = 1024
MAX_DESCRIPTION_BYTES = 2 * 1024
MAX_NODE_ERRORS = 20
MAX_RUN_ENTRIES = 2000  # the run data bound observability.execution_summary enforces

# Tokenized like observability.ingest._sensitive, narrowed to secrets so URLs, bodies and messages stay readable.
_SECRET_PARTS = frozenset({'secret', 'secrets', 'password', 'passwords', 'passwd', 'passphrase', 'pwd', 'authorization',
                           'auth', 'authentication', 'cookie', 'cookies', 'token', 'credential', 'credentials',
                           'apikey', 'signature', 'bearer', 'jwt'})
_SECRET_COMPOUNDS = ('api_key', 'access_key', 'secret_key', 'private_key', 'client_secret', 'subscription_key',
                     'master_key', 'license_key', 'signing_key', 'encryption_key')
# Selector and limit keys (genericAuthType, tokenUrl, passwordField, sendHeaders, maxTokens) are configuration, not secrets.
_CONFIG_LAST = frozenset({'type', 'types', 'mode', 'method', 'methods', 'location', 'url', 'uri', 'endpoint', 'field',
                          'fields', 'property', 'name', 'names', 'header', 'expires', 'expiry', 'expiration', 'format',
                          'enabled', 'required', 'scheme', 'ttl', 'length', 'count', 'placement', 'limit'})
_CONFIG_FIRST = frozenset({'send', 'use', 'allow', 'specify', 'include', 'max', 'min', 'num'})

# Linear-time patterns: variable-length runs are anchored at their start or bounded (remote text is untrusted).
_JWT = re.compile(r'(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*')
_USERINFO = re.compile(r'(?i)\b([a-z][a-z0-9+.-]{1,15}://)([^\s/?#@\'"<>{}]{1,256})@')
_QUERY = re.compile(r'(?i)([?&][A-Za-z0-9_.-]{0,48}(?:token|key|secret|password|passwd|pwd|signature|sig)=)(?!\{\{)([^&#?\s\'"<>]+)')
_SCHEME = re.compile(r'(?i)\b(bearer|basic|token|digest)(\s+)([A-Za-z0-9._~+/=-]{8,})')
_ASSIGNMENT = re.compile(r'(?i)(?<![A-Za-z0-9_-])(["\']?[A-Za-z0-9_-]{0,48}(?:password|passwd|secret|token|api[_-]?key'
                         r'|access[_-]?key|private[_-]?key|authorization)["\']?\s*[:=]\s*)(["\'])([^"\'\n]{1,512})(\2)')
_RUN = re.compile(r'(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{32,}={0,2}')
_UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
_HEX = re.compile(r'[0-9a-fA-F]{32,}')
_EXPRESSION = re.compile(r'=\s*(?:\{\{(?:(?!\}\})[^\'"`])*\}\}\s*)+', re.S)
_SCHEME_ONLY = re.compile(r'(?i)\s*(?:bearer|basic|token|digest)?\s*')


def canonical_bytes(value: Any) -> bytes:
    """Canonical JSON used for hashes and byte counts."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def writable_document(doc: dict) -> dict:
    """A detached copy of the fields a workflow PUT carries, aligned with promotion's ``writable``."""
    result = deepcopy(writable(doc if isinstance(doc, dict) else {}))
    settings = result['settings']
    result['settings'] = {key: value for key, value in settings.items() if key in WRITABLE_SETTINGS} if isinstance(settings, dict) else {}
    return result


def content_hash(doc: dict) -> str:
    """Hash of the writable document only: ``active``, ``versionId``, ids, pinData and metadata never count."""
    return fingerprint(writable_document(doc))


def sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', key).lower()
    parts = [part for part in re.split(r'[^a-z0-9]+', normalized) if part]
    if not parts or normalized == 'authentication' or parts[-1] in _CONFIG_LAST or parts[0] in _CONFIG_FIRST:
        return False
    joined = '_'.join(parts)
    return bool(set(parts) & _SECRET_PARTS) or any(word in joined for word in _SECRET_COMPOUNDS)


def _tokenish(value: str) -> bool:
    if len(value) >= 24:
        return True
    if any(char.isdigit() or char in '._~+/=-' for char in value):
        return True
    return any(char.isupper() for char in value) and any(char.islower() for char in value) and not re.fullmatch(r'[A-Z][a-z]+', value)


def _random_run(run: str) -> bool:
    core = run.rstrip('=')
    if len(core) < 32 or _UUID.fullmatch(core):
        return False
    if _HEX.fullmatch(core):
        return True
    if '/' in core and any(re.fullmatch(r'[a-z]{3,}', part) for part in core.split('/')):
        return False  # a URL path, not base64
    strong = sum(char.isdigit() or char.isupper() for char in core)
    return any(char.islower() for char in core) and any(char.isdigit() for char in core) and strong / len(core) >= 0.25


def mask_text(text: str) -> tuple[str, bool]:
    """Replace recognizable secret values inside free text. Returns (text, changed)."""
    original = text
    text = _JWT.sub(MASK, text)
    text = _USERINFO.sub(lambda m: m.group(0) if m.group(2) == MASK else m.group(1) + MASK + '@', text)
    text = _QUERY.sub(lambda m: m.group(0) if m.group(2).startswith(REDACTED_MARKER) else m.group(1) + MASK, text)
    text = _SCHEME.sub(lambda m: m.group(1) + m.group(2) + MASK if _tokenish(m.group(3)) else m.group(0), text)
    text = _ASSIGNMENT.sub(lambda m: m.group(0) if '{{' in m.group(3) or m.group(3).startswith('$') or REDACTED_MARKER in m.group(3)
                           or _SCHEME_ONLY.fullmatch(m.group(3)) else m.group(1) + m.group(2) + MASK + m.group(4), text)
    text = _RUN.sub(lambda m: MASK if _random_run(m.group(0)) else m.group(0), text)
    return text, text != original


def cap_text(text: str, limit: int) -> tuple[str, bool]:
    """Cut to ``limit`` UTF-8 bytes including the truncation marker. Returns (text, truncated)."""
    data = text.encode('utf-8')
    if len(data) <= limit:
        return text, False
    return data[:max(0, limit - len(TRUNCATED.encode('utf-8')))].decode('utf-8', 'ignore') + TRUNCATED, True


def _join(path: str, key: str) -> str:
    return f'{path}.{key}' if path else key


def mask_value(value: Any, path: str, redacted: list[str], truncated: list[str] | None = None, *,
               force: bool = False, limit: int | None = MAX_STRING_BYTES, limits: dict[str, int] | None = None) -> Any:
    """Masked copy of a JSON tree. Paths use ParamPath syntax below ``path``; ``force`` masks every scalar.

    ``limit`` caps strings in UTF-8 bytes (None: uncapped); ``limits`` overrides it for direct children by key.
    """
    if isinstance(value, dict):
        name = next((value[k] for k in ('name', 'key') if isinstance(value.get(k), str)), None)
        pair = 'value' in value and name is not None and (sensitive_key(name) or name.strip().lower() == 'key')
        result = {}
        for key, child in value.items():
            shown, changed = mask_text(key) if isinstance(key, str) else (key, False)
            child_path = _join(path, str(shown))
            if changed:
                redacted.append(child_path)
            result[shown] = mask_value(child, child_path, redacted, truncated, limit=(limits or {}).get(key, limit),
                                       force=force or sensitive_key(key) or (pair and key == 'value'))
        return result
    if isinstance(value, list):
        return [mask_value(child, f'{path}[{index}]', redacted, truncated, force=force, limit=limit) for index, child in enumerate(value)]
    if isinstance(value, str):
        if force and value and not _EXPRESSION.fullmatch(value):
            redacted.append(path)
            return MASK
        value, changed = mask_text(value)
        if changed:
            redacted.append(path)
        if limit is not None:
            value, cut = cap_text(value, limit)
            if cut and truncated is not None:
                truncated.append(path)
        return value
    if force and isinstance(value, (int, float)) and not isinstance(value, bool):
        redacted.append(path)
        return MASK
    return value


def _node_view(node: dict, redacted: list[str], truncated: list[str]) -> dict:
    name = node['name']
    parameters = node.get('parameters') if isinstance(node.get('parameters'), dict) else {}
    shown = mask_value(parameters, f'{name}.parameters', redacted, truncated,
                       limits=dict.fromkeys(CODE_KEYS, MAX_CODE_CONTEXT_BYTES))
    version = node.get('typeVersion')
    credentials = node.get('credentials')
    return {'name': name, 'type': node['type'],
            'type_version': version if isinstance(version, (int, float)) and not isinstance(version, bool) else None,
            'disabled': node.get('disabled') is True, 'parameters': shown,
            'credential_types': sorted(key for key in credentials if isinstance(key, str)) if isinstance(credentials, dict) else []}


def iter_edges(connections: Any):
    """Yield (source, type, output_index, target, input_index) from n8n's connection structure."""
    if not isinstance(connections, dict):
        return
    for source, channels in connections.items():
        for kind, outputs in (channels.items() if isinstance(channels, dict) else ()):
            for output, edges in enumerate(outputs if isinstance(outputs, list) else ()):
                for edge in (edges if isinstance(edges, list) else ()):
                    if isinstance(edge, dict) and isinstance(edge.get('node'), str):
                        yield source, kind, output, edge['node'], edge.get('index', 0)


def _nodes(document: dict) -> list[dict]:
    nodes = document.get('nodes')
    return [node for node in nodes if isinstance(node, dict) and isinstance(node.get('name'), str)] if isinstance(nodes, list) else []


def mask_workflow(doc: dict, *, focus_nodes: list[str] | tuple = (), failing_node: str | None = None,
                  max_bytes: int = 48 * 1024) -> dict:
    """A ``WorkflowContext`` dict: masked, capped, and summarized around the failing/focus nodes when large."""
    document = doc if isinstance(doc, dict) else {}
    base = writable_document(document)
    views = []
    for node in _nodes(base):
        node = {**node, 'type': node['type'] if isinstance(node.get('type'), str) else ''}
        redacted, truncated = [], []
        views.append((node, _node_view(node, redacted, truncated), redacted, truncated))
    settings_redacted: list[str] = []
    settings = mask_value(base['settings'], 'settings', settings_redacted, [], limit=MAX_STRING_BYTES)
    connections = base['connections'] if isinstance(base['connections'], dict) else {}
    digest = content_hash(document)

    def build(keep: set[str] | None) -> dict:
        nodes, redacted, truncated, summarized = [], list(settings_redacted), [], []
        for node, view, node_redacted, node_truncated in views:
            if keep is None or node['name'] in keep:
                nodes.append(view)
                redacted += node_redacted
                truncated += node_truncated
            else:
                nodes.append({'name': node['name'], 'type': node['type']})
                summarized.append(node['name'])
                # The model has not seen this code at all, so set_code must be refused like truncated code.
                parameters = node.get('parameters') if isinstance(node.get('parameters'), dict) else {}
                truncated += [f"{node['name']}.parameters.{key}" for key in CODE_KEYS if isinstance(parameters.get(key), str)]
        return {'id': str(document['id']) if isinstance(document.get('id'), (str, int)) else '',
                'name': base['name'] if isinstance(base['name'], str) else '', 'active': document.get('active') is True,
                'version_id': document['versionId'] if isinstance(document.get('versionId'), str) else None,
                'content_hash': digest, 'settings': settings, 'connections': connections, 'nodes': nodes,
                'redacted_paths': redacted, 'truncated_paths': truncated, 'summarized_nodes': summarized}

    result = build(None)
    if len(canonical_bytes(result)) <= max_bytes:
        return result
    focus = {name for name in (*focus_nodes, failing_node) if isinstance(name, str)}
    keep = set(focus)
    for source, _, _, target, _ in iter_edges(connections):
        if source in focus:
            keep.add(target)
        if target in focus:
            keep.add(source)
    return build(keep)


def _usable(execution: Any) -> bool:
    data = execution.get('data') if isinstance(execution, dict) else None
    result = data.get('resultData') if isinstance(data, dict) else None
    runs = result.get('runData', {}) if isinstance(result, dict) else None
    return (isinstance(runs, dict) and len(runs) <= MAX_RUN_ENTRIES
            and all(isinstance(entries, list) and len(entries) <= MAX_RUN_ENTRIES for entries in runs.values()))


def _http_code(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        return str(value)
    return value if isinstance(value, str) and re.fullmatch(r'[1-5]\d\d', value) else None


def _saved_vs_failed(workflow_data: Any, saved: Any) -> dict:
    if not isinstance(workflow_data, dict) or not isinstance(saved, dict):
        return {}
    try:
        if content_hash(workflow_data) == content_hash(saved):
            return {'changed': False}
        run, current = ({**writable_document(item), 'nodes': _nodes(item)} for item in (workflow_data, saved))
        diff = workflow_diff(run, current)
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        return {}
    names = set(diff['nodes_added']) | set(diff['nodes_removed']) | {entry['name'] for entry in diff['nodes_changed']}
    return {'changed': True, 'nodes_changed': sorted(names)}


def _scrub_error(execution: dict | None, error_row: dict | None, saved_workflow: dict | None,
                 send_error_text: bool) -> tuple[dict, int]:
    masks = 0

    def text(value: Any, limit: int) -> str | None:
        nonlocal masks
        if not isinstance(value, str) or not value:
            return None
        value, changed = mask_text(value)
        masks += changed
        return cap_text(value, limit)[0]

    row = error_row if isinstance(error_row, dict) else {}
    usable = _usable(execution)
    result = execution['data']['resultData'] if usable else {}
    top = result.get('error') if isinstance(result.get('error'), dict) else {}
    run_errors = []
    for node, entries in (result.get('runData') or {}).items():
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get('error'), dict):
                run_errors.append((node, entry['error']))
    first = run_errors[0][1] if run_errors else {}
    failed_node = top.get('node', {}).get('name') if isinstance(top.get('node'), dict) else None
    last_node = next((value for value in (result.get('lastNodeExecuted'), failed_node, row.get('node_name'))
                      if isinstance(value, str) and value), None)
    identifier = execution.get('id') if isinstance(execution, dict) else None
    identifier = identifier if isinstance(identifier, (str, int)) and not isinstance(identifier, bool) else row.get('execution_id') or None
    error = {'error_id': str(row['id']) if isinstance(row.get('id'), (str, int)) and not isinstance(row.get('id'), bool) else None,
             'execution_id': str(identifier) if identifier is not None else None,
             'status': text(execution.get('status'), 32) if isinstance(execution, dict) else None,
             'last_node': text(last_node, 256),
             'name': text(top.get('name') or first.get('name') or (None if usable else row.get('error_type')), 256),
             'message': None, 'description': None,
             'http_code': _http_code(top.get('httpCode')) or _http_code(first.get('httpCode')),
             'node_errors': [], 'saved_vs_failed_run': _saved_vs_failed(execution.get('workflowData') if usable else None, saved_workflow),
             'detail': 'available' if usable else 'unavailable'}
    if not send_error_text:
        error['detail'] = 'withheld'
        return error, masks
    error['message'] = text(top.get('message') or first.get('message') or (None if usable else row.get('error_message')), MAX_MESSAGE_BYTES)
    error['description'] = text(top.get('description') or first.get('description'), MAX_DESCRIPTION_BYTES)
    seen = set()
    for node, run_error in run_errors:
        message = text(run_error.get('message'), MAX_MESSAGE_BYTES)
        shown = text(node, 256)
        if message and shown and (shown, message) not in seen and len(error['node_errors']) < MAX_NODE_ERRORS:
            seen.add((shown, message))
            error['node_errors'].append({'node': shown, 'message': message})
    return error, masks


def scrub_error(execution: dict | None, error_row: dict | None, saved_workflow: dict | None, *, send_error_text: bool) -> dict:
    """An ``ErrorContext`` dict: status, failing node, masked message and HTTP code. Never stacks, context, causes or items."""
    return _scrub_error(execution, error_row, saved_workflow, send_error_text)[0]


def build_context(instance_id: str, *, workflow: dict | None, execution: dict | None, error_row: dict | None,
                  send_error_text: bool, focus_nodes: list[str] | tuple = ()) -> dict:
    """A ``RepairContextResponse`` dict whose ``bytes`` equals the length of its own compact JSON encoding."""
    error, masks = (None, 0)
    if execution is not None or error_row is not None:
        error, masks = _scrub_error(execution, error_row, workflow, send_error_text)
    context = None
    if isinstance(workflow, dict):
        context = mask_workflow(workflow, focus_nodes=focus_nodes, failing_node=error['last_node'] if error else None)
        masks += len(context['redacted_paths'])
    response = {'contract': CONTRACT_VERSION, 'trust': UNTRUSTED, 'instance_id': instance_id, 'bytes': 0,
                'workflow': context, 'error': error,
                'redaction': {'applied': context is not None or error is not None, 'count': masks}}
    for _ in range(8):  # the byte count is part of the document it measures; converges in a few steps
        size = len(canonical_bytes(response))
        if size == response['bytes']:
            break
        response['bytes'] = size
    return response
