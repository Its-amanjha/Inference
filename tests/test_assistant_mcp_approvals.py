import json

import httpx
import pytest

from test_fleet import fleet


@pytest.fixture
async def remote(fleet):
    fleet.methods = []
    fleet.initializations = 0
    fleet.change = None
    fleet.schema = {'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value'], 'additionalProperties': False}
    async def server(request):
        msg = json.loads(request.content)
        method = msg['method']
        fleet.methods.append(method)
        if method == 'initialize':
            fleet.initializations += 1
            if fleet.change == 'demote' and fleet.initializations == 3:
                await fleet.app.state.db.execute("UPDATE users SET role='operator' WHERE username='owner'")
            result = {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fixture', 'version': '1'}}
        elif method == 'notifications/initialized':
            return httpx.Response(202)
        elif method == 'tools/list':
            result = {'tools': [{'name': 'write', 'inputSchema': fleet.schema, 'annotations': {'readOnlyHint': True}}]}
        else:
            result = {'content': [{'type': 'text', 'text': 'done'}]}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': msg['id'], 'result': result})
    fleet.replies[('mcp.example', 'POST', '/mcp')] = server
    result = await fleet.client.put('/api/assistant/config/mcp', headers=fleet.headers, json={'id': 'remote', 'config': {'url': 'https://mcp.example/mcp'}})
    assert result.status_code == 200, result.text
    return fleet


async def proposal(remote, arguments=None):
    response = await remote.client.post('/api/assistant/tools/run', headers=remote.headers, json={'name': 'mcp_call',
        'arguments': {'server_id': 'remote', 'tool_name': 'write', 'arguments': arguments if arguments is not None else {'value': 'hello'}}})
    return response


async def test_readonly_annotation_still_requires_single_use_confirmation(remote):
    response = await proposal(remote)
    assert response.status_code == 200, response.text
    action = response.json()['pending_action']
    assert 'remote resource state' in action['warning']
    assert 'tools/call' not in remote.methods
    result = await remote.client.post('/api/assistant/tools/confirm', headers=remote.headers, json={'action_id': action['id']})
    assert result.status_code == 200, result.text
    assert remote.methods.count('tools/call') == 1
    result = await remote.client.post('/api/assistant/tools/confirm', headers=remote.headers, json={'action_id': action['id']})
    assert result.status_code == 409
    assert remote.methods.count('tools/call') == 1


async def test_admin_demotion_during_final_handshake_blocks_tool(remote):
    action = (await proposal(remote)).json()['pending_action']['id']
    remote.change = 'demote'
    result = await remote.client.post('/api/assistant/tools/confirm', headers=remote.headers, json={'action_id': action})
    assert result.status_code == 403, result.text
    assert 'tools/call' not in remote.methods


async def test_changed_tool_schema_or_invalid_arguments_block(remote):
    assert (await proposal(remote, {'value': 1})).status_code == 422
    action = (await proposal(remote)).json()['pending_action']['id']
    remote.schema['description'] = 'changed definition'
    result = await remote.client.post('/api/assistant/tools/confirm', headers=remote.headers, json={'action_id': action})
    assert result.status_code == 409, result.text
    assert 'tools/call' not in remote.methods
