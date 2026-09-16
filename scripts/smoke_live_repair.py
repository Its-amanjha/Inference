"""Live AI repair verification against a disposable Inference container, its managed n8n bundle and a real
tool-calling provider.

Manual only. It is not part of any suite and refuses to start without --confirm-live. The browser scenario
(scripts/frontend_repair_checks.py) covers the same flow against a simulator; this check exists to confirm the
n8n public API and a real model behave the same way.

Every step prints PASS or NOT VERIFIED with a reason, and the run ends with a summary. Nothing is retried: a
provider 429 stops the run, because hy3 (api.b.ai) answered a back-to-back request with HTTP 429 and the same
request 60 seconds later with 200, without rate-limit headers.

The provider key never appears in arguments. Supply it in an environment variable (default
INFERENCE_LIVE_PROVIDER_KEY); the script stores it as a vault secret inside the disposable container and
configures the connection to reference it. Pass --provider-key-ref instead to reuse an existing reference.

What the run proves, in order:
 1. A disposable container and managed n8n bundle come up through the ownership-checked API.
 2. A workflow like the simulator's wf-orders fails for a real reason: Webhook -> HTTP Request -> Code, where
    the HTTP Request calls a misspelled webhook path on the same n8n (HTTP 404) and carries a per-run fake
    secret (sk-live-<random>) in an Authorization header parameter. An order-service workflow owns the correct
    path, so the fix the model must find is a one-character URL correction.
 3. Inference records the failed execution and pins it as a repair target.
 4. GET /repair/context masks the fake secret.
 5. The provider passes the tool-calling check.
 6. The repair conversation sends one model request per human action and ends with a patch proposal.
 7. Apply writes once after its linked deactivate, the readback matches, and a replayed confirm returns 409.
 8. Undo restores the pre-repair workflow.
 9. The fake secret is absent from the chat transcript, approvals and context responses.
10. Bundle and container are removed.
"""
import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time

import httpx

MIN_PACE_SECONDS = 60
N8N_VERSION = '2.19.0'
SERVICE_PATH = 'order-status'
BROKEN_PATH = 'order-stauts'  # one deliberate typo: the whole repair is correcting this URL
STEPS = (
    'provision_bundle', 'create_failing_workflow', 'pin_target', 'check_context_masking', 'check_tool_calling',
    'repair_conversation', 'apply_with_deactivate', 'undo_with_restore', 'search_for_secret', 'cleanup',
)


class Pacer:
    """Enforces a minimum interval between provider requests. Never retries."""

    def __init__(self, seconds):
        if seconds < MIN_PACE_SECONDS:
            raise ValueError(f'Pacing must be at least {MIN_PACE_SECONDS} seconds.')
        self.seconds = seconds
        self.last = None

    def before_request(self):
        if self.last is not None:
            remaining = self.seconds - (time.monotonic() - self.last)
            if remaining > 0:
                print(f'Waiting {remaining:.0f} s before the next provider request.', flush=True)
                time.sleep(remaining)
        self.last = time.monotonic()


class ProviderRateLimited(RuntimeError):
    """Raised on a provider 429. The run stops; no automatic retry."""


class Report:
    """PASS / NOT VERIFIED per step. A step never raises on its own; the summary decides the exit code."""

    def __init__(self):
        self.rows = []

    def passed(self, step, detail=''):
        self.rows.append((step, 'PASS', detail))
        print(f'PASS: {step}' + (f' — {detail}' if detail else ''), flush=True)

    def unverified(self, step, reason):
        self.rows.append((step, 'NOT VERIFIED', reason))
        print(f'NOT VERIFIED: {step} — {reason}', flush=True)

    def finish(self):
        print('\n--- live repair summary ---', flush=True)
        for step, outcome, detail in self.rows:
            print(f'{outcome:13} {step}' + (f' — {detail}' if detail else ''), flush=True)
        missing = [step for step in STEPS if step not in {row[0] for row in self.rows}]
        for step in missing:
            print(f'{"NOT RUN":13} {step}', flush=True)
        return 0 if not missing and all(row[1] == 'PASS' for row in self.rows) else 1


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, timeout=120).strip()


def checked(response):
    """Raise with method, path, status and error code only; response bodies may contain secrets."""
    if not response.is_success:
        code = 'non_json'
        if response.headers.get('content-type', '').startswith('application/json'):
            code = response.json().get('code', 'unknown')
        if code in ('provider_rate_limited', 'assistant_rate_limit'):
            raise ProviderRateLimited(f'{response.request.method} {response.request.url.path}: HTTP {response.status_code} ({code})')
        raise RuntimeError(f'{response.request.method} {response.request.url.path}: HTTP {response.status_code} ({code})')
    return response.json() if response.content else {}


def fake_secret():
    return 'sk-live-' + secrets.token_hex(12)


def http_node_url(workflow):
    node = next((item for item in workflow.get('nodes') or [] if item.get('type') == 'n8n-nodes-base.httpRequest'), None)
    return (node or {}).get('parameters', {}).get('url')


def failing_workflow(trigger_path, secret):
    """Webhook -> HTTP Request (misspelled webhook path, fake secret header) -> Code."""
    return {
        'name': 'Order status sync (live repair check)',
        'settings': {'executionOrder': 'v1', 'saveDataErrorExecution': 'all', 'saveDataSuccessExecution': 'all'},
        'nodes': [
            {'id': 'trigger', 'name': 'Webhook', 'type': 'n8n-nodes-base.webhook', 'typeVersion': 2, 'position': [0, 0],
             'webhookId': trigger_path, 'parameters': {'path': trigger_path, 'httpMethod': 'GET', 'responseMode': 'lastNode'}},
            {'id': 'fetch', 'name': 'Fetch order status', 'type': 'n8n-nodes-base.httpRequest', 'typeVersion': 4.2,
             'position': [250, 0], 'parameters': {
                 'method': 'GET', 'url': f'http://127.0.0.1:5678/webhook/{BROKEN_PATH}', 'sendHeaders': True,
                 'headerParameters': {'parameters': [{'name': 'Authorization', 'value': f'Bearer {secret}'}]}, 'options': {}}},
            {'id': 'shape', 'name': 'Code', 'type': 'n8n-nodes-base.code', 'typeVersion': 2, 'position': [500, 0],
             'parameters': {'jsCode': 'return items;'}},
        ],
        'connections': {'Webhook': {'main': [[{'node': 'Fetch order status', 'type': 'main', 'index': 0}]]},
                        'Fetch order status': {'main': [[{'node': 'Code', 'type': 'main', 'index': 0}]]}},
    }


def order_service_workflow():
    """Owns /webhook/order-status, so the correct fix is reachable and provable."""
    return {
        'name': 'Order service (live repair fixture)',
        'settings': {'executionOrder': 'v1'},
        'nodes': [
            {'id': 'service-trigger', 'name': 'Webhook', 'type': 'n8n-nodes-base.webhook', 'typeVersion': 2,
             'position': [0, 0], 'webhookId': SERVICE_PATH,
             'parameters': {'path': SERVICE_PATH, 'httpMethod': 'GET', 'responseMode': 'lastNode'}},
            {'id': 'service-ok', 'name': 'Order', 'type': 'n8n-nodes-base.noOp', 'typeVersion': 1,
             'position': [250, 0], 'parameters': {}},
        ],
        'connections': {'Webhook': {'main': [[{'node': 'Order', 'type': 'main', 'index': 0}]]}},
    }


def start_container(name, bootstrap):
    docker('run', '-d', '--name', name, '--label', 'inference.live-repair-check=true',
           '--group-add', '0', '--publish', '127.0.0.1::3000',
           '--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
           '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,size=128m',
           '--tmpfs', '/data:rw,nosuid,size=128m,uid=10001,gid=10001',
           '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
           '--env', 'INFERENCE_SECURE_COOKIES=false', '--env', 'INFERENCE_BOOTSTRAP_TOKEN',
           '--env', 'INFERENCE_EGRESS_ALLOW_CIDRS=0.0.0.0/0',
           'inference:local')
    info = json.loads(docker('inspect', name))[0]
    return info['NetworkSettings']['Ports']['3000/tcp'][0]['HostPort']


def wait_for_health(client):
    for _ in range(90):
        try:
            if client.get('/healthz', timeout=2).status_code == 200:
                return True
        except httpx.TransportError:
            pass
        time.sleep(1)
    return False


def configure_provider(app, args, report):
    """Save the key as a vault secret (from the environment) and point the connection at that reference."""
    reference = args.provider_key_ref
    if not reference:
        key = os.environ.get(args.provider_key_env, '')
        if not key:
            raise RuntimeError(f'Set {args.provider_key_env} to the provider key, or pass --provider-key-ref.')
        checked(app.post('/api/admin/secrets', json={'name': 'LIVE_PROVIDER_KEY', 'value': key}))
        reference = '$LIVE_PROVIDER_KEY'
    checked(app.put('/api/assistant/config/provider', json={'id': args.provider_id, 'config': {
        'kind': args.provider_kind, 'base_url': args.provider_base_url, 'model': args.provider_model,
        'api_key_ref': reference, 'tools_enabled': True}}))
    report.passed('configure_provider', f'{args.provider_id} ({args.provider_model}) with tool calling enabled')


def repair_conversation(app, args, pacer, target, report):
    """One model request per human action. Returns the patch proposal, or None."""
    message = 'This workflow failed in its latest run. Diagnose the error and propose the smallest fix.'
    body = {'provider_id': args.provider_id, 'message': message, 'context': target}
    chat_id, proposal = None, None
    for turn in range(1, args.turns + 1):
        pacer.before_request()
        data = checked(app.post('/api/assistant/chat', json=body))
        chat_id = data.get('id') or chat_id
        steps = [(step.get('kind'), step.get('tool'), step.get('status'), step.get('code')) for step in data.get('steps') or []]
        print(f'  turn {turn}: status={data.get("status")} steps={steps}', flush=True)
        proposal = next((action for action in data.get('pending_actions') or []
                         if action.get('name') == 'propose_workflow_patch'), None)
        if proposal:
            report.passed('repair_conversation', f'proposal after {turn} model request(s)')
            return chat_id, proposal
        if data.get('status') in ('awaiting_continue', 'interrupted'):
            body = {'provider_id': args.provider_id, 'chat_id': chat_id, 'context': target, 'continue_turn': True}
        else:
            body = {'provider_id': args.provider_id, 'chat_id': chat_id, 'context': target,
                    'message': 'Propose the fix now with propose_workflow_patch, using the content_hash from the workflow context.'}
    report.unverified('repair_conversation', f'no proposal after {args.turns} model requests')
    return chat_id, None


def run(args, report):
    name = 'inference-live-repair-' + secrets.token_hex(6)
    secret = fake_secret()
    trigger_path = 'order-sync-' + secrets.token_hex(4)
    pacer = Pacer(args.pace_seconds)
    bootstrap = secrets.token_urlsafe(32)
    os.environ['INFERENCE_BOOTSTRAP_TOKEN'] = bootstrap
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        n8n_port = sock.getsockname()[1]
    created, authenticated, joined_network, app = False, False, None, None
    try:
        port = start_container(name, bootstrap)
        created = True
        base = f'http://127.0.0.1:{port}'
        app = httpx.Client(base_url=base, timeout=300, trust_env=False, headers={'Origin': base})
        if not wait_for_health(app):
            raise RuntimeError('The disposable Inference container did not become healthy.')
        checked(app.post('/api/auth/setup', json={'username': 'live_repair_owner', 'password': secrets.token_urlsafe(24)},
                         headers={'X-Inference-Bootstrap': bootstrap}))
        authenticated = True
        app.headers['X-AGD-CSRF'] = app.cookies['agd_csrf']
        bundle = checked(app.post('/api/docker_mgr/bundles', json={
            'spec': {'name': 'live-repair-check', 'version': args.n8n_version, 'port': n8n_port, 'bind_host': '127.0.0.1',
                     'exporter_base_url': f'http://host.docker.internal:{port}/api/otel'},
            'instance_url': f'http://host.docker.internal:{n8n_port}', 'idempotency_key': name, 'readiness_timeout': 240}))
        if bundle['status'] != 'ready':
            report.unverified('provision_bundle', f'bundle stage {bundle.get("stage")}: {bundle.get("diagnostic")}')
            return
        instance_id = bundle['instance_id']
        joined_network = bundle['resources']['network']
        docker('network', 'connect', joined_network, name)
        report.passed('provision_bundle', f'n8n {args.n8n_version} bundle ready on 127.0.0.1:{n8n_port}')

        with httpx.Client(base_url=f'http://127.0.0.1:{n8n_port}', trust_env=False, timeout=90) as n8n:
            checked(n8n.post('/rest/owner/setup', json={'email': 'live-repair@example.test', 'firstName': 'Live',
                                                        'lastName': 'Repair', 'password': 'Live9!' + secrets.token_urlsafe(24)}))
            # Loopback-only HTTP harness: forward the freshly issued session cookie for API-key creation.
            n8n.headers['Cookie'] = '; '.join(f'{cookie.name}={cookie.value}' for cookie in n8n.cookies.jar)
            scopes = checked(n8n.get('/rest/api-keys/scopes'))['data']
            api_key = checked(n8n.post('/rest/api-keys', json={'label': 'Live repair check', 'scopes': scopes,
                                                               'expiresAt': None}))['data']['rawApiKey']
            del n8n.headers['Cookie']
            n8n.cookies.clear()
            n8n.headers['X-N8N-API-KEY'] = api_key
            checked(app.patch(f'/api/n8n_proxy/instances/{instance_id}', json={'api_key': api_key, 'url': 'http://n8n:5678'}))

            service_id = checked(n8n.post('/api/v1/workflows', json=order_service_workflow()))['id']
            checked(n8n.post(f'/api/v1/workflows/{service_id}/activate'))
            workflow_id = checked(n8n.post('/api/v1/workflows', json=failing_workflow(trigger_path, secret)))['id']
            checked(n8n.post(f'/api/v1/workflows/{workflow_id}/activate'))
            n8n.get(f'/webhook/{trigger_path}')
            rows = []
            for _ in range(45):
                rows = checked(n8n.get('/api/v1/executions', params={'workflowId': workflow_id, 'limit': 10})).get('data') or []
                if any(row.get('status') in ('error', 'crashed') for row in rows):
                    break
                time.sleep(1)
            failed = next((row for row in rows if row.get('status') in ('error', 'crashed')), None)
            if failed is None:
                report.unverified('create_failing_workflow', 'the workflow did not fail; nothing to repair')
                return
            execution_id = str(failed['id'])
            before = checked(n8n.get(f'/api/v1/workflows/{workflow_id}'))
            report.passed('create_failing_workflow', f'execution {execution_id} failed on the misspelled path')

            target = {'mode': 'repair', 'instance_id': instance_id, 'workflow_id': workflow_id,
                      'execution_id': execution_id, 'send_error_text': True}
            listed = checked(app.get('/api/n8n_proxy/workflows', params={'instance_id': instance_id})).get('data') or []
            if not any(str(row.get('id')) == str(workflow_id) for row in listed):
                report.unverified('pin_target', 'Inference does not list the workflow for this instance')
                return
            report.passed('pin_target', f'workflow {workflow_id} on instance {instance_id}')

            context = app.get('/api/assistant/repair/context', params={
                'instance_id': instance_id, 'workflow_id': workflow_id, 'execution_id': execution_id, 'send_error_text': 'true'})
            checked(context)
            if secret in context.text:
                report.unverified('check_context_masking', 'the fake secret reached the repair context')
                return
            report.passed('check_context_masking', 'fake secret absent; redaction markers present'
                          if '[redacted' in context.text else 'fake secret absent (no redaction marker seen)')

            configure_provider(app, args, report)
            pacer.before_request()
            check = checked(app.post(f'/api/assistant/providers/{args.provider_id}/tool-test'))
            status = (check.get('tool_calling') or {}).get('status')
            if status != 'supported':
                report.unverified('check_tool_calling', f'status {status}: {check.get("detail")}')
                return
            report.passed('check_tool_calling', f'{args.provider_id} supports repair')

            chat_id, proposal = repair_conversation(app, args, pacer, target, report)
            if not proposal:
                return
            preview = proposal['preview']
            print('  proposal:', preview['summary_text'], flush=True)
            for change in preview['changes']:
                print('   change:', change.get('kind'), change.get('node'), change.get('path'), '|',
                      str(change.get('before'))[:120], '->', str(change.get('after'))[:120], flush=True)
            print('   risks:', [risk['code'] for risk in preview['risks']], flush=True)

            for item in preview['preconditions']:
                if not item['satisfied'] and item.get('action_id'):
                    checked(app.post('/api/assistant/tools/confirm', json={'action_id': item['action_id']}))
            applied = checked(app.post('/api/assistant/tools/confirm', json={'action_id': proposal['id']}))
            result = applied.get('result') or {}
            after = checked(n8n.get(f'/api/v1/workflows/{workflow_id}'))
            replay = app.post('/api/assistant/tools/confirm', json={'action_id': proposal['id']})
            url_now = http_node_url(after) or ''
            if result.get('verified') is not True:
                report.unverified('apply_with_deactivate', f'apply reported verified={result.get("verified")}')
            elif after.get('active') is not False:
                report.unverified('apply_with_deactivate', 'the workflow is not inactive after the write')
            elif replay.status_code != 409:
                report.unverified('apply_with_deactivate', f'replayed confirm returned HTTP {replay.status_code}, expected 409')
            elif before.get('versionId') == after.get('versionId'):
                report.unverified('apply_with_deactivate', 'n8n did not record a new workflow version')
            else:
                report.passed('apply_with_deactivate', f'one write, readback verified, URL now {url_now}')
            if trigger_path.lower() in url_now.lower():
                # The server's self_call_blocked guard should prevent this; the harness never activates a self-caller.
                report.unverified('apply_with_deactivate', "the applied URL targets this workflow's own trigger")
                return

            deactivate = checked(app.post('/api/assistant/tools/run', json={
                'name': 'deactivate_workflow', 'arguments': {'instance_id': instance_id, 'workflow_id': workflow_id}}))
            pending = deactivate.get('pending_action') or deactivate
            if pending.get('id'):
                checked(app.post('/api/assistant/tools/confirm', json={'action_id': pending['id']}))
            restore = checked(app.post('/api/assistant/tools/run', json={
                'name': 'propose_restore_workflow', 'chat_id': chat_id,
                'arguments': {'instance_id': instance_id, 'workflow_id': workflow_id,
                              'base_content_hash': result.get('content_hash'), 'repair_id': result.get('repair_id')}}))
            restore_action = restore.get('pending_action') or restore
            checked(app.post('/api/assistant/tools/confirm', json={'action_id': restore_action['id']}))
            restored = checked(n8n.get(f'/api/v1/workflows/{workflow_id}'))
            if http_node_url(restored) == http_node_url(before):
                report.passed('undo_with_restore', 'the pre-repair URL is back')
            else:
                report.unverified('undo_with_restore', f'URL after undo is {http_node_url(restored)}')

            transcript = app.get(f'/api/assistant/chats/{chat_id}') if chat_id else None
            approvals = app.get('/api/assistant/approvals')
            leaked = [where for where, response in (('chat transcript', transcript), ('approvals', approvals),
                                                    ('repair context', context))
                      if response is not None and response.is_success and secret in response.text]
            if leaked:
                report.unverified('search_for_secret', 'fake secret found in ' + ', '.join(leaked))
            else:
                report.passed('search_for_secret', 'absent from the transcript, approvals and context')
    finally:
        if joined_network:
            try:
                docker('network', 'disconnect', joined_network, name)
            except subprocess.SubprocessError:
                pass
        if app is not None and authenticated:
            try:
                for row in checked(app.get('/api/docker_mgr/bundles'))['data']:
                    outcome = checked(app.delete(f'/api/docker_mgr/bundles/{row["id"]}', params={'purge_data': 'true'}))
                    if outcome['status'] != 'removed':
                        raise RuntimeError('Bundle cleanup incomplete')
                report.passed('cleanup', 'bundle, network and data removed through the ownership-checked API')
            except Exception as error:  # keep the container for reconciliation, and say so
                report.unverified('cleanup', f'{type(error).__name__}; retaining container {name}')
                created = False
            finally:
                app.close()
        if created:
            docker('rm', '-f', name)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--confirm-live', action='store_true',
                        help='Required. Creates disposable containers and sends real provider requests.')
    parser.add_argument('--plan', action='store_true', help='Print the planned steps and exit.')
    parser.add_argument('--provider-id', default='hy3', help='Saved connection to create and verify (default: hy3).')
    parser.add_argument('--provider-kind', default='custom', help='Provider adapter (default: custom).')
    parser.add_argument('--provider-base-url', default='https://api.b.ai/v1', help='Completion base URL ending in /v1.')
    parser.add_argument('--provider-model', default='hy3', help='Model ID the provider expects.')
    parser.add_argument('--provider-key-env', default='INFERENCE_LIVE_PROVIDER_KEY',
                        help='Environment variable holding the provider key (never passed as an argument).')
    parser.add_argument('--provider-key-ref', help='Existing vault reference to use instead of the environment variable.')
    parser.add_argument('--pace-seconds', type=int, default=MIN_PACE_SECONDS,
                        help=f'Seconds between provider requests (minimum {MIN_PACE_SECONDS}).')
    parser.add_argument('--turns', type=int, default=6, help='Maximum model requests in the repair conversation.')
    parser.add_argument('--n8n-version', default=N8N_VERSION)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.plan:
        for number, step in enumerate(STEPS, 1):
            print(f'{number:2}. {step}', flush=True)
        return 0
    if not args.confirm_live:
        print('NOT RUN: live repair verification needs --confirm-live (disposable containers, real provider requests).', flush=True)
        return 0
    report = Report()
    try:
        run(args, report)
    except ProviderRateLimited as error:
        print(f'STOPPED: the provider rate-limited this run ({error}). Nothing is retried automatically.', flush=True)
    except Exception as error:
        print(f'STOPPED: {type(error).__name__}: {error}', flush=True)
    return report.finish()


if __name__ == '__main__':
    sys.exit(main())
