import json
from copy import deepcopy
from pathlib import Path

import pytest

from backend.modules.assistant import repair_contract as contract
from backend.modules.assistant.repair_context import (MASK, TRUNCATED, WRITABLE_KEYS, build_context, content_hash, mask_text,
                                                      mask_workflow, scrub_error, sensitive_key, writable_document)

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'repair_proposal_v1.json').read_text(encoding='utf-8'))
SECRET = 'sk-fixture-secret'
ROW = {'id': 42, 'instance_id': 'source', 'workflow_id': 'wf-orders', 'workflow_name': 'Order synchronization',
       'execution_id': '4821', 'node_name': 'HTTP Request', 'error_type': 'ExecutionError',
       'error_message': 'Execution evidence confirms a node error.', 'count': 1, 'status': 'open'}


def workflow():
    return {
        'id': 'wf-orders', 'name': 'Order synchronization', 'active': True, 'isArchived': False,
        'versionId': '7c1e2d3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f', 'updatedAt': '2026-09-15T10:00:00.000Z',
        'pinData': {'Webhook': [{'json': {'pinned': 'pinned-private'}}]}, 'staticData': {'last': 'static-private'},
        'meta': {'instanceId': 'meta-private'}, 'tags': [{'id': 't1', 'name': 'tag-private'}], 'shared': [{'role': 'role-private'}],
        'nodes': [
            {'id': 'n1', 'name': 'Webhook', 'type': 'n8n-nodes-base.webhook', 'typeVersion': 2, 'position': [0, 0],
             'webhookId': 'hook-private-id', 'parameters': {'path': 'orders', 'httpMethod': 'POST'}},
            {'id': 'n2', 'name': 'HTTP Request', 'type': 'n8n-nodes-base.httpRequest', 'typeVersion': 4.2, 'position': [200, 0],
             'parameters': {'url': 'https://api.example.com/v1/orders',
                            'headerParameters': {'parameters': [{'name': 'Authorization', 'value': f'Bearer {SECRET}'}]}},
             'credentials': {'httpHeaderAuth': {'id': 'cred-private-id', 'name': 'credential-name-private'}}},
            {'id': 'n3', 'name': 'Code', 'type': 'n8n-nodes-base.code', 'typeVersion': 2, 'position': [400, 0],
             'parameters': {'jsCode': 'return item;'}},
        ],
        'connections': {'Webhook': {'main': [[{'node': 'HTTP Request', 'type': 'main', 'index': 0}]]}},
        'settings': {'executionOrder': 'v1'},
    }


def execution(message='Request failed with status code 404'):
    return {
        'id': '4821', 'status': 'error', 'finished': False, 'mode': 'webhook', 'workflowId': 'wf-orders', 'workflowData': workflow(),
        'data': {'startData': {}, 'resultData': {
            'lastNodeExecuted': 'HTTP Request',
            'error': {'name': 'NodeApiError', 'message': message, 'httpCode': '404', 'level': 'warning',
                      'stack': f'NodeApiError: {message}\n    at Authorization: Bearer {SECRET}',
                      'context': {'headers': {'Authorization': f'Bearer {SECRET}'}, 'itemIndex': 0},
                      'cause': {'config': {'headers': {'Authorization': f'Bearer {SECRET}'}}},
                      'node': workflow()['nodes'][1]},
            'runData': {
                'Webhook': [{'executionStatus': 'success', 'data': {'main': [[{'json': {'body': 'item-body-private'}}]]}}],
                'HTTP Request': [{'executionStatus': 'error', 'data': {'main': [[{'json': {'echo': SECRET}}]]},
                                  'error': {'name': 'NodeApiError', 'message': message, 'httpCode': '404',
                                            'stack': f'at Bearer {SECRET}', 'context': {'request': 'request-private'}}}]}},
            'executionData': {'nodeExecutionStack': [{'data': {'main': [[{'json': {'queued': SECRET}}]]}}]}},
    }


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


def test_webhook_directory_lists_other_active_triggers_without_parameters():
    from backend.modules.assistant.repair_context import attach_webhook_directory, webhook_endpoints
    service = {'id': 'wf-service', 'name': 'Order service', 'active': True, 'nodes': [
        {'name': 'Webhook', 'type': 'n8n-nodes-base.webhook',
         'parameters': {'path': 'order-status', 'httpMethod': 'get', 'options': {'secret': SECRET}},
         'credentials': {'httpBasicAuth': {'id': 'cred-private-id'}}},
        {'name': 'Reply', 'type': 'n8n-nodes-base.respondToWebhook', 'parameters': {}}]}
    paused = {**deepcopy(service), 'id': 'wf-paused', 'active': False}
    form = {'id': 'wf-form', 'name': 'Intake', 'active': True,
            'nodes': [{'name': 'Form', 'type': 'n8n-nodes-base.formTrigger', 'webhookId': 'intake-form', 'parameters': {}}]}
    endpoints = webhook_endpoints([service, paused, form, workflow(), 'junk'], exclude_workflow_id='wf-orders')
    assert endpoints == [
        {'workflow_id': 'wf-service', 'workflow_name': 'Order service', 'method': 'GET', 'route': '/webhook/order-status'},
        {'workflow_id': 'wf-form', 'workflow_name': 'Intake', 'method': 'GET', 'route': '/form/intake-form'}]
    assert SECRET not in dumps(endpoints) and 'cred-private-id' not in dumps(endpoints)
    context = build_context('source', workflow=workflow(), execution=None, error_row=None, send_error_text=True)
    attached = attach_webhook_directory(context, endpoints)
    contract.RepairContextResponse.model_validate(attached)
    assert attached['bytes'] == len(json.dumps(attached, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))
    assert attach_webhook_directory(context, []) is context


def test_content_hash_ignores_metadata_and_key_order_but_not_content():
    original = workflow()
    digest = content_hash(original)
    assert len(digest) == 64
    moved = deepcopy(original)
    moved.update(active=False, versionId='other', updatedAt='later', id='elsewhere', pinData={}, staticData=None, meta=None, tags=[])
    moved.pop('shared')
    moved['settings']['unlistedInternal'] = {'ignored': True}
    reordered = dict(reversed(list(moved.items())))
    reordered['nodes'] = [dict(reversed(list(node.items()))) for node in moved['nodes']]
    assert content_hash(moved) == content_hash(reordered) == digest
    for mutate in (lambda w: w['nodes'][2]['parameters'].update(jsCode='return items;'), lambda w: w['settings'].update(timezone='UTC'),
                   lambda w: w['settings'].update(callerPolicy='any'), lambda w: w.update(name='Renamed'),
                   lambda w: w['nodes'][1]['credentials']['httpHeaderAuth'].update(id='other')):
        changed = workflow()
        mutate(changed)
        assert content_hash(changed) != digest
    document = writable_document(original)
    assert tuple(document) == WRITABLE_KEYS
    document['nodes'][0]['parameters']['path'] = 'changed'
    assert original == workflow()


def test_mask_workflow_matches_fixture_context_and_does_not_mutate():
    source = workflow()
    context = mask_workflow(source)
    expected = FIXTURE['context']['workflow']
    contract.WorkflowContext.model_validate(context)
    assert source == workflow()
    assert context['content_hash'] == content_hash(source)
    assert {k: v for k, v in context.items() if k != 'content_hash'} == {k: v for k, v in expected.items() if k != 'content_hash'}


def test_dropped_fields_and_credential_identities_never_present():
    text = dumps(mask_workflow(workflow()))
    for private in ('pinned-private', 'static-private', 'meta-private', 'tag-private', 'role-private', 'cred-private-id',
                    'credential-name-private', 'hook-private-id', 'webhookId', 'position', 'updatedAt', 'isArchived', SECRET):
        assert private not in text


def test_sensitive_keys_pairs_and_value_patterns_are_masked_with_paths():
    source = workflow()
    source['nodes'].append({'name': 'Secrets', 'type': 'n8n-nodes-base.httpRequest', 'typeVersion': 4, 'parameters': {
        'apiKey': 'plain-value-123', 'options': {'password': 'hunter2', 'timeout': 30, 'accessToken': 42},
        'queryParameters': {'parameters': [{'name': 'X-API-Key', 'value': 'abc'}, {'name': 'page', 'value': '2'}]},
        'url': 'https://user:pa55word@example.com/path?api_key=AIzaFixtureKey123&page=2',
        'jsonBody': '={"token": "abc123def"}', 'sample': 'value eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmN',
        'note': 'hex 0123456789abcdef0123456789abcdef0123 end', 'token': '={{ $json.token }}',
        'authentication': 'genericCredentialType', 'genericAuthType': 'httpHeaderAuth', 'tokenUrl': 'https://auth.example.com/token'}})
    context = mask_workflow(source)
    parameters = context['nodes'][3]['parameters']
    assert parameters['apiKey'] == parameters['options']['password'] == parameters['options']['accessToken'] == MASK
    assert parameters['options']['timeout'] == 30
    assert parameters['queryParameters']['parameters'] == [{'name': 'X-API-Key', 'value': MASK}, {'name': 'page', 'value': '2'}]
    assert parameters['url'] == f'https://{MASK}@example.com/path?api_key={MASK}&page=2'
    assert parameters['jsonBody'] == f'={{"token": "{MASK}"}}'
    assert parameters['sample'] == f'value {MASK}' and parameters['note'] == f'hex {MASK} end'
    for kept in ('token', 'authentication', 'genericAuthType', 'tokenUrl'):
        assert parameters[kept] == source['nodes'][3]['parameters'][kept]
    assert set(context['redacted_paths']) == {'HTTP Request.parameters.headerParameters.parameters[0].value'} | {
        f'Secrets.parameters.{path}' for path in ('apiKey', 'options.password', 'options.accessToken',
                                                  'queryParameters.parameters[0].value', 'url', 'jsonBody', 'sample', 'note')}
    text = dumps(context)
    assert not any(secret in text for secret in ('hunter2', 'pa55word', 'AIzaFixtureKey123', 'abc123def', 'plain-value-123', SECRET))


@pytest.mark.parametrize('text,expected', [
    (f'Authorization: Bearer {SECRET}', f'Authorization: Bearer {MASK}'),
    ('Basic dXNlcjpwYXNz', f'Basic {MASK}'),
    ('Basic authentication failed', None),
    ('see https://bob:hunter2@db.example.com:5432/app', f'see https://{MASK}@db.example.com:5432/app'),
    ('GET /v1?token=abc&page=2', f'GET /v1?token={MASK}&page=2'),
    ('?key={{ $json.key }}', None),
    ('jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U', f'jwt {MASK}'),
    ('key AKfycbx9Q2mZ7Lp4Rt8Wv3Nk6Hj5Gd1Sa0Ue2Yo7Ib', f'key {MASK}'),
    ('id 7c1e2d3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f', None),
    ('path /api/v1/orders/processing/batch/items/archive', None),
    ('const token = "abc123secret";', f'const token = "{MASK}";'),
    ("headers: { Authorization: 'Bearer ' + token }", None),
])
def test_mask_text_patterns(text, expected):
    assert mask_text(text) == ((expected, True) if expected else (text, False))


@pytest.mark.parametrize('key,secret', [
    ('apiKey', True), ('X-API-Key', True), ('Authorization', True), ('client_secret', True), ('accessToken', True),
    ('password', True), ('Cookie', True), ('Ocp-Apim-Subscription-Key', True), ('authentication', False),
    ('genericAuthType', False), ('nodeCredentialType', False), ('tokenUrl', False), ('sendHeaders', False),
    ('url', False), ('body', False), ('jsonBody', False), ('key', False), ('maxTokens', False), ('tokenLimit', False),
])
def test_sensitive_key_heuristic(key, secret):
    assert sensitive_key(key) is secret


@pytest.mark.parametrize('text', [
    'a-' * 100000, 'k' * 200000, 'eyJa-' * 40000, '"' + 'k' * 200000 + '": "v"', '?key=' * 40000, 'https://' * 30000,
    'Bearer ' * 40000, '={{' + 'a}}{{' * 20000 + 'a}}x',
], ids=['hyphen-run', 'word-run', 'jwt-prefixes', 'quoted-key', 'query-keys', 'schemes', 'bearers', 'expression'])
def test_masking_stays_linear_on_adversarial_text(text):
    source = {'id': 'w', 'name': 'W', 'nodes': [{'name': 'N', 'type': 't', 'parameters': {'token': text, 'plain': text}}],
              'connections': {}, 'settings': {}}
    parameters = mask_workflow(source, max_bytes=10**7)['nodes'][0]['parameters']
    assert parameters['token'] == MASK and len(parameters['plain'].encode()) <= 4096


def test_string_and_code_caps_mark_truncated_paths():
    source = workflow()
    source['nodes'][1]['parameters']['body'] = 'word ' * 1000
    source['nodes'][1]['parameters']['accents'] = 'é' * 3000
    source['nodes'][2]['parameters']['jsCode'] = 'return 1;\n' * 2000
    source['nodes'][0]['parameters']['path'] = 'p' * 10
    context = mask_workflow(source, max_bytes=10**6)
    http, code = context['nodes'][1]['parameters'], context['nodes'][2]['parameters']
    assert http['body'].endswith(TRUNCATED) and len(http['body'].encode()) <= 4096
    assert http['accents'].endswith(TRUNCATED) and len(http['accents'].encode()) <= 4096
    assert code['jsCode'].endswith(TRUNCATED) and 4096 < len(code['jsCode'].encode()) <= 16 * 1024
    assert context['truncated_paths'] == ['HTTP Request.parameters.body', 'HTTP Request.parameters.accents', 'Code.parameters.jsCode']
    source['nodes'][2]['parameters']['jsCode'] = 'return 1;\n' * 1000
    assert 'Code.parameters.jsCode' not in mask_workflow(source, max_bytes=10**6)['truncated_paths']


def test_large_workflow_keeps_failing_focus_and_direct_neighbours_only():
    nodes = [{'name': f'Node {i}', 'type': 'n8n-nodes-base.set', 'typeVersion': 3, 'parameters': {'text': 'lorem ipsum ' * 300}}
             for i in range(30)]
    nodes[20] = {'name': 'Node 20', 'type': 'n8n-nodes-base.code', 'typeVersion': 2, 'parameters': {'jsCode': 'return items;'}}
    connections = {f'Node {i}': {'main': [[{'node': f'Node {i + 1}', 'type': 'main', 'index': 0}]]} for i in range(29)}
    source = {'id': 'big', 'name': 'Big', 'active': False, 'nodes': nodes, 'connections': connections, 'settings': {}}
    assert mask_workflow(source, max_bytes=10**6)['summarized_nodes'] == []
    context = mask_workflow(source, failing_node='Node 10', focus_nodes=['Node 3'])
    contract.WorkflowContext.model_validate(context)
    full = [node['name'] for node in context['nodes'] if 'parameters' in node]
    assert full == ['Node 2', 'Node 3', 'Node 4', 'Node 9', 'Node 10', 'Node 11']
    assert context['summarized_nodes'] == [f'Node {i}' for i in range(30) if f'Node {i}' not in full]
    assert context['nodes'][0] == {'name': 'Node 0', 'type': 'n8n-nodes-base.set'}
    assert 'Node 20.parameters.jsCode' in context['truncated_paths']
    assert context['connections'] == connections
    assert len(json.dumps(context).encode()) < len(json.dumps(mask_workflow(source, max_bytes=10**6)).encode())


def test_scrub_error_drops_payloads_and_masks_secrets():
    error = scrub_error(execution(f'Authorization failed: Bearer {SECRET}'), ROW, workflow(), send_error_text=True)
    contract.ErrorContext.model_validate(error)
    text = dumps(error)
    for private in (SECRET, 'stack', 'context', 'cause', 'item-body-private', 'request-private', 'itemIndex', 'credential', 'headers'):
        assert private not in text
    assert error == {'error_id': '42', 'execution_id': '4821', 'status': 'error', 'last_node': 'HTTP Request', 'name': 'NodeApiError',
                     'message': f'Authorization failed: Bearer {MASK}', 'description': None, 'http_code': '404',
                     'node_errors': [{'node': 'HTTP Request', 'message': f'Authorization failed: Bearer {MASK}'}],
                     'saved_vs_failed_run': {'changed': False}, 'detail': 'available'}


def test_scrub_error_caps_messages_and_reports_saved_workflow_changes():
    run = execution('x ' * 2000)
    run['data']['resultData']['error']['description'] = 'y ' * 3000
    saved = workflow()
    saved.update(active=False, versionId='other')
    assert scrub_error(run, None, saved, send_error_text=True)['saved_vs_failed_run'] == {'changed': False}
    saved['nodes'][2]['parameters']['jsCode'] = 'return items;'
    saved['nodes'].append({'name': 'Added', 'type': 'n8n-nodes-base.noOp', 'parameters': {}})
    error = scrub_error(run, None, saved, send_error_text=True)
    assert error['saved_vs_failed_run'] == {'changed': True, 'nodes_changed': ['Added', 'Code']}
    assert len(error['message'].encode()) <= 1024 and len(error['description'].encode()) <= 2048
    assert error['error_id'] is None
    run.pop('workflowData')
    assert scrub_error(run, None, saved, send_error_text=True)['saved_vs_failed_run'] == {}


def test_error_text_withheld_keeps_structure_only():
    error = scrub_error(execution(f'Bearer {SECRET} rejected'), ROW, workflow(), send_error_text=False)
    contract.ErrorContext.model_validate(error)
    assert error['detail'] == 'withheld'
    assert error['message'] is None and error['description'] is None and error['node_errors'] == []
    assert (error['last_node'], error['http_code'], error['name'], error['status']) == ('HTTP Request', '404', 'NodeApiError', 'error')
    assert SECRET not in dumps(error) and 'rejected' not in dumps(error)


@pytest.mark.parametrize('run', [
    None,
    {'id': '4821', 'status': 'error', 'data': {'resultData': {'runData': {'private': 'payload'}}}},
    {'id': '4821', 'status': 'error', 'data': {'resultData': {'runData': {f'Node {i}': [] for i in range(2001)}}}},
])
def test_missing_malformed_or_oversize_execution_is_unavailable(run):
    error = scrub_error(run, ROW, workflow(), send_error_text=True)
    contract.ErrorContext.model_validate(error)
    assert error['detail'] == 'unavailable'
    assert (error['execution_id'], error['last_node'], error['name']) == ('4821', 'HTTP Request', 'ExecutionError')
    assert error['message'] == ROW['error_message'] and error['node_errors'] == [] and error['saved_vs_failed_run'] == {}
    assert 'payload' not in dumps(error)
    assert scrub_error(run, None, None, send_error_text=True)['error_id'] is None


def test_build_context_matches_fixture_and_counts_its_own_bytes():
    response = build_context('source', workflow=workflow(), execution=execution(), error_row=ROW, send_error_text=True)
    contract.RepairContextResponse.model_validate(response)
    contract.RepairContextResponse.model_validate(FIXTURE['context'])  # Persisted v1 remains readable.
    expected = {**FIXTURE['context'], 'contract': contract.CONTRACT_VERSION}
    assert set(response) == set(expected)
    assert response['bytes'] == len(json.dumps(response, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    assert {k: response[k] for k in ('contract', 'trust', 'instance_id', 'error', 'redaction')} == {
        k: expected[k] for k in ('contract', 'trust', 'instance_id', 'error', 'redaction')}
    assert {**response['workflow'], 'content_hash': None} == {**expected['workflow'], 'content_hash': None}
    assert SECRET not in dumps(response)
    masked = build_context('source', workflow=workflow(), execution=execution(f'Bearer {SECRET}'), error_row=ROW, send_error_text=True)
    assert masked['redaction'] == {'applied': True, 'count': 3}  # header value, top-level message, node error message
    empty = build_context('source', workflow=None, execution=None, error_row=None, send_error_text=True)
    contract.RepairContextResponse.model_validate(empty)
    assert (empty['workflow'], empty['error'], empty['redaction']) == (None, None, {'applied': False, 'count': 0})


def test_build_context_focuses_large_workflow_on_the_failing_node():
    source = workflow()
    for i in range(20):
        source['nodes'].append({'name': f'Filler {i}', 'type': 'n8n-nodes-base.set', 'parameters': {'text': 'lorem ipsum ' * 400}})
    response = build_context('source', workflow=source, execution=execution(), error_row=None, send_error_text=False)
    contract.RepairContextResponse.model_validate(response)
    full = {node['name'] for node in response['workflow']['nodes'] if 'parameters' in node}
    assert full == {'Webhook', 'HTTP Request'}
    assert response['error']['detail'] == 'withheld'


def test_fixture_samples_validate_against_contract():
    contract.RepairContextResponse.model_validate(FIXTURE['context'])
