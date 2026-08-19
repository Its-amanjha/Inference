"""Versioned, authenticated backup bytes; no filesystem extraction occurs here."""
from __future__ import annotations

import base64
import io
import json
import os
import re
import stat
import struct
import zipfile

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from backend.modules.notes.storage import MAX_NOTE_BYTES, NoteError, validate_path

MAGIC = b'INFERENCE-BACKUP\x00\x01'
# Archives exported before the rename; accepted for restore, never written.
LEGACY_MAGIC = b'VECTISOPS-BACKUP\x00\x01'
SALT_BYTES = 16
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILES = 1100
MAX_ZIP_BYTES = MAX_TOTAL_BYTES + 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = len(MAGIC) + SALT_BYTES + 4 * ((MAX_ZIP_BYTES + 128 + 2) // 3)
_REQUIRED = {'dashboard.db', '.secret_key', 'secrets.json'}
_ROOT_FILES = _REQUIRED | {'config.json', 'users.json'}
_SECRET_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')


class ArchiveError(ValueError):
    """A safe error suitable for display without revealing archive contents."""


def _cipher(passphrase: str, salt: bytes) -> Fernet:
    try:
        password = passphrase.encode('utf-8')
    except (AttributeError, UnicodeError):
        raise ArchiveError('Invalid backup passphrase.') from None
    if not 1 <= len(password) <= 1024:
        raise ArchiveError('Backup passphrase must contain 1 to 1024 UTF-8 bytes.')
    key = Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(password)
    return Fernet(base64.urlsafe_b64encode(key))


def _path(name: str) -> None:
    if name in _ROOT_FILES:
        return
    if isinstance(name, str) and name.startswith('notes/'):
        try:
            validate_path(name[6:])
            return
        except (NoteError, UnicodeError):
            pass
    raise ArchiveError('Backup contains an unsupported or unsafe path.')


def _validate(files: dict[str, bytes]) -> None:
    if not isinstance(files, dict) or not _REQUIRED.issubset(files):
        raise ArchiveError('Backup requires the database, vault key and encrypted secrets.')
    if len(files) > MAX_FILES:
        raise ArchiveError('Backup file count exceeds the limit.')
    seen: set[str] = set()
    total = 0
    for name, data in files.items():
        _path(name)
        folded = name.casefold()
        if folded in seen:
            raise ArchiveError('Backup contains colliding paths.')
        seen.add(folded)
        if not isinstance(data, bytes):
            raise ArchiveError('Backup files must contain bytes.')
        total += len(data)
        if total > MAX_TOTAL_BYTES or (name.startswith('notes/') and len(data) > MAX_NOTE_BYTES):
            raise ArchiveError('Backup content exceeds the size limit.')
        if name.startswith('notes/'):
            try:
                data.decode('utf-8')
            except UnicodeError:
                raise ArchiveError('Backup note is not valid UTF-8.') from None
    # Reject file/directory collisions even on case-insensitive restore filesystems.
    for name in seen:
        parts = name.split('/')
        if any('/'.join(parts[:i]) in seen for i in range(1, len(parts))):
            raise ArchiveError('Backup contains colliding paths.')
    try:
        vault = Fernet(files['.secret_key'])
        envelope = json.loads(files['secrets.json'])
        if not isinstance(envelope, dict) or type(envelope.get('version')) is not int or envelope['version'] != 1:
            raise ValueError
        values = json.loads(vault.decrypt(envelope['ciphertext'].encode('ascii')))
        if not isinstance(values, dict) or any(not _SECRET_NAME.fullmatch(name) for name in values):
            raise ValueError
        for name in ('config.json', 'users.json'):
            if name in files:
                json.loads(files[name])
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, InvalidToken, RecursionError):
        raise ArchiveError('Backup vault or configuration is invalid or cannot be authenticated.') from None


def encrypt_archive(files: dict[str, bytes], passphrase: str) -> bytes:
    """Validate a snapshot and encrypt its stored ZIP using a fresh random salt."""
    _validate(files)
    salt = os.urandom(SALT_BYTES)
    cipher = _cipher(passphrase, salt)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, data)
    plaintext = output.getvalue()
    if len(plaintext) > MAX_ZIP_BYTES:
        raise ArchiveError('Backup content exceeds the size limit.')
    return MAGIC + salt + cipher.encrypt(plaintext)


def decrypt_archive(data: bytes, passphrase: str) -> dict[str, bytes]:
    """Authenticate before parsing; reject unsafe entries without extracting them."""
    if not isinstance(data, bytes) or len(data) > MAX_ARCHIVE_BYTES:
        raise ArchiveError('Backup exceeds the size limit or is invalid.')
    magic = next((value for value in (MAGIC, LEGACY_MAGIC) if data.startswith(value)), None)
    if magic is None or len(data) <= len(magic) + SALT_BYTES:
        raise ArchiveError('Unsupported or invalid backup format.')
    offset = len(magic)
    cipher = _cipher(passphrase, data[offset:offset + SALT_BYTES])
    try:
        plaintext = cipher.decrypt(data[offset + SALT_BYTES:])
    except InvalidToken:
        raise ArchiveError('Backup authentication failed; verify the passphrase and archive.') from None
    if len(plaintext) > MAX_ZIP_BYTES:
        raise ArchiveError('Backup content exceeds the size limit.')
    files: dict[str, bytes] = {}
    try:
        # Bound the central directory before ZipFile allocates ZipInfo objects.
        # This format never emits ZIP64, comments, split archives or prepended data.
        if len(plaintext) < 22 or plaintext[-22:-18] != b'PK\x05\x06':
            raise ArchiveError('Backup archive is malformed or unsupported.')
        disk, directory_disk, disk_count, count, directory_size, directory_start, comment = struct.unpack(
            '<4H2IH', plaintext[-18:])
        if (disk or directory_disk or comment or disk_count != count or
                count > MAX_FILES or directory_size > 2 * 1024 * 1024 or
                directory_start + directory_size != len(plaintext) - 22):
            raise ArchiveError('Backup archive exceeds size or file count limits or has an unsupported directory.')
        with zipfile.ZipFile(io.BytesIO(plaintext)) as archive:
            entries = archive.infolist()
            if len(entries) != count or len(entries) > MAX_FILES or sum(info.file_size for info in entries) > MAX_TOTAL_BYTES:
                raise ArchiveError('Backup content exceeds the size or file count limit.')
            seen: set[str] = set()
            for info in entries:
                _path(info.filename)
                if info.filename != info.orig_filename or info.filename.casefold() in seen:
                    raise ArchiveError('Backup contains duplicate or colliding paths.')
                seen.add(info.filename.casefold())
                mode = info.external_attr >> 16
                if (info.is_dir() or info.external_attr & 0x10 or
                        stat.S_IFMT(mode) not in (0, stat.S_IFREG) or
                        info.compress_type != zipfile.ZIP_STORED or info.flag_bits & 1 or
                        info.compress_size != info.file_size):
                    raise ArchiveError('Backup contains an unsupported filesystem entry.')
                if info.filename.startswith('notes/') and info.file_size > MAX_NOTE_BYTES:
                    raise ArchiveError('Backup note exceeds the size limit.')
                files[info.filename] = archive.read(info)
        _validate(files)
        return files
    except ArchiveError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError, RuntimeError, NotImplementedError, EOFError):
        raise ArchiveError('Backup archive is malformed or unsupported.') from None
