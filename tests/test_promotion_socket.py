"""Loopback promotion integration through the production pinned transport."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from urllib.parse import urlsplit

import httpx
import pytest

from backend.config import Settings
from backend.main import create_app


@pytest.mark.asyncio
async def test_real_http_promotion_is_inactive_isolated_and_idempotent(tmp_path):
    original = {
        'id': 'source-wf', 'name': 'Loopback promotion', 'active': True,
        'nodes': [{'id': 'start', 'name': 'Start', 'type': 'n8n-nodes-base.manualTrigger',
                   'typeVersion': 1, 'position': [0, 0], 'parameters': {}}],
        'connections': {}, 'settings': {},
    }
    state = {'source': copy.deepcopy(original), 'target': None, 'calls': [], 'writes': []}

    class N8nFixture(BaseHTTPRequestHandler):
        def handle_api(self):
            path = urlsplit(self.path).path
            state['calls'].append((self.command, path))
            instance = path.split('/')[1]
            if instance not in {'source', 'target'}:
                self.send_error(404)
                return
            if self.headers.get('X-N8N-API-KEY') != f'{instance}-fixture-key':
                self.send_error(401)
                return
            if self.headers.get('Cookie'):
                self.send_error(400)
                return
            if self.command == 'GET' and path == '/source/api/v1/workflows/source-wf':
                body = state['source']
            elif self.command == 'GET' and path == '/target/api/v1/credentials':
                body = {'data': [], 'nextCursor': None}
            elif self.command == 'GET' and path == '/target/api/v1/workflows/target-wf' and state['target']:
                body = state['target']
            elif self.command == 'POST' and path == '/target/api/v1/workflows':
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                state['writes'].append(payload)
                state['target'] = {**payload, 'id': 'target-wf', 'active': False}
                body = state['target']
            else:
                self.send_error(404)
                return
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = handle_api
        do_POST = handle_api

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), N8nFixture)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    app = create_app(settings=Settings(data_dir=tmp_path, allow_cidrs=('127.0.0.1/32',)))
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 50000)), base_url='https://localhost') as browser:
                account = {'username': 'owner', 'password': 'loopback promotion test password'}
                assert (await browser.post('/api/auth/setup', json=account)).status_code == 200
                assert (await browser.post('/api/auth/logout', headers={'X-AGD-CSRF': browser.cookies['agd_csrf']})).status_code == 200
                assert (await browser.post('/api/auth/login', json=account)).status_code == 200
                csrf = {'X-AGD-CSRF': browser.cookies['agd_csrf']}
                instances = {}
                for name in ('source', 'target'):
                    response = await browser.post('/api/n8n_proxy/instances', headers=csrf, json={
                        'name': name, 'url': f'http://127.0.0.1:{server.server_address[1]}/{name}',
                        'api_key': f'{name}-fixture-key',
                    })
                    assert response.status_code == 200, response.text
                    instances[name] = response.json()['id']
                preview = await browser.post('/api/n8n_promote/preflight', headers=csrf, json={
                    'source_instance_id': instances['source'], 'target_instance_id': instances['target'],
                    'workflow_id': 'source-wf',
                })
                assert preview.status_code == 200, preview.text
                assert preview.json()['can_deploy'] is True
                assert not state['writes']
                body = {'preview_id': preview.json()['preview_id'], 'idempotency_key': 'loopback-once'}
                deployed = await browser.post('/api/n8n_promote/deploy', headers=csrf, json=body)
                assert deployed.status_code == 200, deployed.text
                assert deployed.json()['status'] == 'deployed', deployed.text
                calls_after_deploy = len(state['calls'])
                repeated = await browser.post('/api/n8n_promote/deploy', headers=csrf, json=body)
                assert repeated.status_code == 200, repeated.text
                assert repeated.json()['id'] == deployed.json()['id']
                assert repeated.json()['status'] == 'deployed'
                assert len(state['calls']) == calls_after_deploy
                assert len(state['writes']) == 1
                assert set(state['writes'][0]) == {'name', 'nodes', 'connections', 'settings'}
                assert state['source'] == original
                assert state['target']['active'] is False
                assert state['target']['nodes'] == original['nodes']
                assert not any(method != 'GET' and path.startswith('/source/') for method, path in state['calls'])
                mapping = await app.state.db.fetchone('SELECT * FROM workflow_mappings WHERE source_instance_id=? AND target_instance_id=?', (instances['source'], instances['target']))
                assert mapping['source_workflow_id'] == 'source-wf'
                assert mapping['target_workflow_id'] == 'target-wf'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()
