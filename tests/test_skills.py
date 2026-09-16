"""Bundled skill library: disk parsing, the boot sync, the read API and the get_skill tool."""
import json

from backend.modules.skills.loader import initialize, parse_skill, read_skills
from test_fleet import fleet, add  # noqa: F401
from test_assistant_chat import answer, call, configure  # noqa: F401

PROVIDER = ('local-model.example', 'POST', '/v1/chat/completions')


def write_skill(directory, name, body='# Body\n\nGuidance.', description='Describes the skill.'):
    folder = directory / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: {description}\n---\n\n{body}\n', encoding='utf-8')
    return folder


def test_parse_skill_splits_frontmatter_from_body():
    name, description, body = parse_skill('---\nname: n8n-code\ndescription: Write code.\n---\n\n# Title\n\nText.\n')
    assert (name, description) == ('n8n-code', 'Write code.')
    assert body.startswith('# Title') and 'description:' not in body


def test_parse_skill_keeps_the_body_when_frontmatter_is_missing():
    # A guide without frontmatter still loads: the body is what the model needs.
    name, description, body = parse_skill('# Just a title\n\nText.')
    assert (name, description) == ('', '')
    assert body.startswith('# Just a title')


def test_parse_skill_ignores_nested_keys():
    # Only top-level name/description are read; indented keys belong to other structures.
    name, description, _ = parse_skill('---\nname: outer\nmeta:\n  name: inner\ndescription: Real.\n---\n\nBody.')
    assert (name, description) == ('outer', 'Real.')


def test_read_skills_skips_unusable_folders(tmp_path):
    write_skill(tmp_path, 'good-skill')
    (tmp_path / 'no-markdown').mkdir()
    (tmp_path / 'loose-file.md').write_text('not a folder', encoding='utf-8')
    write_skill(tmp_path, 'Bad_Name')            # uppercase and underscore fail the id pattern
    write_skill(tmp_path, 'empty-body', body='')  # nothing for the model to read
    found = {skill['id'] for skill in read_skills(tmp_path)}
    assert found == {'good-skill'}


def test_read_skills_reads_the_shipped_library():
    # The library that ships with the application must actually parse.
    skills = read_skills()
    assert len(skills) >= 15
    assert {'n8n-code-javascript', 'n8n-error-handling'} <= {skill['id'] for skill in skills}
    assert all(skill['description'] and skill['content'] for skill in skills)
    assert not any(skill['content'].startswith('---') for skill in skills)


async def test_boot_sync_adds_updates_and_removes(fleet, tmp_path, monkeypatch):
    from backend.modules.skills import loader
    monkeypatch.setattr(loader, 'SKILLS_DIR', tmp_path)
    write_skill(tmp_path, 'first', body='Original guidance.')
    write_skill(tmp_path, 'second')
    await initialize(fleet.app)
    rows = await fleet.app.state.db.fetchall('SELECT id,content FROM skills ORDER BY id')
    assert [row['id'] for row in rows] == ['first', 'second']

    # Disk is the source of truth: an edited guide is replaced and a deleted folder drops its row.
    write_skill(tmp_path, 'first', body='Replaced guidance.')
    (tmp_path / 'second' / 'SKILL.md').unlink()
    (tmp_path / 'second').rmdir()
    await initialize(fleet.app)
    rows = await fleet.app.state.db.fetchall('SELECT id,content FROM skills ORDER BY id')
    assert [row['id'] for row in rows] == ['first']
    assert 'Replaced guidance.' in rows[0]['content'] and 'Original guidance.' not in rows[0]['content']


async def test_listing_omits_bodies_and_detail_returns_markdown(fleet):
    listing = await fleet.client.get('/api/skills')
    assert listing.status_code == 200
    rows = listing.json()['data']
    assert len(rows) >= 15
    # Bodies run to tens of kilobytes each; the list must stay a list.
    assert all('content' not in row for row in rows)
    assert all(row['description'] for row in rows)

    detail = await fleet.client.get('/api/skills/n8n-code-javascript')
    assert detail.status_code == 200
    body = detail.json()
    assert body['id'] == 'n8n-code-javascript' and len(body['content']) > 5000
    assert not body['content'].startswith('---')


async def test_unknown_and_malformed_skill_ids_are_refused(fleet):
    missing = await fleet.client.get('/api/skills/does-not-exist')
    assert missing.status_code == 404 and missing.json()['code'] == 'skill_not_found'
    # A path-like id never reaches the filesystem: the route pattern rejects it first.
    for identifier in ('..%2F..%2Fetc', 'UPPER', 'has space'):
        assert (await fleet.client.get(f'/api/skills/{identifier}')).status_code in (404, 422)


async def test_get_skill_tool_returns_the_guide_to_the_model(fleet):
    await configure(fleet)
    rounds = []

    async def provider(request):
        rounds.append(json.loads(request.content))
        if len(rounds) == 1:
            return answer('', [call('get_skill', {'skill_name': 'n8n-error-handling'})])
        return answer('Use the error output branch.')

    fleet.replies[PROVIDER] = provider
    result = await fleet.client.post('/api/assistant/chat', headers=fleet.headers,
                                     json={'provider_id': 'local', 'message': 'How do I handle node errors?'})
    assert result.status_code == 200, result.text
    assert len(rounds) == 2 and result.json()['content'] == 'Use the error output branch.'
    # The guide itself must reach the model, not merely a success flag.
    injected = [message for message in rounds[1]['messages'] if message['role'] == 'tool']
    assert len(injected) == 1
    payload = json.loads(injected[0]['content'])
    assert payload['skill'] == 'n8n-error-handling' and payload['truncated'] is False
    assert len(payload['content']) > 1000
    assert payload['content'].startswith('#')


async def test_unknown_guide_is_reported_instead_of_ending_the_conversation(fleet):
    """The model chooses this name itself, so a wrong guess must not fail the whole turn."""
    await configure(fleet)
    rounds = []

    async def provider(request):
        rounds.append(json.loads(request.content))
        if len(rounds) == 1:
            return answer('', [call('get_skill', {'skill_name': 'not-a-skill'})])
        return answer('That guide is not installed.')

    fleet.replies[PROVIDER] = provider
    result = await fleet.client.post('/api/assistant/chat', headers=fleet.headers,
                                     json={'provider_id': 'local', 'message': 'read a missing guide'})
    assert result.status_code == 200, result.text
    assert len(rounds) == 2 and result.json()['content'] == 'That guide is not installed.'
    # The model is told what it may ask for, so the next round can correct itself.
    injected = [message for message in rounds[1]['messages'] if message['role'] == 'tool']
    payload = json.loads(injected[0]['content'])
    assert payload['error']['code'] == 'skill_not_installed' and payload['skill'] == 'not-a-skill'
    assert 'n8n-code-javascript' in payload['available']
    # A missing guide is never an approval.
    assert result.json()['pending_actions'] == []
    assert await fleet.app.state.db.fetchall('SELECT * FROM approval_actions') == []


async def test_get_skill_is_a_read_and_never_creates_an_approval(fleet):
    await configure(fleet)
    fleet.replies[PROVIDER] = answer('', [call('get_skill', {'skill_name': 'n8n-code-javascript'})])
    direct = await fleet.client.post('/api/assistant/tools/run', headers=fleet.headers,
                                     json={'name': 'get_skill', 'arguments': {'skill_name': 'n8n-code-javascript'}})
    assert direct.status_code == 200, direct.text
    assert 'pending_action' not in direct.json()
    assert await fleet.app.state.db.fetchall('SELECT * FROM approval_actions') == []


async def test_a_name_the_model_invents_does_not_end_the_turn(fleet):
    """Regression: a real qwen2.5:3b asked for "Code" when the id is n8n-code-javascript.

    A schema rejection raises before the tool answers, and standard_chat does not catch tool errors,
    so a near-miss used to end the conversation with a 422 instead of letting the model correct itself.
    """
    await configure(fleet)
    rounds = []

    async def provider(request):
        rounds.append(json.loads(request.content))
        if len(rounds) == 1:
            return answer('', [call('get_skill', {'skill_name': 'Code'})])
        return answer('Return items, not item.')

    fleet.replies[PROVIDER] = provider
    result = await fleet.client.post('/api/assistant/chat', headers=fleet.headers,
                                     json={'provider_id': 'local', 'message': 'items is not defined'})
    assert result.status_code == 200, result.text
    assert len(rounds) == 2 and result.json()['content'] == 'Return items, not item.'
    payload = json.loads([m for m in rounds[1]['messages'] if m['role'] == 'tool'][0]['content'])
    assert payload['error']['code'] == 'skill_not_installed'
    assert 'n8n-code-javascript' in payload['available']


async def test_skill_names_resolve_regardless_of_case_and_spacing(fleet):
    """Models write the name their own way; the id they meant is what matters."""
    await configure(fleet)
    for written in ('n8n-code-javascript', 'N8N-Code-JavaScript', 'n8n code javascript', 'n8n_code_javascript'):
        result = await fleet.client.post('/api/assistant/tools/run', headers=fleet.headers,
                                         json={'name': 'get_skill', 'arguments': {'skill_name': written}})
        assert result.status_code == 200, f'{written!r}: {result.text}'
        assert result.json()['skill'] == 'n8n-code-javascript', written


async def test_get_skill_rejects_arguments_that_could_reach_the_filesystem(fleet):
    await configure(fleet)
    # A loose name is accepted so a near-miss can be answered, but nothing path-like ever is.
    for argument in ('../../etc/passwd', 'n8n/../../secret', '/etc/passwd', ''):
        result = await fleet.client.post('/api/assistant/tools/run', headers=fleet.headers,
                                         json={'name': 'get_skill', 'arguments': {'skill_name': argument}})
        assert result.status_code == 422, f'{argument!r} returned {result.status_code}'
        assert result.json()['code'] == 'invalid_tool_arguments'
        # The rejection names the field, never echoes the value.
        assert argument not in result.json()['detail'] or not argument
