"""Bounded markdown storage. POSIX operations pin no-follow directory descriptors.

Windows rejects static symlinks and reparse points; an administrator concurrently
replacing directories remains outside the process's filesystem trust boundary.
New notes install without replacing existing entries. Existing-note revision
checks cannot provide atomic compare-and-swap against external filesystem edits.
File and supported directory fsync improve durability; directory creation and
note edits are not a whole-filesystem transaction.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import errno
import hashlib
import os
from pathlib import Path
import re
import stat
import threading
from typing import Iterator
import uuid

MAX_NOTE_BYTES = 256 * 1024
MAX_FILES = 1000
MAX_SCAN_BYTES = 32 * 1024 * 1024
MAX_SCAN_ENTRIES = 10000
_POSIX = os.name == 'posix'
_DEVICES = re.compile(r'^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|CLOCK\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)', re.I)


class NoteError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def validate_path(path: str) -> str:
    if (not isinstance(path, str) or not path or len(path) > 512
            or not path.endswith('.md') or any(c in path for c in '\\%:<>"|?*')
            or any(ord(c) < 32 or ord(c) == 127 for c in path)):
        raise NoteError(400, 'invalid_note_path', 'Use a relative markdown path.')
    parts = path.split('/')
    if len(parts) > 32 or any(not p or p in ('.', '..') or p.endswith((' ', '.'))
                            or _DEVICES.match(p) or len(p.encode('utf-8')) > 255 for p in parts):
        raise NoteError(400, 'invalid_note_path', 'Use a canonical relative markdown path.')
    return path


def _safe(info: os.stat_result, directory: bool = False) -> None:
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or (directory and not stat.S_ISDIR(info.st_mode))
            or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))):
        raise NoteError(400, 'unsafe_note_path', 'Unsafe note filesystem entry.')


class NotesStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(root))
        self._lock = threading.RLock()

    @contextmanager
    def _guard(self) -> Iterator[None]:
        with self._lock:
            try:
                yield
            except NoteError:
                raise
            except FileNotFoundError:
                raise NoteError(404, 'note_not_found', 'Note not found.') from None
            except (OSError, UnicodeError, ValueError):
                raise NoteError(500, 'notes_storage_error', 'Note storage operation failed.') from None

    @contextmanager
    def _directory(self, parts: list[str], create: bool = False) -> Iterator[int | Path]:
        """Pin every ancestor on POSIX; never resolve a link to its destination."""
        with ExitStack() as stack:
            if _POSIX:
                current = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                stack.callback(os.close, current)
                for part in [*self.root.parts[1:], *parts]:
                    try:
                        info = os.stat(part, dir_fd=current, follow_symlinks=False)
                    except FileNotFoundError:
                        if not create:
                            raise
                        os.mkdir(part, mode=0o700, dir_fd=current)
                        info = os.stat(part, dir_fd=current, follow_symlinks=False)
                    _safe(info, True)
                    current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                    stack.callback(os.close, current)
                yield current
            else:
                current = Path(self.root.anchor)
                _safe(current.lstat(), True)
                for part in [*self.root.parts[1:], *parts]:
                    current = current / part
                    try:
                        info = current.lstat()
                    except FileNotFoundError:
                        if not create:
                            raise
                        current.mkdir(mode=0o700)
                        info = current.lstat()
                    _safe(info, True)
                yield current

    @staticmethod
    def _args(parent: int | Path, name: str) -> tuple[str | Path, dict]:
        return (name, {'dir_fd': parent}) if isinstance(parent, int) else (parent / name, {})

    def _read(self, parent: int | Path, name: str) -> bytes:
        path, kwargs = self._args(parent, name)
        _safe(os.stat(path, follow_symlinks=False, **kwargs))
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                     | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0), **kwargs)
        with os.fdopen(fd, 'rb') as stream:
            _safe(os.fstat(stream.fileno()))
            content = stream.read(MAX_NOTE_BYTES + 1)
        if len(content) > MAX_NOTE_BYTES:
            raise NoteError(413, 'note_too_large', 'Note exceeds 256 KiB.')
        content.decode('utf-8')
        return content

    @staticmethod
    def _result(path: str, content: bytes) -> dict:
        return {'path': path, 'content': content.decode('utf-8'), 'revision': hashlib.sha256(content).hexdigest()}

    def read(self, path: str) -> dict:
        with self._guard():
            validate_path(path)
            parts = path.split('/')
            with self._directory(parts[:-1]) as parent:
                return self._result(path, self._read(parent, parts[-1]))

    def list(self) -> list[dict]:
        with self._guard():
            results = []
            scanned = total = 0
            def scan(parts: list[str]) -> None:
                nonlocal scanned, total
                with self._directory(parts) as parent:
                    with os.scandir(parent) as entries:
                        for entry in entries:
                            scanned += 1
                            if scanned > MAX_SCAN_ENTRIES or len(parts) > 32:
                                raise NoteError(413, 'notes_scan_limit', 'Notes scan limit exceeded.')
                            info = entry.stat(follow_symlinks=False)
                            if stat.S_ISDIR(info.st_mode):
                                _safe(info, True)
                                scan([*parts, entry.name])
                            elif stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                                raise NoteError(400, 'unsafe_note_path', 'Unsafe note filesystem entry.')
                            elif entry.name.endswith('.md'):
                                path = validate_path('/'.join([*parts, entry.name]))
                                content = self._read(parent, entry.name)
                                total += len(content)
                                if len(results) >= MAX_FILES or total > MAX_SCAN_BYTES:
                                    raise NoteError(413, 'notes_scan_limit', 'Notes scan limit exceeded.')
                                results.append({'path': path, 'revision': hashlib.sha256(content).hexdigest()})
            try:
                with self._directory([]):
                    pass
            except FileNotFoundError:
                return []
            scan([])
            return sorted(results, key=lambda row: row['path'])

    def _compare(self, parent: int | Path, name: str, expected_revision: str | None) -> None:
        try:
            current = self._read(parent, name)
        except FileNotFoundError:
            if expected_revision is None:
                return
        else:
            if expected_revision is not None and hashlib.sha256(current).hexdigest() == expected_revision:
                return
        raise NoteError(409, 'note_conflict', 'Note changed; reload before saving.')

    @staticmethod
    def _sync_directory(parent: int | Path) -> None:
        if isinstance(parent, int):
            try:
                os.fsync(parent)
            except OSError as error:
                if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise

    def write(self, path: str, content: str, expected_revision: str | None) -> dict:
        with self._guard():
            validate_path(path)
            encoded = content.encode('utf-8')
            if len(encoded) > MAX_NOTE_BYTES:
                raise NoteError(413, 'note_too_large', 'Note exceeds 256 KiB.')
            parts = path.split('/')
            with self._directory(parts[:-1], create=True) as parent:
                self._compare(parent, parts[-1], expected_revision)
                temporary = '.note-' + uuid.uuid4().hex + '.tmp'
                temp_path, kwargs = self._args(parent, temporary)
                fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600, **kwargs)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                    self._compare(parent, parts[-1], expected_revision)
                    if expected_revision is None:
                        try:
                            if isinstance(parent, int):
                                os.link(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent,
                                        follow_symlinks=False)
                            else:
                                os.link(temp_path, parent / parts[-1], follow_symlinks=False)
                        except FileExistsError:
                            raise NoteError(409, 'note_conflict', 'Note changed; reload before saving.') from None
                    elif isinstance(parent, int):
                        os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                    else:
                        os.replace(temp_path, parent / parts[-1])
                finally:
                    try:
                        os.unlink(temp_path, **kwargs)
                    except FileNotFoundError:
                        pass
                self._sync_directory(parent)
            return self._result(path, encoded)

    def delete(self, path: str, expected_revision: str) -> None:
        with self._guard():
            validate_path(path)
            if not expected_revision:
                raise NoteError(409, 'note_conflict', 'A current revision is required.')
            parts = path.split('/')
            with self._directory(parts[:-1]) as parent:
                self._compare(parent, parts[-1], expected_revision)
                target, kwargs = self._args(parent, parts[-1])
                os.unlink(target, **kwargs)
                self._sync_directory(parent)
