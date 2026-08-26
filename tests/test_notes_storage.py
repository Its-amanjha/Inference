import hashlib
import os

import pytest

from backend.modules.notes.storage import NoteError, NotesStore


def test_roundtrip_and_stale(tmp_path):
    root = tmp_path / 'vault'
    store = NotesStore(root)
    assert store.list() == []
    assert not root.exists()
    first = store.write('folder/hello.md', '# Héllo', None)
    assert first == {'path': 'folder/hello.md', 'content': '# Héllo', 'revision': hashlib.sha256('# Héllo'.encode()).hexdigest()}
    assert store.read(first['path']) == first
    assert store.list() == [{'path': first['path'], 'revision': first['revision']}]
    for revision in (None, 'stale'):
        with pytest.raises(NoteError) as error:
            store.write(first['path'], 'bad', revision)
        assert error.value.status == 409
    second = store.write(first['path'], 'new', first['revision'])
    with pytest.raises(NoteError):
        store.delete(first['path'], first['revision'])
    store.delete(first['path'], second['revision'])
    assert store.list() == []


@pytest.mark.parametrize('path', ['../x.md', '/x.md', 'C:/x.md', 'a\\x.md', 'a//x.md', './x.md', 'a/../x.md', '%2e%2e/x.md', 'x.md:stream', 'CON.md', 'a/NUL.md', 'COM1.md', 'a. /x.md', 'a./x.md', 'x.txt', '', 'x.md\x00'])
def test_invalid_paths(tmp_path, path):
    with pytest.raises(NoteError) as error:
        NotesStore(tmp_path).write(path, 'x', None)
    assert error.value.status == 400


def test_hardlinks_rejected(tmp_path):
    source = tmp_path / 'outside'
    source.write_text('private')
    root = tmp_path / 'vault'
    root.mkdir()
    os.link(source, root / 'note.md')
    store = NotesStore(root)
    for action in (lambda: store.read('note.md'), store.list, lambda: store.write('note.md', 'changed', None), lambda: store.delete('note.md', 'old')):
        with pytest.raises(NoteError):
            action()
    assert source.read_text() == 'private'


@pytest.mark.parametrize('placement', ['root', 'parent', 'file'])
def test_symlinks_rejected(tmp_path, placement):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'note.md').write_text('private')
    root = tmp_path / 'vault'
    try:
        if placement == 'root':
            root.symlink_to(outside, target_is_directory=True)
            path = 'note.md'
        else:
            root.mkdir()
            if placement == 'parent':
                (root / 'folder').symlink_to(outside, target_is_directory=True)
                path = 'folder/note.md'
            else:
                (root / 'note.md').symlink_to(outside / 'note.md')
                path = 'note.md'
    except OSError:
        pytest.skip('Creating symlinks requires OS permission')
    store = NotesStore(root)
    for action in (lambda: store.read(path), store.list, lambda: store.write(path, 'changed', None)):
        with pytest.raises(NoteError):
            action()


def test_atomic_failure_cleans_tempfile(tmp_path, monkeypatch):
    store = NotesStore(tmp_path)
    old = store.write('note.md', 'old', None)
    def fail(*args, **kwargs):
        raise OSError('sensitive absolute path')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(NoteError) as error:
        store.write('note.md', 'new', old['revision'])
    assert 'sensitive' not in str(error.value)
    assert store.read('note.md') == old
    assert [p.name for p in tmp_path.iterdir()] == ['note.md']


def test_size_and_scan_limits(tmp_path, monkeypatch):
    import backend.modules.notes.storage as storage
    store = NotesStore(tmp_path)
    with pytest.raises(NoteError) as error:
        store.write('big.md', 'é' * (256 * 1024), None)
    assert error.value.status == 413
    store.write('one.md', 'one', None)
    store.write('two.md', 'two', None)
    monkeypatch.setattr(storage, 'MAX_FILES', 1)
    with pytest.raises(NoteError) as error:
        store.list()
    assert error.value.code == 'notes_scan_limit'
    monkeypatch.setattr(storage, 'MAX_FILES', 1000)
    monkeypatch.setattr(storage, 'MAX_SCAN_BYTES', 5)
    with pytest.raises(NoteError):
        store.list()


def test_invalid_utf8_and_external_oversized_note(tmp_path):
    (tmp_path / 'bad.md').write_bytes(b'\xff')
    with pytest.raises(NoteError):
        NotesStore(tmp_path).read('bad.md')
    (tmp_path / 'bad.md').write_bytes(b'x' * (256 * 1024 + 1))
    with pytest.raises(NoteError) as error:
        NotesStore(tmp_path).read('bad.md')
    assert error.value.status == 413


def test_reparse_point_rejected():
    import stat
    from types import SimpleNamespace
    from backend.modules.notes.storage import _safe
    with pytest.raises(NoteError) as error:
        _safe(SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400), True)
    assert error.value.code == 'unsafe_note_path'


def test_fsync_failure_preserves_note(tmp_path, monkeypatch):
    store = NotesStore(tmp_path)
    old = store.write('note.md', 'old', None)
    def fail(fd):
        raise OSError('disk error')
    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(NoteError):
        store.write('note.md', 'new', old['revision'])
    assert store.read('note.md') == old
    assert [p.name for p in tmp_path.iterdir()] == ['note.md']


@pytest.mark.parametrize('path', ['CONIN$.md', 'CONOUT$.md', 'CLOCK$.md'])
def test_additional_windows_devices(tmp_path, path):
    with pytest.raises(NoteError):
        NotesStore(tmp_path).write(path, 'text', None)


def test_create_race_never_overwrites_external_note(tmp_path, monkeypatch):
    original_link = os.link
    def racing_link(source, destination, **kwargs):
        (tmp_path / 'note.md').write_bytes(b'external writer')
        return original_link(source, destination, **kwargs)
    monkeypatch.setattr(os, 'link', racing_link)
    with pytest.raises(NoteError) as error:
        NotesStore(tmp_path).write('note.md', 'our writer', None)
    assert error.value.code == 'note_conflict'
    assert (tmp_path / 'note.md').read_bytes() == b'external writer'
    assert [p.name for p in tmp_path.iterdir()] == ['note.md']


@pytest.mark.skipif(os.name != 'posix', reason='Directory fsync is POSIX-only')
def test_directory_fsync_after_save_and_delete(tmp_path, monkeypatch):
    import stat
    original_fsync = os.fsync
    synced_directories = []
    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced_directories.append(fd)
        return original_fsync(fd)
    monkeypatch.setattr(os, 'fsync', fsync)
    store = NotesStore(tmp_path)
    first = store.write('note.md', 'first', None)
    assert len(synced_directories) == 1
    second = store.write('note.md', 'second', first['revision'])
    assert len(synced_directories) == 2
    store.delete('note.md', second['revision'])
    assert len(synced_directories) == 3
