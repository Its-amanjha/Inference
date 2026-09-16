import io
import json
import stat
import struct
import zipfile
from contextlib import nullcontext

from cryptography.fernet import Fernet
import pytest

from backend.modules.backups import archive as module
from backend.modules.backups.archive import ArchiveError, decrypt_archive, encrypt_archive


@pytest.fixture
def files():
    key = Fernet.generate_key()
    return {
        'dashboard.db': b'database snapshot',
        '.secret_key': key,
        'secrets.json': json.dumps({'version': 1, 'ciphertext': Fernet(key).encrypt(b'{"TOKEN":"private"}').decode()}).encode(),
        'notes/folder/hello.md': '# Hello \u2603 [[other]]'.encode(),
        'config.json': b'{"theme":"dark"}',
    }


def packaged(entries, compression=zipfile.ZIP_STORED):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=compression) as archive:
        for name, content in entries:
            if isinstance(name, str):
                raw_name = name
                name = zipfile.ZipInfo(raw_name)
                # Preserve malicious wire names; ZipInfo construction on Windows
                # normalizes backslashes and truncates NULs before writing.
                name.filename = raw_name
                name.orig_filename = raw_name
                name.compress_type = compression
            archive.writestr(name, content)
    salt = b'x' * module.SALT_BYTES
    return module.MAGIC + salt + module._cipher('passphrase', salt).encrypt(stream.getvalue())


def test_roundtrip_randomized_and_confidential(files):
    first = encrypt_archive(files, 'a separate export passphrase')
    second = encrypt_archive(files, 'a separate export passphrase')
    assert first != second
    assert b'database snapshot' not in first
    assert files['.secret_key'] not in first
    assert decrypt_archive(first, 'a separate export passphrase') == files


def test_archive_requires_the_inference_marker(files):
    current = encrypt_archive(files, 'passphrase')
    assert current.startswith(b'INFERENCE-BACKUP')
    assert decrypt_archive(current, 'passphrase') == files
    with pytest.raises(ArchiveError, match='Unsupported'):
        decrypt_archive(b'UNKNOWN--BACKUP\x00\x01' + current[len(module.MAGIC):], 'passphrase')


def test_wrong_passphrase_and_tamper(files):
    encrypted = encrypt_archive(files, 'passphrase')
    with pytest.raises(ArchiveError, match='authentication failed'):
        decrypt_archive(encrypted, 'wrong')
    changed = bytearray(encrypted)
    changed[-10] ^= 1
    with pytest.raises(ArchiveError, match='authentication failed'):
        decrypt_archive(bytes(changed), 'passphrase')


@pytest.mark.parametrize('path', ['../evil', '/etc/passwd', 'notes/../evil.md', 'notes/a\\b.md', 'notes/CON.md', 'dashboard.db-wal', 'notes/a.md\x00suffix'])
def test_untrusted_archive_rejects_paths(files, path):
    data = packaged([*files.items(), (path, b'bad')])
    with pytest.raises(ArchiveError):
        decrypt_archive(data, 'passphrase')


@pytest.mark.parametrize('path', ['notes/folder/hello.md', 'notes/FOLDER/HELLO.md'])
def test_duplicates_and_casefold_collisions(files, path):
    with pytest.warns(UserWarning) if path == 'notes/folder/hello.md' else nullcontext():
        data = packaged([*files.items(), (path, b'bad')])
    with pytest.raises(ArchiveError, match='collid|duplicate'):
        decrypt_archive(data, 'passphrase')


def test_file_directory_collision(files):
    files['notes/a.md'] = b'parent file'
    files['notes/a.md/b.md'] = b'child file'
    with pytest.raises(ArchiveError, match='colliding'):
        encrypt_archive(files, 'passphrase')


@pytest.mark.parametrize('mode', [stat.S_IFLNK, stat.S_IFDIR, stat.S_IFIFO])
def test_special_zip_entries(files, mode):
    entry = zipfile.ZipInfo('notes/special.md')
    entry.create_system = 3
    entry.external_attr = (mode | 0o600) << 16
    data = packaged([*files.items(), (entry, b'target')])
    with pytest.raises(ArchiveError, match='filesystem entry'):
        decrypt_archive(data, 'passphrase')


def test_compression_rejected(files):
    data = packaged(files.items(), zipfile.ZIP_DEFLATED)
    with pytest.raises(ArchiveError, match='filesystem entry'):
        decrypt_archive(data, 'passphrase')


@pytest.mark.parametrize('missing', ['dashboard.db', '.secret_key', 'secrets.json'])
def test_missing_required_file(files, missing):
    del files[missing]
    with pytest.raises(ArchiveError, match='requires'):
        encrypt_archive(files, 'passphrase')
    with pytest.raises(ArchiveError, match='requires'):
        decrypt_archive(packaged(files.items()), 'passphrase')


@pytest.mark.parametrize('replacement', [b'bad key', Fernet.generate_key()])
def test_vault_key_authentication(files, replacement):
    files['.secret_key'] = replacement
    with pytest.raises(ArchiveError, match='vault'):
        encrypt_archive(files, 'passphrase')
    with pytest.raises(ArchiveError, match='vault'):
        decrypt_archive(packaged(files.items()), 'passphrase')


def test_limits_before_archive_read(files, monkeypatch):
    encrypted = packaged(files.items())
    monkeypatch.setattr(module, 'MAX_FILES', 2)
    with pytest.raises(ArchiveError, match='count'):
        encrypt_archive(files, 'passphrase')
    with pytest.raises(ArchiveError, match='count'):
        decrypt_archive(encrypted, 'passphrase')


def test_content_limit(files, monkeypatch):
    encrypted = packaged(files.items())
    monkeypatch.setattr(module, 'MAX_TOTAL_BYTES', 8)
    with pytest.raises(ArchiveError, match='size'):
        encrypt_archive(files, 'passphrase')
    with pytest.raises(ArchiveError, match='size'):
        decrypt_archive(encrypted, 'passphrase')


def test_note_size_and_utf8(files, monkeypatch):
    monkeypatch.setattr(module, 'MAX_NOTE_BYTES', 2)
    with pytest.raises(ArchiveError, match='size'):
        encrypt_archive(files, 'passphrase')
    files['notes/folder/hello.md'] = b'\xff'
    with pytest.raises(ArchiveError, match='UTF-8'):
        encrypt_archive(files, 'passphrase')


def test_malformed_zip_and_header():
    with pytest.raises(ArchiveError, match='format'):
        decrypt_archive(b'not a backup', 'passphrase')
    salt = b'x' * module.SALT_BYTES
    data = module.MAGIC + salt + module._cipher('passphrase', salt).encrypt(b'not a zip')
    with pytest.raises(ArchiveError, match='malformed'):
        decrypt_archive(data, 'passphrase')


def test_central_directory_is_bounded_before_zip_parsing(monkeypatch):
    raw = b'PK\x05\x06' + struct.pack('<4H2IH', 0, 0, 1101, 1101, 0, 0, 0)
    salt = b'x' * module.SALT_BYTES
    data = module.MAGIC + salt + module._cipher('passphrase', salt).encrypt(raw)
    def must_not_parse(*args, **kwargs):
        pytest.fail('Unbounded central directory reached ZipFile')
    monkeypatch.setattr(module.zipfile, 'ZipFile', must_not_parse)
    with pytest.raises(ArchiveError, match='count'):
        decrypt_archive(data, 'passphrase')


@pytest.mark.parametrize('passphrase', ['', '\ud800', 'a' * 1025, None])
def test_bad_passphrase(files, passphrase):
    with pytest.raises(ArchiveError, match='passphrase'):
        encrypt_archive(files, passphrase)
