"""Pure, constrained Docker API templates for one n8n and external runner pair."""

from collections import deque
from ipaddress import ip_address
import re
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BundleSpec(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)

    name: str = Field(pattern=r'^[a-z0-9][a-z0-9-]*$', max_length=63)
    version: str = Field(pattern=r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$', max_length=32)
    port: int = Field(default=5678, ge=1024, le=65535)
    bind_host: str = '127.0.0.1'
    exporter_base_url: str = Field(max_length=2048)

    @field_validator('version')
    @classmethod
    def supported_version(cls, value: str) -> str:
        # Official separate JS/Python runner image is supported from 1.111.0.
        if tuple(map(int, value.split('.'))) < (1, 111, 0):
            raise ValueError('External runner bundles require n8n >=1.111.0')
        return value

    @field_validator('bind_host')
    @classmethod
    def literal_bind_address(cls, value: str) -> str:
        address = ip_address(value)
        if '%' in value or address.is_multicast or address.is_link_local:
            raise ValueError('Bind host must be an unscoped unicast IP address')
        return str(address)

    @field_validator('exporter_base_url')
    @classmethod
    def exporter_url(cls, value: str) -> str:
        if any(ord(char) <= 32 or ord(char) == 127 for char in value) or '\\' in value:
            raise ValueError('Invalid exporter URL')
        url = urlsplit(value)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment
                or url.path not in ('/api/otel', '/api/otel/')):
            raise ValueError('Exporter URL must be an HTTP(S) base ending /api/otel without credentials or query')
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError('Invalid exporter port')
        return value.rstrip('/')


def labels(owner_id: str, bundle_id: str, role: str) -> dict[str, str]:
    """Use application ownership, never generic compose labels, for management."""
    for value in (owner_id, bundle_id, role):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
            raise ValueError('Invalid Docker ownership identifier')
    return {'com.inference.managed': 'true', 'com.inference.owner': owner_id,
            'com.inference.bundle': bundle_id, 'com.inference.role': role}


def sort_graph(graph: dict[str, list[str]]) -> list[str]:
    """Kahn ordering; dependencies must be explicit nodes, including leaf nodes."""
    degrees = {node: len(set(dependencies)) for node, dependencies in graph.items()}
    dependents: dict[str, list[str]] = {node: [] for node in graph}
    for node, dependencies in graph.items():
        for dependency in dict.fromkeys(dependencies):
            if dependency not in graph:
                raise ValueError('Bundle graph has an unknown dependency')
            dependents[dependency].append(node)
    ready = deque(node for node, degree in degrees.items() if degree == 0)
    result: list[str] = []
    while ready:
        node = ready.popleft()
        result.append(node)
        for dependent in dependents[node]:
            degrees[dependent] -= 1
            if degrees[dependent] == 0:
                ready.append(dependent)
    if len(result) != len(graph):
        raise ValueError('Bundle graph contains a cycle')
    return result


def _healthcheck(url: str) -> dict[str, Any]:
    # Both official images contain node; no shell, wget or curl dependency.
    script = ("require('http').get('" + url + "',r=>{r.resume();process.exit(r.statusCode===200?0:1)})"
              ".on('error',()=>process.exit(1)).setTimeout(3000,function(){this.destroy();process.exit(1)})")
    return {'Test': ['CMD', 'node', '-e', script], 'Interval': 5_000_000_000,
            'Timeout': 4_000_000_000, 'Retries': 6, 'StartPeriod': 20_000_000_000}


def build(spec: BundleSpec, owner_id: str, bundle_id: str,
          runner_token: str, telemetry_token: str) -> dict[str, Any]:
    """Return Docker create bodies. Caller must journal effects and await health.

    Native n8n OTLP export is available from 2.19.0. Older versions need
    compatible instrumentation; injection alone does not enable that feature.
    """
    labels(owner_id, bundle_id, 'n8n')
    for token in (runner_token, telemetry_token):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,512}', token):
            raise ValueError('Invalid generated bundle token')
    stem = f'inference-{bundle_id}'
    network, volume = f'{stem}-network', f'{stem}-data'
    containers: dict[str, Any] = {}
    for role, image in [('n8n', f'docker.n8n.io/n8nio/n8n:{spec.version}'),
                        ('runners', f'n8nio/runners:{spec.version}')]:
        config: dict[str, Any] = {
            'Image': image, 'Labels': labels(owner_id, bundle_id, role),
            'Env': [f'N8N_RUNNERS_AUTH_TOKEN={runner_token}'],
            'HostConfig': {
                'NetworkMode': network, 'RestartPolicy': {'Name': 'unless-stopped'},
                'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges:true'],
                'Memory': 2 * 1024 ** 3, 'NanoCpus': 2_000_000_000, 'PidsLimit': 256,
                'LogConfig': {'Type': 'json-file', 'Config': {'max-size': '10m', 'max-file': '3'}},
            },
            'NetworkingConfig': {'EndpointsConfig': {network: {'Aliases': [role]}}},
            'Healthcheck': _healthcheck('http://127.0.0.1:5678/healthz/readiness' if role == 'n8n'
                                        else 'http://127.0.0.1:5680/healthz'),
        }
        containers[role] = {'name': f'{stem}-{role}', 'config': config}
    main = containers['n8n']['config']
    main['Env'] += [
        'N8N_RUNNERS_ENABLED=true', 'N8N_RUNNERS_MODE=external',
        'N8N_RUNNERS_BROKER_LISTEN_ADDRESS=0.0.0.0', 'N8N_RUNNERS_BROKER_PORT=5679',
        'N8N_NATIVE_PYTHON_RUNNER=true', 'N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS=true',
        'N8N_DIAGNOSTICS_ENABLED=false', 'N8N_OTEL_ENABLED=true',
        f'N8N_OTEL_EXPORTER_OTLP_ENDPOINT={spec.exporter_base_url}',
        'N8N_OTEL_EXPORTER_OTLP_TRACING_PATH=/v1/traces',
        f'N8N_OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer {telemetry_token}',
        f'OTEL_EXPORTER_OTLP_ENDPOINT={spec.exporter_base_url}',
        f'OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer {telemetry_token}',
        'OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf',
    ]
    main['ExposedPorts'] = {'5678/tcp': {}}
    main['HostConfig']['PortBindings'] = {
        '5678/tcp': [{'HostIp': spec.bind_host, 'HostPort': str(spec.port)}]}
    main['HostConfig']['Mounts'] = [{'Type': 'volume', 'Source': volume, 'Target': '/home/node/.n8n'}]
    runners = containers['runners']['config']
    runners['Env'] += ['N8N_RUNNERS_TASK_BROKER_URI=http://n8n:5679',
                       'N8N_RUNNERS_LAUNCHER_HEALTH_CHECK_PORT=5680']
    runners['Cmd'] = ['javascript', 'python']
    warnings = ['Container health does not verify JavaScript/Python execution or trace delivery.']
    if tuple(map(int, spec.version.split('.'))) < (2, 19, 0):
        warnings.insert(0, 'Native OTLP export requires n8n >=2.19.0; this older version '
                        'needs compatible instrumentation and will not export merely from these variables.')
    return {'network': {'Name': network, 'Driver': 'bridge', 'CheckDuplicate': True,
                        'Labels': labels(owner_id, bundle_id, 'network')},
            'volume': {'Name': volume, 'Labels': labels(owner_id, bundle_id, 'volume')},
            'containers': containers, 'order': sort_graph({'n8n': [], 'runners': ['n8n']}),
            'warnings': warnings}
