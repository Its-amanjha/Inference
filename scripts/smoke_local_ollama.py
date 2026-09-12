"""Run the local assistant check with a disposable CPU Ollama server.

Model cache is retained in a labeled Docker volume; server is removed on exit.
"""
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path
import httpx


def docker(*args):
    return subprocess.check_output(['docker',*args],text=True,timeout=90).strip()


def main():
    name='inference-ollama-test-'+secrets.token_hex(6)
    volume='inference-ollama-integration-cache'
    listing=docker('volume','ls','--filter','name=^'+volume+'$','--format','{{.Name}}')
    if listing:
        labels=json.loads(docker('volume','inspect',volume))[0]['Labels'] or {}
        if labels.get('inference.integration')!='ollama':
            raise RuntimeError('Refusing an existing unowned model cache volume')
    else:
        docker('volume','create','--label','inference.integration=ollama',volume)
    created=False
    try:
        docker('run','-d','--name',name,'--label','inference.integration=ollama',
               '--publish','127.0.0.1::11434','--mount',f'type=volume,src={volume},dst=/root/.ollama',
               '--cap-drop','ALL','--security-opt','no-new-privileges:true',
               '--memory','3g','--cpus','4','--env','OLLAMA_CONTEXT_LENGTH=2048',
               '--env','OLLAMA_NUM_PARALLEL=1','ollama/ollama:0.34.0')
        created=True
        info=json.loads(docker('inspect',name))[0]
        port=info['NetworkSettings']['Ports']['11434/tcp'][0]['HostPort']
        base=f'http://127.0.0.1:{port}'
        with httpx.Client(base_url=base,trust_env=False,timeout=900) as client:
            for _ in range(30):
                try:
                    if client.get('/api/version',timeout=2).status_code==200:break
                except httpx.TransportError:pass
                time.sleep(1)
            print('Ollama runtime:',client.get('/api/version').json()['version'],flush=True)
            print('Downloading qwen3:0.6b into the dedicated integration cache...',flush=True)
            last=-1
            with client.stream('POST','/api/pull',json={'model':'qwen3:0.6b','stream':True}) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    item=json.loads(line)
                    if 'error' in item:raise RuntimeError('Ollama model pull failed')
                    if item.get('total'):
                        step=int(item.get('completed',0)*4/item['total'])
                        if step>last:
                            print('Model download:',min(step*25,100),'%',flush=True);last=step
            # Warm up separately; app request timeout must remain bounded.
            response=client.post('/api/chat',json={'model':'qwen3:0.6b','messages':[{'role':'user','content':'Say ready.'}],'think':False,'stream':False,'options':{'num_predict':8}})
            response.raise_for_status()
            print('Local model loaded on CPU.',flush=True)
        subprocess.run([sys.executable,'scripts/smoke_assistant_integrations.py','--mcp-python',
                        'test-results/mcp-sdk-env/Scripts/python.exe','--ollama-url',base],check=True)
    finally:
        if created:docker('rm','-f',name)
        print('Temporary Ollama server removed; model cache retained in '+volume,flush=True)


if __name__=='__main__':main()
