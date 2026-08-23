import pytest
from fastapi.testclient import TestClient


def test_vault_resolution_and_key_loss(tmp_path, monkeypatch):
    from backend.config import Vault
    vault = Vault(tmp_path)
    vault.set('DB', {'password': 'vault-value'})
    assert vault.resolve('$DB.password') == 'vault-value'
    monkeypatch.setenv('DB', '{"password":"env-value"}')
    assert vault.resolve('$DB.password') == 'env-value'
    assert 'vault-value' not in (tmp_path / 'secrets.json').read_text()
    (tmp_path / '.secret_key').unlink()
    with pytest.raises(RuntimeError):
        Vault(tmp_path)


def test_auth_csrf_and_restart(tmp_path):
    from backend.main import create_app
    with TestClient(create_app(data_dir=tmp_path), base_url='https://localhost', client=('127.0.0.1', 50000)) as c:
        assert c.get('/api/admin/users').status_code == 401
        setup = c.post('/api/auth/setup', json={'username':'owner','password':'long enough password 2026'})
        assert setup.status_code == 200, setup.text
        assert c.get('/api/auth/status').json()['user']['role'] == 'admin'
        assert c.post('/api/admin/users',json={'username':'viewer','password':'long enough password 2026','role':'viewer'}).status_code == 403
        headers = {'X-AGD-CSRF': c.cookies['agd_csrf']}
        assert c.post('/api/admin/users',headers=headers,json={'username':'viewer','password':'long enough password 2026','role':'viewer'}).status_code == 200
        assert c.post('/api/auth/logout',headers=headers).status_code == 200
        assert c.post('/api/auth/login',json={'username':'viewer','password':'long enough password 2026'}).status_code == 200
        assert c.get('/api/admin/users').status_code == 403
    with TestClient(create_app(data_dir=tmp_path), base_url='https://localhost', client=('127.0.0.1', 50000)) as c:
        assert c.post('/api/auth/setup',json={'username':'x','password':'long enough password 2026'}).status_code == 409


@pytest.mark.asyncio
async def test_transactions_rollback_and_migrations(tmp_path):
    from backend.database import Database
    db = Database(tmp_path / 'db.sqlite')
    await db.open()
    with pytest.raises(ValueError):
        async with db.transaction() as tx:
            await tx.execute('INSERT INTO documents(key,value_json) VALUES (?,?)', ('test','{}'))
            raise ValueError('rollback')
    assert await db.fetchone('SELECT * FROM documents WHERE key=?', ('test',)) is None
    await db.close()
    await db.open()
    assert (await db.fetchone('PRAGMA user_version'))['user_version'] >= 1
    await db.close()


def test_egress_blocks_metadata_even_when_allowlisted():
    from backend.net import allowed_ip
    assert not allowed_ip('169.254.169.254', ('0.0.0.0/0',))
    assert not allowed_ip('::ffff:169.254.169.254', ('::/0',))
    assert not allowed_ip('127.0.0.1', ())
    assert allowed_ip('127.0.0.1', ('127.0.0.1/32',))
