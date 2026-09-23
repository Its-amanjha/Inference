"""Browser AI repair checks (round 1, scope A: repair and undo) against the loopback n8n simulator.

Runs inside the fleet fixture after the approval and fleet-search scenarios. The model is a scripted
OpenAI-compatible endpoint on the simulator: no real inference, accounts or data. Dashboard metrics are
served from a fixture here because the fleet aggregate check owns the real 4/minute budget.
"""
import json
import re
import secrets
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout, expect

from frontend_fleet_fixture import (REPAIR_ERROR_MESSAGE, REPAIR_EXECUTION_ID, REPAIR_SECRET, REPAIR_WORKFLOW_ID,
                                    enable_repair, queue_provider)

# Every UI name and selector this check depends on; reconcile frontend changes here only.
UI = {
    'heading': 'AI workspace',
    'workspace_tab': 'AI workspace',
    'code_tab': 'Code editor (manual)',
    'target': 'div.repair-target[role=group][aria-label="Repair target"]',
    'context': 'details.repair-context',
    'context_summary': 'What the AI sees',
    'send_error_text': 'Send scrubbed error text to the AI',
    'card': 'article.proposal-card',
    'card_id': 'article.proposal-card[data-action-id="{}"]',
    'status': '.proposal-status',
    'preconditions': '.proposal-preconditions',
    'changes': '.proposal-changes',
    'risks': '.proposal-risks',
    'model_summary': 'AI description (unverified)',
    'apply': 'Apply change',
    'restore': 'Restore version',
    'cancel': 'Cancel',
    'deactivate': 'Deactivate workflow',
    'reread': 'Ask AI to re-read',
    'reconcile': 'Check current state',
    'undo': 'Undo this change',
    'activate': 'Activate workflow',
    'stale': 'Nothing was written',
    'continue': 'Continue',
    'try_again': 'Try again',
    'rate_limited': 'Provider rate limited; wait about a minute, then try again.',
    'cut_off': "The model's answer was cut off. Nothing was proposed or applied.",
    'repair_link': 'Repair with AI',
    'badge': 'span.tool-calling-badge',
    'badge_untested': 'Supports repair: untested',
    'badge_yes': 'Supports repair: yes',
    'check_tool_calling': 'Check tool calling',
    'admin_error_text': 'Allow error text in repair conversations',
    'admin_webhook_directory': 'Share other workflows’ trigger addresses with the AI',
    'settings_button': 'Assistant settings',
    'message': 'Your message',
    'send': 'Send message',
    'refresh_proposals': 'Refresh proposals',
    'dialog': 'dialog[open]',
    'dialog_confirm': '.confirm-actions button',
    # Cards expose the raw approval status in data-status; badge labels ("Applied", "Outcome unknown") are prose.
    'status_attribute': 'data-status',
    'status_succeeded': 'succeeded',
    'status_cancelled': 'cancelled',
    'status_uncertain': 'uncertain',
    'status_stale': 'stale',
}
PROVIDER_ID = 'scripted-repair'
V1_URL = 'https://api.example.com/v1/orders'
V2_URL = 'https://api.example.com/v2/orders'
V3_URL = 'https://api.example.com/v3/orders'
CHAT_PATH = '/api/assistant/chat'
CONFIRM_PATH = '/api/assistant/tools/confirm'
CONTEXT_PATH = '/api/assistant/repair/context'
DEACTIVATE_WRITE = ('source', 'POST', f'workflows/{REPAIR_WORKFLOW_ID}/deactivate')
PUT_WRITE = ('source', 'PUT', f'workflows/{REPAIR_WORKFLOW_ID}')



def close_settings(page):
    """Settings open in a modal drawer over the workspace; close it once a check is done with it."""
    drawer = page.locator('dialog.settings-drawer[open]')
    if drawer.count():
        page.keyboard.press('Escape')
        expect(drawer).to_have_count(0)

def call_api(page, path, method='GET', body=None):
    """Same-origin call through the app's CSRF-aware fetch; returns (status, JSON or None). Never prints bodies."""
    return page.evaluate('''async ([path, method, body]) => {
        const {apiFetch} = await import('/js/api.js');
        const response = await apiFetch(path, body === null ? {method} : {method, json: body});
        let data = null;
        try { data = await response.json(); } catch { /* Non-JSON responses are reported by status only. */ }
        return [response.status, data];
    }''', [path, method, body])


def ok(page, path, method='GET', body=None):
    status, data = call_api(page, path, method, body)
    assert 200 <= status < 300, f'{method} {urlsplit(path).path}: HTTP {status} ({(data or {}).get("code")})'
    return data


def wait_until(page, predicate, what, timeout=30.0):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f'Timed out waiting for {what}')
        page.wait_for_timeout(100)


def response_json(response):
    try:
        data = response.json()
    except (PlaywrightError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_chat(response):
    return urlsplit(response.url).path == CHAT_PATH and response.request.method == 'POST'


def is_confirm(response):
    return urlsplit(response.url).path == CONFIRM_PATH and response.request.method == 'POST'


class NetworkLog:
    """Records browser traffic in event handlers; bodies are read afterwards, outside the handlers."""

    def __init__(self, page):
        self.page, self.requests, self.responses, self.routes = page, [], [], []
        # Playwright listeners must be Python functions or bound methods, not builtins such as list.append.
        # Only bodies of fully finished requests are read: an aborted or still-streaming response never resolves body().
        self.finished = set()
        page.on('request', self._request)
        page.on('response', self._response)
        page.on('requestfinished', self._finished)

    def _request(self, request):
        self.requests.append(request)

    def _finished(self, request):
        self.finished.add(request)

    def _response(self, response):
        # Frame URLs are tracked locally by Playwright (hash changes included), so this adds no round trip.
        try:
            route = urlsplit(response.frame.url).fragment.split('?')[0]
        except PlaywrightError:
            route = ''
        self.responses.append(response)
        self.routes.append(route)

    def close(self):
        self.page.remove_listener('request', self._request)
        self.page.remove_listener('response', self._response)
        self.page.remove_listener('requestfinished', self._finished)

    def after(self, start, predicate):
        return [response for response in self.responses[start:] if predicate(response)]

    def wait(self, start, predicate, what, timeout=30.0):
        return wait_until(self.page, lambda: next(iter(self.after(start, predicate)), None), what, timeout)

    def assert_clean(self, secret):
        """No secret in API request bodies or readable API responses, in any view.

        The workflow proxy masks secret parameter values, so fleet views no longer receive them either.
        """
        unreadable = 0
        for request in self.requests:
            try:
                sent = request.post_data or ''
            except (PlaywrightError, UnicodeError):
                sent = ''
            assert secret not in sent, f'Secret sent by the browser to {urlsplit(request.url).path}'
        for response, route in zip(self.responses, self.routes):
            path = urlsplit(response.url).path
            if not path.startswith('/api/'):
                continue
            if response.request not in self.finished:
                unreadable += 1
                continue
            try:
                body = response.body().decode('utf-8', 'replace')
            except PlaywrightError:
                unreadable += 1
                continue
            assert secret not in body, f'Secret returned to the browser by {path} (view #{route or "dashboard"})'
        return unreadable


def assert_page_clean(page, where):
    assert REPAIR_SECRET not in page.content(), f'Secret rendered in the DOM ({where})'
    assert not page.evaluate("secret => [...document.querySelectorAll('input,textarea')].some(node => node.value.includes(secret))",
                             REPAIR_SECRET), f'Secret placed in a form value ({where})'


def provider_count(state):
    return len(state['provider']['requests'])


def card(page, action_id):
    return page.locator(UI['card_id'].format(action_id))


def control(scope, name):
    return scope.get_by_role('button', name=name, exact=True).first


def confirm_dialog(dialog):
    """Confirm a lib/confirm.js dialog, typing the requested text when it asks for one."""
    typed = dialog.locator('input[type=text]')
    if typed.count():
        match = re.search(r'Type (.+) to confirm', dialog.inner_text())
        assert match, 'Typed confirmation dialog without instructions'
        typed.first.fill(match.group(1))
    dialog.locator(UI['dialog_confirm']).last.click()


def act(page, log, scope, name, predicate, what, timeout=60.0):
    """Click an enabled action, confirm its dialog if one opens (once), and return the first matching response."""
    start = len(log.responses)
    button = control(scope, name)
    expect(button).to_be_enabled()
    button.click()
    dialog = page.locator(UI['dialog'])
    confirmed = []

    def done():
        found = log.after(start, predicate)
        if found:
            return found[0]
        if not confirmed and dialog.count() and dialog.first.is_visible():
            confirmed.append(True)
            confirm_dialog(dialog.first)
        return None
    return wait_until(page, done, what, timeout)


def send_chat(page, log, state, text, requests=1, propose=True):
    """Send one message as the human; assert exactly `requests` provider requests. Returns (response, data, writes).

    These checks exercise proposals, so they switch the composer to Propose changes; Ask is the page default."""
    before, writes, start = provider_count(state), len(state['writes']), len(log.responses)
    main = page.locator('#main')
    # The mode is a dropdown in the composer (button "Mode: …" and a listbox).
    main.get_by_role('button', name=re.compile(r'^Mode:')).click()
    main.locator(f'.composer-controls .select-menu-list [role=option][data-value="{"propose" if propose else "ask"}"]').click()
    main.get_by_label(UI['message'], exact=True).fill(text)
    control(main, UI['send']).click()
    response = log.wait(start, is_chat, 'the chat response', timeout=90)
    # The browser cannot read this fetch body back, so check what the model was actually offered.
    offered = [set(request['tools'] or []) for request in state['provider']['requests'][before:]]
    for tools in offered:
        if propose:
            continue  # Later requests of a turn may legitimately carry no tools.
        assert not {name for name in tools if name.startswith('propose_')}, f'Ask mode offered proposal tools: {sorted(tools)}'
    if propose and offered:
        assert any(name.startswith('propose_') for name in offered[0]), f'Propose mode offered no proposal tool: {sorted(offered[0])}'
    data = response_json(response)
    sent = provider_count(state) - before
    assert sent == requests, f'{sent} provider requests for one human message (expected {requests})'
    return response, data, writes


def click_turn(page, log, state, button, what):
    """A human continuation (Continue, Try again): exactly one provider request."""
    before, start = provider_count(state), len(log.responses)
    button.click()
    response = log.wait(start, is_chat, what, timeout=90)
    sent = provider_count(state) - before
    assert sent == 1, f'{sent} provider requests for one click on {what}'
    return response, response_json(response)


def assert_no_automatic_request(page, state, what, wait_ms=1500):
    before = provider_count(state)
    page.wait_for_timeout(wait_ms)
    assert provider_count(state) == before, f'{what} sent a provider request without a human action'


def instance_ids(page, state):
    ids = state.get('instance_ids')
    if not ids:
        rows = ok(page, '/api/n8n_proxy/instances')['data']
        ids = {row['name']: row['id'] for row in rows if row['name'] in ('source', 'target')}
    assert 'source' in ids, 'The source fixture instance is not configured'
    return ids


def configure_provider(page, fixture_url):
    """A custom OpenAI-compatible connection to the simulator, created only in this temporary installation."""
    ok(page, '/api/assistant/config/provider', 'PUT', {'id': PROVIDER_ID, 'config': {
        'kind': 'custom', 'base_url': fixture_url + '/v1', 'model': 'scripted-repair-model',
        'api_key_ref': None, 'tools_enabled': True}})


def ingest_error(page, source_id):
    """Record a real error row through token-scoped OTLP ingestion; returns its numeric error ID."""
    token = ok(page, '/api/otel/tokens', 'POST', {'instance_id': source_id, 'name': 'Repair browser exporter'})['token']
    now = time.time_ns()
    attributes = [{'key': key, 'value': {'stringValue': value}} for key, value in (
        ('n8n.workflow.id', REPAIR_WORKFLOW_ID), ('n8n.workflow.name', 'Order synchronization'),
        ('n8n.execution.id', REPAIR_EXECUTION_ID), ('n8n.node.name', 'HTTP Request'))]
    span = {'traceId': secrets.token_hex(16), 'spanId': secrets.token_hex(8), 'name': 'HTTP Request', 'kind': 1,
            'startTimeUnixNano': str(now - 45_000_000), 'endTimeUnixNano': str(now), 'status': {'code': 2},
            'attributes': attributes}
    body = json.dumps({'resourceSpans': [{'resource': {'attributes': []}, 'scopeSpans': [{'spans': [span]}]}]})
    status = page.evaluate('''async ([token, body]) => (await fetch('/api/otel/v1/traces', {method: 'POST',
        headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, body})).status''', [token, body])
    assert status == 200, f'OTLP ingestion: HTTP {status}'
    rows = ok(page, '/api/errors?' + urlencode({'instance_id': source_id, 'status': 'open', 'limit': 100}))['data']
    matches = [row['id'] for row in rows
               if row.get('workflow_id') == REPAIR_WORKFLOW_ID and row.get('execution_id') == REPAIR_EXECUTION_ID]
    assert len(matches) == 1, f'{len(matches)} error rows for the repair execution'
    return matches[0]


def badges(page):
    return page.locator(UI['badge']).filter(has_text='Supports repair:')


def expect_badges(page, text):
    """Every rendered tool-calling badge shows `text` (the scripted connection is the only one)."""
    found = badges(page)
    expect(found.first).to_have_text(text)
    for index in range(found.count()):
        expect(found.nth(index)).to_have_text(text)


def tool_check_button(page):
    main = page.locator('#main')
    buttons = main.get_by_role('button', name=UI['check_tool_calling'], exact=True)
    if not (buttons.count() and buttons.first.is_visible()):
        control(main, UI['settings_button']).click()
    expect(buttons.first).to_be_visible()
    # A `has` locator is resolved inside each candidate, so it must be built from the page root.
    rows = main.locator('li, tr, article, section, div').filter(has_text=PROVIDER_ID).filter(
        has=page.get_by_role('button', name=UI['check_tool_calling'], exact=True))
    return (rows.last.get_by_role('button', name=UI['check_tool_calling'], exact=True) if rows.count() else buttons).first


def is_tool_test(response):
    path = urlsplit(response.url).path
    return path.startswith('/api/assistant/providers/') and path.endswith('/tool-test') and response.request.method == 'POST'


def exercise_tool_calling(page, log, state):
    """Untested becomes yes after an admin check against the scripted model; a rate-limited re-check keeps yes."""
    page.evaluate("location.hash = 'assistant'")
    page.get_by_role('heading', name=UI['heading'], exact=True).wait_for()
    button = tool_check_button(page)
    expect_badges(page, UI['badge_untested'])
    queue_provider(state, {'kind': 'tool_call', 'name': '*'})
    before, start = provider_count(state), len(log.responses)
    button.click()
    tested = log.wait(start, is_tool_test, 'the tool-calling check')
    assert urlsplit(tested.url).path == f'/api/assistant/providers/{PROVIDER_ID}/tool-test', 'Checked the wrong connection'
    assert tested.status == 200, f'Tool-calling check: HTTP {tested.status} ({response_json(tested).get("code")})'
    assert (response_json(tested).get('tool_calling') or {}).get('status') == 'supported', 'The scripted tool call was not accepted'
    assert provider_count(state) == before + 1, 'The tool-calling check must send exactly one provider request'
    expect_badges(page, UI['badge_yes'])
    queue_provider(state, {'kind': 'status', 'status': 429})
    start = len(log.responses)
    tool_check_button(page).click()
    limited = log.wait(start, is_tool_test, 'the rate-limited tool-calling check')
    assert provider_count(state) == before + 2, 'The rate-limited check must send exactly one provider request'
    assert limited.status != 200, f'A rate-limited tool-calling check must not report a result (HTTP {limited.status})'
    assert_no_automatic_request(page, state, 'The rate-limited tool-calling check')
    expect_badges(page, UI['badge_yes'])
    page.evaluate("location.hash = 'assistant?view=recheck'")  # Re-render: the stored result survives the 429.
    page.get_by_role('heading', name=UI['heading'], exact=True).wait_for()
    tool_check_button(page)
    expect_badges(page, UI['badge_yes'])
    close_settings(page)
    print('PASS: tool-calling check marks the scripted connection "Supports repair: yes"; a rate-limited re-check keeps it and does not retry.', flush=True)


def tab(page, name):
    return page.get_by_role('tab', name=name, exact=True).or_(page.get_by_role('button', name=name, exact=True)).first


def open_target(page, ids, error_id=None):
    params = {'mode': 'repair', 'instance_id': ids['source'], 'workflow_id': REPAIR_WORKFLOW_ID}
    if error_id:
        params['error_id'] = error_id
    page.evaluate('(value) => { location.hash = value; }', 'assistant?' + urlencode(params))
    page.get_by_role('heading', name=UI['heading'], exact=True).wait_for()
    target = page.locator(UI['target'])
    expect(target).to_be_visible()
    return target


def open_context(page):
    context = page.locator(UI['context'])
    if context.get_attribute('open') is None:
        context.locator('summary').filter(has_text=UI['context_summary']).click()
    expect(context).to_have_attribute('open', '')
    return context


def context_response(log, start):
    return log.wait(start, lambda r: urlsplit(r.url).path == CONTEXT_PATH and r.status == 200, 'the repair context')


def exercise_target(page, log, state, ids, error_id):
    """The deep link pins the target and What the AI sees is masked. Returns the context request path."""
    start = len(log.responses)
    target = open_target(page, ids, error_id)
    expect(target).to_contain_text(re.compile(r'Order synchronization|wf-orders'))
    expect(tab(page, UI['workspace_tab'])).to_be_visible()
    expect(tab(page, UI['code_tab'])).to_be_visible()
    context = open_context(page)
    expect(context).to_contain_text('[redacted')
    expect(context).not_to_contain_text(REPAIR_SECRET)
    expect(context.get_by_label(UI['send_error_text'], exact=True)).to_be_checked()
    response = context_response(log, start)
    raw = response.text()
    assert REPAIR_SECRET not in raw, 'The repair context contains the secret'
    data = json.loads(raw)
    workflow, error = data.get('workflow') or {}, data.get('error') or {}
    assert data.get('trust') == 'untrusted_remote_text' and workflow.get('id') == REPAIR_WORKFLOW_ID, 'Unexpected repair context'
    assert '[redacted' in json.dumps(workflow.get('nodes')), 'The Authorization header value was not masked'
    assert error.get('error_id') == str(error_id) and error.get('http_code') == '404', 'Error context does not match the pinned error'
    assert error.get('detail') == 'available' and error.get('message') == REPAIR_ERROR_MESSAGE, 'Scrubbed error text missing by default'
    assert '[redacted' in (error.get('description') or ''), 'The secret-bearing error description was not masked'
    state['provider']['content_hash'] = workflow['content_hash']
    assert_page_clean(page, 'repair target and context')
    parts = urlsplit(response.url)
    print('PASS: repair deep link pins workflow/error target; What the AI sees masks header and error-text secrets.', flush=True)
    return parts.path + ('?' + parts.query if parts.query else '')


def patch_arguments(instance_id, content_hash, url=V2_URL, code='return items;'):
    operations = [{'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'url', 'value': url}]
    if code is not None:
        operations.append({'op': 'set_code', 'node': 'Code', 'code': code})
    return {'instance_id': instance_id, 'workflow_id': REPAIR_WORKFLOW_ID, 'base_content_hash': content_hash,
            'summary': f'Use {url} and read all items.' if code is not None else f'Use {url}.', 'operations': operations}


def is_cancel(response):
    return urlsplit(response.url).path == '/api/assistant/tools/cancel' and response.request.method == 'POST'


def exercise_chat(page, log, state, ids):
    """One provider request per human action: text, 429 then Try again, cut-off, read tool then Continue."""
    main = page.locator('#main')
    queue_provider(state, {'kind': 'text', 'content': 'Scripted diagnosis: the HTTP Request node receives HTTP 404.'})
    response, data, writes = send_chat(page, log, state, 'Why does this workflow fail?')
    assert data.get('status') == 'completed', f'Text turn: HTTP {response.status}, status {data.get("status")}, code {data.get("code")}'
    assert not data.get('pending_actions') and len(state['writes']) == writes, 'A text answer must not propose or write'
    expect(main.get_by_text('Scripted diagnosis: the HTTP Request node receives HTTP 404.', exact=True)).to_be_visible()
    chat_id = data.get('id')
    assert chat_id, 'The repair conversation has no ID'

    queue_provider(state, {'kind': 'status', 'status': 429}, {'kind': 'text', 'content': 'Scripted answer after waiting.'})
    response, data, writes = send_chat(page, log, state, 'Check the failing node again.')
    codes = [step.get('code') for step in data.get('steps') or []]
    assert data.get('status') == 'interrupted', f'429 turn: HTTP {response.status}, status {data.get("status")}, code {data.get("code")}'
    assert 'provider_rate_limited' in codes, f'429 turn step codes: {codes}'
    assert not data.get('pending_actions') and len(state['writes']) == writes, 'A rate-limited turn must not propose or write'
    expect(main.get_by_text(UI['rate_limited'], exact=True)).to_be_visible()
    retry = control(main, UI['try_again'])
    expect(retry).to_be_visible()
    assert_no_automatic_request(page, state, 'A rate-limited turn')
    response, data = click_turn(page, log, state, retry, 'Try again')
    assert data.get('status') == 'completed', f'Try again: HTTP {response.status}, status {data.get("status")}'
    expect(main.get_by_text('Scripted answer after waiting.', exact=True)).to_be_visible()
    assert data.get('id', chat_id) == chat_id, 'Try again started a different conversation'

    queue_provider(state, {'kind': 'length'})
    response, data, writes = send_chat(page, log, state, 'Explain the complete fix.')
    expect(main.get_by_text(UI['cut_off'], exact=True)).to_be_visible()
    assert not data.get('pending_actions') and len(state['writes']) == writes, 'A cut-off answer must not propose or write'

    queue_provider(state,
                   {'kind': 'tool_call', 'name': 'get_workflow_context',
                    'arguments': {'instance_id': ids['source'], 'workflow_id': REPAIR_WORKFLOW_ID}},
                   {'kind': 'tool_call', 'name': 'propose_workflow_patch',
                    'arguments': patch_arguments(ids['source'], '{{content_hash}}')})
    response, data, writes = send_chat(page, log, state, 'Read the workflow, then propose a fix.')
    declared = state['provider']['requests'][-1]['tools']
    assert data.get('status') == 'awaiting_continue', f'Read-tool turn: status {data.get("status")}, code {data.get("code")}, declared {declared}'
    assert any(step.get('kind') == 'context_read' and step.get('tool') == 'get_workflow_context' for step in data.get('steps') or []), \
        'The read tool is not reported as a context_read step'
    assert not data.get('pending_actions') and len(state['writes']) == writes
    assert_no_automatic_request(page, state, 'A turn awaiting Continue')
    response, data = click_turn(page, log, state, control(main, UI['continue']), 'Continue')
    actions = data.get('pending_actions') or []
    names = sorted(str(action.get('name')) for action in actions)
    # The workflow is active, so the patch arrives with its linked deactivate proposal.
    assert names == ['deactivate_workflow', 'propose_workflow_patch'], \
        f'Continue: HTTP {response.status}, status {data.get("status")}, code {data.get("code")}, actions {names}'
    assert len(state['writes']) == writes, 'A chat proposal must not write to n8n'
    node = card(page, next(action['id'] for action in actions if action.get('name') == 'propose_workflow_patch'))
    expect(node).to_be_visible()
    expect(node.locator(UI['changes'])).to_contain_text(V2_URL)
    expect(node).to_contain_text(UI['model_summary'])
    cancelled = act(page, log, node, UI['cancel'], is_cancel, 'cancelling the chat proposal')
    assert cancelled.status == 200, f'Cancel: HTTP {cancelled.status}'
    expect(node).to_have_attribute(UI['status_attribute'], UI['status_cancelled'])
    assert node.evaluate("n => ![...n.childNodes].some(child => child.nodeType === 3 && child.textContent === 'null')"), 'Absent proposal footer must not render literal null'
    assert len(state['writes']) == writes, 'Cancel must not write to n8n'

    # Per-conversation consent: with the toggle off, the scrubbed error text does not reach the provider.
    toggle = open_context(page).get_by_label(UI['send_error_text'], exact=True)
    toggle.uncheck()
    queue_provider(state, {'kind': 'text', 'content': 'Scripted answer without error text.'})
    send_chat(page, log, state, 'Without the error text, what should be checked first?')
    assert REPAIR_ERROR_MESSAGE not in state['provider']['requests'][-1]['body'], 'Error text reached the provider with the toggle off'
    toggle.check()
    assert all(REPAIR_SECRET not in request['body'] for request in state['provider']['requests']), 'A secret reached the provider'
    assert_page_clean(page, 'repair conversation')
    print('PASS: one provider request per human action (text, 429 + Try again, cut-off, read tool + Continue), chat proposal card and cancel, conversation error-text toggle; no secret sent to the provider.', flush=True)
    return chat_id


def is_assistant_post(response):
    return response.request.method == 'POST' and urlsplit(response.url).path.startswith('/api/assistant/')


def propose(page, state, name, arguments, chat_id):
    """A scripted proposal through the tool API, bound to the repair conversation. Never writes."""
    writes = len(state['writes'])
    status, data = call_api(page, '/api/assistant/tools/run', 'POST', {'name': name, 'arguments': arguments, 'chat_id': chat_id})
    assert status == 200 and isinstance((data or {}).get('pending_action'), dict), f'{name}: HTTP {status} ({(data or {}).get("code")})'
    assert len(state['writes']) == writes, 'A proposal must not write to n8n'
    return data['pending_action']


def show_card(page, action_id):
    """Proposals made outside the chat render inline for this conversation; refresh proposals if they are not shown yet."""
    node = card(page, action_id)
    try:
        node.wait_for(state='visible', timeout=3000)
    except PlaywrightTimeout:
        # Refresh lives in the Review drawer (a closed dialog hides it from role queries); the cards it
        # mounts are in the conversation behind it, so close Review again afterwards.
        drawer = page.locator('dialog.review-drawer[open]')
        if not drawer.count():
            page.get_by_role('button', name=re.compile(r'^Review')).click()
            drawer.wait_for()
        page.locator('#main').get_by_role('button', name=UI['refresh_proposals'], exact=True).click()
        page.keyboard.press('Escape')
        expect(drawer).to_have_count(0)
        node.wait_for(state='visible', timeout=15000)
    return node


def current_hash(page, context_path):
    return ok(page, context_path)['workflow']['content_hash']


def content(doc):
    return json.loads(json.dumps({key: doc.get(key) for key in ('name', 'nodes', 'connections', 'settings')}))


def parameters_of(state, name):
    return next(node for node in state['repair']['workflow']['nodes'] if node['name'] == name)['parameters']


def card_ids(page):
    return page.locator(UI['card']).evaluate_all('nodes => nodes.map(node => node.dataset.actionId)')


def exercise_proposals(page, log, state, ids, chat_id, context_path, artifacts):
    source, repair = ids['source'], state['repair']
    first_hash = current_hash(page, context_path)

    # Proposal while active: server diff, unverified model text, Apply blocked behind a linked deactivate.
    p1 = propose(page, state, 'propose_workflow_patch', patch_arguments(source, first_hash), chat_id)
    assert p1['target'].get('active') is True and p1['preview']['apply']['label'] == UI['apply'], 'Unexpected patch preview'
    assert [(item['code'], item['satisfied']) for item in p1['preview']['preconditions']] == [('workflow_active', False)]
    first = show_card(page, p1['id'])
    for text in (V1_URL, V2_URL, 'return item;', 'return items;'):
        expect(first.locator(UI['changes'])).to_contain_text(text)
    expect(first).to_contain_text(UI['model_summary'])
    expect(first.locator(UI['preconditions'])).to_be_visible()
    expect(control(first, UI['apply'])).to_be_disabled()
    expect(control(first, UI['deactivate'])).to_be_visible()
    assert_page_clean(page, 'proposal card')
    first.scroll_into_view_if_needed()
    page.screenshot(path=str(artifacts / 'repair-desktop.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    try:
        expect(first).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'The repair workspace overflows at 390 px'
        first.scroll_into_view_if_needed()
        page.screenshot(path=str(artifacts / 'repair-mobile.png'), full_page=True)
    finally:
        page.set_viewport_size({'width': 1440, 'height': 1000})

    # The server refuses to apply while active, whatever the UI shows (separate proposal, cancelled afterwards).
    blocked = propose(page, state, 'propose_workflow_patch', patch_arguments(source, first_hash, code=None), chat_id)
    writes = len(state['writes'])
    status, data = call_api(page, CONFIRM_PATH, 'POST', {'action_id': blocked['id']})
    assert (status, (data or {}).get('code')) == (409, 'workflow_active'), f'Apply while active: HTTP {status} ({(data or {}).get("code")})'
    assert state['writes'][writes:] == [], 'Apply while active wrote to n8n'
    call_api(page, '/api/assistant/tools/cancel', 'POST', {'action_id': blocked['id']})  # 409 if the refusal consumed it.

    # Linked deactivate: exactly one POST. Then another editor saves before Apply: stale, nothing written.
    act(page, log, first, UI['deactivate'], is_assistant_post, 'the linked deactivate')
    wait_until(page, lambda: len(state['writes']) > writes, 'the deactivate write')
    expect(control(first, UI['apply'])).to_be_enabled()
    assert state['writes'][writes:] == [DEACTIVATE_WRITE], state['writes'][writes:]
    assert repair['workflow']['active'] is False
    repair['concurrent_edit'] = True
    writes = len(state['writes'])
    stale = act(page, log, first, UI['apply'], is_confirm, 'the stale apply')
    expect(first).to_have_attribute(UI['status_attribute'], UI['status_stale'])
    expect(first).to_contain_text(UI['stale'])
    expect(control(first, UI['reread'])).to_be_visible()
    assert repair['remote_edits'] == 1 and not repair['concurrent_edit'], 'The concurrent edit did not happen before confirm'
    assert state['writes'][writes:] == [], f'A stale apply wrote to n8n: {state["writes"][writes:]}'
    assert stale.status == 409 or response_json(stale).get('status') == 'stale', f'Stale apply: HTTP {stale.status}'

    # Re-enabled in n8n (not through Inference): the re-proposal is blocked again.
    repair['workflow']['active'] = True
    second_hash = current_hash(page, context_path)
    assert second_hash != first_hash, 'The concurrent edit did not change the content hash'
    p2 = propose(page, state, 'propose_workflow_patch', patch_arguments(source, second_hash), chat_id)
    second = show_card(page, p2['id'])
    expect(control(second, UI['apply'])).to_be_disabled()
    expect(control(second, UI['deactivate'])).to_be_visible()

    # Deactivate, then Apply: one POST and one PUT, verified readback, real secret kept; replay refused.
    writes = len(state['writes'])
    act(page, log, second, UI['deactivate'], is_assistant_post, 'the second linked deactivate')
    wait_until(page, lambda: len(state['writes']) > writes, 'the second deactivate write')
    expect(control(second, UI['apply'])).to_be_enabled()
    assert state['writes'][writes:] == [DEACTIVATE_WRITE], state['writes'][writes:]
    before = content(repair['workflow'])
    writes = len(state['writes'])
    applied = act(page, log, second, UI['apply'], is_confirm, 'the apply')
    result = response_json(applied)
    assert applied.status == 200 and result.get('status') == 'succeeded', \
        f'Apply: HTTP {applied.status}, status {result.get("status")}, code {result.get("code")}'
    assert (result.get('result') or {}).get('verified') is True, 'The readback did not verify the write'
    expect(second).to_have_attribute(UI['status_attribute'], UI['status_succeeded'])
    assert state['writes'][writes:] == [PUT_WRITE], state['writes'][writes:]
    assert repair['rejected'] == [], f'n8n rejected a write: {repair["rejected"]}'
    http = parameters_of(state, 'HTTP Request')
    assert http['url'] == V2_URL and http['options'] == {'timeout': 10000}, 'Unexpected saved HTTP Request parameters'
    assert http['headerParameters']['parameters'][0]['value'] == 'Bearer ' + REPAIR_SECRET, 'The write replaced the real header value'
    assert parameters_of(state, 'Code')['jsCode'] == 'return items;' and repair['workflow']['active'] is False
    expect(control(second, UI['undo'])).to_be_visible()
    expect(control(second, UI['activate'])).to_be_visible()
    status, _ = call_api(page, CONFIRM_PATH, 'POST', {'action_id': p2['id']})
    assert status == 409 and state['writes'][writes:] == [PUT_WRITE], f'Replayed confirm: HTTP {status}'

    # Undo: restore the pre-repair snapshot with exactly one PUT.
    known, start = set(card_ids(page)), len(log.responses)
    control(second, UI['undo']).click()
    restorable = page.locator(UI['card']).filter(has=page.get_by_role('button', name=UI['restore'], exact=True))

    def restore_card():
        dialog = page.locator(UI['dialog'])
        if dialog.count() and dialog.first.is_visible():
            confirm_dialog(dialog.first)
        found = restorable.evaluate_all('nodes => nodes.map(node => node.dataset.actionId)')
        return next((value for value in found if value not in known), None)
    restore = card(page, wait_until(page, restore_card, 'the restore proposal card'))
    runs = log.after(start, lambda r: urlsplit(r.url).path == '/api/assistant/tools/run' and r.request.method == 'POST')
    assert not runs or (response_json(runs[0]).get('pending_action') or {}).get('name') == 'propose_restore_workflow'
    for text in (V1_URL, 'return item;'):
        expect(restore.locator(UI['changes'])).to_contain_text(text)
    writes = len(state['writes'])
    restored = act(page, log, restore, UI['restore'], is_confirm, 'the restore')
    outcome = response_json(restored)
    assert restored.status == 200 and outcome.get('status') == 'succeeded', f'Restore: HTTP {restored.status}, code {outcome.get("code")}'
    expect(restore).to_have_attribute(UI['status_attribute'], UI['status_succeeded'])
    assert state['writes'][writes:] == [PUT_WRITE], state['writes'][writes:]
    assert content(repair['workflow']) == before, 'Restore did not return the pre-repair content'

    # Readback divergence: n8n accepts the PUT but stores other content. Uncertain, checked read-only, never retried.
    p3 = propose(page, state, 'propose_workflow_patch', patch_arguments(source, current_hash(page, context_path), url=V3_URL, code=None), chat_id)
    third = show_card(page, p3['id'])
    repair['readback_divergence'] = True
    writes = len(state['writes'])
    uncertain = act(page, log, third, UI['apply'], is_confirm, 'the diverging apply')
    body = response_json(uncertain)
    assert body.get('status') == 'uncertain' or body.get('code') == 'upstream_outcome_unknown', \
        f'Diverging apply: HTTP {uncertain.status}, status {body.get("status")}, code {body.get("code")}'
    expect(third).to_have_attribute(UI['status_attribute'], UI['status_uncertain'])
    assert state['writes'][writes:] == [PUT_WRITE] and not repair['readback_divergence'], state['writes'][writes:]
    reconcile_path = f'/api/assistant/repair/{p3["id"]}/reconcile'
    checked = act(page, log, third, UI['reconcile'], lambda r: urlsplit(r.url).path == reconcile_path, 'Check current state')
    assert checked.status == 200, f'Check current state: HTTP {checked.status}'
    page.wait_for_timeout(500)
    assert state['writes'][writes:] == [PUT_WRITE], f'Check current state wrote to n8n: {state["writes"][writes:]}'
    again = third.get_by_role('button', name=UI['apply'], exact=True)
    assert again.count() == 0 or again.first.is_disabled(), 'An uncertain write offers Apply again'
    assert_page_clean(page, 'proposal outcomes')
    print('PASS: inline server diffs; active workflows blocked (UI and 409); linked deactivate; stale apply writes nothing; apply writes one PUT with verified readback and replay 409; undo restores with one PUT; readback divergence is uncertain and reconcile writes nothing.', flush=True)


def is_repair_settings_put(response):
    return urlsplit(response.url).path == '/api/assistant/repair/settings' and response.request.method == 'PUT'


def admin_switch_box(page, label):
    """An administrator switch, in Assistant settings or in the Settings view."""
    main = page.locator('#main')
    box = main.get_by_label(label, exact=True)
    try:
        box.wait_for(state='attached', timeout=2000)
        # Assistant settings keep their switches in a drawer: open it when the switch is there but closed away.
        if not box.is_visible():
            settings = main.get_by_role('button', name=UI['settings_button'], exact=True)
            if settings.count():
                settings.first.click()
            box.wait_for(state='visible', timeout=5000)
    except PlaywrightTimeout:
        settings = main.get_by_role('button', name=UI['settings_button'], exact=True)
        if settings.count():
            settings.first.click()
        try:
            box.wait_for(state='attached', timeout=2000)
        except PlaywrightTimeout:
            page.evaluate("location.hash = 'settings'")
            box.wait_for(state='attached', timeout=15000)
    return box


def set_admin_switch(page, log, label, key, enabled):
    box = admin_switch_box(page, label)
    start = len(log.responses)
    box.set_checked(enabled)
    try:
        saved = log.wait(start, is_repair_settings_put, 'the repair settings save', timeout=3)
    except AssertionError:
        # The switch may save through its form's button rather than on change.
        holder = page.locator('#main').locator('form, section, details').filter(
            has=page.get_by_label(label, exact=True)).last
        holder.get_by_role('button', name=re.compile(r'^save', re.I)).first.click()
        saved = log.wait(start, is_repair_settings_put, 'the repair settings save')
    assert saved.status == 200, f'Repair settings save: HTTP {saved.status} ({response_json(saved).get("code")})'
    assert ok(page, '/api/assistant/repair/settings').get(key) is enabled, 'Repair settings did not persist'
    close_settings(page)


def set_admin_error_text(page, log, enabled):
    set_admin_switch(page, log, UI['admin_error_text'], 'error_text_enabled', enabled)


def exercise_error_text_switch(page, log, state, ids, error_id):
    """With the administrator switch off, scrubbed error text is withheld from the context and the provider."""
    set_admin_error_text(page, log, False)
    try:
        start = len(log.responses)
        open_target(page, ids, error_id)
        open_context(page)
        error = response_json(context_response(log, start)).get('error') or {}
        assert error.get('detail') == 'withheld' and not error.get('message'), f'Error text not withheld: detail {error.get("detail")}'
        expect(page.locator(UI['context'])).not_to_contain_text(REPAIR_ERROR_MESSAGE)
        queue_provider(state, {'kind': 'text', 'content': 'Scripted answer while error text is disabled.'})
        send_chat(page, log, state, 'What should be checked while error text is disabled?')
        assert REPAIR_ERROR_MESSAGE not in state['provider']['requests'][-1]['body'], 'Error text reached the provider while disabled'
    finally:
        set_admin_error_text(page, log, True)
    print('PASS: administrator switch withholds scrubbed error text from the context and the provider; restored afterwards.', flush=True)


def exercise_directory_switch(page, log):
    """The trigger-address sharing switch saves on its own change and leaves the error-text switch as it was."""
    set_admin_switch(page, log, UI['admin_webhook_directory'], 'webhook_directory_enabled', False)
    try:
        assert ok(page, '/api/assistant/repair/settings').get('error_text_enabled') is True, 'Turning off the directory changed the error-text switch'
    finally:
        set_admin_switch(page, log, UI['admin_webhook_directory'], 'webhook_directory_enabled', True)
    print("PASS: administrator switch for sharing other workflows' trigger addresses saves and restores; error-text switch unchanged.", flush=True)


# Served instead of real dashboard metrics: the fleet aggregate check already used the 4/minute budget.
DASHBOARD_FIXTURE = {
    'workflows': 3, 'executions_24h': 1, 'terminal_executions_24h': 1, 'success_rate_percent': 0,
    'confirmed_silent_error_spans_24h': 0, 'window_start': '2026-09-14T00:00:00+00:00', 'window_end': '2026-09-15T00:00:00+00:00',
    'data': [], 'scope': 'Browser fixture for the repair link check.',
    'limits': {'pages_per_resource': 10, 'rows_per_page': 250, 'concurrency': 3, 'instance_timeout_seconds': 30}}


def assert_repair_href(link, expected):
    expect(link).to_have_count(1)
    href = link.get_attribute('href') or ''
    assert href.startswith('#assistant?'), f'Repair link target: {href}'
    params = dict(parse_qsl(href.split('?', 1)[1]))
    for key, value in expected.items():
        assert params.get(key) == value, f'Repair link {key}={params.get(key)!r}, expected {value!r}'


def exercise_links(page, ids, error_id):
    """Operators get "Repair with AI" entry points that carry the explicit target."""
    target = {'mode': 'repair', 'instance_id': ids['source'], 'workflow_id': REPAIR_WORKFLOW_ID}
    with_error = {**target, 'error_id': str(error_id)}
    page.evaluate("location.hash = 'errors'")
    row = page.get_by_role('region', name='Errors table').locator('tbody tr').filter(has_text='Order synchronization')
    expect(row).to_have_count(1)
    # Error rows keep secondary actions in the row menu; the repair entry is a menu item there.
    row.get_by_role('button', name=re.compile(r'^More actions for Order synchronization')).click()
    assert_repair_href(page.get_by_role('menuitem', name=UI['repair_link'], exact=True), with_error)
    page.keyboard.press('Escape')
    page.evaluate("location.hash = 'workflows'")
    section = page.get_by_role('region', name='Workflows on source', exact=True)
    section.get_by_role('button', name='More actions for Order synchronization on source', exact=True).click()
    assert_repair_href(page.get_by_role('menu', name='More actions for Order synchronization on source').get_by_role('menuitem', name=UI['repair_link'], exact=True), target)
    page.keyboard.press('Escape')
    page.route('**/api/n8n_proxy/dashboard*', lambda route: route.fulfill(json=DASHBOARD_FIXTURE))
    try:
        page.evaluate("location.hash = 'dashboard'")
        finding = page.locator('.finding-row').filter(has_text='Order synchronization')
        expect(finding).to_have_count(1)
        assert_repair_href(finding.get_by_role('link', name='Repair with AI: Order synchronization', exact=True), with_error)
        # Leave before unrouting so the overview's 30-second refresh cannot spend a real metrics request.
        page.evaluate("location.hash = 'errors'")
        page.get_by_role('region', name='Errors table').wait_for()
    finally:
        page.unroute('**/api/n8n_proxy/dashboard*')
    print('PASS: "Repair with AI" links on errors, workflows and dashboard findings carry instance, workflow and error targets.', flush=True)


def exercise_repair(page, fixture_url, state, artifacts):
    """AI repair round 1 in a real browser against the n8n simulator and a scripted model."""
    ids = instance_ids(page, state)
    enable_repair(state)
    log = NetworkLog(page)
    accept = lambda dialog: dialog.accept()
    page.on('dialog', accept)
    try:
        configure_provider(page, fixture_url)
        error_id = ingest_error(page, ids['source'])
        exercise_tool_calling(page, log, state)
        context_path = exercise_target(page, log, state, ids, error_id)
        chat_id = exercise_chat(page, log, state, ids)
        exercise_proposals(page, log, state, ids, chat_id, context_path, artifacts)
        exercise_error_text_switch(page, log, state, ids, error_id)
        exercise_directory_switch(page, log)
        exercise_links(page, ids, error_id)
        assert all(REPAIR_SECRET not in request['body'] for request in state['provider']['requests']), 'A secret reached the provider'
        unreadable = log.assert_clean(REPAIR_SECRET)
        print(f'PASS: no fixture secret in the DOM, form values, provider requests or browser API traffic '
              f'({len(log.responses)} responses, {unreadable} bodies unavailable).', flush=True)
    finally:
        page.remove_listener('dialog', accept)
        log.close()
        page.set_viewport_size({'width': 1440, 'height': 1000})
