"""Self-call guard URL normalization: prefix case and percent-encoding must not evade the block."""
from copy import deepcopy

import httpx

from test_assistant_repair_approvals import patch_arguments, read_context, run, workflow
from test_fleet import add, fleet  # noqa: F401  (fixtures)


async def _repair_fixture(fleet):
    fleet.instance = await add(fleet)
    fleet.workflow = workflow(active=False)

    async def read(request):
        return httpx.Response(200, json=deepcopy(fleet.workflow))

    fleet.replies[('alpha.example', 'GET', '/api/v1/workflows/wf')] = read
    return fleet


async def test_self_call_blocked_despite_prefix_case(fleet):
    await _repair_fixture(fleet)
    digest = (await read_context(fleet))['workflow']['content_hash']
    response = await run(fleet, 'propose_workflow_patch', patch_arguments(fleet, digest, [
        {'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'url',
         'value': 'http://localhost:5678/WEBHOOK/orders'}]))
    assert response.status_code == 422, response.text
    assert response.json()['code'] == 'self_call_blocked'


async def test_self_call_blocked_despite_percent_encoding(fleet):
    await _repair_fixture(fleet)
    digest = (await read_context(fleet))['workflow']['content_hash']
    response = await run(fleet, 'propose_workflow_patch', patch_arguments(fleet, digest, [
        {'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'url',
         'value': 'https://alpha.example/webhook/%6Frders'}]))
    assert response.status_code == 422, response.text
    assert response.json()['code'] == 'self_call_blocked'
