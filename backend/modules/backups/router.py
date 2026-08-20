"""Admin-only encrypted exports; passphrases are never persisted."""
import asyncio
import os
from pathlib import Path
import stat
import tempfile

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from backend.auth_gate import fail, require_role, require_current_role
from backend.database import _settle
from backend.modules.notes.storage import NoteError
from .archive import ArchiveError, encrypt_archive

router = APIRouter()
MAX_BYTES = 128 * 1024 * 1024


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    passphrase: SecretStr = Field(min_length=14, max_length=256)


def read_regular(path: Path, limit: int = MAX_BYTES) -> bytes:
    if limit < 0:
        raise ArchiveError('Backup exceeds the supported size limit.')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ArchiveError('Backup source must be a regular file.')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(descriptor, 'rb') as handle:
        actual = os.fstat(handle.fileno())
        if not stat.S_ISREG(actual.st_mode) or (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino):
            raise ArchiveError('Backup source changed during capture.')
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ArchiveError('Backup exceeds the supported size limit.')
    return data


def capture_files(request: Request, snapshot: Path) -> dict[str, bytes]:
    state = request.app.state
    files = {'dashboard.db': read_regular(snapshot, MAX_BYTES)}
    for name in ('.secret_key', 'secrets.json', 'config.json', 'users.json'):
        path = state.settings.data_dir / name
        if name in ('.secret_key', 'secrets.json') or path.exists() or path.is_symlink():
            files[name] = read_regular(path, MAX_BYTES - sum(map(len, files.values())))
    for note in state.notes.list():
        files['notes/' + note['path']] = state.notes.read(note['path'])['content'].encode('utf-8')
        if sum(map(len, files.values())) > MAX_BYTES:
            raise ArchiveError('Backup exceeds the supported size limit.')
    return files


@router.post('/export')
async def export_backup(body: ExportRequest, request: Request) -> Response:
    require_role(request, 'admin')
    state = request.app.state
    # Reject concurrent expensive exports rather than accumulating passphrases in a queue.
    if state.backup_lock.locked():
        fail(409, 'backup_busy', 'An export is already running.')
    async with state.backup_lock:
        async def export() -> bytes:
            with tempfile.TemporaryDirectory(prefix='.backup-', dir=state.settings.data_dir) as temporary:
                snapshot = Path(temporary) / 'dashboard.db'
                async with state.notes_lock:
                    async with state.db.transaction() as tx:
                        await require_current_role(request, 'admin', tx)
                    await state.db.backup(snapshot)
                    files = await asyncio.to_thread(capture_files, request, snapshot)
                encrypted = await asyncio.to_thread(encrypt_archive, files, body.passphrase.get_secret_value())
                async with state.db.transaction() as tx:
                    await require_current_role(request, 'admin', tx)
                    await tx.execute('INSERT INTO audit_events(actor,action,target) VALUES(?,?,?)',
                                     (request.state.user['username'], 'backups.exported', 'local'))
                return encrypted
        try:
            data = await _settle(export())
        except (ArchiveError, NoteError, OSError):
            fail(503, 'backup_failed', 'Could not create a valid encrypted backup. Check local storage and size limits.')
    return Response(data, media_type='application/octet-stream',
                    headers={'Content-Disposition': 'attachment; filename="inference.ibackup"'})


from .schedules import router as schedule_router
router.include_router(schedule_router)
