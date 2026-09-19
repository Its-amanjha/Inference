import json

import httpx
import pytest
from fastapi import HTTPException, Request

from backend.auth_gate import authenticate
from backend.modules.assistant import tools
from test_fleet import fleet, add
from test_codelab import lab


async def actor(fleet):
    request = Request({'type': 'http', 'app': fleet.app, 'method': 'POST',
                       'path': '/api/assistant', 'headers': [(b'cookie',
                       f"inference_session={fleet.client.cookies['inference_session']}".encode())]})
    request.state.user = await authenticate(request)
    return request


HASH = '0' * 64


@pytest.mark.parametrize('name,args', [
    ('unknown', {}), ('activate_workflow', {'instance_id': 'a', 'workflow_id': '../x'}),
    ('list_workflows', {'instance_id': 'a', 'limit': 26}),
    ('list_workflows', {'instance_id': 'a', 'limit': '2'}),
    ('update_node', {'instance_id': 'a', 'workflow_id': 'w', 'node_id': 'n', 'code': '', 'expected_hash': 'x'}),
    ('delete_execution', {'execution_id': 'e'}),
    ('get_error_context', {'instance_id': 'a'}),
    ('get_error_context', {'instance_id': 'a', 'error_id': '1', 'execution_id': 'e'}),
    ('get_workflow_context', {'instance_id': 'a', 'workflow_id': 'w', 'focus_nodes': 'Code'}),
    ('propose_restore_workflow', {'instance_id': 'a', 'workflow_id': 'w', 'base_content_hash': HASH,
                                  'repair_id': 'r', 'version_id': '7c1e2d3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f'}),
    ('propose_workflow_patch', {'instance_id': 'a', 'workflow_id': 'w', 'base_content_hash': HASH, 'summary': 's', 'operations': []}),
    ('propose_workflow_patch', {'instance_id': 'a', 'workflow_id': 'w', 'base_content_hash': HASH, 'summary': 's',
                                'operations': [{'op': 'set_parameter', 'node': 'N', 'path': 'credentials.id', 'value': 'x'}]}),
    ('propose_workflow_patch', {'instance_id': 'a', 'workflow_id': 'w', 'base_content_hash': HASH, 'summary': 's',
                                'operations': [{'op': 'set_parameter', 'node': 'N', 'path': 'url', 'value': '[redacted:secret]'}]}),
])
def test_strict_arguments(name, args):
    with pytest.raises(HTTPException):
        tools.validate(name, args)


def test_declarations_follow_roles():
    names = lambda role, **scope: {item['function']['name'] for item in tools.declarations(role, **scope)}
    # get_skill reads bundled local guides: viewer level, no instance argument, never a mutation.
    assert names('viewer') == {'list_workflows', 'get_execution', 'list_errors', 'get_workflow_context', 'get_error_context', 'get_skill'}
    assert names('operator') == names('viewer') | tools.MUTATIONS
    assert {'propose_workflow_patch', 'propose_restore_workflow'} <= tools.MUTATIONS
    assert names('invalid') == set()
    assert tools.role('update_node') == 'operator'
    assert tools.role('propose_workflow_patch') == tools.role('propose_restore_workflow') == 'operator'
    assert tools.role('get_workflow_context') == tools.role('get_error_context') == 'viewer'
    assert tools.role('get_skill') == 'viewer' and 'get_skill' not in tools.MUTATIONS
    # Skills are available in both scopes: repair is exactly where n8n code guidance is needed.
    assert names('operator', scope='standard') == {'list_workflows', 'get_execution', 'list_errors', 'activate_workflow',
                                                   'deactivate_workflow', 'delete_execution', 'update_node', 'get_skill'}
    assert names('operator', scope='repair') == {'get_workflow_context', 'get_error_context', 'propose_workflow_patch',
                                                 'propose_restore_workflow', 'activate_workflow', 'deactivate_workflow', 'get_skill',
                                                 'propose_create_workflow', 'propose_retry_execution'}
    assert names('viewer', scope='repair') == {'get_workflow_context', 'get_error_context', 'get_skill'}


def test_declarations_are_flattened_for_every_provider():
    declared = tools.declarations('admin')
    text = json.dumps(declared)
    for marker in ('$ref', '$defs', 'discriminator', '"title"'):
        assert marker not in text
    patch = next(item for item in declared if item['function']['name'] == 'propose_workflow_patch')
    operation = patch['function']['parameters']['properties']['operations']['items']
    assert operation['type'] == 'object' and operation['required'] == ['op'] and operation['additionalProperties'] is False
    assert set(operation['properties']['op']['enum']) == {'set_parameter', 'remove_parameter', 'set_code', 'add_node', 'remove_node',
                                                          'set_disabled', 'connect', 'disconnect', 'set_setting'}
    node_types = {option.get('type') for option in operation['properties']['node']['anyOf']}
    assert node_types == {'string', 'object'}  # a node name, or a new node for add_node
    restore = next(item for item in declared if item['function']['name'] == 'propose_restore_workflow')
    assert restore['function']['parameters']['properties']['repair_id'] == {'pattern': '^[A-Za-z0-9_-]{1,128}$', 'type': 'string'}


async def test_reads_are_bounded_redacted_and_instance_specific(fleet):
    alpha = await add(fleet)
    beta = await add(fleet, 'Beta', 'https://beta.example', 'beta-secret')
    for host in ('alpha.example', 'beta.example'):
        fleet.replies[(host, 'GET', '/api/v1/workflows')] = httpx.Response(200, json={
            'data': [{'id': str(i), 'name': host, 'active': False, 'nodes': [{'code': 'PRIVATE'}],
                      'credentials': 'PRIVATE'} for i in range(40)]})
    request = await actor(fleet)
    for instance, host in ((alpha, 'alpha.example'), (beta, 'beta.example')):
        result = await tools.execute(request, 'list_workflows', {'instance_id': instance})
        assert len(result['data']) == 25
        assert result['data'][0] == {'id': '0', 'name': host, 'active': False}
        assert 'PRIVATE' not in json.dumps(result)
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions/same')] = httpx.Response(200, json={
        'id': 'same', 'status': 'error', 'data': {'password': 'PRIVATE'}, 'error': 'PRIVATE'})
    result = await tools.execute(request, 'get_execution', {'instance_id': alpha, 'execution_id': 'same'})
    assert 'PRIVATE' not in json.dumps(result)
    assert fleet.calls[-1].url.params['includeData'] == 'false'
    await fleet.app.state.db.execute("INSERT INTO errors(instance_id,workflow_id,error_message) VALUES(?,?,?)", (alpha,'w','PRIVATE'))
    assert 'PRIVATE' not in json.dumps(await tools.execute(request, 'list_errors', {'instance_id': alpha}))
    assert (await tools.execute(request, 'list_errors', {'instance_id': beta}))['data'] == []


async def test_fingerprint_binds_connection_and_complete_resource(lab):
    request = await actor(lab)
    args = {'instance_id': lab.instance, 'workflow_id': 'same', 'node_id': 'code', 'code': 'return [];'}
    initial = await tools.fingerprint(request, 'update_node', args)
    lab.workflow['tags'].append({'id': 'changed'})
    changed = await tools.fingerprint(request, 'update_node', args)
    assert initial != changed
    await lab.app.state.db.execute('UPDATE instances SET api_key_enc=? WHERE id=?', (lab.app.state.vault.encrypt('rotated'), lab.instance))
    assert changed != await tools.fingerprint(request, 'update_node', args)
    lab.workflow['active'] = True
    with pytest.raises(HTTPException, match='deactivate'):
        await tools.fingerprint(request, 'update_node', args)


async def test_execution_reuses_code_service_and_redacts_result(lab):
    request = await actor(lab)
    result = await tools.execute(request, 'update_node', {'instance_id': lab.instance, 'workflow_id': 'same', 'node_id': 'code', 'code': 'PRIVATE'})
    assert lab.workflow['nodes'][0]['parameters']['jsCode'] == 'PRIVATE'
    assert 'PRIVATE' not in json.dumps(result)
    assert await lab.app.state.db.fetchone("SELECT id FROM audit_events WHERE action='fleet.node_updated' AND outcome='success'")


@pytest.mark.parametrize('name,args', [('activate_workflow', {'workflow_id': 'same'}), ('list_errors', {})])
async def test_live_authorization_even_for_local_reads(fleet, name, args):
    instance = await add(fleet)
    request = await actor(fleet)
    await fleet.app.state.db.execute('UPDATE users SET disabled=1 WHERE username=?', ('owner',))
    with pytest.raises(HTTPException) as exc:
        await tools.execute(request, name, {'instance_id': instance, **args})
    assert exc.value.status_code == 401
    assert fleet.calls == []


@pytest.mark.parametrize('name,resource,method,path', [
    ('activate_workflow', 'workflow_id', 'POST', '/api/v1/workflows/same/activate'),
    ('deactivate_workflow', 'workflow_id', 'POST', '/api/v1/workflows/same/deactivate'),
    ('delete_execution', 'execution_id', 'DELETE', '/api/v1/executions/same'),
])
async def test_mutations_target_requested_instance_and_obey_live_role(fleet, name, resource, method, path):
    await add(fleet)
    beta = await add(fleet, 'Beta', 'https://beta.example', 'beta-secret')
    request = await actor(fleet)
    args = {'instance_id': beta, resource: 'same'}
    result = await tools.execute(request, name, args)
    assert result['ok'] is True
    assert fleet.calls[-1].url.host == 'beta.example'
    assert fleet.calls[-1].url.path == path
    assert fleet.calls[-1].method == method
    await fleet.app.state.db.execute("UPDATE users SET role='viewer' WHERE username='owner'")
    previous = len(fleet.calls)
    with pytest.raises(HTTPException) as exc:
        await tools.execute(request, name, args)
    assert exc.value.status_code == 403
    assert len(fleet.calls) == previous


async def test_delete_fingerprint_tracks_execution_payload_without_returning_it(fleet):
    instance = await add(fleet)
    request = await actor(fleet)
    args = {'instance_id': instance, 'execution_id': 'same'}
    first = await tools.fingerprint(request, 'delete_execution', args)
    fleet.replies[('alpha.example', 'GET', '/api/v1/executions/same')] = httpx.Response(200, json={
        'id': 'same', 'status': 'error', 'data': {'resultData': {'runData': {'private': 'payload'}}}})
    second = await tools.fingerprint(request, 'delete_execution', args)
    assert first != second
    assert len(second) == 64 and 'payload' not in second


async def test_unsupported_code_target_cannot_be_proposed(lab):
    request = await actor(lab)
    lab.workflow['nodes'][0]['type'] = 'n8n-nodes-base.httpRequest'
    with pytest.raises(HTTPException) as exc:
        await tools.fingerprint(request, 'update_node', {'instance_id': lab.instance,
            'workflow_id': 'same', 'node_id': 'code', 'code': ''})
    assert exc.value.detail['code'] == 'unsupported_node'
    assert not any(call.method == 'PUT' for call in lab.calls)


async def test_code_update_rechecks_approved_snapshot_before_adopting_hash(lab):
    request = await actor(lab)
    args = {'instance_id': lab.instance, 'workflow_id': 'same', 'node_id': 'code', 'code': 'new code'}
    approved = await tools.fingerprint(request, 'update_node', args)
    lab.workflow['nodes'][0]['parameters']['jsCode'] = 'changed remotely'
    with pytest.raises(HTTPException) as exc:
        await tools.execute(request, 'update_node', args, expected_fingerprint=approved)
    assert exc.value.detail['code'] == 'approval_stale'
    assert not any(call.method == 'PUT' for call in lab.calls)


@pytest.mark.parametrize('name,path,args', [
    ('list_workflows', '/api/v1/workflows', {}),
    ('get_execution', '/api/v1/executions/same', {'execution_id': 'same'}),
])
async def test_read_revocation_while_upstream_in_flight(fleet, name, path, args):
    instance = await add(fleet)
    request = await actor(fleet)
    async def revoked_response(outbound):
        await fleet.app.state.db.execute("UPDATE users SET disabled=1 WHERE username='owner'")
        return httpx.Response(200, json={'data': [{'id': 'sensitive'}], 'id': 'sensitive'})
    fleet.replies[('alpha.example', 'GET', path)] = revoked_response
    with pytest.raises(HTTPException) as exc:
        await tools.execute(request, name, {'instance_id': instance, **args})
    assert exc.value.status_code == 401
