"""Exercise the real local backend in a temporary directory; no user data touched."""
from pathlib import Path
import gzip
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest, ExportTraceServiceResponse

ROOT=Path(__file__).resolve().parents[1]


def main() -> None:
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0))
        port=reservation.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='inference-smoke-') as folder:
        env={**os.environ,'INFERENCE_DATA_DIR':folder,'INFERENCE_SECURE_COOKIES':'false',
             'INFERENCE_BOOTSTRAP_TOKEN':'','INFERENCE_ALLOWED_ORIGINS':''}
        env.pop('INFERENCE_SESSION_HOURS',None)
        password=secrets.token_urlsafe(24)
        secret=secrets.token_urlsafe(24)
        server=None
        log_path=Path(folder)/'server.log'

        def start(log):
            process=subprocess.Popen([sys.executable,'-m','uvicorn','backend.main:create_app','--factory',
                                      '--host','127.0.0.1','--port',str(port),'--no-proxy-headers','--no-access-log'],
                                     cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            return process

        def stop(process):
            if process is not None and process.poll() is None:
                if os.name=='nt':
                    # Windows venv Python can be a launcher with a server child.
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],
                                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False,
                                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                else:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

        def ready(client,process):
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                if process.poll() is not None:
                    raise RuntimeError(log_path.read_text())
                try:
                    if client.get('/healthz').status_code==200:
                        return
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            raise RuntimeError('Local backend did not become healthy in 15 seconds')

        with log_path.open('w') as log, httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=2) as client:
            try:
                server=start(log)
                ready(client,server)
                assert client.get('/api/admin/users').status_code==401
                assert client.post('/api/auth/setup',json={'username':'owner','password':password}).status_code==200
                csrf={'X-AGD-CSRF':client.cookies['agd_csrf']}
                assert client.post('/api/admin/secrets',json={'name':'SMOKE','value':secret}).status_code==403
                assert client.post('/api/admin/secrets',headers=csrf,json={'name':'SMOKE','value':secret}).status_code==200
                assert client.get('/api/admin/secrets').json()['data']==['SMOKE']
                assert secret not in (Path(folder)/'secrets.json').read_text()
                instance=client.post('/api/n8n_proxy/instances',headers=csrf,json={'name':'Smoke telemetry','url':'https://smoke-fixture.invalid','api_key':secrets.token_urlsafe(24)})
                assert instance.status_code==200,instance.text
                instance_id=instance.json()['id']
                issued=client.post('/api/otel/tokens',headers=csrf,json={'instance_id':instance_id,'name':'Smoke exporter'})
                assert issued.status_code==200,issued.text
                telemetry_headers={'Authorization':'Bearer '+issued.json()['token'],'Content-Type':'application/x-protobuf','Content-Encoding':'gzip'}
                traces=ExportTraceServiceRequest()
                span=traces.resource_spans.add().scope_spans.add().spans.add()
                span.trace_id=bytes.fromhex('a'*32)
                span.span_id=bytes.fromhex('b'*16)
                span.name='Local smoke'
                span.start_time_unix_nano=100
                span.end_time_unix_nano=200
                payload=gzip.compress(traces.SerializeToString())
                assert client.post('/api/auth/logout',headers=csrf).status_code==200
                assert client.get('/api/admin/users').status_code==401
                telemetry=client.post('/api/otel/v1/traces',headers=telemetry_headers,content=payload)
                assert telemetry.status_code==200,telemetry.text
                assert ExportTraceServiceResponse.FromString(telemetry.content).partial_success.rejected_spans==0
                stop(server)
                server=start(log)
                ready(client,server)
                assert client.post('/api/auth/login',json={'username':'owner','password':password}).status_code==200
                assert client.get('/api/admin/secrets').json()['data']==['SMOKE']
                assert client.get('/api/admin/audit').json()['data']
                assert client.post('/api/otel/v1/traces',headers=telemetry_headers,content=payload).status_code==200
                detail=client.get('/api/otel/traces/'+'a'*32,params={'instance_id':instance_id})
                assert detail.status_code==200 and len(detail.json()['data'])==1
                csrf={'X-AGD-CSRF':client.cookies['agd_csrf']}
                assert client.put('/api/notes/note',headers=csrf,json={'path':'Smoke.md','content':'Restore smoke'}).status_code==200
                policy=client.get('/api/admin/settings').json()
                changed=client.put('/api/admin/settings',headers=csrf,json={'expected_revision':policy['revision'],
                    'session_hours':2,'default_preferences':{'theme':'light','page_size':50,'error_notifications':False}})
                assert changed.status_code==200,changed.text
                template=client.post('/api/admin/secret-templates/api_key/apply',headers=csrf,
                    json={'name':'SMOKE_KEY','values':{'api_key':secret}})
                assert template.status_code==200,template.text
                scheduled=client.post('/api/backups/schedules',headers=csrf,json={'name':'Smoke backup','passphrase_ref':'$SMOKE'})
                assert scheduled.status_code==200,scheduled.text
                scheduled_id=scheduled.json()['id']
                run=client.post('/api/backups/schedules/'+scheduled_id+'/run',headers=csrf,timeout=15)
                assert run.status_code==200 and run.json()['status']=='succeeded',run.text
                assert client.get('/api/backups/runs/'+run.json()['id']+'/download').status_code==200
                export_password=secrets.token_urlsafe(32)
                exported=client.post('/api/backups/export',headers=csrf,json={'passphrase':export_password},timeout=15)
                assert exported.status_code==200,exported.text
                stop(server)
                sys.path.insert(0,str(ROOT))
                from backend.modules.backups.restore import restore_archive
                from backend.config import Vault
                restored=Path(folder)/'restored'
                restore_archive(exported.content,export_password,restored)
                assert Vault(restored).resolve('$SMOKE')==secret
                assert Vault(restored).resolve('$SMOKE_KEY.api_key')==secret
                env['INFERENCE_DATA_DIR']=str(restored)
                client.cookies.clear()
                server=start(log)
                ready(client,server)
                assert client.post('/api/auth/login',json={'username':'owner','password':password}).status_code==200
                assert client.get('/api/notes/note',params={'path':'Smoke.md'}).json()['content']=='Restore smoke'
                assert client.get('/api/admin/settings').json()['session_hours']==2
                assert client.get('/api/admin/preferences').json()['theme']=='light'
                assert client.get('/api/backups/schedules').json()['data'][0]['id']==scheduled_id
                assert client.get('/api/backups/runs').json()['data'][0]['id']==run.json()['id']
                assert client.post('/api/otel/v1/traces',headers=telemetry_headers,content=payload).status_code==200
                assert len(client.get('/api/otel/traces/'+'a'*32,params={'instance_id':instance_id}).json()['data'])==1
            finally:
                stop(server)
    print('PASS: real Uvicorn auth/CSRF, admin settings/templates, backup/download/history, export/restore, restored settings/vault/notes/telemetry/schedules, audit and cleanup.')


if __name__=='__main__':
    main()
