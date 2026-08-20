"""Durable administrator grants for scheduled encrypted backups."""
import asyncio
import hashlib
import json
import logging
import os
import re
import errno
from pathlib import Path
import stat
import tempfile
import time
from types import SimpleNamespace
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.auth_gate import fail, require_current_role, require_role
from backend.database import _settle
from .archive import ArchiveError, MAX_ARCHIVE_BYTES, encrypt_archive

router = APIRouter()
logger = logging.getLogger('inference')
REFERENCE = r'^\$[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$'
MAX_LOCAL_BYTES = 1024 * 1024 * 1024
POLL_SECONDS = 30


class StorageFull(ArchiveError):
    """A known pre-write capacity rejection, not an uncertain file effect."""


ARCHIVE_SUFFIX = '.ibackup'
LEGACY_ARCHIVE_SUFFIX = '.vbackup'


def archive_path(app, run_id: str, *, existing: bool = False) -> Path:
    if not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise ArchiveError('Invalid backup identifier.')
    root = storage_root(app)
    path = root / (run_id + ARCHIVE_SUFFIX)
    legacy = root / (run_id + LEGACY_ARCHIVE_SUFFIX)
    # Archives written before the rename keep their original file name.
    if existing and not os.path.lexists(path) and os.path.lexists(legacy):
        return legacy
    return path


def sync_directory(root: Path) -> None:
    if os.name != 'posix':
        return
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(descriptor)


class S3Target(BaseModel):
    model_config = ConfigDict(extra='forbid')
    endpoint: str = Field(max_length=2048)
    bucket: str = Field(min_length=3, max_length=63)
    region: str = Field(min_length=1, max_length=64)
    prefix: str = Field(default='inference', max_length=256)
    credentials_ref: str = Field(pattern=REFERENCE, max_length=256)


class ScheduleInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    interval_minutes: int = Field(default=1440, ge=15, le=525600)
    passphrase_ref: str = Field(pattern=REFERENCE, max_length=256)
    s3: S3Target | None = None


def storage_root(app) -> Path:
    root = app.state.settings.data_dir / 'backups'
    existed = root.exists()
    root.mkdir(exist_ok=True, mode=0o700)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ArchiveError('Backup storage must be an ordinary directory.')
    if not existed:
        sync_directory(root.parent)
    return root


def install(app, run_id: str, data: bytes) -> None:
    root = storage_root(app)
    total = 0
    for index, path in enumerate(root.iterdir()):
        if index >= 999:
            raise StorageFull('Local backup storage is full.')
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ArchiveError('Local backup storage contains an unsafe entry.')
        total += info.st_size
    if total + len(data) > MAX_LOCAL_BYTES:
        raise StorageFull('Local backup storage is full.')
    descriptor, temporary = tempfile.mkstemp(prefix='.archive-', dir=root)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, archive_path(app, run_id))
    finally:
        Path(temporary).unlink(missing_ok=True)
        sync_directory(root)


def read_archive(app, row: dict) -> bytes:
    from .router import read_regular
    data = read_regular(archive_path(app, row['id'], existing=True), MAX_ARCHIVE_BYTES)
    if len(data) != row['size_bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
        raise ArchiveError('Local backup does not match its recorded hash.')
    return data


async def authorize(tx, schedule_id: str, version: str, request=None) -> dict:
    if request is not None:
        await require_current_role(request, 'admin', tx)
    row = await tx.fetchone('SELECT * FROM backup_schedules WHERE id=?', (schedule_id,))
    if not row or not row['enabled'] or row['version'] != version:
        fail(409, 'backup_schedule_changed', 'The schedule was disabled or changed.')
    owner = await tx.fetchone('SELECT role,disabled FROM users WHERE username=?', (row['owner'],))
    if not owner or owner['disabled'] or owner['role'] != 'admin':
        fail(403, 'backup_owner_revoked', 'The granting administrator is no longer enabled.')
    return row


async def finish(app, run_id: str, status: str, code: str = '', remote: str | None = None) -> dict:
    async with app.state.db.transaction() as tx:
        await tx.execute('UPDATE backup_runs SET status=?,code=?,completed_at=?,remote_status=COALESCE(?,remote_status) WHERE id=?',
                         (status, code, time.time(), remote, run_id))
        row = await tx.fetchone('SELECT * FROM backup_runs WHERE id=?', (run_id,))
        await tx.execute('INSERT INTO audit_events(actor,action,target,outcome) VALUES(?,?,?,?)',
                         (row['actor'], 'backups.run_completed', run_id, status))
    try:
        await app.state.hub.broadcast({'event': 'backups.changed', 'run_id': run_id, 'status': status})
    except Exception:
        logger.warning('Backup completed but notification delivery failed.')
    return row


async def run_schedule(app, schedule_id: str, version: str, request=None, *, due_only: bool = False) -> dict:
    if app.state.backup_lock.locked():
        fail(409, 'backup_busy', 'A backup is already running.')
    async with app.state.backup_lock:
        async def perform() -> dict:
            run_id = uuid4().hex
            async with app.state.db.transaction() as tx:
                row = await authorize(tx, schedule_id, version, request)
                if due_only and row['next_run_at'] > time.time():
                    fail(409, 'backup_not_due', 'This schedule is no longer due.')
                await tx.execute('INSERT INTO backup_runs(id,schedule_id,actor,status,created_at) VALUES(?,?,?,?,?)',
                                 (run_id, schedule_id, request.state.user['username'] if request is not None else row['owner'], 'running', time.time()))
                await tx.execute('UPDATE backup_schedules SET next_run_at=? WHERE id=?',
                                 (time.time() + row['interval_seconds'], schedule_id))
            installed = False
            storing = False
            upload_started = False
            try:
                config = json.loads(app.state.vault.decrypt(row['config_enc']))
                try:
                    password = app.state.vault.resolve(config['passphrase_ref'])
                except (KeyError, ValueError, TypeError):
                    return await finish(app, run_id, 'failed', 'backup_password_unavailable')
                if not isinstance(password, str) or not 14 <= len(password) <= 256:
                    return await finish(app, run_id, 'failed', 'backup_password_invalid')
                from .router import capture_files
                with tempfile.TemporaryDirectory(prefix='.backup-', dir=app.state.settings.data_dir) as temporary:
                    snapshot = Path(temporary) / 'dashboard.db'
                    async with app.state.notes_lock:
                        await app.state.db.backup(snapshot)
                        files = await asyncio.to_thread(capture_files, SimpleNamespace(app=app), snapshot)
                    data = await asyncio.to_thread(encrypt_archive, files, password)
                async with app.state.db.transaction() as tx:
                    await authorize(tx, schedule_id, version, request)
                    if app.state.vault.resolve(config['passphrase_ref']) != password:
                        fail(409, 'backup_secret_changed', 'The export password changed during capture.')
                    await tx.execute('UPDATE backup_runs SET status=?,size_bytes=?,sha256=?,remote_status=? WHERE id=?',
                                     ('storing', len(data), hashlib.sha256(data).hexdigest(),
                                      'pending' if config.get('s3') else 'not_requested', run_id))
                storing = True
                async with app.state.db.transaction() as tx:
                    await authorize(tx, schedule_id, version, request)
                    await asyncio.to_thread(install, app, run_id, data)
                installed = True
                if config.get('s3'):
                    from .s3 import upload, S3Error
                    target = config['s3']
                    async with app.state.db.transaction() as tx:
                        await authorize(tx, schedule_id, version, request)
                        credentials = app.state.vault.resolve(target['credentials_ref'])
                        await tx.execute("UPDATE backup_runs SET status='uploading' WHERE id=?", (run_id,))
                    try:
                        upload_started = True
                        await upload(app.state.http, target, credentials, run_id + ARCHIVE_SUFFIX, data)
                    except S3Error as exc:
                        return await finish(app, run_id, 'partial', 's3_upload_failed', 'unknown' if exc.uncertain else 'rejected')
                    return await finish(app, run_id, 'succeeded', remote='uploaded')
                return await finish(app, run_id, 'succeeded')
            except StorageFull:
                return await finish(app, run_id, 'failed', 'local_storage_full')
            except Exception:
                # No external response or secret values enter status or logs.
                remote = 'unknown' if upload_started else ('not_attempted' if installed and config.get('s3') else None)
                return await finish(app, run_id, 'partial' if installed else ('uncertain' if storing else 'failed'), 'backup_failed', remote)
        return await _settle(perform())


async def initialize(app) -> None:
    async with app.state.db.transaction() as tx:
        await tx.execute("UPDATE backup_runs SET status='failed',code='interrupted_before_store',completed_at=? WHERE status='running'", (time.time(),))
        await tx.execute("UPDATE backup_runs SET status='uncertain',code='interrupted_local_write',completed_at=? WHERE status IN ('storing','deleting')", (time.time(),))
        await tx.execute("UPDATE backup_runs SET status='partial',remote_status='unknown',code='interrupted_upload',completed_at=? WHERE status='uploading'", (time.time(),))


async def tick(app) -> None:
    if app.state.backup_lock.locked():
        return
    due = await app.state.db.fetchall('SELECT id,version FROM backup_schedules WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at LIMIT 20', (time.time(),))
    for row in due:
        try:
            await run_schedule(app, row['id'], row['version'], due_only=True)
        except HTTPException as exc:
            if exc.detail.get('code') == 'backup_owner_revoked':
                async with app.state.db.transaction() as tx:
                    await tx.execute('UPDATE backup_schedules SET enabled=0 WHERE id=? AND version=?', (row['id'], row['version']))
            elif exc.detail.get('code') == 'backup_busy':
                return


async def maintenance(app) -> None:
    while True:
        await asyncio.sleep(POLL_SECONDS)
        try:
            await tick(app)
        except Exception as exc:
            # A transient database/storage failure must not kill future scheduling.
            logger.warning('Backup scheduler tick failed: %s', type(exc).__name__)


async def save_schedule(request: Request, body: ScheduleInput, schedule_id: str | None = None) -> dict:
    actor = require_role(request, 'admin')
    config = {'passphrase_ref': body.passphrase_ref, 's3': body.s3.model_dump() if body.s3 else None}
    if body.s3:
        from .s3 import validate_config
        try:
            validate_config(config['s3'])
        except ValueError:
            fail(422, 'invalid_s3_target', 'Use a valid S3 endpoint, bucket, region and prefix.')
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request, 'admin', tx)
        if schedule_id is not None:
            if not await tx.fetchone('SELECT id FROM backup_schedules WHERE id=?', (schedule_id,)):
                fail(404, 'schedule_not_found', 'Backup schedule was not found.')
        elif (await tx.fetchone('SELECT COUNT(*) AS count FROM backup_schedules'))['count'] >= 100:
            fail(409, 'schedule_limit', 'At most 100 backup schedules are supported.')
        schedule_id = schedule_id or uuid4().hex
        await tx.execute('INSERT INTO backup_schedules(id,name,owner,enabled,interval_seconds,next_run_at,version,config_enc,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,owner=excluded.owner,enabled=excluded.enabled,interval_seconds=excluded.interval_seconds,next_run_at=excluded.next_run_at,version=excluded.version,config_enc=excluded.config_enc',
                         (schedule_id, body.name, actor['username'], int(body.enabled), body.interval_minutes * 60,
                          time.time() + body.interval_minutes * 60, uuid4().hex,
                          request.app.state.vault.encrypt(json.dumps(config)), time.time()))
        await tx.execute('INSERT INTO audit_events(actor,action,target) VALUES(?,?,?)', (actor['username'], 'backups.schedule_saved', schedule_id))
    return {'id': schedule_id}


@router.post('/schedules')
async def create_schedule(body: ScheduleInput, request: Request) -> dict:
    return await save_schedule(request, body)


@router.put('/schedules/{schedule_id}')
async def update_schedule(schedule_id: str, body: ScheduleInput, request: Request) -> dict:
    return await save_schedule(request, body, schedule_id)


@router.delete('/schedules/{schedule_id}')
async def delete_schedule(schedule_id: str, request: Request) -> dict:
    require_role(request, 'admin')
    async with request.app.state.db.transaction() as tx:
        actor = await require_current_role(request, 'admin', tx)
        if not await tx.fetchone('SELECT id FROM backup_schedules WHERE id=?', (schedule_id,)):
            fail(404, 'schedule_not_found', 'Backup schedule was not found.')
        await tx.execute('DELETE FROM backup_schedules WHERE id=?', (schedule_id,))
        await tx.execute('INSERT INTO audit_events(actor,action,target) VALUES(?,?,?)',
                         (actor['username'], 'backups.schedule_deleted', schedule_id))
    return {'id': schedule_id, 'deleted': True}


@router.get('/schedules')
async def schedules(request: Request) -> dict:
    require_role(request, 'admin')
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request, 'admin', tx)
        rows = await tx.fetchall('SELECT * FROM backup_schedules ORDER BY created_at')
        for row in rows:
            config = json.loads(request.app.state.vault.decrypt(row.pop('config_enc')))
            row.update(config)
            row['enabled'] = bool(row['enabled'])
            row['interval_minutes'] = row.pop('interval_seconds') // 60
    return {'data': rows}


@router.post('/schedules/{schedule_id}/run')
async def manual_run(schedule_id: str, request: Request) -> dict:
    require_role(request, 'admin')
    row = await request.app.state.db.fetchone('SELECT version FROM backup_schedules WHERE id=?', (schedule_id,))
    if not row:
        fail(404, 'schedule_not_found', 'Backup schedule was not found.')
    return await run_schedule(request.app, schedule_id, row['version'], request)


@router.get('/runs')
async def runs(request: Request, limit: int = Query(default=50, ge=1, le=100), before: str | None = None) -> dict:
    require_role(request, 'admin')
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request, 'admin', tx)
        cursor = await tx.fetchone('SELECT created_at,id FROM backup_runs WHERE id=?', (before,)) if before else None
        if before and not cursor:
            fail(400, 'invalid_cursor', 'Backup history cursor was not found.')
        rows = await tx.fetchall('SELECT * FROM backup_runs ' + ('WHERE (created_at,id)<(?,?) ' if cursor else '') + 'ORDER BY created_at DESC,id DESC LIMIT ?',
                                (cursor['created_at'], cursor['id'], limit + 1) if cursor else (limit + 1,))
    return {'data': rows[:limit], 'next_cursor': rows[limit - 1]['id'] if len(rows) > limit else None}


@router.get('/runs/{run_id}/download')
async def download(run_id: str, request: Request) -> Response:
    require_role(request, 'admin')
    async with request.app.state.backup_lock:
        row = await request.app.state.db.fetchone('SELECT * FROM backup_runs WHERE id=?', (run_id,))
        if not row or not row['sha256'] or row['status'] == 'deleted':
            fail(404, 'backup_not_found', 'A local backup was not found.')
        try:
            data = await _settle(asyncio.to_thread(read_archive, request.app, row))
        except (ArchiveError, OSError):
            fail(409, 'backup_file_invalid', 'The local archive is missing or does not match its recorded hash.')
        async with request.app.state.db.transaction() as tx:
            await require_current_role(request, 'admin', tx)
    return Response(data, media_type='application/octet-stream', headers={'Content-Disposition': 'attachment; filename="inference.ibackup"'})


@router.post('/runs/{run_id}/reconcile')
async def reconcile(run_id: str, request: Request) -> dict:
    require_role(request, 'admin')
    async with request.app.state.backup_lock:
        row = await request.app.state.db.fetchone('SELECT * FROM backup_runs WHERE id=?', (run_id,))
        if not row or not row['sha256'] or row['status'] not in ('uncertain', 'partial'):
            fail(409, 'backup_not_reconcilable', 'This run does not require local reconciliation.')
        try:
            await _settle(asyncio.to_thread(read_archive, request.app, row))
        except (ArchiveError, OSError):
            fail(409, 'backup_file_invalid', 'The local archive is missing or differs from its recorded hash.')
        async with request.app.state.db.transaction() as tx:
            actor = await require_current_role(request, 'admin', tx)
            status = 'succeeded' if row['remote_status'] == 'not_requested' else 'partial'
            await tx.execute('UPDATE backup_runs SET status=?,code=? WHERE id=?',
                             (status, '' if status == 'succeeded' else 'remote_unconfirmed', run_id))
            await tx.execute('INSERT INTO audit_events(actor,action,target) VALUES(?,?,?)',
                             (actor['username'], 'backups.reconciled', run_id))
            return await tx.fetchone('SELECT * FROM backup_runs WHERE id=?', (run_id,))


@router.delete('/runs/{run_id}/file')
async def delete_archive(run_id: str, request: Request) -> dict:
    require_role(request, 'admin')
    async with request.app.state.backup_lock:
        async def remove() -> dict:
            row = await request.app.state.db.fetchone('SELECT * FROM backup_runs WHERE id=?', (run_id,))
            if not row or not row['sha256']:
                fail(404, 'backup_not_found', 'A local backup was not found.')
            async with request.app.state.db.transaction() as tx:
                actor = await require_current_role(request, 'admin', tx)
                if row['status'] == 'deleted':
                    return {'id': run_id, 'deleted': True}
            try:
                await asyncio.to_thread(read_archive, request.app, row)
            except (ArchiveError, OSError):
                fail(409, 'backup_file_invalid', 'The local archive is missing or differs from its recorded hash.')
            async with request.app.state.db.transaction() as tx:
                await require_current_role(request, 'admin', tx)
                await tx.execute("UPDATE backup_runs SET status='deleting' WHERE id=?", (run_id,))
            def unlink() -> None:
                path = archive_path(request.app, run_id, existing=True)
                path.unlink()
                sync_directory(path.parent)
            try:
                async with request.app.state.db.transaction() as tx:
                    await require_current_role(request, 'admin', tx)
                    await asyncio.to_thread(unlink)
                    await tx.execute("UPDATE backup_runs SET status='deleted',code='' WHERE id=?", (run_id,))
                    await tx.execute('INSERT INTO audit_events(actor,action,target) VALUES(?,?,?)',
                                     (actor['username'], 'backups.local_deleted', run_id))
            except Exception:
                await finish(request.app, run_id, 'uncertain', 'local_delete_unconfirmed')
                fail(503, 'backup_delete_uncertain', 'Local deletion could not be confirmed. Inspect the run before retrying.')
            return {'id': run_id, 'deleted': True}
        return await _settle(remove())
