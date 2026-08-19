"""Shared notes vault; readers see text, operators save exact revisions."""
import asyncio
import html
import os
from pathlib import PurePosixPath
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.auth_gate import fail, require_current_role, require_role
from backend.database import _settle
from .storage import NotesStore, NoteError

router = APIRouter()
LINK = re.compile(r'\[\[([^\[\]\r\n]{1,512})\]\]')


class SaveNote(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=256 * 1024)
    expected_revision: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')


def initialize(app: Any) -> None:
    app.state.notes = NotesStore(app.state.settings.data_dir / 'notes')
    app.state.notes_lock = asyncio.Lock()


async def read_operation(request: Request, operation: Any) -> Any:
    require_role(request, 'viewer')
    async with request.app.state.notes_lock:
        try:
            result = await _settle(asyncio.to_thread(operation))
        except NoteError as exc:
            fail(exc.status, exc.code, exc.message)
        async with request.app.state.db.transaction() as tx:
            await require_current_role(request, 'viewer', tx)
    return result


async def mutate(request: Request, action: str, path: str, operation: Any) -> Any:
    require_role(request, 'operator')
    async with request.app.state.notes_lock:
        applied = False
        async def commit() -> Any:
            nonlocal applied
            # Serialize account changes with this bounded local filesystem mutation.
            async with request.app.state.db.transaction() as tx:
                await require_current_role(request, 'operator', tx)
                try:
                    result = await asyncio.to_thread(operation)
                except NoteError as exc:
                    if exc.status >= 500:
                        fail(503, 'notes_outcome_unknown', 'The filesystem outcome could not be confirmed. Reload the note before retrying.')
                    fail(exc.status, exc.code, exc.message)
                applied = True
                await tx.execute('INSERT INTO audit_events(actor,action,target) VALUES(?,?,?)',
                                 (request.state.user['username'], action, path))
                return result
        try:
            result = await _settle(commit())
        except Exception as exc:
            if applied and not isinstance(exc, HTTPException):
                fail(503, 'notes_outcome_unknown', 'The note changed but audit persistence failed. Reload before retrying.')
            raise
    await request.app.state.hub.broadcast({'event': 'notes.changed', 'path': path})
    return result


@router.get('')
async def notes(request: Request) -> dict[str, Any]:
    return {'data': await read_operation(request, request.app.state.notes.list)}


@router.get('/note')
async def note(request: Request, path: str = Query(min_length=1, max_length=512)) -> dict[str, Any]:
    return await read_operation(request, lambda: request.app.state.notes.read(path))


@router.put('/note')
async def save(body: SaveNote, request: Request) -> dict[str, Any]:
    return await mutate(request, 'notes.saved', body.path,
                        lambda: request.app.state.notes.write(body.path, body.content, body.expected_revision))


@router.delete('/note')
async def delete(request: Request, path: str = Query(min_length=1, max_length=512),
                 expected_revision: str = Query(pattern=r'^[0-9a-f]{64}$')) -> dict[str, Any]:
    await mutate(request, 'notes.deleted', path, lambda: request.app.state.notes.delete(path, expected_revision))
    return {'path': path, 'deleted': True}


def snapshot(store: NotesStore) -> list[dict[str, Any]]:
    result = []
    size = 0
    for row in store.list():
        note = store.read(row['path'])
        size += len(note['content'].encode('utf-8'))
        if size > 32 * 1024 * 1024:
            fail(413, 'notes_scan_limit', 'The note scan exceeded its supported size.')
        result.append(note)
    return result


@router.get('/search')
async def search(request: Request, q: str = Query(min_length=1, max_length=256), case_sensitive: bool = False) -> dict[str, Any]:
    def operation() -> list[dict[str, Any]]:
        needle = q if case_sensitive else q.casefold()
        def matches(text: str) -> bool:
            return needle in (text if case_sensitive else text.casefold())
        rows = []
        for note in snapshot(request.app.state.notes):
            if matches(note['path']) or matches(note['content']):
                excerpt = next((line[:200] for line in note['content'].splitlines() if matches(line)), note['content'][:200])
                rows.append({'path': note['path'], 'revision': note['revision'], 'excerpt': excerpt})
        return rows
    return {'data': await read_operation(request, operation)}


def link_index(paths: set[str]) -> tuple[dict[str, str], dict[str, list[str]]]:
    normalize = str.casefold if os.name == 'nt' else lambda s: s
    exact = {normalize(path): path for path in paths}
    basenames: dict[str, list[str]] = {}
    for path in paths:
        basenames.setdefault(normalize(PurePosixPath(path).name), []).append(path)
    return exact, basenames


def resolve_link(source: str, target: str, index: tuple[dict[str, str], dict[str, list[str]]]) -> str | None:
    target = target.split('|', 1)[0].split('#', 1)[0].strip()
    if not target:
        return source
    if any(c in target for c in ('\\', ':', '%')) or target.startswith('/') or any(p in ('.', '..') for p in target.split('/')):
        return None
    if not target.endswith('.md'):
        target += '.md'
    normalize = str.casefold if os.name == 'nt' else lambda s: s
    lookup, basenames = index
    for candidate in (str(PurePosixPath(source).parent / target), target):
        if normalize(candidate) in lookup:
            return lookup[normalize(candidate)]
    if '/' not in target:
        matching = basenames.get(normalize(target), [])
        if len(matching) == 1:
            return matching[0]
    return None


@router.get('/backlinks')
async def backlinks(request: Request, path: str = Query(min_length=1, max_length=512)) -> dict[str, Any]:
    def operation() -> list[dict[str, Any]]:
        target = request.app.state.notes.read(path)['path']
        all_notes = snapshot(request.app.state.notes)
        paths = {note['path'] for note in all_notes}
        index = link_index(paths)
        equal = (lambda a, b: a.casefold() == b.casefold()) if os.name == 'nt' else lambda a, b: a == b
        result = []
        examined = 0
        for note in all_notes:
            if equal(note['path'], target):
                continue
            for match in LINK.finditer(note['content']):
                examined += 1
                if examined > 100000:
                    fail(413, 'notes_scan_limit', 'The backlink scan exceeded its supported link count.')
                resolved = resolve_link(note['path'], match.group(1), index)
                if resolved is not None and equal(resolved, target):
                    result.append({'path': note['path'], 'revision': note['revision']})
                    break
        return result
    return {'data': await read_operation(request, operation)}


@router.get('/render')
async def render(request: Request, path: str = Query(min_length=1, max_length=512)) -> dict[str, Any]:
    result = await note(request, path)
    return {'path': result['path'], 'revision': result['revision'],
            'html': '<pre class="note-text">' + html.escape(result['content'], quote=True) + '</pre>'}
