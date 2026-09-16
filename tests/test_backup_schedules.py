import asyncio
import threading
import time
import sqlite3

import pytest


from test_fleet import fleet


async def configure(fleet, **changes):
    fleet.app.state.vault.set('BACKUP_PASSWORD', 'separate export password for schedules')
    body = {'name': 'Daily', 'interval_minutes': 1440, 'passphrase_ref': '$BACKUP_PASSWORD', **changes}
    return await fleet.client.post('/api/backups/schedules', headers=fleet.headers, json=body)


async def test_schedule_runs_and_downloads_real_archive(fleet):
    from backend.modules.backups.archive import decrypt_archive
    created = await configure(fleet)
    assert created.status_code == 200, created.text
    schedule = created.json()['id']
    run = await fleet.client.post(f'/api/backups/schedules/{schedule}/run', headers=fleet.headers)
    assert run.status_code == 200, run.text
    assert run.json()['status'] == 'succeeded'
    downloaded = await fleet.client.get('/api/backups/runs/' + run.json()['id'] + '/download')
    assert downloaded.status_code == 200
    files = decrypt_archive(downloaded.content, 'separate export password for schedules')
    assert 'dashboard.db' in files
    history = await fleet.client.get('/api/backups/runs')
    assert history.json()['data'][0]['id'] == run.json()['id']
    assert 'separate export password' not in (await fleet.client.get('/api/backups/schedules')).text


async def test_due_tick_runs_once_and_disabled_owner_cannot_run(fleet):
    from backend.modules.backups.schedules import tick
    created = await configure(fleet)
    schedule = created.json()['id']
    await fleet.app.state.db.execute('UPDATE backup_schedules SET next_run_at=? WHERE id=?', (time.time() - 1, schedule))
    await tick(fleet.app)
    await tick(fleet.app)
    rows = await fleet.app.state.db.fetchall('SELECT * FROM backup_runs')
    assert len(rows) == 1 and rows[0]['status'] == 'succeeded'
    await fleet.app.state.db.execute("UPDATE users SET role='viewer' WHERE username='owner'")
    await fleet.app.state.db.execute('UPDATE backup_schedules SET next_run_at=0 WHERE id=?', (schedule,))
    await tick(fleet.app)
    assert len(await fleet.app.state.db.fetchall('SELECT * FROM backup_runs')) == 1
    assert (await fleet.app.state.db.fetchone('SELECT enabled FROM backup_schedules WHERE id=?', (schedule,)))['enabled'] == 0


async def test_schedule_requires_admin_csrf_and_secret_reference(fleet):
    bad = await configure(fleet, passphrase_ref='plaintext password')
    assert bad.status_code == 422
    assert (await fleet.client.post('/api/backups/schedules', json={})).status_code == 403
    await fleet.app.state.db.execute("UPDATE users SET role='operator' WHERE username='owner'")
    assert (await configure(fleet)).status_code == 403
    assert (await fleet.client.get('/api/backups/runs')).status_code == 403
    assert (await fleet.client.get('/api/backups/schedules')).status_code == 403


async def test_schedule_update_delete_and_archive_delete_keep_history(fleet):
    created = (await configure(fleet)).json()['id']
    run = (await fleet.client.post(f'/api/backups/schedules/{created}/run', headers=fleet.headers)).json()
    changed = await fleet.client.put('/api/backups/schedules/' + created, headers=fleet.headers,
        json={'name': 'Paused', 'enabled': False, 'passphrase_ref': '$BACKUP_PASSWORD'})
    assert changed.status_code == 200
    assert (await fleet.client.post(f'/api/backups/schedules/{created}/run', headers=fleet.headers)).status_code == 409
    assert (await fleet.client.delete(f'/api/backups/schedules/{created}', headers=fleet.headers)).status_code == 200
    assert (await fleet.client.get('/api/backups/schedules')).json()['data'] == []
    assert (await fleet.client.delete(f"/api/backups/runs/{run['id']}/file", headers=fleet.headers)).status_code == 200
    assert (await fleet.client.get(f"/api/backups/runs/{run['id']}/download")).status_code == 404
    history = (await fleet.client.get('/api/backups/runs')).json()['data']
    assert history[0]['status'] == 'deleted'


async def test_revoked_schedule_during_capture_prevents_install(fleet, monkeypatch):
    from backend.modules.backups import schedules
    schedule = (await configure(fleet)).json()['id']
    started, release = threading.Event(), threading.Event()
    original = schedules.encrypt_archive
    def blocked(*args):
        started.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(schedules, 'encrypt_archive', blocked)
    task = asyncio.create_task(fleet.client.post(f'/api/backups/schedules/{schedule}/run', headers=fleet.headers))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        await fleet.client.delete('/api/backups/schedules/' + schedule, headers=fleet.headers)
    finally:
        release.set()
    assert (await task).json()['status'] == 'failed'
    assert not list(fleet.app.state.settings.data_dir.glob('backups/*.ibackup'))


async def test_restart_preserves_uncertain_write_and_local_reconcile(fleet):
    from backend.modules.backups.schedules import initialize
    schedule = (await configure(fleet)).json()['id']
    run = (await fleet.client.post(f'/api/backups/schedules/{schedule}/run', headers=fleet.headers)).json()
    await fleet.app.state.db.execute("UPDATE backup_runs SET status='storing' WHERE id=?", (run['id'],))
    await initialize(fleet.app)
    assert (await fleet.app.state.db.fetchone('SELECT status FROM backup_runs WHERE id=?', (run['id'],)))['status'] == 'uncertain'
    result = await fleet.client.post(f"/api/backups/runs/{run['id']}/reconcile", headers=fleet.headers)
    assert result.status_code == 200 and result.json()['status'] == 'succeeded'
    assert len(await fleet.app.state.db.fetchall('SELECT * FROM backup_runs')) == 1


async def test_s3_timeout_keeps_local_archive_without_retry(fleet, monkeypatch):
    from backend.modules.backups import s3
    calls = []
    async def failed(client, config, credentials, key, data):
        calls.append((key, data))
        raise s3.S3Error('Safe error', uncertain=True)
    monkeypatch.setattr(s3, 'upload', failed)
    fleet.app.state.vault.set('S3', {'access_key_id': 'example', 'secret_access_key': 'private-value'})
    created = await configure(fleet, s3={'endpoint': 'https://storage.example', 'bucket': 'backup-bucket',
                                      'region': 'us-east-1', 'credentials_ref': '$S3'})
    assert created.status_code == 200, created.text
    run = (await fleet.client.post('/api/backups/schedules/' + created.json()['id'] + '/run', headers=fleet.headers)).json()
    assert run['status'] == 'partial' and run['remote_status'] == 'unknown'
    assert len(calls) == 1 and b'private-value' not in calls[0][1]
    assert (await fleet.client.get('/api/backups/runs/' + run['id'] + '/download')).status_code == 200
    reconciled = (await fleet.client.post('/api/backups/runs/' + run['id'] + '/reconcile', headers=fleet.headers)).json()
    assert reconciled['status'] == 'partial' and reconciled['remote_status'] == 'unknown'
    assert len(calls) == 1


async def test_storage_cap_and_changed_archive_fail_honestly(fleet, monkeypatch):
    from backend.modules.backups import schedules
    schedule = (await configure(fleet)).json()['id']
    run = (await fleet.client.post(f'/api/backups/schedules/{schedule}/run', headers=fleet.headers)).json()
    path = fleet.app.state.settings.data_dir / 'backups' / (run['id'] + '.ibackup')
    path.write_bytes(b'changed-file')
    assert (await fleet.client.get('/api/backups/runs/' + run['id'] + '/download')).status_code == 409
    assert (await fleet.client.delete('/api/backups/runs/' + run['id'] + '/file', headers=fleet.headers)).status_code == 409
    monkeypatch.setattr(schedules, 'MAX_LOCAL_BYTES', 1)
    failed = (await fleet.client.post(f'/api/backups/schedules/{schedule}/run', headers=fleet.headers)).json()
    assert failed['status'] == 'failed' and failed['code'] == 'local_storage_full'
    assert path.read_bytes() == b'changed-file'


async def test_invalid_s3_target_is_rejected_without_secret_echo(fleet):
    response = await configure(fleet, s3={'endpoint': 'https://user:private@example.com', 'bucket': 'backup-bucket',
                                        'region': 'us-east-1', 'credentials_ref': '$S3'})
    assert response.status_code == 422 and 'private' not in response.text


async def test_cancelled_run_finishes_local_write_and_unlocks(fleet, monkeypatch):
    from backend.modules.backups import schedules
    schedule = (await configure(fleet)).json()['id']
    started, release = threading.Event(), threading.Event()
    original = schedules.install
    def blocked(*args):
        started.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(schedules, 'install', blocked)
    task = asyncio.create_task(fleet.client.post(f'/api/backups/schedules/{schedule}/run', headers=fleet.headers))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        assert fleet.app.state.backup_lock.locked()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not fleet.app.state.backup_lock.locked()
    row = await fleet.app.state.db.fetchone('SELECT * FROM backup_runs')
    assert row['status'] == 'succeeded'
    assert (await fleet.client.get('/api/backups/runs/' + row['id'] + '/download')).status_code == 200


async def test_restore_migrates_existing_version_five_archive(fleet, tmp_path):
    from backend.database import SCHEMA_VERSION
    from backend.modules.backups.archive import encrypt_archive, decrypt_archive
    from backend.modules.backups.restore import restore_archive
    password = 'separate export password for schedules'
    response = await fleet.client.post('/api/backups/export', headers=fleet.headers, json={'passphrase': password})
    files = decrypt_archive(response.content, password)
    old = tmp_path / 'version5.db'
    old.write_bytes(files['dashboard.db'])
    connection = sqlite3.connect(old)
    try:
        connection.execute('DROP TABLE backup_schedules')
        connection.execute('DROP TABLE backup_runs')
        connection.execute('PRAGMA user_version=5')
        connection.commit()
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    finally:
        connection.close()
    files['dashboard.db'] = old.read_bytes()
    destination = tmp_path / 'migrated'
    await asyncio.to_thread(restore_archive, encrypt_archive(files, password), password, destination)
    connection = sqlite3.connect(destination / 'dashboard.db')
    try:
        # Restoring an old archive migrates it to the current schema, whatever that version is now.
        assert connection.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert connection.execute('SELECT * FROM backup_schedules').fetchall() == []
    finally:
        connection.close()


async def test_backup_schedule_ciphertext_blocks_key_replacement(fleet, tmp_path):
    from backend.config import Vault, VaultError
    await configure(fleet)
    isolated = tmp_path / 'keyloss'
    isolated.mkdir()
    await fleet.app.state.db.backup(isolated / 'dashboard.db')
    connection = sqlite3.connect(isolated / 'dashboard.db')
    try:
        connection.execute('DELETE FROM documents')
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(VaultError):
        Vault(isolated)
    assert not (isolated / '.secret_key').exists()


async def test_scheduled_s3_upload_over_real_verified_loopback(fleet):
    from backend.net import create_http_client
    from backend.modules.backups.archive import decrypt_archive
    received = asyncio.get_running_loop().create_future()
    async def receive(reader, writer):
        try:
            headers = await reader.readuntil(b'\r\n\r\n')
            length = next(int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n') if line.lower().startswith(b'content-length:'))
            body = await reader.readexactly(length)
            received.set_result((headers, body))
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(receive, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    original = fleet.app.state.http
    try:
        async with create_http_client(('127.0.0.1/32',)) as client:
            fleet.app.state.http = client
            fleet.app.state.vault.set('S3', {'access_key_id': 'EXAMPLE', 'secret_access_key': 'fixture-secret'})
            created = await configure(fleet, s3={'endpoint': f'http://127.0.0.1:{port}', 'bucket': 'backups',
                                                'region': 'us-east-1', 'credentials_ref': '$S3'})
            assert created.status_code == 200, created.text
            run = (await fleet.client.post('/api/backups/schedules/' + created.json()['id'] + '/run', headers=fleet.headers)).json()
            assert run['status'] == 'succeeded' and run['remote_status'] == 'uploaded', run
            headers, data = await asyncio.wait_for(received, 3)
            assert headers.startswith(f"PUT /backups/inference/{run['id']}.ibackup HTTP/1.1".encode())
            assert b'fixture-secret' not in headers and b'cookie:' not in headers.lower()
            assert b'authorization: AWS4-HMAC-SHA256' in headers
            assert 'dashboard.db' in decrypt_archive(data, 'separate export password for schedules')
    finally:
        fleet.app.state.http = original
        server.close()
        await server.wait_closed()


async def test_background_loop_runs_due_schedule_and_stops(fleet, monkeypatch):
    from backend.modules.backups import schedules
    schedule = (await configure(fleet)).json()['id']
    await fleet.app.state.db.execute('UPDATE backup_schedules SET next_run_at=0 WHERE id=?', (schedule,))
    monkeypatch.setattr(schedules, 'POLL_SECONDS', 0.01)
    task = asyncio.create_task(schedules.maintenance(fleet.app))
    try:
        async def completed():
            while True:
                row = await fleet.app.state.db.fetchone("SELECT * FROM backup_runs WHERE status='succeeded'")
                if row:
                    return row
                await asyncio.sleep(0.01)
        assert (await asyncio.wait_for(completed(), 10))['schedule_id'] == schedule
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not fleet.app.state.backup_lock.locked()


async def test_missing_password_records_failed_run_without_file(fleet):
    schedule = (await configure(fleet, passphrase_ref='$MISSING_BACKUP_PASSWORD')).json()['id']
    run = (await fleet.client.post(f'/api/backups/schedules/{schedule}/run', headers=fleet.headers)).json()
    assert run['status'] == 'failed' and run['code'] == 'backup_password_unavailable'
    assert not list(fleet.app.state.settings.data_dir.glob('backups/*.ibackup'))


async def test_manual_run_audits_requesting_admin(fleet):
    schedule = (await configure(fleet)).json()['id']
    password = 'a separate password for second admin'
    assert (await fleet.client.post('/api/admin/users', headers=fleet.headers,
        json={'username': 'second', 'password': password, 'role': 'admin'})).status_code == 200
    assert (await fleet.client.post('/api/auth/login', json={'username': 'second', 'password': password})).status_code == 200
    headers = {'X-AGD-CSRF': fleet.client.cookies['agd_csrf']}
    run = (await fleet.client.post(f'/api/backups/schedules/{schedule}/run', headers=headers)).json()
    assert run['status'] == 'succeeded' and run['actor'] == 'second'
    audit = await fleet.app.state.db.fetchone("SELECT actor FROM audit_events WHERE target=? AND action='backups.run_completed'", (run['id'],))
    assert audit['actor'] == 'second'
