import asyncio
import sqlite3
import threading

import pytest

from test_fleet import fleet, add


async def test_export_restores_database_vault_and_notes(fleet, tmp_path):
    from backend.modules.backups.restore import restore_archive
    from backend.config import Vault
    instance = await add(fleet)
    fleet.app.state.vault.set('EXAMPLE', 'private-value')
    await fleet.client.put('/api/notes/note', headers=fleet.headers,
                           json={'path': 'folder/Runbook.md', 'content': '# Restore me'})
    response = await fleet.client.post('/api/backups/export', headers=fleet.headers,
                                      json={'passphrase': 'a separate long export passphrase'})
    assert response.status_code == 200, response.text
    assert b'private-value' not in response.content
    destination = tmp_path / 'restored'
    await asyncio.to_thread(restore_archive, response.content, 'a separate long export passphrase', destination)
    vault = Vault(destination)
    assert vault.resolve('$EXAMPLE') == 'private-value'
    assert (destination / 'notes/folder/Runbook.md').read_text() == '# Restore me'
    with sqlite3.connect(destination / 'dashboard.db') as db:
        key = db.execute('SELECT api_key_enc FROM instances WHERE id=?', (instance,)).fetchone()[0]
        assert vault.decrypt(key) == 'alpha-secret-key'
    audit = await fleet.app.state.db.fetchone("SELECT * FROM audit_events WHERE action='backups.exported'")
    assert audit is not None
    from backend.main import create_app
    restored_app = create_app(data_dir=destination)
    async with restored_app.router.lifespan_context(restored_app):
        assert restored_app.state.vault.resolve('$EXAMPLE') == 'private-value'
        assert (await restored_app.state.db.fetchone('SELECT id FROM instances WHERE id=?', (instance,)))['id'] == instance


async def test_export_requires_admin_and_csrf(fleet):
    body = {'passphrase': 'a separate long export passphrase'}
    assert (await fleet.client.post('/api/backups/export', json=body)).status_code == 403
    await fleet.app.state.db.execute("UPDATE users SET role='operator' WHERE username='owner'")
    assert (await fleet.client.post('/api/backups/export', json=body, headers=fleet.headers)).status_code == 403


async def test_restore_rejects_wrong_password_and_existing_data(fleet, tmp_path):
    from backend.modules.backups.restore import restore_archive
    from backend.modules.backups.archive import ArchiveError
    response = await fleet.client.post('/api/backups/export', headers=fleet.headers,
                                      json={'passphrase': 'a separate long export passphrase'})
    destination = tmp_path / 'existing'
    destination.mkdir()
    (destination / 'keep').write_text('original')
    with pytest.raises(ArchiveError):
        await asyncio.to_thread(restore_archive, response.content, 'a separate long export passphrase', destination)
    assert list(destination.iterdir()) == [destination / 'keep']
    absent = tmp_path / 'absent'
    with pytest.raises(ArchiveError):
        await asyncio.to_thread(restore_archive, response.content, 'wrong password', absent)
    assert not absent.exists()


async def test_role_revoked_during_encryption_discards_export(fleet, monkeypatch):
    from backend.modules.backups import router
    started, release = threading.Event(), threading.Event()
    original = router.encrypt_archive
    def blocked(*args):
        started.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(router, 'encrypt_archive', blocked)
    task = asyncio.create_task(fleet.client.post('/api/backups/export', headers=fleet.headers,
        json={'passphrase': 'a separate long export passphrase'}))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        await fleet.app.state.db.execute("UPDATE users SET role='viewer' WHERE username='owner'")
    finally:
        release.set()
    assert (await task).status_code == 403
    assert not list(fleet.app.state.settings.data_dir.glob('.backup-*'))
    assert not await fleet.app.state.db.fetchone("SELECT * FROM audit_events WHERE action='backups.exported'")


async def test_cancelled_export_settles_cleanup_and_lock(fleet, monkeypatch):
    from backend.modules.backups import router
    started, release = threading.Event(), threading.Event()
    original = router.encrypt_archive
    def blocked(*args):
        started.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(router, 'encrypt_archive', blocked)
    task = asyncio.create_task(fleet.client.post('/api/backups/export', headers=fleet.headers,
        json={'passphrase': 'a separate long export passphrase'}))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        assert fleet.app.state.backup_lock.locked()
        busy = await fleet.client.post('/api/backups/export', headers=fleet.headers,
            json={'passphrase': 'a separate long export passphrase'})
        assert busy.status_code == 409
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not fleet.app.state.backup_lock.locked()
    assert not list(fleet.app.state.settings.data_dir.glob('.backup-*'))


async def test_restore_rejects_invalid_database_before_destination(fleet, tmp_path):
    from backend.modules.backups.archive import encrypt_archive, decrypt_archive, ArchiveError
    from backend.modules.backups.restore import restore_archive
    password = 'a separate long export passphrase'
    response = await fleet.client.post('/api/backups/export', headers=fleet.headers, json={'passphrase': password})
    files = decrypt_archive(response.content, password)
    files['dashboard.db'] = b'not a sqlite database'
    destination = tmp_path / 'invalid-restored'
    with pytest.raises(ArchiveError):
        await asyncio.to_thread(restore_archive, encrypt_archive(files, password), password, destination)
    assert not destination.exists()


async def test_restore_rejects_incompatible_schema(fleet, tmp_path):
    from backend.modules.backups.archive import encrypt_archive, decrypt_archive, ArchiveError
    from backend.modules.backups.restore import restore_archive
    password = 'a separate long export passphrase'
    response = await fleet.client.post('/api/backups/export', headers=fleet.headers, json={'passphrase': password})
    files = decrypt_archive(response.content, password)
    altered = tmp_path / 'altered.db'
    altered.write_bytes(files['dashboard.db'])
    connection = sqlite3.connect(altered)
    try:
        connection.execute('DROP TABLE otel_spans')
        connection.commit()
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    finally:
        connection.close()
    files['dashboard.db'] = altered.read_bytes()
    destination = tmp_path / 'bad-schema'
    with pytest.raises(ArchiveError):
        await asyncio.to_thread(restore_archive, encrypt_archive(files, password), password, destination)
    assert not destination.exists()


async def test_export_bounds_root_files_and_redacts_passphrase(fleet, monkeypatch):
    from backend.modules.backups import router
    result = await fleet.client.post('/api/backups/export', headers=fleet.headers, json={'passphrase': 'short-secret'})
    assert result.status_code == 422 and 'short-secret' not in result.text
    monkeypatch.setattr(router, 'MAX_BYTES', 1)
    result = await fleet.client.post('/api/backups/export', headers=fleet.headers,
                                    json={'passphrase': 'a separate long export passphrase'})
    assert result.status_code == 503
    assert not list(fleet.app.state.settings.data_dir.glob('.backup-*'))
