"""Real HTTP interoperability checks: official MCP SDK and optional local Ollama.

All Inference state is temporary. No external accounts or user execution payloads.
"""
import argparse
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


def checked(response):
    if not response.is_success:
        data=response.json()
        raise RuntimeError(f'{response.request.url.path}: HTTP {response.status_code}, {data.get("code","unknown")}')
    return response.json()


def start(args, log, env=None):
    return subprocess.Popen(args,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


def stop(process):
    if os.name == 'nt':
        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    else:
        process.terminate()
    process.wait(timeout=15)


def ready(url, process):
    with httpx.Client(trust_env=False,timeout=2) as client:
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError('Integration process exited; inspect its local test log.')
            try:
                if client.get(url).status_code < 500:
                    return
            except httpx.TransportError:
                pass
            time.sleep(.5)
    raise RuntimeError('Integration process did not start.')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--mcp-python',type=Path,required=True)
    parser.add_argument('--ollama-url')
    parser.add_argument('--model',default='qwen3:0.6b')
    args=parser.parse_args()
    artifacts=ROOT/'test-results'
    with tempfile.TemporaryDirectory(prefix='inference-assistant-') as folder:
        base=f'http://127.0.0.1:{port()}'
        env={**os.environ,'INFERENCE_DATA_DIR':folder,'INFERENCE_SECURE_COOKIES':'false',
             'INFERENCE_EGRESS_ALLOW_CIDRS':'127.0.0.1/32','INFERENCE_BOOTSTRAP_TOKEN':'', 'INFERENCE_ALLOWED_ORIGINS':''}
        with (artifacts/'assistant-integration.log').open('w') as log:
            app=start([sys.executable,'-m','uvicorn','backend.main:create_app','--factory','--host','127.0.0.1','--port',base.rsplit(':',1)[1],'--no-access-log'],log,env)
            try:
                ready(base+'/healthz',app)
                with httpx.Client(base_url=base,trust_env=False,timeout=90,headers={'Origin':base}) as client:
                    checked(client.post('/api/auth/setup',json={'username':'integration','password':secrets.token_urlsafe(24)}))
                    client.headers['X-AGD-CSRF']=client.cookies['agd_csrf']
                    for use_json in (False,True):
                        sdk_port=port()
                        counter=Path(folder)/f'calls-{use_json}.txt'
                        command=[str(args.mcp_python.resolve()),str(ROOT/'scripts/mcp_sdk_server.py'),'--port',str(sdk_port),'--counter',str(counter)]
                        if use_json:
                            command.append('--json-response')
                        sdk=start(command,log)
                        try:
                            url=f'http://127.0.0.1:{sdk_port}/mcp'
                            ready(url,sdk)
                            checked(client.put('/api/assistant/config/mcp',json={'id':'sdk','config':{'url':url}}))
                            tools=checked(client.get('/api/assistant/mcp/sdk/tools'))['data']
                            assert any(tool['name']=='add' for tool in tools)
                            payload={'name':'mcp_call','arguments':{'server_id':'sdk','tool_name':'add','arguments':{'a':19,'b':23}}}
                            proposal=checked(client.post('/api/assistant/tools/run',json=payload))['pending_action']
                            assert not counter.exists(), 'Tool ran before approval'
                            confirmed=checked(client.post('/api/assistant/tools/confirm',json={'action_id':proposal['id']}))
                            assert confirmed['status']=='succeeded'
                            assert '42' in str(confirmed['result']), 'SDK result missing'
                            assert counter.read_text()=='1'
                            assert client.post('/api/assistant/tools/confirm',json={'action_id':proposal['id']}).status_code==409
                            proposal=checked(client.post('/api/assistant/tools/run',json=payload))['pending_action']
                            checked(client.post('/api/assistant/tools/cancel',json={'action_id':proposal['id']}))
                            assert counter.read_text()=='1', 'Cancellation invoked tool'
                            print(f'PASS: official MCP SDK {"JSON" if use_json else "SSE"} discovery, approval-before-call, result 42, replay and cancellation.',flush=True)
                        finally:
                            stop(sdk)
                    if args.ollama_url:
                        checked(client.put('/api/assistant/config/provider',json={'id':'ollama','config':{'kind':'ollama','base_url':args.ollama_url.rstrip('/')+'/v1','model':args.model,'tools_enabled':False}}))
                        response=checked(client.post('/api/assistant/chat',json={'provider_id':'ollama','message':'/no_think Reply with the number that is six multiplied by seven. Use one short sentence.'}))
                        assert response['content'].strip(), 'Model response empty'
                        assert '42' in response['content'], 'Unexpected arithmetic response'
                        assert not response['pending_actions']
                        history=checked(client.get('/api/assistant/chats/'+response['id']))['data']
                        assert history[-1]['content']==response['content']
                        print('PASS: real local Ollama chat returned 42 and persisted conversation history.',flush=True)
                    else:
                        print('NOT RUN: Ollama model inference (provide --ollama-url).',flush=True)
            finally:
                stop(app)


if __name__=='__main__':
    main()
