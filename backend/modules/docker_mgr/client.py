"""Bounded Unix-socket Docker access with live authorization and ownership checks."""
import asyncio
import json
import os
import re
from collections.abc import Callable, Awaitable
from typing import Any

from fastapi import HTTPException, Request

from backend.auth_gate import fail, require_current_role

LABEL_PREFIX = 'com.inference.'
MANAGED_LABEL = LABEL_PREFIX + 'managed'
OWNER_LABEL = LABEL_PREFIX + 'owner'
BUNDLE_LABEL = LABEL_PREFIX + 'bundle'
ROLE_LABEL = LABEL_PREFIX + 'role'
CALL_TIMEOUT = 30


def owned_labels(owner: str, bundle: str, role: str) -> dict[str, str]:
    return {MANAGED_LABEL: 'true', OWNER_LABEL: owner, BUNDLE_LABEL: bundle, ROLE_LABEL: role}


async def open_docker(socket_path: str) -> Any | None:
    """Never discover ambient Docker endpoints, including on Windows."""
    if os.name == 'nt' or not socket_path.startswith('/') or '\x00' in socket_path:
        return None
    try:
        from aiodocker import Docker
        from aiohttp import ClientTimeout
        return Docker(url='unix://' + socket_path, timeout=ClientTimeout(total=CALL_TIMEOUT))
    except (ImportError, OSError, ValueError):
        return None


async def close_docker(client: Any | None) -> None:
    if client is not None:
        await client.close()


class DockerGateway:
    def __init__(self, request: Request):
        self.request = request
        self.client = getattr(request.app.state, 'docker', None)
        self.owner = getattr(request.app.state, 'docker_owner_id', '')

    async def _authorize(self) -> None:
        async with self.request.app.state.db.transaction() as tx:
            await require_current_role(self.request, 'admin', tx)
        if self.client is None:
            fail(503, 'docker_unavailable', 'The configured Docker Unix socket is unavailable.')

    async def _call(self, operation: Callable[[], Awaitable[Any]], *, write: bool = False,
                    timeout: int = CALL_TIMEOUT) -> Any:
        await self._authorize()
        try:
            result = await asyncio.wait_for(operation(), timeout)
        except HTTPException:
            raise
        except Exception as exc:
            status = getattr(exc, 'status', None)
            if status == 0 and hasattr(exc, 'error_detail'):
                fail(409, 'docker_error', 'Docker reported a failure in the operation progress stream.')
            if isinstance(status, int) and 400 <= status < 500:
                fail(404 if status == 404 else 409, 'docker_error', 'Docker rejected the resource operation.')
            if write:
                fail(503, 'docker_outcome_unknown', 'Docker did not confirm the operation. Reconcile before retrying.')
            fail(503, 'docker_unavailable', 'The configured Docker Unix socket is unavailable.')
        if not write:
            await self._authorize()
        return result

    def _owned(self, labels: Any, bundle: str, role: str) -> None:
        if not self.owner or not bundle or role not in {'n8n', 'runners', 'network', 'volume'}:
            fail(409, 'docker_ownership', 'The Docker resource does not belong to this bundle.')
        if not isinstance(labels, dict) or not all(
                labels.get(k) == v for k, v in owned_labels(self.owner, bundle, role).items()):
            fail(409, 'docker_ownership', 'The Docker resource does not belong to this bundle.')

    @staticmethod
    def _identifier(value: str) -> None:
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,255}', value):
            fail(422, 'invalid_resource', 'Invalid Docker resource identifier.')

    async def status(self) -> dict[str, Any]:
        await self._call(lambda: self.client.version())
        return {'available': True, 'transport': 'unix'}

    async def inventory(self) -> list[dict[str, Any]]:
        result = []
        filters = json.dumps({'label': [f'{MANAGED_LABEL}=true', f'{OWNER_LABEL}={self.owner}']})
        containers = await self._call(lambda: self.client.containers.list(all=True, filters=filters))
        for container in containers:
            data = await self._call(container.show)
            labels = data.get('Config', {}).get('Labels') or {}
            bundle, role = labels.get(BUNDLE_LABEL, ''), labels.get(ROLE_LABEL, '')
            try:
                self._owned(labels, bundle, role)
            except HTTPException:
                continue
            if role in {'n8n', 'runners'}:
                result.append(self._container_metadata(data, bundle, role))
        return result

    @staticmethod
    def _container_metadata(data: dict, bundle: str, role: str) -> dict[str, Any]:
        state = data.get('State') or {}
        return {'id': str(data['Id']), 'name': str(data.get('Name', '')).lstrip('/'),
                'image': str(data.get('Config', {}).get('Image', '')), 'bundle_id': bundle, 'role': role,
                'state': state.get('Status', 'unknown'), 'running': bool(state.get('Running', False)),
                'health': (state.get('Health') or {}).get('Status', 'none'), 'exit_code': state.get('ExitCode')}

    async def _container(self, identifier: str, bundle: str, role: str, *, allow_name: bool = False) -> tuple[Any, dict]:
        self._identifier(identifier)
        obj = await self._call(lambda: self.client.containers.get(identifier))
        data = await self._call(obj.show)
        # Require immutable complete IDs, never mutate a name that can be replaced.
        if data.get('Id') != obj.id or (data.get('Id') != identifier and
                not (allow_name and str(data.get('Name', '')).lstrip('/') == identifier)):
            fail(409, 'docker_ownership', 'The Docker resource identity changed.')
        self._owned(data.get('Config', {}).get('Labels'), bundle, role)
        return obj, data

    async def inspect_container(self, identifier: str, bundle_id: str, role: str) -> dict[str, Any]:
        _, data = await self._container(identifier, bundle_id, role, allow_name=True)
        return self._container_metadata(data, bundle_id, role)

    async def start_container(self, identifier: str, bundle_id: str, role: str) -> None:
        obj, _ = await self._container(identifier, bundle_id, role)
        await self._call(obj.start, write=True)

    async def stop_container(self, identifier: str, bundle_id: str, role: str) -> None:
        obj, _ = await self._container(identifier, bundle_id, role)
        await self._call(lambda: obj.stop(t=10), write=True)

    async def delete_container(self, identifier: str, bundle_id: str, role: str) -> None:
        obj, _ = await self._container(identifier, bundle_id, role)
        await self._call(lambda: obj.delete(force=False, v=False), write=True)

    def _creation(self, config: dict, role: str | None = None) -> None:
        labels = config.get('Labels') or {}
        actual = labels.get(ROLE_LABEL, '')
        if role is None and actual not in {'n8n', 'runners'}:
            fail(422, 'invalid_resource', 'Invalid container role.')
        self._owned(labels, labels.get(BUNDLE_LABEL, ''), role or actual)

    async def create_container(self, name: str, config: dict) -> str:
        self._identifier(name)
        self._creation(config)
        obj = await self._call(lambda: self.client.containers.create(config, name=name), write=True)
        return str(obj.id)

    async def create_network(self, config: dict) -> str:
        self._identifier(config.get('Name', ''))
        self._creation(config, 'network')
        obj = await self._call(lambda: self.client.networks.create({**config, 'CheckDuplicate': True}), write=True)
        return str(obj.id)

    async def create_volume(self, config: dict) -> str:
        name = config.get('Name', '')
        self._identifier(name)
        self._creation(config, 'volume')
        try:
            await self._call(lambda: self.client.volumes.get(name))
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        else:
            fail(409, 'docker_ownership', 'A Docker volume already uses this name.')
        obj = await self._call(lambda: self.client.volumes.create(config), write=True)
        # Docker's volume API has no create-only precondition. Verify returned ownership.
        await self.inspect_volume(name, config['Labels'][BUNDLE_LABEL])
        return str(obj.name)

    async def _resource(self, identifier: str, bundle: str, role: str, *, allow_name: bool = False) -> tuple[Any, dict]:
        self._identifier(identifier)
        collection = self.client.networks if role == 'network' and self.client else getattr(self.client, 'volumes', None)
        obj = await self._call(lambda: collection.get(identifier))
        data = await self._call(obj.show)
        actual_id = data.get('Id' if role == 'network' else 'Name')
        if not actual_id or (actual_id != identifier and not (allow_name and data.get('Name') == identifier)):
            fail(409, 'docker_ownership', 'The Docker resource identity changed.')
        if (obj.id if role == 'network' else obj.name) != actual_id:
            fail(409, 'docker_ownership', 'The Docker resource identity changed.')
        self._owned(data.get('Labels'), bundle, role)
        return obj, {'id': actual_id, 'name': data.get('Name', identifier), 'bundle_id': bundle, 'role': role}

    async def inspect_network(self, identifier: str, bundle_id: str) -> dict[str, Any]:
        return (await self._resource(identifier, bundle_id, 'network', allow_name=True))[1]

    async def inspect_volume(self, name: str, bundle_id: str) -> dict[str, Any]:
        return (await self._resource(name, bundle_id, 'volume'))[1]

    async def delete_network(self, identifier: str, bundle_id: str) -> None:
        obj, _ = await self._resource(identifier, bundle_id, 'network')
        await self._call(obj.delete, write=True)

    async def delete_volume(self, name: str, bundle_id: str) -> None:
        obj, _ = await self._resource(name, bundle_id, 'volume')
        await self._call(obj.delete, write=True)

    async def ensure_image(self, image: str) -> None:
        # Caller accepts only explicitly requested, matching-version n8n/runner images.
        if not re.fullmatch(r'(?:docker\.n8n\.io/n8nio/n8n|n8nio/(?:n8n|runners)):[0-9]+\.[0-9]+\.[0-9]+', image):
            fail(422, 'invalid_image', 'An exact supported n8n image version is required.')
        repository, tag = image.rsplit(':', 1)
        progress = await self._call(lambda: self.client.images.pull(repository, tag=tag), write=True, timeout=300)
        # Older supported SDK releases return embedded error frames instead of raising.
        frames = progress if isinstance(progress, list) else [progress]
        if any(isinstance(frame, dict) and ('error' in frame or 'errorDetail' in frame) for frame in frames):
            fail(409, 'docker_error', 'Docker reported a failure while pulling the requested image.')

