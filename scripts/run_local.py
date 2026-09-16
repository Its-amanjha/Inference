"""Start Inference locally using the settings in .env.

The application reads configuration from the process environment only; this launcher loads .env
into that environment and starts uvicorn. Variables already set in your shell take precedence.

    python scripts/run_local.py
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        values[key.strip()] = value
    return values


def main() -> int:
    env_file = ROOT / '.env'
    if not env_file.is_file():
        print('No .env found. Copy .env.example to .env and adjust it.', file=sys.stderr)
        return 1
    env = {**read_env(env_file), **os.environ}
    host = env.get('INFERENCE_HOST') or '127.0.0.1'
    port = env.get('INFERENCE_PORT') or '3000'
    command = [sys.executable, '-m', 'uvicorn', 'backend.main:create_app', '--factory',
               '--host', host, '--port', port, '--no-proxy-headers']
    print(f'Starting Inference on http://{host}:{port}')
    return subprocess.call(command, cwd=ROOT, env=env)


if __name__ == '__main__':
    raise SystemExit(main())
