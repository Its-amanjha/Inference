"""Ask mode: a question can never become an approval, whatever the model does."""
import json

from backend.modules.assistant import tools
from test_fleet import fleet, add
from test_assistant_chat import answer, call, configure
from test_assistant_repair_approvals import repair, remote_writes
from test_assistant_repair_chat import workspace, target, patch, tool_call, chat

PROVIDER = ('local-model.example', 'POST', '/v1/chat/completions')


def offered(request_body):
    return {tool['function']['name'] for tool in request_body.get('tools', [])}


def test_declarations_without_changes_offer_no_mutation():
    for scope in ('all', 'standard', 'repair'):
        names = {d['function']['name'] for d in tools.declarations('admin', scope=scope, allow_changes=False)}
        assert not names & tools.MUTATIONS, scope
    # Reads stay available, so the model can still look before it answers.
    assert 'get_workflow_context' in {d['function']['name'] for d in tools.declarations('admin', scope='repair', allow_changes=False)}


async def test_standard_ask_answers_in_text_with_no_mutation_offered(fleet):
    await add(fleet)
    await configure(fleet)
    rounds = []

    async def provider(request):
        rounds.append(json.loads(request.content))
        return answer('It is inactive because nobody activated it.')
    fleet.replies[PROVIDER] = provider
    result = await fleet.client.post('/api/assistant/chat', headers=fleet.headers,
                                     json={'provider_id': 'local', 'message': 'why is it inactive?', 'allow_changes': False})
    assert result.status_code == 200, result.text
    assert result.json()['pending_actions'] == []
    assert result.json()['content'] == 'It is inactive because nobody activated it.'
    assert not offered(rounds[0]) & tools.MUTATIONS and 'mcp_call' not in offered(rounds[0])
    assert 'Answer in plain text' in rounds[0]['messages'][0]['content']


async def test_standard_ask_refuses_a_mutation_the_model_was_not_offered(fleet):
    instance = await add(fleet)
    await configure(fleet)
    # A model ignoring its instructions and calling a tool it was not given.
    fleet.replies[PROVIDER] = answer('', [call('activate_workflow', {'instance_id': instance, 'workflow_id': 'same'})])
    result = await fleet.client.post('/api/assistant/chat', headers=fleet.headers,
                                     json={'provider_id': 'local', 'message': 'why is it inactive?', 'allow_changes': False})
    assert result.status_code == 502 and result.json()['code'] == 'provider_invalid_response'
    assert (await fleet.app.state.db.fetchone('SELECT COUNT(*) AS n FROM approval_actions'))['n'] == 0
    assert not any(c.method == 'POST' and c.url.host == 'alpha.example' for c in fleet.calls)


async def test_repair_ask_offers_no_proposal_and_refuses_one_anyway(workspace):
    workspace.script = [answer('', [tool_call('propose_workflow_patch', patch(workspace))])]
    response = await chat(workspace, message='was just testing', context=target(workspace), allow_changes=False)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['pending_actions'] == []
    assert data['steps'][-1]['code'] == 'provider_invalid_response'
    sent = workspace.provider[0]
    assert not offered(sent) & tools.MUTATIONS
    assert 'Answer in plain text' in sent['messages'][0]['content']
    assert (await workspace.app.state.db.fetchone('SELECT COUNT(*) AS n FROM approval_actions'))['n'] == 0
    assert not remote_writes(workspace)


async def test_repair_propose_mode_still_proposes(workspace):
    workspace.script = [answer('', [tool_call('propose_workflow_patch', patch(workspace))])]
    response = await chat(workspace, message='Fix the 404', context=target(workspace), allow_changes=True)
    assert response.status_code == 200, response.text
    assert len(response.json()['pending_actions']) == 1
    assert 'propose_workflow_patch' in offered(workspace.provider[0])
