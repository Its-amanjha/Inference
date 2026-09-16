"""One application-owned SQLite connection with serialized transactions.

Schema changes and their user_version updates commit together. Public SQL is
trusted application SQL; use transaction() for multi-statement units and its
Transaction methods inside the context. Do not issue transaction-control SQL.
"""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import aiosqlite


Params = Sequence[Any] | Mapping[str, Any]
Row = dict[str, Any]
T = TypeVar("T")
SCHEMA_VERSION = 7

# Bundled skill library, synced from backend/skills/ at boot. Disk is the source of truth; this table
# is the queryable cache the API and the assistant's get_skill tool both read.
_SKILLS = {
    'skills': {
        'id': 'TEXT PRIMARY KEY', 'name': 'TEXT NOT NULL', 'description': "TEXT NOT NULL DEFAULT ''",
        'content': 'TEXT NOT NULL', 'updated_at': 'TEXT DEFAULT CURRENT_TIMESTAMP',
    },
}

_BACKUPS = {
    'backup_schedules': {
        'id': 'TEXT PRIMARY KEY', 'name': 'TEXT NOT NULL', 'owner': 'TEXT NOT NULL',
        'enabled': 'INTEGER NOT NULL', 'interval_seconds': 'INTEGER NOT NULL',
        'next_run_at': 'REAL NOT NULL', 'version': 'TEXT NOT NULL', 'config_enc': 'TEXT NOT NULL',
        'created_at': 'REAL NOT NULL',
    },
    'backup_runs': {
        'id': 'TEXT PRIMARY KEY', 'schedule_id': 'TEXT NOT NULL', 'actor': 'TEXT NOT NULL',
        'status': 'TEXT NOT NULL', 'code': "TEXT NOT NULL DEFAULT ''",
        'created_at': 'REAL NOT NULL', 'completed_at': 'REAL',
        'size_bytes': 'INTEGER', 'sha256': 'TEXT',
        'remote_status': "TEXT NOT NULL DEFAULT 'not_requested'",
    },
}

# Column declarations also define the minimum shape accepted on reopening.
# Extra columns are retained, so operator additions are never silently lost.
_BASELINE: dict[str, dict[str, str]] = {
    "instances": {
        "id": "TEXT PRIMARY KEY",
        "name": "TEXT NOT NULL",
        "url": "TEXT NOT NULL",
        "api_key_enc": "TEXT NOT NULL",
        "is_active": "INTEGER DEFAULT 0",
        "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    },
    "errors": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "instance_id": "TEXT NOT NULL DEFAULT ''",
        "workflow_id": "TEXT NOT NULL",
        "workflow_name": "TEXT NOT NULL DEFAULT 'Unknown Workflow'",
        "execution_id": "TEXT DEFAULT ''",
        "node_name": "TEXT DEFAULT ''",
        "error_type": "TEXT DEFAULT 'Error'",
        "error_message": "TEXT NOT NULL",
        "count": "INTEGER DEFAULT 1",
        "occurred_at": "TEXT NOT NULL DEFAULT (datetime('now'))",
    },
    "otel_spans": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "trace_id": "TEXT NOT NULL",
        "span_id": "TEXT NOT NULL",
        "parent_id": "TEXT NOT NULL DEFAULT ''",
        "instance_id": "TEXT NOT NULL DEFAULT ''",
        "workflow_id": "TEXT NOT NULL DEFAULT ''",
        "workflow_name": "TEXT NOT NULL DEFAULT ''",
        "execution_id": "TEXT NOT NULL DEFAULT ''",
        "name": "TEXT NOT NULL DEFAULT ''",
        "kind": "INTEGER NOT NULL DEFAULT 0",
        "start_ns": "INTEGER NOT NULL DEFAULT 0",
        "end_ns": "INTEGER NOT NULL DEFAULT 0",
        "status": "TEXT NOT NULL DEFAULT ''",
        "attributes_json": "TEXT NOT NULL DEFAULT '{}'",
        "received_at": "TEXT NOT NULL DEFAULT (datetime('now'))",
        "model": "TEXT",
        "tokens_in": "INTEGER",
        "tokens_out": "INTEGER",
        "cost_usd": "REAL",
        "health_status": "TEXT",
        "error_type": "TEXT",
        "error_summary": "TEXT",
        "output_items": "INTEGER",
        "input_items": "INTEGER",
        "node_id": "TEXT",
        "silent": "INTEGER DEFAULT 0",
        "checked_at": "TEXT",
    },
    "otel_instance_map": {
        "resource_hash": "TEXT PRIMARY KEY",
        "instance_id": "TEXT NOT NULL",
        "source": "TEXT NOT NULL DEFAULT 'learned'",
        "learned_at": "TEXT NOT NULL DEFAULT (datetime('now'))",
    },
    "auth_sessions": {
        "id_hash": "TEXT PRIMARY KEY",
        "username": "TEXT NOT NULL",
        "created_at": "TEXT NOT NULL",
        "expires_at": "TEXT NOT NULL",
        "last_seen": "TEXT NOT NULL",
        "user_agent": "TEXT",
        "ip": "TEXT",
    },
    "messages": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "title": "TEXT DEFAULT ''",
        "body": "TEXT DEFAULT ''",
        "level": "TEXT NOT NULL DEFAULT 'info'",
        "source": "TEXT DEFAULT ''",
        "occurred_at": "TEXT NOT NULL DEFAULT (datetime('now'))",
    },
}

_FOUNDATION: dict[str, dict[str, str]] = {
    "users": {
        "username": "TEXT PRIMARY KEY",
        "password_hash": "TEXT NOT NULL",
        "role": "TEXT NOT NULL CHECK (role IN ('viewer','operator','admin'))",
        "disabled": "INTEGER DEFAULT 0",
        "totp_secret_enc": "TEXT",
        "totp_pending_enc": "TEXT",
        "totp_last_counter": "INTEGER DEFAULT -1",
        "created_at": "TEXT NOT NULL DEFAULT (datetime('now'))",
    },
    "recovery_codes": {
        "username": "TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE",
        "code_hash": "TEXT NOT NULL",
    },
    "audit_events": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "actor": "TEXT NOT NULL",
        "action": "TEXT NOT NULL",
        "target": "TEXT NOT NULL",
        "details_json": "TEXT NOT NULL DEFAULT '{}'",
        "outcome": "TEXT NOT NULL DEFAULT 'success'",
        "occurred_at": "TEXT NOT NULL DEFAULT (datetime('now'))",
    },
    "documents": {
        "key": "TEXT PRIMARY KEY",
        "value_json": "TEXT NOT NULL",
        "updated_at": "TEXT NOT NULL DEFAULT (datetime('now'))",
    },
}

_PROMOTION = {
    'credential_mappings': {
        'key': 'TEXT PRIMARY KEY', 'source_instance_id': 'TEXT NOT NULL',
        'target_instance_id': 'TEXT NOT NULL', 'source_credential_id': 'TEXT NOT NULL',
        'target_credential_id': 'TEXT NOT NULL', 'credential_type': 'TEXT NOT NULL',
        'target_name': 'TEXT NOT NULL',
    },
    'workflow_mappings': {
        'key': 'TEXT PRIMARY KEY', 'source_instance_id': 'TEXT NOT NULL',
        'target_instance_id': 'TEXT NOT NULL', 'source_workflow_id': 'TEXT NOT NULL',
        'target_workflow_id': 'TEXT NOT NULL',
    },
    'promotion_runs': {
        'id': 'TEXT PRIMARY KEY', 'actor': 'TEXT NOT NULL', 'preview_id': 'TEXT NOT NULL',
        'idempotency_key': 'TEXT NOT NULL UNIQUE', 'status': 'TEXT NOT NULL',
        'state_enc': 'TEXT NOT NULL', 'created_at': 'TEXT DEFAULT CURRENT_TIMESTAMP',
        'updated_at': 'TEXT DEFAULT CURRENT_TIMESTAMP',
    },
}

_ASSISTANT = {
    'approval_actions': {
        'id': 'TEXT PRIMARY KEY', 'actor': 'TEXT NOT NULL', 'session_id': 'TEXT NOT NULL',
        'name': 'TEXT NOT NULL', 'target': 'TEXT NOT NULL', 'status': 'TEXT NOT NULL',
        'state_enc': 'TEXT NOT NULL', 'expires_at': 'REAL NOT NULL',
        'created_at': 'TEXT DEFAULT CURRENT_TIMESTAMP', 'updated_at': 'TEXT DEFAULT CURRENT_TIMESTAMP',
    },
}

_TELEMETRY = {
    'telemetry_tokens': {
        'id':'TEXT PRIMARY KEY', 'instance_id':'TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE',
        'name':'TEXT NOT NULL', 'token_hash':'TEXT NOT NULL UNIQUE',
        'created_at':'TEXT DEFAULT CURRENT_TIMESTAMP', 'revoked_at':'TEXT',
    },
    'price_book': {'model':'TEXT PRIMARY KEY','value_json':'TEXT NOT NULL'},
    'execution_cursors': {'key':'TEXT PRIMARY KEY','value_json':'TEXT NOT NULL'},
}
_TELEMETRY_COLUMNS = {
    'otel_spans': {'cost_exact':'TEXT','pricing_json':'TEXT','node_name':"TEXT NOT NULL DEFAULT ''"},
    'errors': {'status':"TEXT NOT NULL DEFAULT 'open'",'resolved_at':'TEXT','error_key':'TEXT'},
}

# The final boolean denotes uniqueness. Identifiers here are static, trusted SQL.
_INDEXES = {
    "idx_errors_occurred": ("errors", "occurred_at DESC", False),
    "idx_errors_instance": ("errors", "instance_id, occurred_at DESC", False),
    "idx_otel_spans_trace": ("otel_spans", "trace_id", False),
    "idx_otel_spans_instance": ("otel_spans", "instance_id, start_ns DESC", False),
    "idx_otel_spans_node": ("otel_spans", "node_id, start_ns DESC", False),
    "idx_auth_sessions_user": ("auth_sessions", "username", False),
    "idx_otel_spans_unique": ("otel_spans", "instance_id, trace_id, span_id", True),
}


async def _settle(operation: Awaitable[T]) -> T:
    """Finish SQLite cleanup before propagating even repeated cancellation."""
    task = asyncio.ensure_future(operation)
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancellation = exc
    if cancellation is not None:
        raise cancellation
    return result


async def _execute(connection: aiosqlite.Connection, sql: str, params: Params = ()) -> int:
    async with connection.execute(sql, params) as cursor:
        return cursor.lastrowid or 0


async def _fetchone(connection: aiosqlite.Connection, sql: str, params: Params = ()) -> Row | None:
    async with connection.execute(sql, params) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row is not None else None


async def _fetchall(connection: aiosqlite.Connection, sql: str, params: Params = ()) -> list[Row]:
    async with connection.execute(sql, params) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def _create_tables(connection: aiosqlite.Connection, tables: dict[str, dict[str, str]]) -> None:
    for table, columns in tables.items():
        declarations = [f'"{name}" {definition}' for name, definition in columns.items()]
        if table == "recovery_codes":
            declarations.append("PRIMARY KEY (username, code_hash)")
        await _execute(connection, f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(declarations)})')


async def _validate_tables(connection: aiosqlite.Connection, tables: dict[str, dict[str, str]]) -> None:
    for table, columns in tables.items():
        actual = {row["name"]: row for row in await _fetchall(connection, f'PRAGMA table_info("{table}")')}
        for name, definition in columns.items():
            column = actual.get(name)
            primary_key = int("PRIMARY KEY" in definition)
            if table == "recovery_codes":
                primary_key = ("username", "code_hash").index(name) + 1
            if (
                column is None
                or column["type"].upper() != definition.split()[0]
                or column["pk"] != primary_key
                or ("NOT NULL" in definition and not column["notnull"])
            ):
                raise RuntimeError(f"Incompatible database schema: {table}.{name}")


async def _validate_indexes(connection: aiosqlite.Connection) -> None:
    for name, (table, columns, unique) in _INDEXES.items():
        listed = await _fetchall(connection, f'PRAGMA index_list("{table}")')
        index = next((row for row in listed if row["name"] == name), None)
        actual = await _fetchall(connection, f'PRAGMA index_info("{name}")')
        expected = [column.strip().split()[0] for column in columns.split(",")]
        if index is None or bool(index["unique"]) != unique or index["partial"] or [row["name"] for row in actual] != expected:
            raise RuntimeError(f"Incompatible database schema: index {name}")


async def _initialize(connection: aiosqlite.Connection) -> None:
    version = await _fetchone(connection, "PRAGMA user_version")
    assert version is not None
    current = version["user_version"]
    if current > SCHEMA_VERSION or current < 0:
        raise RuntimeError("Database schema is newer than or unsupported by this application")
    await _execute(connection, "PRAGMA busy_timeout=5000")
    await _execute(connection, "PRAGMA foreign_keys=ON")
    await _execute(connection, "PRAGMA journal_mode=WAL")
    await _execute(connection, "BEGIN IMMEDIATE")
    try:
        if current == 0:
            await _create_tables(connection, _BASELINE)
            await _validate_tables(connection, _BASELINE)
            await _execute(connection, "PRAGMA user_version=1")
            current = 1
        await _validate_tables(connection, _BASELINE)
        if current == 1:
            await _create_tables(connection, _FOUNDATION)
            session_columns = await _fetchall(connection, "PRAGMA table_info(auth_sessions)")
            if not any(column["name"] == "csrf_secret" for column in session_columns):
                await _execute(connection, "ALTER TABLE auth_sessions ADD COLUMN csrf_secret TEXT NOT NULL DEFAULT ''")
            # The old unique index prevented different instances sharing trace IDs.
            await _execute(connection, "DROP INDEX IF EXISTS idx_otel_spans_unique")
            for name, (table, columns, unique) in _INDEXES.items():
                await _execute(connection, f'CREATE {"UNIQUE " if unique else ""}INDEX IF NOT EXISTS "{name}" ON "{table}" ({columns})')
            await _execute(connection, "PRAGMA user_version=2")
            current = 2
        if current == 2:
            await _create_tables(connection, _PROMOTION)
            await _execute(connection, "PRAGMA user_version=3")
            current = 3
        if current == 3:
            await _create_tables(connection, _TELEMETRY)
            for table,columns in _TELEMETRY_COLUMNS.items():
                for name,definition in columns.items():
                    await _execute(connection,f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
            await _execute(connection,'CREATE UNIQUE INDEX idx_errors_key ON errors(error_key)')
            await _execute(connection,'CREATE INDEX idx_spans_received ON otel_spans(received_at)')
            await _execute(connection,'CREATE INDEX idx_spans_execution ON otel_spans(instance_id,execution_id)')
            await _execute(connection,'PRAGMA user_version=4')
            current = 4
        if current == 4:
            await _create_tables(connection, _ASSISTANT)
            await _execute(connection, 'CREATE INDEX idx_approvals_actor ON approval_actions(actor,session_id,status)')
            await _execute(connection, 'PRAGMA user_version=5')
            current = 5
        if current == 5:
            await _create_tables(connection, _BACKUPS)
            await _execute(connection, 'CREATE INDEX idx_backup_due ON backup_schedules(enabled,next_run_at)')
            await _execute(connection, 'CREATE INDEX idx_backup_runs_time ON backup_runs(created_at,id)')
            await _execute(connection, 'PRAGMA user_version=6')
            current = 6
        if current == 6:
            await _create_tables(connection, _SKILLS)
            await _execute(connection, 'PRAGMA user_version=7')
        await _validate_tables(connection, _SKILLS)
        await _validate_tables(connection, _BACKUPS)
        await _validate_tables(connection, _ASSISTANT)
        await _validate_tables(connection,_TELEMETRY)
        await _validate_tables(connection,_TELEMETRY_COLUMNS)
        await _validate_tables(connection, _PROMOTION)
        await _validate_tables(connection, _FOUNDATION)
        await _validate_tables(connection, {"auth_sessions": {"csrf_secret": "TEXT NOT NULL DEFAULT ''"}})
        await _validate_indexes(connection)
        users_schema = await _fetchone(connection, "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'")
        role_constraint = "CHECK(roleIN('viewer','operator','admin'))"
        if users_schema is None or role_constraint not in "".join(users_schema["sql"].split()):
            raise RuntimeError("Incompatible database schema: users role constraint")
        foreign_keys = await _fetchall(connection, "PRAGMA foreign_key_list(recovery_codes)")
        if not any(row["table"] == "users" and row["from"] == "username" and row["to"] == "username" and row["on_delete"] == "CASCADE" for row in foreign_keys):
            raise RuntimeError("Incompatible database schema: recovery_codes foreign key")
        if await _fetchall(connection, "PRAGMA foreign_key_check"):
            raise RuntimeError("Incompatible database schema: foreign key violations")
        await connection.commit()
    except BaseException:
        await connection.rollback()
        raise


class Transaction:
    """An active transaction, usable only by the task owning its context."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection
        self._owner = asyncio.current_task()
        self._active = True

    def _check_active(self) -> None:
        if not self._active or self._owner is not asyncio.current_task():
            raise RuntimeError("Transaction is inactive or belongs to another task")

    async def execute(self, sql: str, params: Params = ()) -> int:
        self._check_active()
        return await _execute(self._connection, sql, params)

    async def fetchone(self, sql: str, params: Params = ()) -> Row | None:
        self._check_active()
        return await _fetchone(self._connection, sql, params)

    async def fetchall(self, sql: str, params: Params = ()) -> list[Row]:
        self._check_active()
        return await _fetchall(self._connection, sql, params)


class Database:
    """Create without I/O, open during lifespan, and close during shutdown."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None

    def _check_not_owner(self) -> None:
        if self._owner is not None and self._owner is asyncio.current_task():
            raise RuntimeError("Use the active Transaction methods; nested database operations are unsupported")

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database is not open")
        return self._connection

    def _harden_storage(self) -> None:
        """Create privately and chmod opened regular files, never link targets."""
        # Windows requires write access for fchmod; POSIX can harden a read handle.
        access = os.O_RDWR if os.name == "nt" else os.O_RDONLY
        flags = access | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            descriptor: int | None = None
            try:
                try:
                    before = path.lstat()
                except FileNotFoundError:
                    if path != self.path:
                        continue
                    try:
                        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                    except FileExistsError:
                        before = path.lstat()  # Another creator won; do not truncate it.
                    else:
                        before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode):
                    raise RuntimeError("Database storage must use regular files, not symbolic links")
                if descriptor is None:
                    descriptor = os.open(path, flags)
                opened = os.fstat(descriptor)
                if not os.path.samestat(before, opened) or not os.path.samestat(opened, path.lstat()):
                    raise RuntimeError("Database storage changed while being opened")
                try:
                    if hasattr(os, "fchmod"):
                        os.fchmod(descriptor, 0o600)
                    else:
                        os.chmod(path, 0o600, follow_symlinks=False)
                except (OSError, NotImplementedError):
                    if os.name != "nt":
                        raise
                    # Older Windows versions need ACLs rather than POSIX modes.
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    async def open(self) -> None:
        self._check_not_owner()
        async with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._harden_storage()
            connection = aiosqlite.connect(self.path, isolation_level=None)
            try:
                await _settle(connection)
                connection.row_factory = aiosqlite.Row
                await _settle(_initialize(connection))
                self._harden_storage()
            except BaseException:
                await _settle(connection.close())
                raise
            self._connection = connection

    async def close(self) -> None:
        self._check_not_owner()
        async with self._lock:
            if self._connection is not None:
                connection, self._connection = self._connection, None
                await _settle(connection.close())

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        self._check_not_owner()
        async with self._lock:
            connection = self._require_connection()
            tx = Transaction(connection)
            self._owner = asyncio.current_task()
            try:
                await _execute(connection, "BEGIN IMMEDIATE")
                yield tx
                # A commit already submitted to SQLite must finish before reuse.
                await _settle(connection.commit())
            except BaseException:
                try:
                    await _settle(connection.rollback())
                except asyncio.CancelledError:
                    # _settle propagates cancellation only after cleanup finishes.
                    raise
                except BaseException:
                    # A failed rollback leaves the connection unsafe for reuse.
                    self._connection = None
                    await _settle(connection.close())
                    raise
                raise
            finally:
                tx._active = False
                self._owner = None

    async def execute(self, sql: str, params: Params = ()) -> int:
        async with self.transaction() as tx:
            return await tx.execute(sql, params)

    async def fetchone(self, sql: str, params: Params = ()) -> Row | None:
        self._check_not_owner()
        async with self._lock:
            return await _settle(_fetchone(self._require_connection(), sql, params))

    async def fetchall(self, sql: str, params: Params = ()) -> list[Row]:
        self._check_not_owner()
        async with self._lock:
            return await _settle(_fetchall(self._require_connection(), sql, params))

    async def backup(self, path: Path | str) -> None:
        """Take a consistent online snapshot into a new, exclusively created file."""
        self._check_not_owner()
        destination = Path(path)
        async with self._lock:
            connection = self._require_connection()
            if destination.resolve() == self.path.resolve():
                raise ValueError("Backup destination must differ from the live database")
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            target = aiosqlite.connect(destination, isolation_level=None)
            try:
                await _settle(target)
                await _settle(connection.backup(target))
            except BaseException:
                try:
                    await _settle(target.close())
                finally:
                    destination.unlink(missing_ok=True)
                raise
            else:
                await _settle(target.close())
