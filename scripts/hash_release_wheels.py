"""Generate a release lock from the exact downloaded wheels and freeze manifest.

Usage: python scripts/hash_release_wheels.py FREEZE_FILE WHEEL_DIRECTORY OUTPUT
Download in the supported Linux/Python image first; never mix platform wheels.
"""
from email.parser import BytesParser
import hashlib
from pathlib import Path
import re
import sys
import zipfile


def normalize(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def main() -> None:
    manifest, directory, output = map(Path, sys.argv[1:])
    expected = {}
    for line in manifest.read_text(encoding='utf-8-sig').splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        name, version = line.strip().split('==')
        expected[normalize(name)] = version
    found = {}
    for wheel in directory.glob('*.whl'):
        with zipfile.ZipFile(wheel) as archive:
            metadata_paths = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
            assert len(metadata_paths) == 1, wheel.name
            metadata = BytesParser().parsebytes(archive.read(metadata_paths[0]))
        name, version = normalize(metadata['Name']), metadata['Version']
        assert name not in found and expected.get(name) == version, wheel.name
        found[name] = (version, hashlib.sha256(wheel.read_bytes()).hexdigest())
    assert found.keys() == expected.keys(), 'Downloaded wheels do not match the freeze manifest'
    lines = ['# Release target: CPython 3.13, Linux amd64.',
             '# Exact wheels hashed from the verified container dependency set.',
             '# Install with --require-hashes --only-binary=:all:.']
    lines.extend(f'{name}=={version} --hash=sha256:{digest}' for name,(version,digest) in sorted(found.items()))
    output.write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(f'Locked {len(found)} wheel artifacts.')


if __name__ == '__main__':
    main()
