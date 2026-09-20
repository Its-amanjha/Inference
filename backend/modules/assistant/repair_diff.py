"""Server-computed repair patches and previews (contract v1).

Pure functions: no requests, network or database. ``apply_operations`` turns contract operations
into a detached desired document; ``preview`` compares stored before/desired documents, so the
model never authors the diff a human approves. Displayed values are masked like repair context.
"""
from __future__ import annotations

from copy import deepcopy
import difflib
import re
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, TypeAdapter, ValidationError

from backend.modules.n8n_promote.router import workflow_diff
from backend.modules.n8n_proxy.codelab import code_node
from .repair_context import (CODE_KEYS, TRUNCATED, canonical_bytes, cap_text, iter_edges, mask_text, mask_value,
                             writable_document)
from .repair_contract import CONTRACT_VERSION, MAX_OPERATIONS, REDACTED_MARKER, Operation

MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_CHANGES = 100
MAX_CODE_LINES = 400
MAX_CHANGE_BYTES = 2 * 1024
CODE_TYPES = frozenset({'n8n-nodes-base.code', 'n8n-nodes-base.function', 'n8n-nodes-base.functionItem',
                        '@n8n/n8n-nodes-langchain.code', '@n8n/n8n-nodes-langchain.toolCode'})
COMMAND_TYPES = frozenset({'n8n-nodes-base.executeCommand', 'n8n-nodes-base.ssh'})
_LOCAL_TRIGGERS = frozenset({'manualtrigger', 'scheduletrigger', 'errortrigger', 'executeworkflowtrigger', 'n8ntrigger',
                             'workflowtrigger', 'localfiletrigger', 'rssfeedreadtrigger'})
_NODE_FIELDS_SHOWN_ELSEWHERE = frozenset({'name', 'parameters', 'position', 'id', 'webhookId', 'disabled', 'credentials'})
_OPERATIONS = TypeAdapter(list[Operation])
_TOKENS = re.compile(r'\.?([A-Za-z0-9_$-]+)|\[(\d+)\]')
_URL = re.compile(r'(?i)\b(?:https?|wss?|ftps?)://[^\s\'"<>`\\]+')
_MISSING = object()


class PatchError(ValueError):
    """Operations do not apply cleanly. ``message`` is safe to show the model and the user."""
    code = 'invalid_patch'

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def code_field(node: Any) -> tuple[str, str] | None:
    """(language, parameter key) of an n8n Code node using CodeLab's detection; None for any other node."""
    if not isinstance(node, dict):
        return None
    try:
        _, language, field = code_node({'nodes': [{**node, 'id': 'repair'}]}, 'repair')
    except HTTPException:
        return None
    return language, field


def _short_type(kind: Any) -> str:
    return kind.rsplit('.', 1)[-1].lower() if isinstance(kind, str) else ''


def is_trigger_type(kind: Any) -> bool:
    short = _short_type(kind)
    return short != 'respondtowebhook' and (short.endswith('trigger') or 'webhook' in short
                                            or short in ('cron', 'interval', 'start', 'emailreadimap'))


def is_webhook_type(kind: Any) -> bool:
    short = _short_type(kind)
    return short != 'respondtowebhook' and ('webhook' in short or short in ('wait', 'form')
                                            or (short.endswith('trigger') and short not in _LOCAL_TRIGGERS))


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _strings(key)
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _operations(operations: Any) -> list[dict]:
    if not isinstance(operations, (list, tuple)) or not 1 <= len(operations) <= MAX_OPERATIONS:
        raise PatchError(f'Provide between 1 and {MAX_OPERATIONS} operations.')
    items = [item.model_dump() if isinstance(item, BaseModel) else item for item in operations]
    try:
        validated = [item.model_dump() for item in _OPERATIONS.validate_python(items)]
    except (ValidationError, ValueError, TypeError):
        raise PatchError('Operations do not match the repair contract.') from None
    for number, item in enumerate(validated, 1):
        if any(REDACTED_MARKER in text or TRUNCATED in text for text in _strings(item)):
            raise PatchError(f'Operation {number}: values cannot contain redacted or truncated placeholders.')
    return validated


def _find(nodes: list, name: str, number: int) -> int:
    matches = [index for index, node in enumerate(nodes) if isinstance(node, dict) and node.get('name') == name]
    if not matches:
        raise PatchError(f'Operation {number}: the node was not found.')
    if len(matches) > 1:
        raise PatchError(f'Operation {number}: more than one node has that name.')
    return matches[0]


def _tokens(path: str) -> list[str | int]:
    return [int(index) if index else key for key, index in _TOKENS.findall(path)]


def _missing_path(number: int) -> PatchError:
    return PatchError(f'Operation {number}: the parameter path does not exist.')


def _container(parameters: dict, tokens: list, *, create: bool, number: int) -> Any:
    """Walk to the parent of the last token, creating intermediate objects (never list entries) when asked."""
    container: Any = parameters
    for token, following in zip(tokens, tokens[1:]):
        if isinstance(token, int):
            if not isinstance(container, list) or token >= len(container):
                raise _missing_path(number)
        elif not isinstance(container, dict):
            raise _missing_path(number)
        elif token not in container:
            if not create or isinstance(following, int):
                raise _missing_path(number)
            container[token] = {}
        container = container[token]
    last = tokens[-1]
    if isinstance(last, int) and (not isinstance(container, list) or last >= len(container)):
        raise _missing_path(number)
    if isinstance(last, str) and not isinstance(container, dict):
        raise _missing_path(number)
    return container


def _edge_matches(edge: Any, target: str, kind: str, index: int) -> bool:
    return isinstance(edge, dict) and edge.get('node') == target and edge.get('type', kind) == kind and edge.get('index', 0) == index


def _prune(connections: dict, source: str, kind: str) -> None:
    outputs = connections[source][kind]
    while outputs and outputs[-1] == []:
        outputs.pop()
    if not outputs:
        del connections[source][kind]
    if not connections[source]:
        del connections[source]


def _apply(document: dict, op: dict, number: int, truncated: frozenset) -> None:
    nodes, connections, settings = document['nodes'], document['connections'], document['settings']
    kind = op['op']
    if kind == 'add_node':
        spec = op['node']
        if any(isinstance(node, dict) and node.get('name') == spec['name'] for node in nodes):
            raise PatchError(f'Operation {number}: a node with that name already exists.')
        node = {'id': str(uuid4()), 'name': spec['name'], 'type': spec['type'], 'typeVersion': spec['type_version'],
                'position': list(spec['position']), 'parameters': deepcopy(spec['parameters'])}
        if spec['disabled']:
            node['disabled'] = True
        if is_webhook_type(spec['type']):
            node['webhookId'] = str(uuid4())
        nodes.append(node)
    elif kind in ('connect', 'disconnect'):
        source, target, channel = op['from_node'], op['to_node'], op['connection_type']
        output, index = op['output_index'], op['input_index']
        if kind == 'connect':
            _find(nodes, source, number)
            _find(nodes, target, number)
            channels = connections.setdefault(source, {})
            outputs = channels.setdefault(channel, []) if isinstance(channels, dict) else None
            if not isinstance(outputs, list) or any(not isinstance(edges, list) for edges in outputs):
                raise PatchError(f'Operation {number}: the connection structure is invalid.')
            outputs.extend([] for _ in range(output + 1 - len(outputs)))
            if any(_edge_matches(edge, target, channel, index) for edge in outputs[output]):
                raise PatchError(f'Operation {number}: that connection already exists.')
            outputs[output].append({'node': target, 'type': channel, 'index': index})
        else:
            channels = connections.get(source)
            outputs = channels.get(channel) if isinstance(channels, dict) else None
            edges = outputs[output] if isinstance(outputs, list) and output < len(outputs) else None
            match = next((position for position, edge in enumerate(edges) if _edge_matches(edge, target, channel, index)),
                         None) if isinstance(edges, list) else None
            if match is None:
                raise PatchError(f'Operation {number}: that connection does not exist.')
            edges.pop(match)
            _prune(connections, source, channel)
    elif kind == 'set_setting':
        if op['value'] is None:
            settings.pop(op['key'], None)
        else:
            settings[op['key']] = op['value']
    else:
        position = _find(nodes, op['node'], number)
        node = nodes[position]
        if kind == 'remove_node':
            nodes.pop(position)
            connections.pop(op['node'], None)
            for source, channels in list(connections.items()):
                for channel, outputs in list(channels.items()) if isinstance(channels, dict) else ():
                    removed = False
                    for edges in outputs if isinstance(outputs, list) else ():
                        if isinstance(edges, list):
                            kept = [edge for edge in edges if not (isinstance(edge, dict) and edge.get('node') == op['node'])]
                            removed = removed or len(kept) != len(edges)
                            edges[:] = kept
                    if removed:
                        _prune(connections, source, channel)
        elif kind == 'set_disabled':
            if (node.get('disabled') is True) != op['disabled']:
                if op['disabled']:
                    node['disabled'] = True
                else:
                    node.pop('disabled', None)
        elif kind == 'set_code':
            detected = code_field(node)
            if detected is None:
                raise PatchError(f'Operation {number}: set_code only applies to Code nodes.')
            if f"{op['node']}.parameters.{detected[1]}" in truncated:
                raise PatchError(f'Operation {number}: that code was truncated in the context; read the node in full first.')
            node['parameters'][detected[1]] = op['code']
        else:
            if op['path'] in CODE_KEYS and f"{op['node']}.parameters.{op['path']}" in truncated:
                raise PatchError(f'Operation {number}: that code was truncated in the context; read the node in full first.')
            if kind == 'set_parameter' and 'parameters' not in node:
                node['parameters'] = {}
            parameters = node.get('parameters')
            if not isinstance(parameters, dict):
                raise _missing_path(number)
            tokens = _tokens(op['path'])
            container = _container(parameters, tokens, create=kind == 'set_parameter', number=number)
            if kind == 'set_parameter':
                container[tokens[-1]] = deepcopy(op['value'])
            elif isinstance(tokens[-1], int):
                container.pop(tokens[-1])
            elif tokens[-1] in container:
                del container[tokens[-1]]
            else:
                raise _missing_path(number)


def apply_operations(doc: dict, operations: list, *, truncated_paths: set[str] | frozenset = frozenset()) -> dict:
    """Apply contract operations to a detached writable copy of ``doc``; raises PatchError, never mutates input."""
    items = _operations(operations)
    base = writable_document(doc)
    if not isinstance(base['nodes'], list) or not isinstance(base['connections'], dict):
        raise PatchError('The workflow has an unsupported structure.')
    try:
        original = canonical_bytes(base)
    except (TypeError, ValueError, RecursionError):
        raise PatchError('The workflow is not valid JSON.') from None
    document = deepcopy(base)
    blocked = frozenset(truncated_paths or ())
    for number, item in enumerate(items, 1):
        _apply(document, item, number, blocked)
    credentials = {node['name']: node.get('credentials') for node in base['nodes']
                   if isinstance(node, dict) and isinstance(node.get('name'), str)}
    for node in document['nodes']:
        name = node.get('name') if isinstance(node, dict) else None
        if isinstance(name, str) and name in credentials and node.get('credentials') != credentials[name]:
            raise PatchError('Credentials cannot be changed by a repair.')
    encoded = canonical_bytes(document)
    if encoded == original:
        raise PatchError('The operations do not change the workflow.')
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise PatchError('The patched workflow exceeds the 1 MiB size limit.')
    return document


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def _diffable(doc: Any) -> dict:
    document = writable_document(doc)
    nodes = document['nodes'] if isinstance(document['nodes'], list) else []
    document['nodes'] = [node for node in nodes if isinstance(node, dict) and isinstance(node.get('name'), str)]
    if not isinstance(document['connections'], dict):
        document['connections'] = {}
    return document


def _same(left: Any, right: Any) -> bool:
    if left is _MISSING or right is _MISSING:
        return left is right
    try:
        return canonical_bytes(left) == canonical_bytes(right)
    except (TypeError, ValueError):
        return left == right


def _differences(left: Any, right: Any, path: tuple = ()):
    if isinstance(left, dict) and isinstance(right, dict):
        for key in [*left, *(key for key in right if key not in left)]:
            yield from _differences(left.get(key, _MISSING), right.get(key, _MISSING), (*path, key))
    elif isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        for index, pair in enumerate(zip(left, right)):
            yield from _differences(*pair, (*path, index))
    elif not _same(left, right):
        yield path


def _lookup(tree: Any, tokens: tuple) -> Any:
    for token in tokens:
        if isinstance(token, int) and isinstance(tree, list) and token < len(tree):
            tree = tree[token]
        elif isinstance(token, str) and isinstance(tree, dict) and token in tree:
            tree = tree[token]
        else:
            return _MISSING
    return tree


def _path_text(tokens: tuple) -> str:
    return ''.join(f'[{token}]' if isinstance(token, int) else f'.{token}' for token in tokens).lstrip('.')


def _overlaps(path: str, masked: list[str]) -> bool:
    return any(item == path or item.startswith((path + '.', path + '[')) or path.startswith((item + '.', item + '['))
               for item in masked)


def _shown(value: Any) -> tuple[Any, bool]:
    if value is _MISSING:
        return None, False
    if isinstance(value, str):
        return cap_text(value, MAX_CHANGE_BYTES)
    encoded = canonical_bytes(value)
    if len(encoded) <= MAX_CHANGE_BYTES:
        return value, False
    return cap_text(encoded.decode('utf-8'), MAX_CHANGE_BYTES)[0], True


def _line_ops(matcher: difflib.SequenceMatcher, old: list[str], new: list[str], opcodes) -> list[tuple[str, str]]:
    ops = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == 'equal':
            ops += [('keep', line) for line in old[i1:i2]]
        else:
            ops += [('remove', line) for line in old[i1:i2]] + [('add', line) for line in new[j1:j2]]
    return ops


def _code_change(node: str, key: str, before: Any, after: Any) -> dict:
    old = before.splitlines() if isinstance(before, str) else []
    new = after.splitlines() if isinstance(after, str) else []
    matcher = difflib.SequenceMatcher(None, old, new)
    ops = _line_ops(matcher, old, new, matcher.get_opcodes())
    truncated = False
    if len(ops) > MAX_CODE_LINES:  # show changed hunks with three lines of context
        ops = [op for group in matcher.get_grouped_opcodes(3) for op in _line_ops(matcher, old, new, group)]
        truncated = True
    truncated = truncated or len(ops) > MAX_CODE_LINES
    lines = []
    for op, text in ops[:MAX_CODE_LINES]:
        shown, cut = cap_text(mask_text(text)[0], MAX_CHANGE_BYTES)
        truncated = truncated or cut
        lines.append({'op': op, 'text': shown})
    return {'kind': 'code', 'node': node, 'path': f'parameters.{key}', 'language': 'python' if key == 'pythonCode' else 'javaScript',
            'lines': lines, 'truncated': truncated}


def _value_change(kind: str, extra: dict, path: str, before: Any, after: Any, masked_paths: list[str]) -> dict:
    shown_before, cut_before = _shown(before)
    shown_after, cut_after = _shown(after)
    return {'kind': kind, **extra, 'path': path, 'before': shown_before, 'after': shown_after,
            'masked': _overlaps(path, masked_paths), 'truncated': cut_before or cut_after}


def _node_changes(name: str, old: dict, new: dict, added: bool) -> list[dict]:
    changes = []
    if (old.get('disabled') is True) != (new.get('disabled') is True):
        changes.append({'kind': 'node_disabled', 'node': name, 'disabled': new.get('disabled') is True})
    old_params = old.get('parameters') if isinstance(old.get('parameters'), dict) else {}
    new_params = new.get('parameters') if isinstance(new.get('parameters'), dict) else {}
    code = [key for key in CODE_KEYS if isinstance(old_params.get(key), str) or isinstance(new_params.get(key), str)]
    old_rest = {key: value for key, value in old_params.items() if key not in code}
    new_rest = {key: value for key, value in new_params.items() if key not in code}
    masked: list[str] = []
    old_masked = mask_value(old_rest, '', masked, None, limit=None)
    new_masked = mask_value(new_rest, '', masked, None, limit=None)
    for tokens in _differences(old_rest, new_rest):
        path = _path_text(tokens)
        change = _value_change('parameter', {'node': name}, path, _lookup(old_masked, tokens), _lookup(new_masked, tokens), masked)
        changes.append({**change, 'path': f'parameters.{path}'})
    changes += [_code_change(name, key, old_params.get(key), new_params.get(key)) for key in code
                if not _same(old_params.get(key, _MISSING), new_params.get(key, _MISSING))]
    if added:
        return changes
    for field in sorted((old.keys() | new.keys()) - _NODE_FIELDS_SHOWN_ELSEWHERE):
        before, after = old.get(field, _MISSING), new.get(field, _MISSING)
        if not _same(before, after):
            field_masked: list[str] = []
            shown = [mask_value(value, field, field_masked, None, limit=None) if value is not _MISSING else value for value in (before, after)]
            changes.append(_value_change('parameter', {'node': name}, field, *shown, field_masked))
    if not _same(old.get('credentials', _MISSING), new.get('credentials', _MISSING)):
        kinds = [sorted(node['credentials']) if isinstance(node.get('credentials'), dict) else None for node in (old, new)]
        changes.append({'kind': 'parameter', 'node': name, 'path': 'credentials', 'before': kinds[0], 'after': kinds[1],
                        'masked': True, 'truncated': False})
    return changes


def _edge_set(connections: Any) -> set[tuple]:
    return {(source, kind, output, target, index if isinstance(index, int) and not isinstance(index, bool) else 0)
            for source, kind, output, target, index in iter_edges(connections)}


def _changes(old: dict, new: dict) -> list[dict]:
    old_nodes = {node['name']: node for node in old['nodes']}
    new_nodes = {node['name']: node for node in new['nodes']}
    changes = [{'kind': 'node_removed', 'node': name, 'type': str(node.get('type', ''))}
               for name, node in old_nodes.items() if name not in new_nodes]
    for name, node in new_nodes.items():
        if name not in old_nodes:
            changes.append({'kind': 'node_added', 'node': name, 'type': str(node.get('type', ''))})
            changes += _node_changes(name, {}, node, True)
        elif not _same(old_nodes[name], node):
            changes += _node_changes(name, old_nodes[name], node, False)
    old_edges, new_edges = _edge_set(old['connections']), _edge_set(new['connections'])
    for kind, edges in (('connection_removed', old_edges - new_edges), ('connection_added', new_edges - old_edges)):
        changes += [{'kind': kind, 'from_node': source, 'to_node': target, 'connection_type': channel,
                     'output_index': output, 'input_index': index}
                    for source, channel, output, target, index in sorted(edges)]
    masked: list[str] = []
    old_settings = mask_value(old['settings'], 'settings', masked, None, limit=None)
    new_settings = mask_value(new['settings'], 'settings', masked, None, limit=None)
    for key in [*old['settings'], *(key for key in new['settings'] if key not in old['settings'])]:
        if not _same(old['settings'].get(key, _MISSING), new['settings'].get(key, _MISSING)):
            changes.append(_value_change('setting', {}, f'settings.{key}', old_settings.get(key, _MISSING),
                                         new_settings.get(key, _MISSING), masked))
    if not _same(old['name'], new['name']):
        changes.append(_value_change('setting', {}, 'name', old['name'], new['name'], []))
    return changes


def _hosts(value: Any):
    for text in _strings(value):
        for match in _URL.finditer(text):
            try:
                host = urlsplit(match.group(0)).hostname
            except ValueError:
                continue
            if host and re.fullmatch(r'[a-z0-9.:_-]+', host):
                yield host


def _risks(old: dict, new: dict) -> list[dict]:
    old_nodes = {node['name']: node for node in old['nodes']}
    new_nodes = {node['name']: node for node in new['nodes']}
    known = {host for node in old['nodes'] for host in _hosts(node.get('parameters'))}
    risks = []
    for node in new['nodes']:
        for host in _hosts(node.get('parameters')):
            if host not in known:
                known.add(host)
                risks.append({'code': 'new_egress_host', 'node': node['name'], 'detail': mask_text(host)[0][:256]})
    for code, types in (('code_node_added', CODE_TYPES), ('command_node_added', COMMAND_TYPES)):
        risks += [{'code': code, 'node': name, 'detail': node['type']} for name, node in new_nodes.items()
                  if node.get('type') in types and (old_nodes.get(name) or {}).get('type') not in types]
    before, after = old['settings'].get('errorWorkflow', _MISSING), new['settings'].get('errorWorkflow', _MISSING)
    if not _same(before, after):
        detail = str(after)[:256] if after not in (_MISSING, None, '') else 'removed'
        risks.append({'code': 'error_workflow_changed', 'node': None, 'detail': detail})
    for name in [*old_nodes, *(name for name in new_nodes if name not in old_nodes)]:
        previous, current = old_nodes.get(name), new_nodes.get(name)
        if not any(node is not None and is_trigger_type(node.get('type')) for node in (previous, current)):
            continue
        if previous is None or current is None:
            detail = 'added' if previous is None else 'removed'
        elif (any(not _same(previous.get(field, _MISSING), current.get(field, _MISSING)) for field in ('type', 'typeVersion', 'parameters'))
              or (previous.get('disabled') is True) != (current.get('disabled') is True)):
            detail = 'changed'
        else:
            continue
        risks.append({'code': 'trigger_changed', 'node': name, 'detail': detail})
    risks += [{'code': 'node_removed', 'node': name, 'detail': str(node.get('type', ''))[:256]}
              for name, node in old_nodes.items() if name not in new_nodes]
    risks += _self_call_risks(old, new)
    return risks


# n8n serves trigger nodes under these route prefixes; a workflow calling one of its own routes re-triggers itself.
# Express matches routes case-insensitively and on the decoded path by default, so the guard
# normalizes both before comparing: an uppercase prefix or %6Frders reaches /webhook/orders.
_TRIGGER_ROUTE = re.compile(r'/(webhook|webhook-test|webhook-waiting|form|form-test|form-waiting)/([A-Za-z0-9_\-.~:%/]+)',
                            re.IGNORECASE)


def _decoded(text: str) -> str:
    """Percent-decoded text for trigger-route comparison (repeated: %256F is %6F is 'o'). Display keeps the original."""
    decoded = text
    for _ in range(3):
        try:
            nxt = unquote(decoded)
        except Exception:
            break
        if nxt == decoded:
            break
        decoded = nxt
    return decoded


def _trigger_tokens(document: dict) -> set[str]:
    """Paths and webhook ids under which the workflow's own enabled trigger nodes are reachable."""
    tokens = set()
    for node in document['nodes']:
        if not is_webhook_type(node.get('type')) or node.get('disabled') is True:
            continue
        parameters = node.get('parameters') if isinstance(node.get('parameters'), dict) else {}
        for value in (parameters.get('path'), node.get('webhookId')):
            if isinstance(value, str) and value.strip('/'):
                tokens.add(_decoded(value).strip('/').lower())
    return tokens


def _self_calls(document: dict) -> set[tuple[str, str]]:
    """(node, route) pairs where a non-trigger node addresses one of the workflow's own trigger routes.

    Host-independent on purpose: the same n8n is reachable as localhost, a container name or a public URL,
    and expression URLs such as ``={{ $env.BASE }}/webhook/x`` still contain the literal route.
    """
    tokens = _trigger_tokens(document)
    found: set[tuple[str, str]] = set()
    if not tokens:
        return found
    for node in document['nodes']:
        if is_webhook_type(node.get('type')):
            continue
        for text in _strings(node.get('parameters')):
            for match in _TRIGGER_ROUTE.finditer(_decoded(text)):
                route = match.group(2).strip('/').lower()
                if any(route == token or route.startswith(token + '/') for token in tokens):
                    found.add((node['name'], f"/{match.group(1)}/{match.group(2).strip('/')}"))
    return found


def _self_call_risks(old: dict, new: dict) -> list[dict]:
    return [{'code': 'self_webhook_call', 'node': node, 'detail': mask_text(route)[0][:256]}
            for node, route in sorted(_self_calls(new) - _self_calls(old))]


def introduced_self_calls(before: dict, desired: dict) -> list[dict]:
    """Self-calls the desired workflow adds compared with the one it replaces (contract Risk dicts)."""
    return _self_call_risks(_diffable(before), _diffable(desired))


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _summary(kind: str, diff: dict, name: str, renamed: bool) -> str:
    label = f'workflow “{name[:120]}”' if name else 'the workflow'
    changed = [entry['name'] for entry in diff['nodes_changed']]
    parts = [text for present, text in (
        (changed, f"change {_count(len(changed), 'node')}"),
        (diff['nodes_added'], f"add {_count(len(diff['nodes_added']), 'node')}"),
        (diff['nodes_removed'], f"remove {_count(len(diff['nodes_removed']), 'node')}"),
        (diff['connections_changed'], 'update connections'),
        (diff['settings_changed'], 'update settings'),
        (renamed, 'rename the workflow')) if present]
    if not parts and diff['changed']:
        parts = ['update the workflow structure']
    if not parts:
        return f'No changes to {label}.' if kind == 'workflow_patch' else f'Restore {label}: no changes.'
    action = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"
    # Only names of nodes that already exist are listed; model-authored names of added nodes never appear.
    existing = changed + diff['nodes_removed']
    names = ', '.join(item[:64] for item in existing[:5]) + (f' and {len(existing) - 5} more' if len(existing) > 5 else '')
    if kind == 'workflow_patch':
        return f'{action[0].upper()}{action[1:]} in {label}' + (f': {names}.' if names else '.')
    return f'Restore {label}: {action}' + (f' ({names}).' if names else '.')


def preview(before: dict, desired: dict, *, kind: str, summary_text: str | None = None, model_summary: str | None = None,
            preconditions: list[dict] | None = None, workflow_name: str | None = None) -> dict:
    """A contract ``Preview`` dict computed from the stored before/desired documents."""
    if kind not in ('workflow_patch', 'workflow_restore'):
        raise ValueError('Unsupported preview kind.')
    old, new = _diffable(before), _diffable(desired)
    diff = workflow_diff(old, new)
    # Promotion sorts names alphabetically; previews follow workflow order, like the changes list.
    old_order = {node['name']: index for index, node in enumerate(old['nodes'])}
    new_order = {node['name']: index for index, node in enumerate(new['nodes'])}
    diff['nodes_added'].sort(key=new_order.__getitem__)
    diff['nodes_removed'].sort(key=old_order.__getitem__)
    diff['nodes_changed'].sort(key=lambda entry: new_order[entry['name']])
    changes = _changes(old, new)
    name = next((item for item in (workflow_name, old['name'], new['name']) if isinstance(item, str) and item), '')
    return {'contract': CONTRACT_VERSION, 'kind': kind,
            'summary_text': summary_text if summary_text is not None else _summary(kind, diff, name, not _same(old['name'], new['name'])),
            'model_summary': model_summary, 'diff': diff, 'changes': changes[:MAX_CHANGES],
            'changes_omitted': max(0, len(changes) - MAX_CHANGES), 'risks': _risks(old, new),
            'preconditions': list(preconditions or []),
            'apply': {'label': 'Apply change' if kind == 'workflow_patch' else 'Restore version', 'danger': False,
                      'typed_confirmation': None}}
