import asyncio
import threading

import pytest

from test_fleet import fleet


async def save(state, path, content, revision=None):
    return await state.client.put('/api/notes/note', headers=state.headers,
        json={'path': path, 'content': content, 'expected_revision': revision})


async def test_note_create_edit_conflict_delete(fleet):
    result = await save(fleet, 'Operations.md', '# Hello')
    assert result.status_code == 200, result.text
    revision = result.json()['revision']
    assert (await save(fleet, 'Operations.md', 'overwrite')).status_code == 409
    updated = await save(fleet, 'Operations.md', '# Changed', revision)
    assert updated.status_code == 200
    assert (await save(fleet, 'Operations.md', 'stale', revision)).status_code == 409
    response = await fleet.client.delete('/api/notes/note', headers=fleet.headers,
        params={'path': 'Operations.md', 'expected_revision': updated.json()['revision']})
    assert response.status_code == 200
    assert (await fleet.client.get('/api/notes/note', params={'path': 'Operations.md'})).status_code == 404


async def test_literal_search_and_backlinks(fleet):
    await save(fleet, 'Target.md', 'target')
    await save(fleet, 'folder/Source.md', 'Literal .* needle [[Target|alias]] [[Target#section]]')
    await save(fleet, 'Unrelated.md', 'anything')
    search = await fleet.client.get('/api/notes/search', params={'q': '.*'})
    assert search.status_code == 200, search.text
    assert [n['path'] for n in search.json()['data']] == ['folder/Source.md']
    links = await fleet.client.get('/api/notes/backlinks', params={'path': 'Target.md'})
    assert links.status_code == 200, links.text
    assert [n['path'] for n in links.json()['data']] == ['folder/Source.md']


async def test_preview_escapes_untrusted_html(fleet):
    await save(fleet, 'Unsafe.md', '<script>alert(1)</script> & <img src=x onerror=alert(2)>')
    preview = await fleet.client.get('/api/notes/render', params={'path': 'Unsafe.md'})
    assert preview.status_code == 200
    assert '<script>' not in preview.json()['html'] and '<img' not in preview.json()['html']
    assert '&lt;script&gt;' in preview.json()['html']


async def test_viewer_reads_but_cannot_write_and_csrf_required(fleet):
    await save(fleet, 'Allowed.md', 'content')
    denied = await fleet.client.put('/api/notes/note', json={'path': 'Denied.md', 'content': 'no'})
    assert denied.status_code == 403
    await fleet.app.state.db.execute("UPDATE users SET role='viewer' WHERE username='owner'")
    assert (await fleet.client.get('/api/notes/note', params={'path': 'Allowed.md'})).status_code == 200
    assert (await save(fleet, 'Denied.md', 'no')).status_code == 403
    assert not (fleet.app.state.settings.data_dir / 'notes' / 'Denied.md').exists()


async def test_traversal_rejected_and_audit_omits_contents(fleet):
    assert (await save(fleet, '../secret.md', 'no')).status_code == 400
    result = await save(fleet, 'Saved.md', 'private note text')
    assert result.status_code == 200
    rows = await fleet.app.state.db.fetchall("SELECT * FROM audit_events WHERE action LIKE 'notes.%'")
    assert rows and 'private note text' not in str(rows)


async def test_concurrent_saves_preserve_one_revision(fleet):
    old = (await save(fleet, 'Concurrent.md', 'old')).json()['revision']
    results = await asyncio.gather(save(fleet, 'Concurrent.md', 'one', old), save(fleet, 'Concurrent.md', 'two', old))
    assert sorted(r.status_code for r in results) == [200, 409]


async def test_note_read_rechecks_revoked_session_after_io(fleet, monkeypatch):
    await save(fleet, 'Private.md', 'private content')
    store = fleet.app.state.notes
    original = store.read
    started, release = threading.Event(), threading.Event()
    def delayed(path):
        started.set()
        assert release.wait(timeout=5)
        return original(path)
    monkeypatch.setattr(store, 'read', delayed)
    task = asyncio.create_task(fleet.client.get('/api/notes/note', params={'path': 'Private.md'}))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        await fleet.app.state.db.execute("UPDATE users SET disabled=1 WHERE username='owner'")
    finally:
        release.set()
    response = await task
    assert response.status_code == 401 and 'private content' not in response.text


async def test_cancelled_save_finishes_file_and_audit(fleet, monkeypatch):
    from test_assistant_tools import actor
    from backend.modules.notes.router import save as save_route, SaveNote
    original = fleet.app.state.notes.write
    started, release = threading.Event(), threading.Event()
    def delayed(*args):
        started.set()
        assert release.wait(timeout=5)
        return original(*args)
    monkeypatch.setattr(fleet.app.state.notes, 'write', delayed)
    task = asyncio.create_task(save_route(SaveNote(path='Cancelled.md', content='saved'), await actor(fleet)))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fleet.app.state.notes.read('Cancelled.md')['content'] == 'saved'
    assert await fleet.app.state.db.fetchone("SELECT id FROM audit_events WHERE action='notes.saved' AND target='Cancelled.md'")


async def test_backlinks_do_not_guess_ambiguous_basenames(fleet):
    await save(fleet, 'one/Target.md', 'target')
    await save(fleet, 'two/Target.md', 'target')
    await save(fleet, 'Source.md', '[[Target]]')
    await save(fleet, 'one/Local.md', '[[Target|label]]')
    result = await fleet.client.get('/api/notes/backlinks', params={'path': 'one/Target.md'})
    assert result.status_code == 200
    assert [n['path'] for n in result.json()['data']] == ['one/Local.md']


async def test_audit_failure_reports_uncertain_save_without_claiming_rollback(fleet, monkeypatch):
    from backend.database import Transaction
    import sqlite3
    original = Transaction.execute
    async def broken(self, sql, params=()):
        if sql.startswith('INSERT INTO audit_events') and 'notes.saved' in params:
            raise sqlite3.OperationalError('private database detail')
        return await original(self, sql, params)
    monkeypatch.setattr(Transaction, 'execute', broken)
    response = await save(fleet, 'Uncertain.md', 'saved content')
    assert response.status_code == 503
    assert response.json()['code'] == 'notes_outcome_unknown'
    assert 'private database detail' not in response.text
    assert fleet.app.state.notes.read('Uncertain.md')['content'] == 'saved content'
