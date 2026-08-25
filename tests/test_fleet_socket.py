"""Loopback-only integration through the real pinned HTTP transport."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import httpx
import pytest

from backend.config import Settings
from backend.main import create_app


@pytest.mark.asyncio
async def test_real_http_transport_reads_and_mutates_only_selected_local_fixture(tmp_path):
    state={'active':False,'calls':[]}

    class N8nFixture(BaseHTTPRequestHandler):
        def handle_api(self):
            state['calls'].append((self.command,self.path))
            if self.headers.get('X-N8N-API-KEY')!='local-fixture-secret':
                self.send_error(401)
                return
            if self.command=='POST' and self.path=='/api/v1/workflows/local/activate':
                state['active']=True
                body={'id':'local','name':'Local test workflow','active':True}
            elif self.command=='GET' and self.path.startswith('/api/v1/workflows?'):
                body={'data':[{'id':'local','name':'Local test workflow','active':state['active']}],'nextCursor':None}
            elif self.command=='GET' and self.path.startswith('/api/v1/executions?'):
                body={'data':[],'nextCursor':None}
            else:
                self.send_error(404)
                return
            raw=json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET=handle_api
        do_POST=handle_api

        def log_message(self,*args):
            pass

    server=ThreadingHTTPServer(('127.0.0.1',0),N8nFixture)
    thread=Thread(target=server.serve_forever,daemon=True)
    thread.start()
    port=server.server_address[1]
    settings=Settings(data_dir=tmp_path,allow_cidrs=('127.0.0.1/32',))
    app=create_app(settings=settings)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',50000)),base_url='https://localhost') as browser:
                assert (await browser.post('/api/auth/setup',json={'username':'owner','password':'local test password of enough length'})).status_code==200
                csrf={'X-AGD-CSRF':browser.cookies['agd_csrf']}
                added=await browser.post('/api/n8n_proxy/instances',headers=csrf,json={'name':'Socket fixture','url':f'http://127.0.0.1:{port}','api_key':'local-fixture-secret'})
                assert added.status_code==200,added.text
                identifier=added.json()['id']
                probe=await browser.post(f'/api/n8n_proxy/instances/{identifier}/probe',headers=csrf)
                assert probe.status_code==200,probe.text
                assert probe.json()['capabilities']=={'workflow_read':'supported','execution_read':'supported'}
                activated=await browser.post('/api/n8n_proxy/workflows/local/activate',params={'instance_id':identifier},headers=csrf)
                assert activated.status_code==200,activated.text
                assert state['active'] is True
                workflows=await browser.get('/api/n8n_proxy/workflows',params={'instance_id':identifier})
                assert workflows.json()['data'][0]['active'] is True
                assert sum(method=='POST' for method,_ in state['calls'])==1
                removed=await browser.delete(f'/api/n8n_proxy/instances/{identifier}',headers=csrf)
                assert removed.status_code==200
                assert state['active'] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
