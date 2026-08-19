"""Discover trusted local modules; importing manifests performs no application I/O."""
from importlib import import_module
import json
from pathlib import Path
import re
from typing import Any

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, ConfigDict, Field


class Manifest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-z][a-z0-9_]*$')
    version: str = Field(pattern=r'^\d+\.\d+\.\d+$')
    api_version: int
    prefix: str
    title: str = Field(min_length=1, max_length=80)


def discover_modules(directory: Path | None = None) -> list[Manifest]:
    directory = directory or Path(__file__).parent
    manifests: list[Manifest] = []
    prefixes: set[str] = set()
    ids: set[str] = set()
    for path in sorted(directory.glob('*/manifest.json')):
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('Module paths must not be symbolic links')
        manifest = Manifest.model_validate(json.loads(path.read_text(encoding='utf-8')))
        if manifest.id != path.parent.name or manifest.api_version != 1:
            raise ValueError(f'Incompatible module manifest: {path.parent.name}')
        expected = '/api/otel' if manifest.id == 'observability' else f'/api/{manifest.id}'
        if manifest.prefix != expected or manifest.prefix in prefixes or manifest.id in ids:
            raise ValueError(f'Invalid or duplicate module prefix: {manifest.prefix}')
        prefixes.add(manifest.prefix)
        ids.add(manifest.id)
        manifests.append(manifest)
    return manifests


def register_modules(app: FastAPI) -> list[Manifest]:
    manifests = discover_modules()
    for manifest in manifests:
        module = import_module(f'backend.modules.{manifest.id}.router')
        if not isinstance(module.router, APIRouter):
            raise ValueError(f'Module {manifest.id} does not export an APIRouter')
        app.include_router(module.router, prefix=manifest.prefix, tags=[manifest.title])
    return manifests
