"""Verify the built image in a uniquely named disposable container and volume."""
import json
import os
import secrets
import subprocess
import time

import httpx


def docker(*arguments, **kwargs):
    return subprocess.check_output(['docker', *arguments], text=True, timeout=60, **kwargs).strip()


def main():
    name = 'inference-smoke-' + secrets.token_hex(6)
    volume = name + '-data'
    env = {**os.environ, 'INFERENCE_BOOTSTRAP_TOKEN': secrets.token_urlsafe(32),
           'INFERENCE_SECURE_COOKIES': 'false'}
    created = False
    docker('volume', 'create', '--label', 'inference.smoke=true', volume)
    try:
        docker('run', '-d', '--name', name, '--label', 'inference.smoke=true',
               '--publish', '127.0.0.1::3000', '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,size=128m',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
               '--mount', f'type=volume,src={volume},dst=/data',
               '--env', 'INFERENCE_BOOTSTRAP_TOKEN', '--env', 'INFERENCE_SECURE_COOKIES',
               'inference:local', env=env)
        created = True
        info = json.loads(docker('inspect', name))[0]
        port = info['NetworkSettings']['Ports']['3000/tcp'][0]['HostPort']
        assert docker('exec', name, 'id', '-u') == '10001'
        print('Container started as UID 10001.', flush=True)
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=5) as client:
            def ready():
                for _ in range(15):
                    try:
                        if client.get('/healthz').status_code == 200:
                            return
                    except httpx.TransportError:
                        pass
                    time.sleep(1)
                raise RuntimeError('Container did not become ready')
            ready()
            print('HTTP ready.', flush=True)
            assert client.get('/').status_code == 200
            assert client.get('/js/views/notes.js').status_code == 200
            password = secrets.token_urlsafe(24)
            account = {'username':'container_owner', 'password':password}
            result = client.post('/api/auth/setup', json=account,
                headers={'X-Inference-Bootstrap':env['INFERENCE_BOOTSTRAP_TOKEN']})
            assert result.status_code == 200, result.text
            print('Bootstrap passed.', flush=True)
            headers = {'X-AGD-CSRF':client.cookies['agd_csrf']}
            result = client.put('/api/notes/note', headers=headers,
                json={'path':'Container.md','content':'Persistent container smoke','expected_revision':None})
            assert result.status_code == 200, result.text
            docker('restart', name)
            # Docker can allocate a new ephemeral host port after restart.
            info = json.loads(docker('inspect', name))[0]
            port = info['NetworkSettings']['Ports']['3000/tcp'][0]['HostPort']
            client.base_url = f'http://127.0.0.1:{port}'
            print('Container restarted; checking its current host port.', flush=True)
            ready()
            result = client.post('/api/auth/login', json=account)
            assert result.status_code == 200, result.text
            assert client.get('/api/notes/note', params={'path':'Container.md'}).json()['content'] == 'Persistent container smoke'
            assert client.get('/api/admin/settings').status_code == 200
        print('PASS: built image runs as UID 10001 with read-only root; frontend/auth/notes work and persisted data survives restart.')
    finally:
        if created:
            docker('rm', '-f', name)
        docker('volume', 'rm', volume)


if __name__ == '__main__':
    main()
