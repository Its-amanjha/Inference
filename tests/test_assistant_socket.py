"""Local model-shaped HTTP fixture through the real pinned egress transport."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import httpx

from backend.config import Settings
from backend.main import create_app


async def test_local_keyless_chat_through_production_transport(tmp_path):
    received = []
    class ModelFixture(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            raw = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': 'Local fixture reply'}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), ModelFixture)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    app = create_app(settings=Settings(data_dir=tmp_path, allow_cidrs=('127.0.0.1/32',)))
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 50000)), base_url='https://localhost') as browser:
                setup = await browser.post('/api/auth/setup', json={'username': 'owner', 'password': 'local model fixture password'})
                assert setup.status_code == 200
                csrf = {'X-AGD-CSRF': browser.cookies['agd_csrf']}
                configured = await browser.put('/api/assistant/config/provider', headers=csrf, json={'id': 'local', 'config': {
                    'kind': 'ollama', 'base_url': f'http://127.0.0.1:{server.server_address[1]}/v1',
                    'model': 'installed-model:exact-tag', 'tools_enabled': False}})
                assert configured.status_code == 200, configured.text
                response = await browser.post('/api/assistant/chat', headers=csrf, json={'provider_id': 'local', 'message': 'hello'})
                assert response.status_code == 200, response.text
                assert response.json()['content'] == 'Local fixture reply'
                path, headers, body = received[0]
                assert len(received) == 1 and path == '/v1/chat/completions'
                assert body['model'] == 'installed-model:exact-tag'
                assert all(name.lower() not in {'authorization', 'cookie', 'x-n8n-api-key'} for name in headers)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
