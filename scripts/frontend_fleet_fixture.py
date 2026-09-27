"""Local n8n-shaped HTTP fixture and browser scenarios; never a real fleet."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from threading import Lock, Thread
import time
from urllib.parse import urlsplit, parse_qs
from uuid import uuid4

from playwright.sync_api import expect

# Repair scenario data (frontend_repair_checks.py). The secret is fake and exists only in this simulator.
REPAIR_SECRET = 'sk-fixture-secret'
REPAIR_WORKFLOW_ID = 'wf-orders'
REPAIR_EXECUTION_ID = '4821'
REPAIR_ERROR_MESSAGE = 'Request failed with status code 404'
AI_EXECUTION_ID = '4830'
FAILED_EXECUTION_IDS = ('4831', '4832')
# Sits in the failed runs' error description and stack, which must never be stored or shown.
FAILED_SECRET = 'sk-fixture-failed-run-secret-0123456789'
# Prompt and answer text of the AI run; it must never reach a browser.
AI_PRIVATE_TEXT = 'fixture-private-invoice-text'
# n8n 2.19 public API workflow PUT: required writable fields, read-only fields, closed settings object.
PUT_REQUIRED = ('name', 'nodes', 'connections', 'settings')
PUT_WRITABLE = frozenset(PUT_REQUIRED) | {'staticData'}
PUT_READ_ONLY = frozenset({'id', 'active', 'createdAt', 'updatedAt', 'tags'})
PUT_SETTINGS = frozenset({'saveExecutionProgress', 'saveManualExecutions', 'saveDataErrorExecution', 'saveDataSuccessExecution',
                          'executionTimeout', 'errorWorkflow', 'timezone', 'executionOrder', 'callerPolicy', 'callerIds',
                          'timeSavedPerExecution', 'availableInMCP'})





def open_review(page):
    """Pending approvals are listed in the AI workspace's Review drawer; open it unless it already is."""
    drawer = page.locator('dialog.review-drawer[open]')
    if not drawer.count():
        page.get_by_role('button', name=re.compile(r'^Review')).click()
        drawer.wait_for()

def choose_instance(page, value):
    """Pick an instance by ID ('' is All instances) in the top-bar switcher, a button with a listbox."""
    page.get_by_role('button', name=re.compile(r'^Selected instance:')).click()
    page.locator(f'.scope-switcher .select-menu-list [role=option][data-value="{value}"]').click()

def workflow_action(page, scope, name, instance, item):
    """Choose an item from a workflow row's More actions menu."""
    scope.get_by_role('button', name=f'More actions for {name} on {instance}', exact=True).click()
    page.get_by_role('menu', name=f'More actions for {name} on {instance}').get_by_role('menuitem', name=item, exact=True).click()


def expect_workflow_state(page, scope, name, instance, label):
    row = scope.get_by_role('row').filter(has=page.get_by_role('button', name=f'{name} on {instance}', exact=True))
    expect(row.locator('.workflow-state')).to_have_text(label)

def _iso(moment):
    return moment.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def repair_workflow(clock):
    """Webhook -> HTTP Request (secret header, v1 URL) -> Code (`return item;`); active, like a live workflow."""
    return {
        'id': REPAIR_WORKFLOW_ID, 'name': 'Order synchronization', 'active': True, 'isArchived': False,
        'versionId': str(uuid4()), 'createdAt': _iso(clock - timedelta(days=14)), 'updatedAt': _iso(clock),
        'nodes': [
            {'id': 'orders-webhook', 'name': 'Webhook', 'type': 'n8n-nodes-base.webhook', 'typeVersion': 2,
             'position': [0, 0], 'webhookId': 'orders-webhook', 'parameters': {'path': 'orders', 'httpMethod': 'POST'}},
            {'id': 'orders-http', 'name': 'HTTP Request', 'type': 'n8n-nodes-base.httpRequest', 'typeVersion': 4.2,
             'position': [250, 0], 'parameters': {
                 'method': 'GET', 'url': 'https://api.example.com/v1/orders', 'sendHeaders': True,
                 'headerParameters': {'parameters': [{'name': 'Authorization', 'value': 'Bearer ' + REPAIR_SECRET}]},
                 'options': {}},
             'credentials': {'httpHeaderAuth': {'id': 'orders-api', 'name': 'Orders API'}}},
            {'id': 'orders-code', 'name': 'Code', 'type': 'n8n-nodes-base.code', 'typeVersion': 2,
             'position': [500, 0], 'parameters': {'jsCode': 'return item;'}}],
        'connections': {'Webhook': {'main': [[{'node': 'HTTP Request', 'type': 'main', 'index': 0}]]},
                        'HTTP Request': {'main': [[{'node': 'Code', 'type': 'main', 'index': 0}]]}},
        'settings': {'executionOrder': 'v1'}, 'staticData': None, 'pinData': {}, 'meta': None, 'tags': [], 'triggerCount': 1}


def repair_execution(workflow, clock):
    """GET /executions/{id}?includeData=true shape; secrets sit in stack, context, cause and run data items."""
    header = {'Authorization': 'Bearer ' + REPAIR_SECRET}
    http_node = next(node for node in workflow['nodes'] if node['name'] == 'HTTP Request')
    started = clock - timedelta(minutes=12)
    error = {'name': 'NodeApiError', 'message': REPAIR_ERROR_MESSAGE, 'httpCode': '404', 'level': 'warning',
             'description': 'GET https://api.example.com/v1/orders was refused. Authorization: Bearer ' + REPAIR_SECRET,
             'timestamp': int(started.timestamp() * 1000) + 50,
             'stack': 'NodeApiError: ' + REPAIR_ERROR_MESSAGE + '\n    at ExecuteContext.execute (/usr/local/lib/node_modules/n8n/'
                      'node_modules/n8n-nodes-base/dist/nodes/HttpRequest/V3/HttpRequestV3.node.js:812:15)\n'
                      '    request headers {"Authorization":"Bearer ' + REPAIR_SECRET + '"}',
             'context': {'request': {'method': 'GET', 'uri': 'https://api.example.com/v1/orders', 'headers': header}},
             'cause': {'status': 404, 'config': {'headers': header}},
             'node': {'name': 'HTTP Request', 'type': http_node['type'], 'typeVersion': http_node['typeVersion'],
                      'parameters': deepcopy(http_node['parameters'])}}
    run_data = {
        'Webhook': [{'startTime': int(started.timestamp() * 1000), 'executionIndex': 0, 'executionTime': 3,
                     'executionStatus': 'success', 'source': [],
                     'data': {'main': [[{'json': {'headers': {'authorization': 'Bearer ' + REPAIR_SECRET},
                                                  'body': {'order_id': 1042}}}]]}}],
        'HTTP Request': [{'startTime': int(started.timestamp() * 1000) + 5, 'executionIndex': 1, 'executionTime': 45,
                          'executionStatus': 'error', 'source': [{'previousNode': 'Webhook'}], 'error': deepcopy(error)}]}
    return {'id': REPAIR_EXECUTION_ID, 'finished': False, 'mode': 'webhook', 'retryOf': None, 'retrySuccessId': None,
            'status': 'error', 'startedAt': _iso(started), 'stoppedAt': _iso(started + timedelta(seconds=1)),
            'workflowId': REPAIR_WORKFLOW_ID, 'waitTill': None, 'customData': {},
            'data': {'startData': {}, 'resultData': {'error': error, 'runData': run_data, 'lastNodeExecuted': 'HTTP Request'},
                     'executionData': {'contextData': {}, 'nodeExecutionStack': [], 'waitingExecution': {},
                                       'waitingExecutionSource': {}}},
            'workflowData': deepcopy(workflow)}


def new_repair_state():
    clock = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    workflow = repair_workflow(clock)
    repair = {'enabled': False, 'workflow': workflow, 'history': {}, 'execution': repair_execution(workflow, clock),
              # Switches. concurrent_edit: another n8n editor saves before the next wf-orders request (one-shot).
              # readback_divergence: the next PUT is accepted but different content is stored and read back (one-shot).
              'concurrent_edit': False, 'readback_divergence': False,
              'remote_edits': 0, 'rejected': [], 'clock': clock}
    _record_version(repair, workflow)
    return repair


def _record_version(repair, doc):
    repair['history'][doc['versionId']] = {
        'versionId': doc['versionId'], 'workflowId': doc['id'], 'name': doc['name'], 'nodes': deepcopy(doc['nodes']),
        'connections': deepcopy(doc['connections']), 'authors': 'Fixture editor',
        'createdAt': doc['updatedAt'], 'updatedAt': doc['updatedAt']}


def _save_version(repair, doc):
    """Every n8n save gets a new versionId, a later updatedAt and a history entry; prior versions stay readable."""
    repair['clock'] += timedelta(seconds=1)
    doc['versionId'] = str(uuid4())
    doc['updatedAt'] = _iso(repair['clock'])
    _record_version(repair, doc)


def _node(doc, name):
    return next((node for node in doc.get('nodes') or [] if isinstance(node, dict) and node.get('name') == name), None)


def current_repair_workflow(repair):
    """The stored workflow, after applying a pending concurrent edit."""
    doc = repair['workflow']
    if repair['concurrent_edit']:
        repair['concurrent_edit'] = False
        node = _node(doc, 'HTTP Request')
        if node is not None:
            node.setdefault('parameters', {})['options'] = {'timeout': 10000}
        _save_version(repair, doc)
        repair['remote_edits'] += 1
    return doc


def put_repair_workflow(repair, payload):
    """Validate like n8n's public API, save, then optionally diverge so the readback differs from what was accepted."""
    if not isinstance(payload, dict):
        return 400, {'message': 'request/body must be object'}
    for key in sorted(payload):
        if key in PUT_READ_ONLY:
            return 400, {'message': f'request/body/{key} is read-only'}
        if key not in PUT_WRITABLE:
            return 400, {'message': 'request/body must NOT have additional properties'}
    for key in PUT_REQUIRED:
        if key not in payload:
            return 400, {'message': f"request/body must have required property '{key}'"}
    if not isinstance(payload['settings'], dict) or set(payload['settings']) - PUT_SETTINGS:
        return 400, {'message': 'request/body/settings must NOT have additional properties'}
    doc = repair['workflow']
    doc.update(deepcopy(payload))
    _save_version(repair, doc)
    accepted = deepcopy(doc)
    if repair['readback_divergence']:
        repair['readback_divergence'] = False
        code = _node(doc, 'Code')
        if code is not None:
            parameters = code.setdefault('parameters', {})
            parameters['jsCode'] = str(parameters.get('jsCode', '')) + '\n// Changed in n8n while the repair was saved.'
        _save_version(repair, doc)
    return 200, accepted


def execution_view(execution, include_data):
    view = deepcopy(execution)
    if not include_data:
        view.pop('data', None)
        view.pop('workflowData', None)
    return view


def ai_execution():
    """A finished run that called OpenAI twice: with the HTTP Request node (the service's own usage block, a
    model with a bundled price) and with an n8n chat-model sub-node (n8n's tokenUsage, a model with no price)."""
    started = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
    nodes = [
        {'id': 'hook', 'name': 'Webhook', 'type': 'n8n-nodes-base.webhook', 'typeVersion': 2, 'position': [0, 0], 'parameters': {'path': 'invoice'}},
        {'id': 'ask', 'name': 'Summarize invoice', 'type': 'n8n-nodes-base.httpRequest', 'typeVersion': 4.2, 'position': [200, 0],
         'parameters': {'method': 'POST', 'url': 'https://api.openai.com/v1/responses'}},
        {'id': 'agent', 'name': 'Classify invoice', 'type': '@n8n/n8n-nodes-langchain.agent', 'typeVersion': 2, 'position': [400, 0], 'parameters': {}},
        {'id': 'model', 'name': 'OpenAI Chat Model', 'type': '@n8n/n8n-nodes-langchain.lmChatOpenAi', 'typeVersion': 1.2, 'position': [400, 200],
         'parameters': {'model': {'__rl': True, 'value': 'gpt-4o-mini', 'mode': 'list'}}}]
    connections = {'Webhook': {'main': [[{'node': 'Summarize invoice', 'type': 'main', 'index': 0}]]},
                   'Summarize invoice': {'main': [[{'node': 'Classify invoice', 'type': 'main', 'index': 0}]]},
                   'OpenAI Chat Model': {'ai_languageModel': [[{'node': 'Classify invoice', 'type': 'ai_languageModel', 'index': 0}]]}}
    answer = {'object': 'response', 'model': 'gpt-4.1-2025-04-14', 'output': [{'content': [{'text': AI_PRIVATE_TEXT}]}],
              'usage': {'input_tokens': 12000, 'input_tokens_details': {'cached_tokens': 8000}, 'output_tokens': 900,
                        'output_tokens_details': {'reasoning_tokens': 0}, 'total_tokens': 12900}}
    run_data = {
        'Webhook': [{'data': {'main': [[{'json': {'body': {'invoice': AI_PRIVATE_TEXT}}}]]}}],
        'Summarize invoice': [{'data': {'main': [[{'json': answer}]]}}],
        'OpenAI Chat Model': [{'data': {'ai_languageModel': [[{'json': {'response': {'generations': [[{'text': AI_PRIVATE_TEXT}]]},
                                                                         'tokenUsage': {'promptTokens': 600, 'completionTokens': 40, 'totalTokens': 640}}}]]}}],
        'Classify invoice': [{'data': {'main': [[{'json': {'output': AI_PRIVATE_TEXT}}]]}}]}
    return {'id': AI_EXECUTION_ID, 'finished': True, 'mode': 'webhook', 'retryOf': None, 'retrySuccessId': None, 'status': 'success',
            'startedAt': _iso(started), 'stoppedAt': _iso(started + timedelta(seconds=4)), 'workflowId': 'wf-invoices', 'waitTill': None,
            'data': {'resultData': {'runData': run_data}},
            'workflowData': {'id': 'wf-invoices', 'name': 'Invoice summary', 'active': True, 'nodes': nodes, 'connections': connections, 'settings': {}}}


def failed_executions():
    """Two runs of Invoice summary that failed the same way at the same step; only their order numbers differ."""
    runs = []
    for index, (identifier, order) in enumerate(zip(FAILED_EXECUTION_IDS, (1042, 1043))):
        started = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=20 - index * 10)
        error = {'name': 'NodeApiError', 'message': f'Invoice {order} was rejected: Request failed with status code 422',
                 'description': f'POST https://billing.example/invoices/{order} Authorization: Bearer {FAILED_SECRET}',
                 'stack': f'NodeApiError at send ({FAILED_SECRET})', 'node': {'name': 'Send invoice', 'type': 'n8n-nodes-base.httpRequest'}}
        runs.append({'id': identifier, 'finished': False, 'mode': 'webhook', 'retryOf': None, 'retrySuccessId': None, 'status': 'error',
                     'startedAt': _iso(started), 'stoppedAt': _iso(started + timedelta(seconds=2)), 'workflowId': 'wf-invoices', 'waitTill': None,
                     'data': {'resultData': {'error': error, 'runData': {}, 'lastNodeExecuted': 'Send invoice'}},
                     'workflowData': {'id': 'wf-invoices', 'name': 'Invoice summary', 'active': True, 'nodes': [], 'connections': {}, 'settings': {}}})
    return runs


def enable_repair(state):
    """Expose wf-orders and its failed execution on the source instance. Call only after the fleet assertions,
    which count workflows, executions and Activate buttons."""
    state['repair']['enabled'] = True
    return state['repair']


def queue_provider(state, *steps):
    """Queue scripted model replies; each provider request consumes one step, an empty queue answers with text.

    {'kind': 'text', 'content': str} | {'kind': 'length'} | {'kind': 'status', 'status': 429} |
    {'kind': 'tool_call', 'name': tool or '*', 'arguments': dict}. '*' calls the first declared tool with
    schema-derived arguments; the string '{{content_hash}}' becomes the latest workflow content_hash in the request.
    """
    state['provider']['script'].extend(deepcopy(list(steps)))


_CONTEXT_HASH = re.compile(r'(?<![A-Za-z0-9_])content_hash\\*"\s*:\s*\\*"([0-9a-f]{64})')


def fill_placeholders(value, raw, fallback=None):
    if isinstance(value, dict):
        return {key: fill_placeholders(item, raw, fallback) for key, item in value.items()}
    if isinstance(value, list):
        return [fill_placeholders(item, raw, fallback) for item in value]
    if value == '{{content_hash}}':
        found = _CONTEXT_HASH.findall(raw)
        return found[-1] if found else (fallback or value)
    return value


def sample_arguments(schema, depth=0):
    """Minimal valid arguments for a tool this fixture does not know (the tool-calling check's dummy tool)."""
    if not isinstance(schema, dict) or depth > 6:
        return 'probe'
    for key in ('anyOf', 'oneOf'):
        options = [item for item in schema.get(key) or [] if isinstance(item, dict) and item.get('type') != 'null']
        if options:
            return sample_arguments(options[0], depth + 1)
    if 'const' in schema:
        return schema['const']
    if schema.get('enum'):
        return schema['enum'][0]
    kind = schema.get('type')
    if isinstance(kind, list):
        kind = next((item for item in kind if item != 'null'), 'string')
    if kind == 'object' or 'properties' in schema:
        properties = schema.get('properties') or {}
        return {key: sample_arguments(properties.get(key, {}), depth + 1) for key in schema.get('required') or []}
    if kind in ('integer', 'number'):
        return schema.get('minimum', 1)
    if kind == 'boolean':
        return True
    return [] if kind == 'array' else 'probe'


def declared_tools(body):
    tools = body.get('tools') if isinstance(body, dict) else None
    return [tool['function']['name'] for tool in tools or [] if isinstance(tool, dict)
            and isinstance(tool.get('function'), dict) and isinstance(tool['function'].get('name'), str)]


def scripted_completion(step, body, raw, number, fallback_hash=None):
    """One OpenAI-compatible chat completion (or error status) for a queued step."""
    kind = step.get('kind', 'text')
    if kind == 'status':
        return int(step.get('status', 429)), {'error': {'message': step.get('message', 'Rate limit reached for requests.'),
                                                        'type': 'requests', 'param': None, 'code': 'rate_limit_exceeded'}}
    finish, message = 'stop', {'role': 'assistant', 'content': step.get('content', 'Scripted fixture reply. No change proposed.')}
    if kind == 'length':
        finish, message = 'length', {'role': 'assistant', 'content': step.get('content', 'The HTTP Request node should call the v2')}
    elif kind == 'tool_call':
        schemas = {tool['function']['name']: tool['function'].get('parameters') for tool in body.get('tools') or []
                   if isinstance(tool, dict) and isinstance(tool.get('function'), dict)}
        name, arguments = step.get('name', '*'), {}
        if name == '*':
            name = next(iter(schemas), 'undeclared_tool')
            sample = sample_arguments(schemas.get(name) or {})
            arguments = sample if isinstance(sample, dict) else {}
        arguments.update(fill_placeholders(step.get('arguments') or {}, raw, fallback_hash))
        finish = 'tool_calls'
        message = {'role': 'assistant', 'content': step.get('content'), 'tool_calls': [{
            'id': f'call_fixture_{number}', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments, separators=(',', ':'))}}]}
    usage = {'prompt_tokens': max(1, len(raw) // 4), 'completion_tokens': 16}
    usage['total_tokens'] = usage['prompt_tokens'] + usage['completion_tokens']
    return 200, {'id': f'chatcmpl-fixture-{number}', 'object': 'chat.completion', 'created': int(time.time()),
                 'model': body.get('model') or 'scripted-repair', 'usage': usage,
                 'choices': [{'index': 0, 'message': message, 'finish_reason': finish, 'logprobs': None}]}


@contextmanager
def fleet_fixture():
    original = {'id': 'source-wf', 'name': 'Browser workflow', 'active': False,
        'nodes': [{'id': 'code', 'name': 'Code', 'type': 'n8n-nodes-base.code',
                   'typeVersion': 2, 'position': [0, 0], 'parameters': {'jsCode': 'return items;'}}],
        'connections': {}, 'settings': {}}
    # ai_runs: list the AI run on the source instance (only after the fleet assertions, which count executions).
    state = {'source': deepcopy(original), 'target': None, 'writes': [],
             'repair': new_repair_state(), 'provider': {'script': [], 'requests': [], 'content_hash': None},
             'ai_runs': False, 'ai_execution': ai_execution(), 'failed_executions': failed_executions()}
    lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def read_body(self):
            length = int(self.headers.get('Content-Length') or 0)
            return self.rfile.read(length).decode('utf-8') if length else ''

        def handle_provider(self):
            """Scripted OpenAI-compatible model; every request is recorded so checks can count one per human action."""
            if self.command != 'POST':
                self.reply(405, {'error': {'message': 'Use POST.'}})
                return
            raw = self.read_body()
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            if not isinstance(body, dict):
                self.reply(400, {'error': {'message': 'Request body must be a JSON object.'}})
                return
            with lock:
                provider = state['provider']
                step = provider['script'].pop(0) if provider['script'] else {'kind': 'text'}
                provider['requests'].append({'kind': step.get('kind', 'text'), 'model': body.get('model'),
                                             'tools': declared_tools(body), 'messages': len(body.get('messages') or []),
                                             'body': raw})
                number, fallback = len(provider['requests']), provider.get('content_hash')
            self.reply(*scripted_completion(step, body, raw, number, fallback))

        def handle_repair(self, endpoint):
            """wf-orders, its version history and execution 4821 on the source instance; False for other endpoints."""
            repair = state['repair']
            segments = endpoint.split('/')
            if segments == ['executions', REPAIR_EXECUTION_ID] and self.command == 'GET':
                include = parse_qs(urlsplit(self.path).query).get('includeData') == ['true']
                self.reply(200, execution_view(repair['execution'], include))
                return True
            if segments[:2] != ['workflows', REPAIR_WORKFLOW_ID] or len(segments) > 3:
                return False
            with lock:
                doc = current_repair_workflow(repair)
                if self.command == 'GET' and len(segments) == 2:
                    status, body = 200, deepcopy(doc)
                elif self.command == 'GET':
                    version = repair['history'].get(segments[2])
                    status, body = (200, deepcopy(version)) if version else (404, {'message': 'Not Found'})
                elif self.command == 'PUT' and len(segments) == 2:
                    try:
                        payload = json.loads(self.read_body())
                    except ValueError:
                        payload = None
                    status, body = put_repair_workflow(repair, payload)
                elif self.command == 'POST' and len(segments) == 3 and segments[2] in ('activate', 'deactivate'):
                    doc['active'] = segments[2] == 'activate'
                    status, body = 200, deepcopy(doc)
                else:
                    return False
                if self.command != 'GET':
                    if status == 200:
                        state['writes'].append(('source', self.command, endpoint))
                    else:
                        repair['rejected'].append((self.command, endpoint, body.get('message')))
            self.reply(status, body)
            return True

        def handle_api(self):
            if urlsplit(self.path).path == '/v1/chat/completions':
                self.handle_provider()
                return
            parts = urlsplit(self.path).path.strip('/').split('/')
            instance = parts[0]
            if instance not in ('source', 'target') or self.headers.get('X-N8N-API-KEY') != instance + '-fixture-key':
                self.send_error(401)
                return
            if self.headers.get('Cookie'):
                self.send_error(400)
                return
            endpoint = '/'.join(parts[3:])
            if state.get('unavailable') == instance:
                self.send_error(503)
                return
            workflow = state[instance]
            body = None
            repair_on = instance == 'source' and state['repair']['enabled']
            ai_views = {run['id']: run for run in [state['ai_execution'], *state['failed_executions']]}
            if instance == 'source' and state['ai_runs'] and self.command == 'GET' and (endpoint == 'executions' or endpoint.split('/')[-1] in ai_views):
                if endpoint == 'executions':
                    listed = [execution_view(run, False) for run in [state['ai_execution'], *reversed(state['failed_executions'])]]
                    if repair_on:
                        listed.append(execution_view(state['repair']['execution'], False))
                    self.reply(200, {'data': listed, 'nextCursor': None})
                else:
                    run = ai_views[endpoint.split('/')[-1]]
                    self.reply(200, execution_view(run, parse_qs(urlsplit(self.path).query).get('includeData') == ['true']))
                return
            if repair_on and self.handle_repair(endpoint):
                return
            if self.command == 'GET':
                if endpoint in ('credentials', 'executions'):
                    body = {'data': [], 'nextCursor': None}
                    filters = parse_qs(urlsplit(self.path).query)
                    if (repair_on and endpoint == 'executions'
                            and filters.get('workflowId', [REPAIR_WORKFLOW_ID]) == [REPAIR_WORKFLOW_ID]
                            and filters.get('status', ['error']) == ['error']):
                        body['data'] = [execution_view(state['repair']['execution'], False)]
                elif endpoint == 'workflows':
                    body = {'data': [workflow] if workflow else [], 'nextCursor': None}
                    if repair_on:
                        with lock:
                            body['data'].append(deepcopy(current_repair_workflow(state['repair'])))
                    query = parse_qs(urlsplit(self.path).query)
                    if query.get('name'):
                        body['data'] = [row for row in body['data'] if query['name'][0].lower() in row['name'].lower()]
                    elif state.get('pagination') and instance == 'target':
                        if query.get('cursor') == ['second']:
                            body['data'] = [{**workflow, 'name':'Second page workflow'}]
                        else:
                            body['nextCursor'] = 'second'
                elif workflow and endpoint == 'workflows/' + workflow['id']:
                    body = workflow
            elif self.command == 'POST' and endpoint == 'workflows' and instance == 'target':
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                body = state[instance] = {**payload, 'id': 'target-wf', 'active': False}
            elif workflow and self.command == 'PUT' and endpoint == 'workflows/' + workflow['id']:
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                workflow.update(payload)
                body = workflow
            elif workflow and self.command == 'POST' and endpoint in ('workflows/' + workflow['id'] + '/activate', 'workflows/' + workflow['id'] + '/deactivate'):
                workflow['active'] = endpoint.endswith('/activate')
                body = workflow
            if body is None:
                self.send_error(404)
                return
            if self.command != 'GET':
                state['writes'].append((instance, self.command, endpoint))
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PUT = handle_api

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def exercise_fleet(page, fixture_url, state):
    ids = page.evaluate('''async (url) => {
        const {api} = await import('/js/api.js');
        const ids = {};
        for (const name of ['source', 'target']) {
            ids[name] = (await api('/api/n8n_proxy/instances', {method:'POST',json:{name,
                url: url + '/' + name, api_key:name + '-fixture-key'}})).id;
        }
        return ids;
    }''', fixture_url)
    state['instance_ids'] = ids
    page.reload(wait_until='domcontentloaded')
    choose_instance(page, ids['source'])
    page.evaluate("location.hash = 'workflows'")
    page.get_by_role('button', name='Browser workflow on source', exact=True).wait_for()
    accept = lambda dialog: dialog.accept()
    page.on('dialog', accept)
    try:
        # Activation is in the row's More actions menu and confirms in a dialog naming workflow and instance.
        workflow_action(page, page, 'Browser workflow', 'source', 'Activate')
        page.get_by_role('dialog', name='Activate workflow').get_by_role('button', name='Activate workflow', exact=True).click()
        expect_workflow_state(page, page, 'Browser workflow', 'source', 'Active')
        assert state['source']['active'] is True and state['target'] is None
        workflow_action(page, page, 'Browser workflow', 'source', 'Deactivate')
        page.get_by_role('dialog', name='Deactivate workflow').get_by_role('button', name='Deactivate workflow', exact=True).click()
        expect_workflow_state(page, page, 'Browser workflow', 'source', 'Inactive')
        workflow_action(page, page, 'Browser workflow', 'source', 'Open in CodeLab')
        page.get_by_role('button', name='Load workflow', exact=True).click()
        # A link already loaded the workflow; this click reloads it. Edit only once that load has finished.
        expect(page.get_by_role('button', name='Load workflow', exact=True)).to_be_enabled()
        editor = page.get_by_role('textbox', name='Code editor', exact=True)
        expect(editor).to_have_value('return items;')
        editor.fill('return [];')
        save = page.get_by_role('button', name='Deploy to n8n', exact=True)
        save.click()
        # Deployment confirms in an in-app dialog that names the Code node.
        page.get_by_role('dialog', name='Deploy code to n8n').get_by_role('button', name='Deploy code', exact=True).click()
        page.get_by_text('Code saved and verified.', exact=True).wait_for()
        assert state['source']['nodes'][0]['parameters']['jsCode'] == 'return [];'
        state['source']['name'] = 'Changed remotely'
        count = len(state['writes'])
        editor.fill('return [{json:{unsafe:true}}];')
        save.click()
        page.get_by_role('dialog', name='Deploy code to n8n').get_by_role('button', name='Deploy code', exact=True).click()
        page.get_by_text('Reload the node before another deployment. Copy your edits first.', exact=True).wait_for()
        expect(save).to_be_disabled()
        assert len(state['writes']) == count, 'Stale code must not reach n8n'
        page.evaluate("location.hash = 'promote'")
        page.get_by_role('button', name=re.compile(r'^Source instance:')).click()
        page.get_by_role('listbox', name='Source instance').get_by_role('option', name='source', exact=True).click()
        page.get_by_role('button', name=re.compile(r'^Target instance:')).click()
        page.get_by_role('listbox', name='Target instance').get_by_role('option', name='target', exact=True).click()
        source_wf = page.get_by_label('Source workflow', exact=True)
        source_wf.focus()
        source_wf.fill('source-wf')
        page.get_by_role('listbox').get_by_role('option').filter(has_text='source-wf').click()
        page.get_by_role('button', name='Check changes', exact=True).click()
        page.get_by_text('Ready for deployment', exact=True).wait_for()
        count = len(state['writes'])
        state['source']['name'] = 'Changed after preflight'
        with page.expect_response(lambda response: response.url.endswith('/api/n8n_promote/deploy')) as rejected:
            page.get_by_role('button', name='Deploy inactive workflow', exact=True).click()
            # Deployment confirms in an in-app dialog naming both instances.
            page.get_by_role('dialog', name='Deploy inactive workflow').get_by_role('button', name='Deploy inactive', exact=True).click()
        assert rejected.value.status == 200
        assert rejected.value.json()['status'] == 'rejected'
        assert rejected.value.json()['error_code'] == 'stale_preview'
        assert len(state['writes']) == count, 'Stale preview must not write to target'
        page.evaluate("window.fixtureUUID = crypto.randomUUID; crypto.randomUUID = () => 'browser-replay-check'")
        with page.expect_response(lambda response: response.url.endswith('/api/n8n_promote/preflight')) as fresh:
            page.get_by_role('button', name='Check changes', exact=True).click()
        page.get_by_text('Ready for deployment', exact=True).wait_for()
        page.evaluate('crypto.randomUUID = window.fixtureUUID; delete window.fixtureUUID')
        with page.expect_response(lambda response: response.url.endswith('/api/n8n_promote/deploy')) as deployed:
            page.get_by_role('button', name='Deploy inactive workflow', exact=True).click()
            page.get_by_role('dialog', name='Deploy inactive workflow').get_by_role('button', name='Deploy inactive', exact=True).click()
        page.get_by_role('heading', name='Deployment result', exact=True).wait_for()
        assert deployed.value.status == 200
        assert deployed.value.json()['status'] == 'deployed'
        assert len(state['writes']) == count + 1
        assert state['target']['active'] is False
        assert state['target']['nodes'] == state['source']['nodes']
        assert state['writes'][-1] == ('target', 'POST', 'workflows')
        # Chromium does not expose the body of this streamed Request clone.
        replay_body = {'preview_id': fresh.value.json()['preview_id'], 'idempotency_key': 'browser-replay-check'}
        replay = page.evaluate('''async (body) => {
            const {api} = await import('/js/api.js');
            return await api('/api/n8n_promote/deploy', {method:'POST', json:body});
        }''', replay_body)
        assert replay['id'] == deployed.value.json()['id']
        assert len(state['writes']) == count + 1, 'Replay must not create another workflow'
        print('PASS: real browser/API/loopback n8n activation, CodeLab save/stale rejection, stale preview rejection, inactive cross-instance promotion and replay protection.', flush=True)
        exercise_approvals(page, ids['source'], state)
        exercise_fleet_search(page, ids, state)
    finally:
        page.remove_listener('dialog', accept)


def exercise_approvals(page, instance_id, state):
    """Create real durable proposals via the tool API; operate their browser cards."""
    page.evaluate("location.hash = 'assistant'")
    page.get_by_role('heading', name='AI workspace', exact=True).wait_for()

    def propose():
        count = len(state['writes'])
        result = page.evaluate('''async (instance) => {
            const {api} = await import('/js/api.js');
            return await api('/api/assistant/tools/run', {method:'POST', json:{
                name:'activate_workflow', arguments:{instance_id:instance, workflow_id:'source-wf'}}});
        }''', instance_id)
        assert len(state['writes']) == count, 'Proposal must not mutate n8n'
        open_review(page)
        page.get_by_role('button', name='Refresh proposals', exact=True).click()
        page.get_by_role('button', name='Confirm exact action', exact=True).wait_for()
        return result['pending_action']['id']

    def replay(action_id):
        return page.evaluate('''async (id) => {
            const {apiFetch} = await import('/js/api.js');
            const response = await apiFetch('/api/assistant/tools/confirm',
                {method:'POST',json:{action_id:id}});
            return response.status;
        }''', action_id)

    action_id = propose()
    page.reload(wait_until='domcontentloaded')
    page.get_by_role('heading', name='AI workspace', exact=True).wait_for()
    open_review(page)
    page.get_by_role('button', name='Confirm exact action', exact=True).wait_for()
    count = len(state['writes'])
    page.get_by_role('button', name='Cancel', exact=True).click()
    expect(page.get_by_role('button', name='Confirm exact action', exact=True)).to_have_count(0)
    expect(page.locator('.approval-card .badge').filter(has_text='cancelled')).to_have_count(1)
    assert replay(action_id) == 409
    assert len(state['writes']) == count

    action_id = propose()
    state['source']['name'] = 'Changed after approval proposal'
    page.get_by_role('button', name='Confirm exact action', exact=True).click()
    expect(page.get_by_role('button', name='Confirm exact action', exact=True)).to_have_count(0)
    assert len(state['writes']) == count, 'Stale approval must not execute'
    assert replay(action_id) == 409

    action_id = propose()
    page.get_by_role('button', name='Confirm exact action', exact=True).click()
    expect(page.locator('.approval-card .badge').filter(has_text='succeeded')).to_have_count(1)
    expect(page.get_by_role('button', name='Confirm exact action', exact=True)).to_have_count(0)
    assert state['source']['active'] is True
    assert len(state['writes']) == count + 1
    assert replay(action_id) == 409
    assert len(state['writes']) == count + 1
    # Leave the workspace as a person would: close Review so the page behind it is usable again.
    if page.locator('dialog.review-drawer[open]').count():
        page.keyboard.press('Escape')
        expect(page.locator('dialog.review-drawer[open]')).to_have_count(0)
    print('PASS: real durable assistant proposal cards, cancel, stale rejection, confirm and single-use replay protection; no model inference used.', flush=True)


def exercise_fleet_search(page, ids, state):
    state['target']['id'] = state['source']['id']
    state['source']['name'] = 'Source searchable'
    state['target']['name'] = 'Target searchable'
    state['pagination'] = True
    choose_instance(page, '')
    page.evaluate("location.hash = 'workflows'")
    source = page.get_by_role('region', name='Workflows on source', exact=True)
    target = page.get_by_role('region', name='Workflows on target', exact=True)
    expect(source.get_by_role('button', name='Source searchable on source', exact=True)).to_have_count(1)
    expect(target.get_by_role('button', name='Target searchable on target', exact=True)).to_have_count(1)
    # Results follow the typing: the field is the only search control.
    expect(page.get_by_role('button', name='Search', exact=True)).to_have_count(0)
    expect(page.get_by_label('Find workflows', exact=True)).to_be_enabled()
    target.get_by_role('button', name='Next workflow page on target', exact=True).click()
    expect(target.get_by_role('button', name='Second page workflow on target', exact=True)).to_have_count(1)
    expect(source.get_by_role('button', name='Source searchable on source', exact=True)).to_have_count(1)
    page.get_by_label('Find workflows', exact=True).fill('Target searchable')
    suggestions = page.get_by_role('listbox', name='Workflow suggestions')
    expect(suggestions.get_by_role('option')).to_have_count(1)
    expect(suggestions.get_by_role('option')).to_contain_text('target')
    page.get_by_label('Find workflows', exact=True).press('ArrowDown')
    expect(page.get_by_label('Find workflows', exact=True)).to_have_attribute('aria-activedescendant', 'workflow-suggestion-0')
    page.get_by_label('Find workflows', exact=True).press('Enter')
    expect(page.get_by_label('Find workflows', exact=True)).to_have_attribute('aria-expanded', 'false')
    expect(source.get_by_role('heading', name='No matching workflows', exact=True)).to_have_count(1)
    expect(target.get_by_role('button', name='Target searchable on target', exact=True)).to_have_count(1)
    before = len(state['writes'])
    workflow_action(page, target, 'Target searchable', 'target', 'Activate')
    # The dialog is attached to the document body, outside the target section.
    page.get_by_role('dialog', name='Activate workflow').get_by_role('button', name='Activate workflow', exact=True).click()
    expect_workflow_state(page, target, 'Target searchable', 'target', 'Active')
    assert state['writes'][before:] == [('target','POST','workflows/source-wf/activate')]
    workflow_action(page, target, 'Target searchable', 'target', 'Deactivate')
    page.get_by_role('dialog', name='Deactivate workflow').get_by_role('button', name='Deactivate workflow', exact=True).click()
    expect_workflow_state(page, target, 'Target searchable', 'target', 'Inactive')
    workflow_action(page, target, 'Target searchable', 'target', 'Open in CodeLab')
    assert 'instance_id=' + ids['target'] in page.url
    page.get_by_role('button', name='Load workflow', exact=True).click()
    # A link already loaded the workflow; this click reloads it. Edit only once that load has finished.
    expect(page.get_by_role('button', name='Load workflow', exact=True)).to_be_enabled()
    expect(page.get_by_role('textbox', name='Code editor', exact=True)).to_have_value('return [];')
    page.evaluate("location.hash = 'workflows'")
    expect(source.get_by_role('button', name='Source searchable on source', exact=True)).to_have_count(1)
    expect(page.get_by_label('Find workflows', exact=True)).to_be_enabled()
    checked_at = source.locator('.load-as-of time').get_attribute('datetime')
    state['unavailable'] = 'source'
    try:
        source.get_by_role('button', name='Refresh workflows on source', exact=True).click()
        # A 503 is a transient read failure: keep the previous rows and their timestamp,
        # offer recovery, and do not interrupt screen readers with an assertive alert.
        expect(source.locator('.coverage-partial.load-notice[role="status"]')).to_have_count(1)
        expect(source.get_by_role('alert')).to_have_count(0)
        expect(source.get_by_role('button', name='Source searchable on source', exact=True)).to_have_count(1)
        expect(source.locator('.load-as-of time')).to_have_attribute('datetime', checked_at)
        expect(source.get_by_role('button', name='Retry', exact=True)).to_be_enabled()
    finally:
        state.pop('unavailable', None)
    expect(target.get_by_role('button', name='Target searchable on target', exact=True)).to_have_count(1)

    # The n8n fixture's unavailable switch only emits 503. Intercept this one read
    # to exercise the distinct confirmed-failure contract without changing the server.
    workflow_route = '**/api/n8n_proxy/workflows?*'

    def confirmed_failure(route):
        instance = parse_qs(urlsplit(route.request.url).query).get('instance_id')
        if instance == [ids['source']]:
            route.fulfill(status=500, json={'code': 'fixture_confirmed_failure',
                                           'detail': 'Confirmed fixture failure.'})
        else:
            route.continue_()

    page.route(workflow_route, confirmed_failure)
    try:
        source.get_by_role('button', name='Retry', exact=True).click()
        expect(source.locator('.error.load-notice[role="alert"]')).to_have_count(1)
        expect(source.get_by_role('alert')).to_contain_text('Confirmed fixture failure.')
        expect(source.locator('.coverage-partial.load-notice')).to_have_count(0)
        expect(source.get_by_role('button', name='Source searchable on source', exact=True)).to_have_count(1)
        expect(source.locator('.load-as-of time')).to_have_attribute('datetime', checked_at)
        expect(source.get_by_role('button', name='Retry', exact=True)).to_be_enabled()
    finally:
        page.unroute(workflow_route, confirmed_failure)
    source.get_by_role('button', name='Retry', exact=True).click()
    expect(source.get_by_role('button', name='Refresh workflows on source', exact=True)).to_be_enabled()
    expect(source.locator('.load-notice')).to_have_count(0)
    expect(source.locator('.load-as-of time')).not_to_have_attribute('datetime', checked_at)
    expect(source.get_by_role('button', name='Source searchable on source', exact=True)).to_have_count(1)
    print('PASS: fleet-wide search, independent pagination, same-ID target isolation, CodeLab target links, partial failure and recovery.', flush=True)
    state['pagination'] = False
    page.evaluate("location.hash = 'dashboard'")
    page.wait_for_function("() => !document.querySelector('#main')?.textContent.includes('Loading ')")
    # Metric values are plain text named by their label, so screen readers hear the label first.
    total = page.get_by_role('article', name='Total workflows', exact=True)
    expect(total.locator('.metric-number')).to_have_text('2')
    executions = page.get_by_role('article', name='Runs · 24h', exact=True)
    expect(executions.locator('.metric-number')).to_have_text('0')
    print('PASS: dashboard aggregates both instances and shows real zero saved executions.', flush=True)
