"""Offline restore: python -m backend.modules.backups.restore ARCHIVE DESTINATION."""
import argparse
import asyncio
import errno
import getpass
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from .archive import ArchiveError, decrypt_archive
from backend.database import Database, SCHEMA_VERSION


async def validate_schema(path: Path) -> None:
    database = Database(path)
    try:
        await database.open()
    finally:
        await database.close()


def safe_destination(destination: Path) -> None:
    for path in (destination, *destination.parents):
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ArchiveError('Restore destination must use ordinary directories.')
    if destination.exists() and any(destination.iterdir()):
        raise ArchiveError('Restore destination must be empty.')


def restore_archive(data: bytes, passphrase: str, destination: Path) -> None:
    """Validate in private staging before installing without overwriting files.

    Run offline. Local administrators changing directories concurrently are outside
    the filesystem trust boundary. An I/O failure during installation can leave a
    partial destination; it is never reported as restored.
    """
    destination = Path(os.path.abspath(destination))
    safe_destination(destination)
    files = decrypt_archive(data, passphrase)
    with tempfile.TemporaryDirectory(prefix='inference-restore-') as temporary:
        staging = Path(temporary)
        for name, content in files.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(content)
        try:
            connection = sqlite3.connect((staging / 'dashboard.db').as_uri() + '?mode=ro', uri=True)
            try:
                connection.execute('PRAGMA trusted_schema=OFF')
                if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ArchiveError('Backup database integrity check failed.')
                version = connection.execute('PRAGMA user_version').fetchone()[0]
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not 5 <= version <= SCHEMA_VERSION or not {'users', 'instances', 'documents', 'auth_sessions'}.issubset(tables):
                    raise ArchiveError('Backup database version or schema is unsupported.')
                if connection.execute('PRAGMA foreign_key_check').fetchall():
                    raise ArchiveError('Backup database contains invalid references.')
            finally:
                connection.close()
        except sqlite3.Error:
            raise ArchiveError('Backup database is invalid.') from None
        try:
            asyncio.run(validate_schema(staging / 'dashboard.db'))
            # Install the validated, migrated snapshot after its WAL is closed.
            files['dashboard.db'] = (staging / 'dashboard.db').read_bytes()
        except (RuntimeError, sqlite3.Error, OSError):
            raise ArchiveError('Backup database schema is invalid or unsupported.') from None
        safe_destination(destination)
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            for name, content in files.items():
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'wb') as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            if os.name == 'posix':
                directories = {destination, destination.parent}
                for name in files:
                    parent = (destination / name).parent
                    while parent != destination:
                        directories.add(parent)
                        parent = parent.parent
                for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
                    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        try:
                            os.fsync(descriptor)
                        except OSError as exc:
                            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                                raise
                    finally:
                        os.close(descriptor)
        except OSError:
            raise ArchiveError('Restore installation failed; destination may be partial. Do not start the application there.') from None


def main() -> None:
    parser = argparse.ArgumentParser(description='Restore an encrypted Inference backup into an empty directory while offline.')
    parser.add_argument('archive', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    try:
        with args.archive.open('rb') as handle:
            data = handle.read(180 * 1024 * 1024 + 1)
        if len(data) > 180 * 1024 * 1024:
            raise ArchiveError('Archive exceeds the supported size limit.')
        restore_archive(data, getpass.getpass('Export passphrase: '), args.destination)
    except (ArchiveError, OSError):
        parser.exit(1, 'Restore failed. Verify the archive, passphrase and empty destination; any partial destination must not be started.\n')
    print('Restore complete. Configure the destination as INFERENCE_DATA_DIR before starting Inference.')


if __name__ == '__main__':
    main()
