"""SDK-shaped fixtures: never contact a Docker daemon or alter host containers."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from backend.modules.docker_mgr import client as module
from backend.modules.docker_mgr.client import DockerGateway, owned_labels


@pytest.fixture
def gateway():
    current = {'role': 'admin', 'disabled': 0,
               'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    tx = SimpleNamespace(fetchone=AsyncMock(side_effect=lambda *args: dict(current)))

    @asynccontextmanager
    async def transaction():
        yield tx

    docker = SimpleNamespace(version=AsyncMock(return_value={'Version': '27', 'secret': 'hide'}),
                             containers=SimpleNamespace(get=AsyncMock(), create=AsyncMock(), list=AsyncMock()),
                             networks=SimpleNamespace(get=AsyncMock(), create=AsyncMock()),
                             volumes=SimpleNamespace(get=AsyncMock(), create=AsyncMock()),
                             images=SimpleNamespace(pull=AsyncMock()))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(docker=docker,
                              docker_owner_id='owner', db=SimpleNamespace(transaction=transaction))),
                              state=SimpleNamespace(user={'role': 'admin', 'username': 'admin', 'session_id': 'session'}))
    result = DockerGateway(request)
    result.current = current
    return result


def container(gateway, *, labels=None, identifier='abc123'):
    data = {'Id': identifier, 'Name': '/inference',
            'Config': {'Image': 'n8nio/n8n:2.0.0', 'Env': ['TOKEN=SUPERSECRET'],
                       'Labels': owned_labels('owner', 'bundle', 'n8n') if labels is None else labels},
            'State': {'Running': True, 'Status': 'running', 'ExitCode': 0,
                      'Health': {'Status': 'healthy', 'Log': [{'Output': 'SUPERSECRET'}]}}}
    obj = SimpleNamespace(id=identifier, show=AsyncMock(return_value=data), start=AsyncMock(),
                          stop=AsyncMock(), delete=AsyncMock())
    gateway.client.containers.get.return_value = obj
    return obj, data


async def test_safe_inspection_and_sdk_mutation_arguments(gateway):
    obj, _ = container(gateway)
    result = await gateway.inspect_container('abc123', 'bundle', 'n8n')
    assert result['health'] == 'healthy'
    assert 'SUPERSECRET' not in str(result)
    await gateway.start_container('abc123', 'bundle', 'n8n')
    obj.start.assert_awaited_once_with()
    await gateway.stop_container('abc123', 'bundle', 'n8n')
    obj.stop.assert_awaited_once_with(t=10)
    await gateway.delete_container('abc123', 'bundle', 'n8n')
    obj.delete.assert_awaited_once_with(force=False, v=False)


@pytest.mark.parametrize('labels', [
    {'com.docker.compose.project': 'inference'},
    owned_labels('another-owner', 'bundle', 'n8n'),
    owned_labels('owner', 'another-bundle', 'n8n'),
    owned_labels('owner', 'bundle', 'runners'),
    # Ownership labels under any other namespace are not recognized.
    {key.replace('com.inference.', 'com.example.'): value for key, value in owned_labels('owner', 'bundle', 'n8n').items()},
    {'com.inference.managed': 'true', 'com.inference.owner': 'owner', 'com.inference.bundle': 'bundle',
     'com.example.role': 'n8n'},
])
async def test_unowned_containers_never_mutated(gateway, labels):
    obj, _ = container(gateway, labels=labels)
    with pytest.raises(HTTPException) as error:
        await gateway.delete_container('abc123', 'bundle', 'n8n')
    assert error.value.detail['code'] == 'docker_ownership'
    obj.delete.assert_not_awaited()


async def test_replaced_container_identity_is_rejected(gateway):
    obj, _ = container(gateway, identifier='replacement')
    with pytest.raises(HTTPException):
        await gateway.start_container('abc123', 'bundle', 'n8n')
    obj.start.assert_not_awaited()


async def test_reconciliation_reads_expected_name_but_mutations_require_id(gateway):
    obj, _ = container(gateway)
    result = await gateway.inspect_container('inference', 'bundle', 'n8n')
    assert result['id'] == 'abc123'
    with pytest.raises(HTTPException):
        await gateway.start_container('inference', 'bundle', 'n8n')
    obj.start.assert_not_awaited()
    await gateway.start_container(result['id'], 'bundle', 'n8n')
    obj.start.assert_awaited_once()


async def test_role_revoked_during_inspection_blocks_effect(gateway):
    obj, data = container(gateway)
    async def show():
        gateway.current['role'] = 'viewer'
        return data
    obj.show.side_effect = show
    with pytest.raises(HTTPException) as error:
        await gateway.stop_container('abc123', 'bundle', 'n8n')
    assert error.value.status_code == 403
    obj.stop.assert_not_awaited()


async def test_live_role_checked_even_with_cached_admin(gateway):
    gateway.current['disabled'] = 1
    with pytest.raises(HTTPException) as error:
        await gateway.status()
    assert error.value.status_code == 401
    gateway.client.version.assert_not_awaited()


async def test_unavailable_and_error_text_is_never_returned(gateway):
    gateway.client.version.side_effect = OSError('Authorization Bearer SUPERSECRET')
    with pytest.raises(HTTPException) as error:
        await gateway.status()
    assert error.value.status_code == 503
    assert error.value.detail['code'] == 'docker_unavailable'
    assert 'SUPERSECRET' not in str(error.value.detail)
    gateway.client = None
    with pytest.raises(HTTPException) as error:
        await gateway.status()
    assert error.value.status_code == 503


async def test_timeout_write_is_uncertain_without_retry(gateway):
    obj, _ = container(gateway)
    obj.start.side_effect = TimeoutError('SUPERSECRET')
    with pytest.raises(HTTPException) as error:
        await gateway.start_container('abc123', 'bundle', 'n8n')
    assert error.value.detail['code'] == 'docker_outcome_unknown'
    obj.start.assert_awaited_once()
    assert 'SUPERSECRET' not in str(error.value.detail)


async def test_known_docker_rejection_is_redacted(gateway):
    obj, _ = container(gateway)
    exc = RuntimeError('SUPERSECRET')
    exc.status = 409
    obj.delete.side_effect = exc
    with pytest.raises(HTTPException) as error:
        await gateway.delete_container('abc123', 'bundle', 'n8n')
    assert error.value.status_code == 409
    assert error.value.detail['code'] == 'docker_error'
    assert 'SUPERSECRET' not in str(error.value.detail)


async def test_creation_uses_create_not_run_or_replace(gateway):
    config = {'Image': 'n8nio/n8n:2.0.0', 'Labels': owned_labels('owner', 'bundle', 'n8n')}
    gateway.client.containers.create.return_value = SimpleNamespace(id='new')
    assert await gateway.create_container('inference-new', config) == 'new'
    gateway.client.containers.create.assert_awaited_once_with(config, name='inference-new')
    gateway.client.images.pull.assert_not_awaited()
    network = {'Name': 'inference-network','Labels': owned_labels('owner', 'bundle', 'network')}
    gateway.client.networks.create.return_value = SimpleNamespace(id='net')
    assert await gateway.create_network(network) == 'net'
    gateway.client.networks.create.assert_awaited_once_with({**network, 'CheckDuplicate': True})


async def test_volume_create_refuses_existing_name(gateway):
    with pytest.raises(HTTPException) as error:
        await gateway.create_volume({'Name': 'existing', 'Labels': owned_labels('owner', 'bundle', 'volume')})
    assert error.value.detail['code'] == 'docker_ownership'
    gateway.client.volumes.create.assert_not_awaited()


async def test_volume_race_checks_returned_ownership(gateway):
    absent = RuntimeError('absent')
    absent.status = 404
    obj = SimpleNamespace(name='new', show=AsyncMock(return_value={'Name': 'new', 'Labels': {}}))
    gateway.client.volumes.get.side_effect = [absent, obj]
    gateway.client.volumes.create.return_value = obj
    with pytest.raises(HTTPException) as error:
        await gateway.create_volume({'Name': 'new', 'Labels': owned_labels('owner', 'bundle', 'volume')})
    assert error.value.detail['code'] == 'docker_ownership'
    gateway.client.volumes.create.assert_awaited_once()


@pytest.mark.parametrize('role', ['network', 'volume'])
async def test_network_and_volume_ownership_checks(gateway, role):
    data = {'Id': 'resource', 'Name': 'resource', 'Labels': owned_labels('owner', 'bundle', role)}
    obj = SimpleNamespace(id='resource', name='resource', show=AsyncMock(return_value=data), delete=AsyncMock())
    getattr(gateway.client, role + 's').get.return_value = obj
    await getattr(gateway, 'delete_' + role)('resource', 'bundle')
    obj.delete.assert_awaited_once_with()
    data['Labels'] = owned_labels('attacker', 'bundle', role)
    with pytest.raises(HTTPException):
        await getattr(gateway, 'delete_' + role)('resource', 'bundle')
    assert obj.delete.await_count == 1


async def test_inventory_filters_owned_resources_and_redacts(gateway):
    good, _ = container(gateway)
    bad, _ = container(gateway, labels={'com.docker.compose.project': 'inference'})
    runners, _ = container(gateway, identifier='runners1', labels=owned_labels('owner', 'other-bundle', 'runners'))
    gateway.client.containers.list.return_value = [good, bad, runners]
    data = await gateway.inventory()
    assert [(item['id'], item['bundle_id']) for item in data] == [('abc123', 'bundle'), ('runners1', 'other-bundle')]
    assert 'SUPERSECRET' not in str(data)
    filters = [call.kwargs['filters'] for call in gateway.client.containers.list.call_args_list]
    assert len(filters) == 1 and 'com.inference.managed=true' in filters[0] and 'com.inference.owner=owner' in filters[0]
    assert await gateway.status() == {'available': True, 'transport': 'unix'}


async def test_image_pull_is_explicit_and_version_pinned(gateway):
    with pytest.raises(HTTPException):
        await gateway.ensure_image('n8nio/n8n:latest')
    gateway.client.images.pull.assert_not_awaited()
    await gateway.ensure_image('n8nio/runners:2.0.0')
    gateway.client.images.pull.assert_awaited_once_with('n8nio/runners', tag='2.0.0')
    await gateway.ensure_image('docker.n8n.io/n8nio/n8n:2.0.0')


async def test_open_uses_only_explicit_unix_socket(monkeypatch):
    import aiodocker
    constructors = []
    monkeypatch.setattr(module.os, 'name', 'posix')
    monkeypatch.setenv('DOCKER_HOST', 'tcp://malicious:2375')
    monkeypatch.setenv('DOCKER_CONTEXT', 'malicious')
    monkeypatch.setattr(aiodocker, 'Docker', lambda **kwargs: constructors.append(kwargs) or 'client')
    assert await module.open_docker('/var/run/docker.sock') == 'client'
    assert constructors[0]['url'] == 'unix:///var/run/docker.sock'
    assert await module.open_docker('tcp://localhost:2375') is None
    assert len(constructors) == 1


@pytest.mark.parametrize('frame', [{'error': 'SUPERSECRET'}, {'errorDetail': {'message': 'SUPERSECRET'}}])
async def test_legacy_pull_embedded_errors_are_redacted(gateway, frame):
    gateway.client.images.pull.return_value = [{'status': 'Pulling'}, frame]
    with pytest.raises(HTTPException) as error:
        await gateway.ensure_image('n8nio/runners:2.0.0')
    assert error.value.detail['code'] == 'docker_error'
    assert 'SUPERSECRET' not in str(error.value.detail)
    gateway.client.containers.create.assert_not_awaited()


async def test_actual_sdk_pull_parser_rejects_http200_error_frame(gateway):
    from aiodocker.images import DockerImages
    response = SimpleNamespace(content=SimpleNamespace(readline=AsyncMock(side_effect=[
        b'{"status":"Pulling"}\n', b'{"error":"SUPERSECRET"}\n', b''])), close=lambda: None)

    @asynccontextmanager
    async def query(*args, **kwargs):
        yield response

    # Exercise installed SDK JSON stream handling without any network access.
    sdk = SimpleNamespace(_query=query, _resolve_long_running_timeout=lambda value: value)
    gateway.client.images = DockerImages(sdk)
    with pytest.raises(HTTPException) as error:
        await gateway.ensure_image('n8nio/runners:2.0.0')
    assert error.value.detail['code'] == 'docker_error'
    assert 'SUPERSECRET' not in str(error.value.detail)


async def test_actual_sdk_network_name_resolves_canonical_id(gateway):
    from aiodocker.networks import DockerNetworks
    data = {'Id': 'canonical', 'Name': 'expected-name', 'Labels': owned_labels('owner', 'bundle', 'network')}
    sdk = SimpleNamespace(_query_json=AsyncMock(return_value=data))
    gateway.client.networks = DockerNetworks(sdk)
    result = await gateway.inspect_network('expected-name', 'bundle')
    assert result['id'] == 'canonical'
    assert sdk._query_json.call_args_list[0].args[0] == 'networks/expected-name'
    assert sdk._query_json.call_args_list[1].args[0] == 'networks/canonical'
