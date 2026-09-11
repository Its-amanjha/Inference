"""Real, disposable Docker-manager integration. Requires Docker Desktop with its Linux engine.

Uses a fresh application container, its managed n8n bundle and generated credentials.
Never attaches to an existing installation. Run after docker build -t inference:local .
"""
import json
import os
import secrets
import socket
import subprocess
import time

import httpx


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, timeout=90).strip()


def checked(response):
    if not response.is_success:
        # Do not print responses that may contain credentials or execution data.
        code = response.json().get('code', 'upstream_rejected') if response.headers.get('content-type','').startswith('application/json') else 'non_json'
        raise RuntimeError(f'{response.request.method} {response.request.url.path}: HTTP {response.status_code} ({code})')
    return response.json()


def main():
    name = 'inference-live-' + secrets.token_hex(6)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        n8n_port = sock.getsockname()[1]
    created = False
    authenticated = False
    bootstrap = secrets.token_urlsafe(32)
    os.environ['INFERENCE_BOOTSTRAP_TOKEN'] = bootstrap
    bundle_id = None
    joined_network = None
    client = None
    try:
        docker('run', '-d', '--name', name, '--label', 'inference.live-test=true',
               '--group-add', '0', '--publish', '127.0.0.1::3000',
               '--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
               '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,size=128m',
               '--tmpfs', '/data:rw,nosuid,size=128m,uid=10001,gid=10001',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
               '--env', 'INFERENCE_SECURE_COOKIES=false',
               '--env', 'INFERENCE_BOOTSTRAP_TOKEN',
               '--env', 'INFERENCE_EGRESS_ALLOW_CIDRS=127.0.0.1/32,192.168.0.0/16,172.16.0.0/12',
               'inference:local')
        created = True
        info = json.loads(docker('inspect', name))[0]
        port = info['NetworkSettings']['Ports']['3000/tcp'][0]['HostPort']
        base = f'http://127.0.0.1:{port}'
        client = httpx.Client(base_url=base, timeout=660, trust_env=False, headers={'Origin':base})
        for _ in range(60):
            try:
                if client.get('/healthz', timeout=2).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(1)
        checked(client.post('/api/auth/setup', json={'username':'live_owner','password':secrets.token_urlsafe(24)},headers={'X-Inference-Bootstrap':bootstrap}))
        authenticated = True
        client.headers['X-AGD-CSRF'] = client.cookies['agd_csrf']
        checked(client.get('/api/docker_mgr/status'))
        print('PASS: isolated Inference container reaches the real Docker engine.', flush=True)
        body = {'spec':{'name':'live-runner-check','version':os.getenv('N8N_TEST_VERSION','2.19.0'),
                        'port':n8n_port,'bind_host':'127.0.0.1',
                        'exporter_base_url':f'http://host.docker.internal:{port}/api/otel'},
                'instance_url':f'http://host.docker.internal:{n8n_port}',
                'idempotency_key':name,'readiness_timeout':180}
        print('Provisioning matching n8n/runners through the application API...', flush=True)
        result = checked(client.post('/api/docker_mgr/bundles', json=body))
        bundle_id = result['id']
        print('Bundle outcome:', result['status'], 'stage:', result['stage'], flush=True)
        if result['status'] != 'ready':
            raise RuntimeError('Managed bundle did not become ready: '+str(result.get('diagnostic')))
        assert checked(client.post('/api/docker_mgr/bundles',json=body))['id'] == bundle_id
        print('PASS: both containers healthy; provisioning replay returns the same bundle.', flush=True)
        joined_network = result['resources']['network']
        docker('network','connect',joined_network,name)
        with httpx.Client(base_url=f'http://127.0.0.1:{n8n_port}',trust_env=False,timeout=60) as n8n:
            checked(n8n.post('/rest/owner/setup',json={'email':'live@example.test','firstName':'Live','lastName':'Test','password':'Live9!'+secrets.token_urlsafe(24)}))
            # Keep production Secure cookies enabled. This loopback-only HTTP harness
            # explicitly forwards its freshly issued setup cookie for API-key creation.
            n8n.headers['Cookie'] = '; '.join(f'{c.name}={c.value}' for c in n8n.cookies.jar)
            scopes=checked(n8n.get('/rest/api-keys/scopes'))['data']
            key=checked(n8n.post('/rest/api-keys',json={'label':'Disposable integration','scopes':scopes,'expiresAt':None}))['data']['rawApiKey']
            checked(client.patch('/api/n8n_proxy/instances/'+result['instance_id'],json={'api_key':key,'url':'http://n8n:5678'}))
            n8n.headers['X-N8N-API-KEY']=key
            path='live-'+secrets.token_hex(8)
            workflow={'name':'Live JS and Python','settings':{'executionOrder':'v1'},'nodes':[
                {'id':'webhook','name':'Webhook','type':'n8n-nodes-base.webhook','typeVersion':2,'position':[0,0],'webhookId':path,'parameters':{'path':path,'httpMethod':'GET','responseMode':'lastNode'}},
                {'id':'js','name':'JavaScript','type':'n8n-nodes-base.code','typeVersion':2,'position':[250,0],'parameters':{'jsCode':'return [{json:{javascript:21*2}}];'}},
                {'id':'py','name':'Python','type':'n8n-nodes-base.code','typeVersion':2,'position':[500,0],'parameters':{'language':'pythonNative','pythonCode':'return [{"json": {"javascript": _items[0]["json"]["javascript"], "python": 6 * 7}}]'}}],
                'connections':{'Webhook':{'main':[[{'node':'JavaScript','type':'main','index':0}]]},'JavaScript':{'main':[[{'node':'Python','type':'main','index':0}]]}}}
            wf=checked(n8n.post('/api/v1/workflows',json=workflow))
            checked(n8n.post('/api/v1/workflows/'+wf['id']+'/activate'))
            output=checked(n8n.get('/webhook/'+path))
            assert output == {'javascript':42,'python':42}, 'Unexpected runner output'
            print('PASS: real webhook executed JavaScript and native Python, both returned 42.',flush=True)
            rows=checked(client.get('/api/n8n_proxy/executions',params={'instance_id':result['instance_id']}))['data']
            assert rows, 'Execution not visible through fleet proxy'
            for _ in range(30):
                summary=checked(client.get('/api/otel/summary',params={'instance_id':result['instance_id']}))
                if summary['spans']:
                    break
                time.sleep(2)
            assert summary['spans'] > 0, 'Native OTLP did not arrive within 60 seconds'
            print('PASS: execution visible through Inference; native OTLP spans received for its instance.',flush=True)
    finally:
        if joined_network:
            docker('network','disconnect',joined_network,name)
        # Discover any journaled outcome even if provisioning lost its response.
        if client is not None and authenticated:
            try:
                for row in checked(client.get('/api/docker_mgr/bundles'))['data']:
                    outcome=checked(client.delete('/api/docker_mgr/bundles/'+row['id'],params={'purge_data':'true'}))
                    if outcome['status'] != 'removed':
                        raise RuntimeError('Bundle cleanup incomplete')
                print('Managed test containers/network/data removed through ownership-checked API.',flush=True)
            except Exception:
                print('Cleanup incomplete; retaining isolated app container for reconciliation: '+name,flush=True)
                created=False
                raise
            finally:
                client.close()
        if created:
            docker('rm','-f',name)


if __name__ == '__main__':
    main()
