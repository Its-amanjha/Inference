"""Exercise the installed SDK against a loopback-only simulated Docker engine."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit, parse_qs
import hashlib
import json

from aiodocker import Docker

from test_fleet import fleet


async def test_real_sdk_provision_and_owned_teardown(fleet):
    resources = {'networks': {}, 'volumes': {}, 'containers': {}}
    effects = []
    class Engine(BaseHTTPRequestHandler):
        def handle_engine(self):
            url = urlsplit(self.path)
            path = url.path.removeprefix('/v1.45').strip('/')
            bits = path.split('/')
            body = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if int(self.headers.get('Content-Length', '0')) else {}
            status, result = 200, {}
            if path == 'version':
                result = {'ApiVersion': '1.45', 'Version': 'fixture'}
            elif path == 'images/create':
                result = {'status': 'fixture image available'}
            elif self.command == 'POST' and len(bits) == 2 and bits[1] == 'create':
                kind = bits[0]
                if kind == 'containers':
                    name = parse_qs(url.query)['name'][0]
                    identifier = hashlib.sha256(name.encode()).hexdigest()
                    resources[kind][identifier] = {'Id': identifier, 'Name': '/' + name, 'Config': body,
                        'State': {'Running': False, 'Status': 'created', 'Health': {'Status': 'starting'}}}
                    result = {'Id': identifier}
                else:
                    identifier = hashlib.sha256(body['Name'].encode()).hexdigest() if kind == 'networks' else body['Name']
                    resources[kind][identifier] = {**body, 'Id': identifier}
                    result = resources[kind][identifier]
                effects.append(('create', kind))
                status = 201
            elif bits[0] in resources and len(bits) >= 2:
                kind, identifier = bits[:2]
                found = resources[kind].get(identifier)
                if found is None:
                    found = next((v for v in resources[kind].values() if v.get('Name', '').lstrip('/') == identifier), None)
                if found is None:
                    status, result = 404, {'message': 'not found'}
                elif self.command == 'DELETE':
                    resources[kind].pop(found['Id'])
                    effects.append(('delete', kind))
                    status = 204
                elif self.command == 'POST' and bits[-1] in ('start', 'stop'):
                    running = bits[-1] == 'start'
                    found['State'] = {'Running': running, 'Status': 'running' if running else 'exited', 'Health': {'Status': 'healthy'}}
                    status = 204
                else:
                    result = found
            else:
                status, result = 404, {'message': 'unexpected fixture route'}
            encoded = json.dumps(result).encode() + b'\n' if status != 204 else b''
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        do_GET = handle_engine
        do_POST = handle_engine
        do_DELETE = handle_engine
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Engine)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    sdk = Docker(url=f'http://127.0.0.1:{server.server_address[1]}', api_version='v1.45')
    fleet.app.state.docker = sdk
    try:
        response = await fleet.client.post('/api/docker_mgr/bundles', headers=fleet.headers, json={
            'spec': {'name': 'sdk-fixture', 'version': '2.19.0', 'exporter_base_url': 'https://inference.example/api/otel'},
            'instance_url': 'https://local-n8n.example', 'idempotency_key': 'socket-test'})
        assert response.status_code == 200, response.text
        state = response.json()
        assert state['status'] == 'ready'
        assert len(resources['containers']) == 2
        network_id = state['resources']['network']
        assert all(c['Config']['HostConfig']['NetworkMode'] == network_id for c in resources['containers'].values())
        assert any('N8N_OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer ' in env
                   for c in resources['containers'].values() for env in c['Config']['Env'])
        response = await fleet.client.delete('/api/docker_mgr/bundles/' + state['id'], headers=fleet.headers)
        assert response.status_code == 200, response.text
        assert not resources['containers'] and not resources['networks']
        assert len(resources['volumes']) == 1
        assert ('delete', 'volumes') not in effects
    finally:
        await sdk.close()
        fleet.app.state.docker = None
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
