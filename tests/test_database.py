"""Exercise persistence against real SQLite files, including the original schema."""

import asyncio
import os
import sqlite3
import stat
from pathlib import Path

import pytest


async def test_promotion_migration_reopens_and_preserves_journal(tmp_path):
    from backend.database import Database
    path=tmp_path/'promotion.db'
    db=Database(path)
    await db.open()
    try:
        await db.execute("INSERT INTO promotion_runs(id,actor,preview_id,idempotency_key,status,state_enc) VALUES('run','alice','preview','key','writing','encrypted')")
    finally:
        await db.close()
    await db.open()
    try:
        assert (await db.fetchone("SELECT status FROM promotion_runs WHERE id='run'"))['status']=='writing'
        from backend.database import SCHEMA_VERSION
        assert (await db.fetchone('PRAGMA user_version'))['user_version']==SCHEMA_VERSION
        assert await db.fetchall('SELECT * FROM credential_mappings')==[]
        assert await db.fetchall('SELECT * FROM workflow_mappings')==[]
    finally:
        await db.close()


LEGACY_SCHEMA = """
CREATE TABLE instances (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
    api_key_enc TEXT NOT NULL, is_active INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT, instance_id TEXT NOT NULL DEFAULT '',
    workflow_id TEXT NOT NULL, workflow_name TEXT NOT NULL DEFAULT 'Unknown Workflow',
    execution_id TEXT DEFAULT '', node_name TEXT DEFAULT '', error_type TEXT DEFAULT 'Error',
    error_message TEXT NOT NULL, count INTEGER DEFAULT 1,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_errors_occurred ON errors(occurred_at DESC);
CREATE INDEX idx_errors_instance ON errors(instance_id, occurred_at DESC);
CREATE TABLE otel_spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL, span_id TEXT NOT NULL,
    parent_id TEXT NOT NULL DEFAULT '', instance_id TEXT NOT NULL DEFAULT '',
    workflow_id TEXT NOT NULL DEFAULT '', workflow_name TEXT NOT NULL DEFAULT '',
    execution_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
    kind INTEGER NOT NULL DEFAULT 0, start_ns INTEGER NOT NULL DEFAULT 0,
    end_ns INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT '',
    attributes_json TEXT NOT NULL DEFAULT '{}',
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    model TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL,
    health_status TEXT, error_type TEXT, error_summary TEXT,
    output_items INTEGER, input_items INTEGER, node_id TEXT, silent INTEGER DEFAULT 0,
    checked_at TEXT
);
CREATE INDEX idx_otel_spans_trace ON otel_spans(trace_id);
CREATE INDEX idx_otel_spans_instance ON otel_spans(instance_id, start_ns DESC);
CREATE INDEX idx_otel_spans_node ON otel_spans(node_id, start_ns DESC);
CREATE UNIQUE INDEX idx_otel_spans_unique ON otel_spans(trace_id, span_id);
CREATE TABLE otel_instance_map (
    resource_hash TEXT PRIMARY KEY, instance_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'learned',
    learned_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE auth_sessions (
    id_hash TEXT PRIMARY KEY, username TEXT NOT NULL, created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL, last_seen TEXT NOT NULL, user_agent TEXT, ip TEXT
);
CREATE INDEX idx_auth_sessions_user ON auth_sessions(username);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT DEFAULT '', body TEXT DEFAULT '',
    level TEXT NOT NULL DEFAULT 'info', source TEXT DEFAULT '',
    occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@pytest.fixture
async def db(tmp_path):
    from backend.database import Database

    database = Database(tmp_path / "dashboard.db")
    await database.open()
    try:
        yield database
    finally:
        await database.close()


async def test_open_is_idempotent_and_queries_return_dicts(db):
    await db.open()
    assert await db.fetchone("PRAGMA journal_mode") == {"journal_mode": "wal"}
    assert await db.fetchone("PRAGMA foreign_keys") == {"foreign_keys": 1}
    assert await db.fetchone("PRAGMA busy_timeout") == {"timeout": 5000}
    assert (await db.fetchone("PRAGMA user_version"))["user_version"] >= 1
    row_id = await db.execute("INSERT INTO messages(title) VALUES (?)", ("hello",))
    assert isinstance(row_id, int) and row_id > 0
    assert await db.fetchone("SELECT title FROM messages WHERE id=?", (row_id,)) == {"title": "hello"}
    assert await db.fetchall("SELECT title FROM messages") == [{"title": "hello"}]
    assert await db.fetchone("SELECT title FROM messages WHERE id=-1") is None
    await db.close()
    await db.close()
    await db.open()
    assert await db.fetchall("SELECT title FROM messages") == [{"title": "hello"}]


async def test_closed_database_requires_explicit_open(tmp_path):
    from backend.database import Database

    database = Database(tmp_path / "unused" / "dashboard.db")
    assert not database.path.exists()
    with pytest.raises(RuntimeError, match="open"):
        await database.fetchone("SELECT 1")
    await database.close()
    assert not database.path.parent.exists()


async def test_whole_transaction_rolls_back_on_error(db):
    with pytest.raises(ValueError, match="abort"):
        async with db.transaction() as tx:
            await tx.execute("INSERT INTO documents(key,value_json) VALUES (?,?)", ("one", "{}"))
            await tx.execute("INSERT INTO documents(key,value_json) VALUES (?,?)", ("two", "[]"))
            assert await tx.fetchone("SELECT value_json FROM documents WHERE key='one'") == {"value_json": "{}"}
            assert len(await tx.fetchall("SELECT * FROM documents")) == 2
            raise ValueError("abort")
    assert await db.fetchall("SELECT * FROM documents") == []
    async with db.transaction() as tx:
        row_id = await tx.execute("INSERT INTO messages(title) VALUES ('committed')")
        assert row_id > 0
    assert await db.fetchone("SELECT title FROM messages") == {"title": "committed"}


async def test_transaction_cancellation_finishes_rollback_before_next_transaction(db):
    inserted = asyncio.Event()

    async def cancelled_write():
        async with db.transaction() as tx:
            await tx.execute("INSERT INTO documents(key,value_json) VALUES ('cancelled','{}')")
            inserted.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(cancelled_write())
    await asyncio.wait_for(inserted.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(db.execute("INSERT INTO documents(key,value_json) VALUES ('kept','{}')"), timeout=2)
    assert await db.fetchall("SELECT key FROM documents") == [{"key": "kept"}]


async def test_repeated_cancellation_cannot_release_lock_during_rollback(db, monkeypatch):
    import aiosqlite

    inserted = asyncio.Event()
    rollback_started = asyncio.Event()
    finish_rollback = asyncio.Event()
    original_rollback = aiosqlite.Connection.rollback

    async def delayed_rollback(connection):
        # Pause only the cleanup boundary; the rollback still runs on real SQLite.
        rollback_started.set()
        await finish_rollback.wait()
        await original_rollback(connection)

    monkeypatch.setattr(aiosqlite.Connection, "rollback", delayed_rollback)

    async def cancelled_write():
        async with db.transaction() as tx:
            await tx.execute("INSERT INTO documents(key,value_json) VALUES ('cancelled','{}')")
            inserted.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(cancelled_write())
    await asyncio.wait_for(inserted.wait(), timeout=2)
    task.cancel()
    await asyncio.wait_for(rollback_started.wait(), timeout=2)
    task.cancel()
    reader = asyncio.create_task(db.fetchall("SELECT * FROM documents"))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not reader.done(), "A second task must wait for rollback to finish"
    finally:
        finish_rollback.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await asyncio.wait_for(reader, timeout=2) == []


async def test_concurrent_read_modify_write_transactions_do_not_lose_updates(db):
    await db.execute("INSERT INTO documents(key,value_json) VALUES ('count','0')")

    async def increment():
        async with db.transaction() as tx:
            row = await tx.fetchone("SELECT value_json FROM documents WHERE key='count'")
            await asyncio.sleep(0)
            await tx.execute("UPDATE documents SET value_json=? WHERE key='count'", (str(int(row["value_json"]) + 1),))

    await asyncio.wait_for(asyncio.gather(*(increment() for _ in range(30))), timeout=5)
    assert await db.fetchone("SELECT value_json FROM documents WHERE key='count'") == {"value_json": "30"}


async def test_database_reader_cannot_see_uncommitted_transaction(db):
    reader = None
    async with db.transaction() as tx:
        await tx.execute("INSERT INTO documents(key,value_json) VALUES ('one','{}')")
        reader = asyncio.create_task(db.fetchall("SELECT key FROM documents"))
        await asyncio.sleep(0)
        assert not reader.done()
    assert await asyncio.wait_for(reader, timeout=2) == [{"key": "one"}]


async def test_transaction_handles_cannot_escape_or_be_nested(db):
    async with db.transaction() as tx:
        with pytest.raises(RuntimeError):
            async with db.transaction():
                pass
        with pytest.raises(RuntimeError):
            await db.fetchone("SELECT 1")

        async def other_task():
            return await tx.fetchone("SELECT 1")

        with pytest.raises(RuntimeError):
            await asyncio.create_task(other_task())
    with pytest.raises(RuntimeError):
        await tx.execute("INSERT INTO documents(key,value_json) VALUES ('escaped','{}')")
    assert await db.fetchall("SELECT * FROM documents") == []


async def test_auth_schema_enforces_roles_and_recovery_foreign_keys(db):
    await db.execute("INSERT INTO users(username,password_hash,role) VALUES ('owner','hash','admin')")
    owner = await db.fetchone("SELECT * FROM users WHERE username='owner'")
    assert owner["disabled"] == 0
    assert owner["totp_secret_enc"] is None
    assert owner["totp_pending_enc"] is None
    assert owner["totp_last_counter"] == -1
    assert owner["created_at"]
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute("INSERT INTO users(username,password_hash,role) VALUES ('bad','hash','superuser')")
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute("INSERT INTO recovery_codes(username,code_hash) VALUES ('missing','code')")
    await db.execute("INSERT INTO recovery_codes(username,code_hash) VALUES ('owner','code')")
    await db.execute("DELETE FROM users WHERE username='owner'")
    assert await db.fetchall("SELECT * FROM recovery_codes") == []
    audit_id = await db.execute("INSERT INTO audit_events(actor,action,target) VALUES ('owner','test','db')")
    assert await db.fetchone("SELECT details_json,outcome FROM audit_events WHERE id=?", (audit_id,)) == {"details_json": "{}", "outcome": "success"}


@pytest.mark.parametrize("legacy_version", [0, 1])
async def test_original_database_upgrade_preserves_rows_and_is_repeatable(tmp_path, legacy_version):
    from backend.database import Database

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(LEGACY_SCHEMA)
        connection.execute(f"PRAGMA user_version={legacy_version}")
        connection.execute("INSERT INTO instances(id,name,url,api_key_enc) VALUES ('one','Production','https://n8n.example','ciphertext')")
        connection.execute("INSERT INTO otel_spans(trace_id,span_id,instance_id,cost_usd) VALUES ('trace','span','one',0.12)")
        connection.execute("INSERT INTO auth_sessions(id_hash,username,created_at,expires_at,last_seen) VALUES ('session','owner','2026-09-07T00:00:00+00:00','2026-09-08T00:00:00+00:00','2026-09-07T00:00:00+00:00')")
        connection.execute("INSERT INTO messages(title) VALUES ('legacy message')")
    database = Database(path)
    for _ in range(2):
        await database.open()
        try:
            assert await database.fetchone("SELECT name,api_key_enc FROM instances") == {"name": "Production", "api_key_enc": "ciphertext"}
            assert await database.fetchone("SELECT cost_usd FROM otel_spans WHERE instance_id='one'") == {"cost_usd": 0.12}
            assert await database.fetchone("SELECT csrf_secret FROM auth_sessions") == {"csrf_secret": ""}
            assert await database.fetchone("SELECT title FROM messages") == {"title": "legacy message"}
            assert (await database.fetchone("PRAGMA user_version"))["user_version"] >= 2
        finally:
            await database.close()
    await database.open()
    try:
        await database.execute("INSERT INTO otel_spans(trace_id,span_id,instance_id) VALUES ('trace','span','two')")
        with pytest.raises(sqlite3.IntegrityError):
            await database.execute("INSERT INTO otel_spans(trace_id,span_id,instance_id) VALUES ('trace','span','one')")
        assert len(await database.fetchall("SELECT * FROM otel_spans")) == 2
    finally:
        await database.close()


async def test_newer_database_is_rejected_without_modification(tmp_path):
    from backend.database import Database

    path = tmp_path / "future.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE future(value TEXT)")
        connection.execute("INSERT INTO future VALUES ('keep')")
        connection.execute("PRAGMA user_version=99999")
    database = Database(path)
    with pytest.raises(RuntimeError, match="newer|unsupported"):
        await database.open()
    await database.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (99999,)
        assert connection.execute("SELECT * FROM future").fetchall() == [("keep",)]
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("future",)]


async def test_malformed_legacy_schema_fails_atomically(tmp_path):
    from backend.database import Database

    path = tmp_path / "malformed.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE instances(id TEXT PRIMARY KEY, unexpected TEXT)")
        connection.execute("INSERT INTO instances VALUES ('keep','legacy')")
    database = Database(path)
    with pytest.raises(RuntimeError, match="schema"):
        await database.open()
    await database.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert connection.execute("SELECT * FROM instances").fetchall() == [("keep", "legacy")]
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("instances",)]


async def test_modified_current_schema_is_rejected_on_reopen(db):
    await db.execute("ALTER TABLE users RENAME COLUMN password_hash TO password_plaintext")
    await db.close()
    with pytest.raises(RuntimeError, match="schema"):
        await db.open()


async def test_users_schema_missing_role_constraint_is_rejected(tmp_path):
    from backend.database import Database

    path = tmp_path / "unconstrained.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(LEGACY_SCHEMA)
        connection.execute("""CREATE TABLE users (
            username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, role TEXT NOT NULL,
            disabled INTEGER DEFAULT 0, totp_secret_enc TEXT, totp_pending_enc TEXT,
            totp_last_counter INTEGER DEFAULT -1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        connection.execute("PRAGMA user_version=1")
    database = Database(path)
    try:
        with pytest.raises(RuntimeError, match="schema"):
            await database.open()
    finally:
        await database.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)


async def test_failed_rollback_closes_connection_before_another_operation(db, monkeypatch):
    import aiosqlite

    async def failed_rollback(connection):
        raise sqlite3.OperationalError("simulated rollback failure")

    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, "rollback", failed_rollback)
        with pytest.raises(sqlite3.OperationalError, match="rollback failure"):
            async with db.transaction() as tx:
                await tx.execute("INSERT INTO documents(key,value_json) VALUES ('uncommitted','{}')")
                raise ValueError("abort")
        with pytest.raises(RuntimeError, match="open"):
            await db.fetchone("SELECT * FROM documents")
    await db.open()
    assert await db.fetchall("SELECT * FROM documents") == []


async def test_backup_includes_committed_wal_data_and_does_not_overwrite(db, tmp_path):
    await db.execute("INSERT INTO documents(key,value_json) VALUES ('snapshot','{\"ok\":true}')")
    destination = tmp_path / "snapshot.db"
    await db.backup(destination)
    with sqlite3.connect(destination) as snapshot:
        assert snapshot.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert snapshot.execute("SELECT key,value_json FROM documents").fetchall() == [("snapshot", '{"ok":true}')]
        assert snapshot.execute("PRAGMA user_version").fetchone()[0] >= 2
    original_snapshot = destination.read_bytes()
    with pytest.raises(FileExistsError):
        await db.backup(destination)
    assert destination.read_bytes() == original_snapshot
    with pytest.raises((FileExistsError, ValueError)):
        await db.backup(db.path)
    assert await db.fetchone("SELECT key FROM documents") == {"key": "snapshot"}


async def test_failed_backup_removes_only_its_new_destination(db, tmp_path, monkeypatch):
    import aiosqlite

    async def fail_backup(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(aiosqlite.Connection, "backup", fail_backup)
    destination = tmp_path / "failed.db"
    with pytest.raises(OSError, match="disk failure"):
        await db.backup(destination)
    assert not destination.exists()
    assert await db.fetchone("SELECT 1 AS alive") == {"alive": 1}


@pytest.mark.parametrize("existing", [False, True])
async def test_database_files_are_private_before_sqlite_connects(tmp_path, monkeypatch, existing):
    import backend.database as module

    path = tmp_path / "dashboard.db"
    paths = [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]
    if existing:
        with sqlite3.connect(path) as connection:
            connection.executescript(LEGACY_SCHEMA)
            connection.execute("INSERT INTO messages(title) VALUES ('preserved')")
        for sidecar in paths[1:]:
            sidecar.touch()

    hardened = set()
    if hasattr(os, "fchmod"):
        original_chmod = os.fchmod

        def tracked_chmod(descriptor, mode):
            metadata = os.fstat(descriptor)
            original_chmod(descriptor, mode)
            if mode == 0o600:
                hardened.add((metadata.st_dev, metadata.st_ino))

        monkeypatch.setattr(os, "fchmod", tracked_chmod)
    else:
        original_chmod = os.chmod

        def tracked_chmod(target, mode, **kwargs):
            metadata = os.stat(target, follow_symlinks=False)
            original_chmod(target, mode, **kwargs)
            if mode == 0o600:
                hardened.add((metadata.st_dev, metadata.st_ino))

        monkeypatch.setattr(os, "chmod", tracked_chmod)

    original_connect = module.aiosqlite.connect

    def checked_connect(target, **kwargs):
        assert path.is_file(), "Create the database privately before SQLite can create its WAL"
        for present in paths if existing else paths[:1]:
            metadata = present.stat()
            assert (metadata.st_dev, metadata.st_ino) in hardened, f"{present.name} was not hardened before connection"
        return original_connect(target, **kwargs)

    monkeypatch.setattr(module.aiosqlite, "connect", checked_connect)
    database = module.Database(path)
    try:
        await database.open()
        if existing:
            assert await database.fetchone("SELECT title FROM messages") == {"title": "preserved"}
        else:
            assert await database.fetchone("PRAGMA journal_mode") == {"journal_mode": "wal"}
    finally:
        await database.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are not enforced by Windows ACLs")
@pytest.mark.parametrize("existing", [False, True])
async def test_database_and_wal_modes_under_permissive_directory_and_umask(tmp_path, existing):
    from backend.database import Database

    folder = tmp_path / "old-data-dir"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    path = folder / "dashboard.db"
    paths = [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]
    legacy = None
    previous_umask = os.umask(0o022)
    database = Database(path)
    try:
        if existing:
            legacy = sqlite3.connect(path)
            legacy.execute("PRAGMA journal_mode=WAL")
            legacy.executescript(LEGACY_SCHEMA)
            legacy.execute("INSERT INTO messages(title) VALUES ('preserved WAL row')")
            legacy.commit()
            for present in paths:
                present.chmod(0o644)
        await database.open()
        for present in paths:
            assert stat.S_IMODE(present.stat().st_mode) == 0o600, present.name
        if existing:
            assert await database.fetchone("SELECT title FROM messages") == {"title": "preserved WAL row"}
    finally:
        await database.close()
        if legacy is not None:
            legacy.close()
        os.umask(previous_umask)


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
async def test_database_rejects_symlinked_storage_without_touching_target(tmp_path, monkeypatch, suffix):
    import backend.database as module

    path = tmp_path / "dashboard.db"
    target = tmp_path / "unrelated-file"
    target.write_bytes(b"must remain untouched")
    link = Path(str(path) + suffix)
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Creating symbolic links requires unavailable OS privileges")

    def unexpected_connect(*args, **kwargs):
        pytest.fail("Reject symbolic links before asking SQLite to open storage")

    monkeypatch.setattr(module.aiosqlite, "connect", unexpected_connect)
    database = module.Database(path)
    try:
        with pytest.raises(RuntimeError, match="regular|symbolic"):
            await database.open()
    finally:
        await database.close()
    assert target.read_bytes() == b"must remain untouched"
