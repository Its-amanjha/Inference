import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from backend.database import Database
from backend.modules.observability import pricebook


def model_row(model='vendor/model-2026', prompt='0.000002', completion='0.000008'):
    return {'id': model, 'pricing': {'prompt': prompt, 'completion': completion,
                                   'input_cache_read': '0.0000005'}}


@pytest.fixture
async def book(tmp_path):
    db = Database(tmp_path / 'book.db')
    await db.open()
    now = datetime.now(timezone.utc)
    await db.execute("INSERT INTO users(username,password_hash,role) VALUES('admin','unused','admin')")
    await db.execute('INSERT INTO auth_sessions(id_hash,username,created_at,expires_at,last_seen) VALUES(?,?,?,?,?)',
                     ('session', 'admin', now.isoformat(), (now+timedelta(days=1)).isoformat(), now.isoformat()))
    state = SimpleNamespace(calls=[], response=None, role='admin', hook=None)
    async def upstream(request):
        state.calls.append(request)
        if state.hook:
            await state.hook()
        if isinstance(state.response, Exception):
            raise state.response
        return state.response or httpx.Response(200, json={'data': [model_row()]})
    app = FastAPI()
    app.state.db = db
    app.include_router(pricebook.router)
    @app.middleware('http')
    async def identity(request, call_next):
        request.state.user = {'username': 'admin', 'role': state.role, 'session_id': 'session'}
        return await call_next(request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream),
                                headers={'Authorization': 'secret-default'},
                                cookies={'private': 'secret-cookie'}, auth=('user', 'password')) as upstream_client:
        app.state.http = upstream_client
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://test') as client:
            state.app, state.db, state.client = app, db, client
            yield state
    await db.close()


async def test_seed_is_offline_idempotent_and_documented(book):
    await pricebook.seed_prices(book.db)
    first = await book.db.fetchall('SELECT * FROM price_book ORDER BY model')
    await pricebook.seed_prices(book.db)
    assert await book.db.fetchall('SELECT * FROM price_book ORDER BY model') == first
    assert len(first) == len(pricebook.BUNDLED)
    assert not book.calls
    # Every bundled price names the page it was checked on and when.
    for row in first:
        entry = json.loads(row['value_json'])['bundled']
        assert entry['source'] == 'bundled' and entry['estimated'] is True
        assert entry['source_url'].startswith('https://developers.openai.com/api/docs/models/')
        assert entry['updated_at'][:10] in ('2026-09-08', '2026-09-27')


async def test_sync_converts_exact_rates_uses_no_credentials_and_persists_opt_in(book):
    response = await book.client.post('/prices/sync')
    assert response.status_code == 200, response.text
    assert response.json()['updated'] == 1
    sent = book.calls[0]
    assert str(sent.url) == 'https://openrouter.ai/api/v1/models'
    assert 'authorization' not in sent.headers and 'cookie' not in sent.headers
    assert sent.headers['accept-encoding'] == 'identity'
    result = (await book.client.get('/prices')).json()
    entry = result['data'][0]
    assert entry['model'] == 'vendor/model-2026'
    assert entry['input_per_million'] == '2'
    assert entry['output_per_million'] == '8'
    assert entry['cached_input_per_million'] == '0.5'
    assert result['sync']['enabled'] is True
    assert result['sync']['last_success']


async def test_override_wins_and_sync_keeps_override(book):
    assert (await book.client.put('/prices/vendor/model-2026', json={
        'input_per_million': '4.125', 'output_per_million': '9'})).status_code == 200
    assert (await book.client.post('/prices/sync')).status_code == 200
    entry = (await book.client.get('/prices')).json()['data'][0]
    assert entry['input_per_million'] == '4.125'
    assert entry['source'] == 'override'
    row = await book.db.fetchone('SELECT value_json FROM price_book')
    assert set(json.loads(row['value_json'])) == {'override', 'live'}


@pytest.mark.parametrize('invalid', ['-1', 'NaN', 'Infinity', '1e999999', '1e-999999', True, '9'*200])
async def test_override_rejects_invalid_or_unbounded_decimals(book, invalid):
    result = await book.client.put('/prices/x', json={'input_per_million': invalid, 'output_per_million': '1'})
    assert result.status_code == 422
    assert not await book.db.fetchall('SELECT * FROM price_book')


@pytest.mark.parametrize('kind', ['redirect', 'oversize', 'gzip', 'bad_json', 'too_many', 'timeout', 'all_invalid'])
async def test_failed_sync_preserves_last_good_cache_and_safe_errors(book, kind):
    assert (await book.client.post('/prices/sync')).status_code == 200
    old = await book.db.fetchall('SELECT * FROM price_book')
    responses = {
        'redirect': httpx.Response(302, headers={'location': 'http://169.254.169.254/secret'}),
        'oversize': httpx.Response(200, content=b'x'*(4*1024*1024+1)),
        'gzip': httpx.Response(200, headers={'content-encoding': 'gzip'}),
        'bad_json': httpx.Response(200, content=b'secret upstream error'),
        'too_many': httpx.Response(200, json={'data': [model_row()]*10001}),
        'timeout': httpx.ReadTimeout('secret upstream error'),
        'all_invalid': httpx.Response(200, json={'data': [model_row(prompt='-1')]})}
    book.response = responses[kind]
    result = await book.client.post('/prices/sync')
    assert result.status_code == 502, result.text
    assert 'secret' not in result.text
    assert await book.db.fetchall('SELECT * FROM price_book') == old
    assert len(book.calls) == 2
    metadata = (await book.client.get('/prices')).json()['sync']
    assert metadata['last_success'] and metadata['failures'] == 1


async def test_partial_catalog_skips_invalid_rows_and_keeps_previous_entry(book):
    assert (await book.client.post('/prices/sync')).status_code == 200
    book.response = httpx.Response(200, json={'data': [model_row(prompt='-1'), model_row('new/model')]})
    result = await book.client.post('/prices/sync')
    assert result.json()['skipped'] == 1
    assert len((await book.client.get('/prices')).json()['data']) == 2


async def test_live_authorization_checked_after_network_and_before_save(book):
    async def revoke():
        await book.db.execute("UPDATE users SET role='viewer' WHERE username='admin'")
    book.hook = revoke
    result = await book.client.post('/prices/sync')
    assert result.status_code == 403
    assert not await book.db.fetchall('SELECT * FROM price_book')


async def test_viewer_cannot_change_or_enable_prices(book):
    book.role = 'viewer'
    assert (await book.client.get('/prices')).status_code == 200
    assert (await book.client.post('/prices/sync')).status_code == 403
    assert (await book.client.put('/prices/x', json={
        'input_per_million': '1', 'output_per_million': '2'})).status_code == 403
    assert not book.calls


async def test_background_does_not_fetch_without_opt_in_and_cancels(book, monkeypatch):
    polled = asyncio.Event()
    original = book.db.fetchone
    async def fetched(sql, params=()):
        row = await original(sql, params)
        if 'documents' in sql:
            polled.set()
        return row
    monkeypatch.setattr(book.db, 'fetchone', fetched)
    task = asyncio.create_task(pricebook.maintenance(book.app))
    await asyncio.wait_for(polled.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not book.calls
