import pytest
from pydantic import ValidationError

from backend.modules.docker_mgr.bundle import BundleSpec, build, labels, sort_graph


def spec(**changes):
    return BundleSpec(**({'name': 'test-fleet', 'version': '1.111.0',
                         'exporter_base_url': 'http://inference:3000/api/otel'} | changes))


def test_topological_order_and_invalid_graph():
    graph = {'runners': ['n8n'], 'n8n': [], 'worker': ['n8n', 'runners']}
    assert sort_graph(graph) == ['n8n', 'runners', 'worker']
    assert sort_graph({}) == []
    for graph in ({'a': ['a']}, {'a': ['b'], 'b': ['a']}, {'a': ['missing']}):
        with pytest.raises(ValueError):
            sort_graph(graph)
    assert sort_graph({'a': [], 'b': ['a', 'a']}) == ['a', 'b']


@pytest.mark.parametrize('changes', [
    {'name': '../bad'}, {'name': 'UPPER'}, {'name': ''}, {'name': 'a' * 64},
    {'version': 'latest'}, {'version': '1.111'}, {'version': '1.111.0@sha256:evil'},
    {'version': '1.110.9'}, {'version': '01.111.0'}, {'port': True},
    {'port': '5678'}, {'port': 80}, {'port': 65536}, {'bind_host': 'localhost'},
    {'bind_host': 'fe80::1%eth0'}, {'bind_host': '224.0.0.1'},
    {'exporter_base_url': 'file:///api/otel'},
    {'exporter_base_url': 'http://user:secret@host/api/otel'},
    {'exporter_base_url': 'http://host/api/otel?token=x'},
    {'exporter_base_url': 'http://host/api/otel/v1/traces'},
    {'exporter_base_url': 'http://host/api/otel#x'},
    {'privileged': True}, {'binds': ['/var/run/docker.sock:/socket']},
])
def test_spec_rejects_unsafe_or_unsupported_values(changes):
    with pytest.raises(ValidationError):
        spec(**changes)


def test_bundle_isolated_network_volume_ports_and_matching_runner_settings():
    bundle = build(spec(), 'owner1', 'bundle1', 'runner-secret', 'otel-secret')
    other = build(spec(), 'owner1', 'bundle2', 'different-runner', 'different-otel')
    assert bundle['order'] == ['n8n', 'runners']
    network = bundle['network']['Name']
    volume = bundle['volume']['Name']
    assert network != other['network']['Name']
    assert volume != other['volume']['Name']
    configs = {role: item['config'] for role, item in bundle['containers'].items()}
    main, runners = configs['n8n'], configs['runners']
    assert main['Image'] == 'docker.n8n.io/n8nio/n8n:1.111.0'
    assert runners['Image'] == 'n8nio/runners:1.111.0'
    env = {role: dict(value.split('=', 1) for value in config['Env'])
           for role, config in configs.items()}
    assert env['n8n']['N8N_RUNNERS_MODE'] == 'external'
    assert env['n8n']['N8N_NATIVE_PYTHON_RUNNER'] == 'true'
    assert env['n8n']['N8N_RUNNERS_BROKER_LISTEN_ADDRESS'] == '0.0.0.0'
    assert env['n8n']['N8N_RUNNERS_AUTH_TOKEN'] == env['runners']['N8N_RUNNERS_AUTH_TOKEN'] == 'runner-secret'
    assert env['runners']['N8N_RUNNERS_TASK_BROKER_URI'] == 'http://n8n:5679'
    assert env['n8n']['OTEL_EXPORTER_OTLP_HEADERS'] == 'Authorization=Bearer otel-secret'
    assert 'otel-secret' not in str(runners)
    assert main['HostConfig']['PortBindings'] == {'5678/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '5678'}]}
    assert not runners['HostConfig'].get('PortBindings')
    assert main['HostConfig']['Mounts'] == [{'Type': 'volume', 'Source': volume, 'Target': '/home/node/.n8n'}]
    for role, config in configs.items():
        assert config['Labels'] == labels('owner1', 'bundle1', role)
        assert set(config['NetworkingConfig']['EndpointsConfig']) == {network}
        assert config['HostConfig']['CapDrop'] == ['ALL']
        assert config['HostConfig']['SecurityOpt'] == ['no-new-privileges:true']
        assert config['HostConfig']['Memory'] > 0
        assert config['HostConfig']['PidsLimit'] > 0
        assert not config['HostConfig'].get('Binds')
        assert not config['HostConfig'].get('Privileged')
        assert config['Healthcheck']['Test'][0] == 'CMD'
    assert '5678/healthz/readiness' in main['Healthcheck']['Test'][-1]
    assert '5680/healthz' in runners['Healthcheck']['Test'][-1]
    assert bundle['warnings']


def test_bind_ipv6_and_exporter_url_validation():
    assert spec(bind_host='::1', exporter_base_url='https://host/api/otel/').exporter_base_url == 'https://host/api/otel'


def test_native_otel_headers_and_version_specific_warning():
    older = build(spec(), 'owner', 'old', 'runner-secret', 'otel-secret')
    native = build(spec(version='2.19.0'), 'owner', 'new', 'runner-secret', 'otel-secret')
    main = native['containers']['n8n']['config']
    env = dict(item.split('=', 1) for item in main['Env'])
    assert env['N8N_OTEL_EXPORTER_OTLP_HEADERS'] == 'Authorization=Bearer otel-secret'
    assert env['N8N_OTEL_EXPORTER_OTLP_TRACING_PATH'] == '/v1/traces'
    assert any('2.19.0' in warning for warning in older['warnings'])
    assert not any('requires' in warning for warning in native['warnings'])
    assert native['containers']['runners']['config']['Cmd'] == ['javascript', 'python']


@pytest.mark.parametrize('owner,bundle', [('owner', '../bad'), ('', 'bundle'), ('bad\nowner', 'bundle')])
def test_internal_identifiers_cannot_escape_names(owner, bundle):
    with pytest.raises(ValueError):
        build(spec(), owner, bundle, 'runner-secret', 'otel-secret')
