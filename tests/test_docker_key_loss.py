import json
import sqlite3

import pytest

from backend.config import Vault, VaultError


@pytest.mark.parametrize('table,column,value', [
    ('documents', 'value_json', 'envelope'),
    ('approval_actions', 'state_enc', 'raw'),
    ('promotion_runs', 'state_enc', 'raw'),
])
def test_existing_journal_ciphertext_prevents_generating_replacement_key(tmp_path, table, column, value):
    vault = Vault(tmp_path)
    encrypted = vault.encrypt('private journal')
    payload = json.dumps({'encrypted': encrypted}) if value == 'envelope' else encrypted
    with sqlite3.connect(tmp_path / 'dashboard.db') as db:
        db.execute(f'CREATE TABLE {table} ({column} TEXT)')
        db.execute(f'INSERT INTO {table} VALUES (?)', (payload,))
    (tmp_path / '.secret_key').unlink()
    (tmp_path / 'secrets.json').unlink(missing_ok=True)
    with pytest.raises(VaultError):
        Vault(tmp_path)
    assert not (tmp_path / '.secret_key').exists()
