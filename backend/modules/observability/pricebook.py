"""Persist exact model prices and opt-in public catalog refreshes.

OpenRouter schema and per-token units:
https://openrouter.ai/docs/guides/overview/models
Bundled standard text-token rates verified 2026-09-08 on the two source_url
pages below. Catalog/default costs are estimates, not provider billing records.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
import re
import time
from typing import Any

from fastapi import APIRouter, FastAPI, Request
import httpx
from pydantic import BaseModel, ConfigDict, field_validator

from backend import audit
from backend.auth_gate import fail, require_current_role, require_role
from backend.database import Database


router = APIRouter()
CATALOG_URL = 'https://openrouter.ai/api/v1/models'
STATE_KEY = 'observability.price_sync'
MAX_BYTES = 4 * 1024 * 1024
MAX_MODELS = 10000
REFRESH_SECONDS = 6 * 3600
MODEL_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}\Z')
BUNDLED = {
    'gpt-4.1-mini-2025-04-14': {
        'input_per_million': '0.40', 'output_per_million': '1.60',
        'cached_input_per_million': '0.10', 'source': 'bundled',
        'updated_at': '2026-09-08T00:00:00+00:00', 'estimated': True,
        'source_url': 'https://developers.openai.com/api/docs/models/gpt-4.1-mini'},
    'gpt-4.1-nano-2025-04-14': {
        'input_per_million': '0.10', 'output_per_million': '0.40',
        'cached_input_per_million': '0.025', 'source': 'bundled',
        'updated_at': '2026-09-08T00:00:00+00:00', 'estimated': True,
        'source_url': 'https://developers.openai.com/api/docs/models/gpt-4.1-nano'},
}


def _rate(value: Any) -> Decimal:
    """Bound coefficient, scale and magnitude before expanding decimal strings."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('Supply a finite nonnegative rate.')
    text = str(value)
    if len(text) > 48:
        raise ValueError('Rate exceeds supported precision.')
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError('Supply a decimal rate.') from exc
    if (not number.is_finite() or number < 0 or number > 1000000
            or abs(number.as_tuple().exponent) > 18
            or len(number.as_tuple().digits) > 36):
        raise ValueError('Rate must be 0..1000000 with at most 18 decimal places.')
    return number


def _text(number: Decimal) -> str:
    value = format(number, 'f')
    return value.rstrip('0').rstrip('.') if '.' in value else value


class PriceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Decimal | None = None
    reasoning_output_per_million: Decimal | None = None

    @field_validator('*', mode='before')
    @classmethod
    def bounded(cls, value: Any) -> Any:
        return None if value is None else _rate(value)


async def _layers(tx: Any, model: str) -> dict:
    row = await tx.fetchone('SELECT value_json FROM price_book WHERE model=?', (model,))
    return json.loads(row['value_json']) if row else {}


async def _save_layer(tx: Any, model: str, layer: str, entry: dict) -> None:
    layers = await _layers(tx, model)
    layers[layer] = entry
    await tx.execute('INSERT INTO price_book(model,value_json) VALUES(?,?) '
                     'ON CONFLICT(model) DO UPDATE SET value_json=excluded.value_json',
                     (model, json.dumps(layers)))


async def seed_prices(db: Database) -> None:
    """Seed documented defaults locally without overwriting other layers."""
    async with db.transaction() as tx:
        for model, entry in BUNDLED.items():
            if 'bundled' not in await _layers(tx, model):
                await _save_layer(tx, model, 'bundled', entry)


async def _state(tx: Any) -> dict:
    row = await tx.fetchone('SELECT value_json FROM documents WHERE key=?', (STATE_KEY,))
    return json.loads(row['value_json']) if row else {'enabled': False, 'failures': 0}


async def _save_state(tx: Any, state: dict) -> None:
    await tx.execute('INSERT INTO documents(key,value_json) VALUES(?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=CURRENT_TIMESTAMP',
                     (STATE_KEY, json.dumps(state)))


@router.get('/prices')
async def prices(request: Request) -> dict:
    require_role(request, 'viewer')
    async with request.app.state.db.transaction() as tx:
        rows = await tx.fetchall('SELECT model,value_json FROM price_book ORDER BY model')
        state = await _state(tx)
    data = []
    for row in rows:
        layers = json.loads(row['value_json'])
        entry = layers.get('override') or layers.get('live') or layers.get('bundled')
        if entry:
            data.append({'model': row['model'], **entry})
    return {'data': data, 'sync': state}


@router.put('/prices/{model:path}')
async def override_price(model: str, body: PriceInput, request: Request) -> dict:
    require_role(request, 'admin')
    if not MODEL_PATTERN.fullmatch(model):
        fail(422, 'invalid_model', 'Supply an exact model identifier of at most 256 characters.')
    entry = {key: _text(value) for key, value in body.model_dump(exclude_none=True).items()}
    entry.update(source='override', updated_at=datetime.now(timezone.utc).isoformat(), estimated=False)
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request, 'admin', tx)
        await _save_layer(tx, model, 'override', entry)
    await audit.record(request, 'observability.price_override', model)
    return {'model': model, **entry}


async def _download(client: httpx.AsyncClient) -> bytes:
    # Constructing Request directly avoids client defaults, cookies and headers.
    outbound = httpx.Request('GET', CATALOG_URL,
        headers={'Accept': 'application/json', 'Accept-Encoding': 'identity'},
        extensions={'timeout': httpx.Timeout(30).as_dict()})
    response = await client.send(outbound, auth=None, follow_redirects=False, stream=True)
    try:
        if response.status_code != 200:
            raise ValueError('Unexpected catalog response')
        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
            raise ValueError('Compressed catalog is unsupported')
        raw = bytearray()
        if response.is_stream_consumed:
            # MockTransport and preloaded responses may already be consumed.
            raw.extend(response.content)
            if len(raw) > MAX_BYTES:
                raise ValueError('Catalog exceeds size limit')
        else:
            async for chunk in response.aiter_raw():
                if len(raw) + len(chunk) > MAX_BYTES:
                    raise ValueError('Catalog exceeds size limit')
                raw.extend(chunk)
        return bytes(raw)
    finally:
        await response.aclose()


def _parse(raw: bytes, timestamp: str) -> tuple[dict, int]:
    catalog = json.loads(raw)
    rows = catalog.get('data') if isinstance(catalog, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_MODELS:
        raise ValueError('Invalid catalog envelope')
    result, skipped = {}, 0
    for row in rows:
        try:
            model = row['id']
            if not isinstance(model, str) or not MODEL_PATTERN.fullmatch(model) or model in result:
                raise ValueError('Invalid or duplicated identifier')
            source = row['pricing']
            entry = {'source': 'openrouter', 'updated_at': timestamp,
                     'source_url': CATALOG_URL, 'estimated': True}
            fields = {'prompt': 'input_per_million', 'completion': 'output_per_million',
                      'input_cache_read': 'cached_input_per_million'}
            with localcontext() as context:
                context.prec = 48
                for upstream, field in fields.items():
                    if upstream == 'input_cache_read' and source.get(upstream) is None:
                        continue
                    entry[field] = _text(_rate(_rate(source[upstream]) * 1000000))
            result[model] = entry
        except (ValueError, TypeError, KeyError, InvalidOperation, AttributeError):
            skipped += 1
    if not result:
        raise ValueError('Catalog contains no supported prices')
    return result, skipped


def _lock(app: FastAPI) -> asyncio.Lock:
    if not hasattr(app.state, 'price_sync_lock'):
        app.state.price_sync_lock = asyncio.Lock()
    return app.state.price_sync_lock


async def _sync(app: FastAPI, request: Request | None = None) -> dict:
    lock = _lock(app)
    if lock.locked():
        if request is not None:
            fail(409, 'price_sync_busy', 'A price refresh is already running.')
        return {'busy': True}
    async with lock:
        # Persist the opt-in before external work, with a current session check.
        async with app.state.db.transaction() as tx:
            state = await _state(tx)
            if request is not None:
                await require_current_role(request, 'admin', tx)
                state['enabled'] = True
                await _save_state(tx, state)
            elif not state.get('enabled'):
                return {'enabled': False}
        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            raw = await asyncio.wait_for(_download(app.state.http), timeout=35)
            entries, skipped = _parse(raw, timestamp)
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError, RecursionError):
            async with app.state.db.transaction() as tx:
                if request is not None:
                    await require_current_role(request, 'admin', tx)
                state = await _state(tx)
                failures = min(int(state.get('failures', 0)) + 1, 20)
                state.update(failures=failures, last_attempt=timestamp,
                             last_error='price_sync_failed',
                             next_attempt=time.time() + min(60 * 2**(failures-1), REFRESH_SECONDS))
                await _save_state(tx, state)
            if request is not None:
                await audit.record(request, 'observability.price_sync', 'openrouter', outcome='failed')
                fail(502, 'price_sync_failed', 'Price refresh failed; the previous cache was retained.')
            return {'ok': False}
        async with app.state.db.transaction() as tx:
            if request is not None:
                await require_current_role(request, 'admin', tx)
            for model, entry in entries.items():
                await _save_layer(tx, model, 'live', entry)
            state = await _state(tx)
            state.update(last_attempt=timestamp, last_success=timestamp, failures=0,
                         last_error=None, next_attempt=time.time()+REFRESH_SECONDS,
                         updated=len(entries), skipped=skipped)
            await _save_state(tx, state)
        if request is not None:
            await audit.record(request, 'observability.price_sync', 'openrouter',
                               {'updated': len(entries), 'skipped': skipped})
        return {'ok': True, 'updated': len(entries), 'skipped': skipped, 'enabled': True}


@router.post('/prices/sync')
async def sync_prices(request: Request) -> dict:
    require_role(request, 'admin')
    return await _sync(request.app, request)


async def maintenance(app: FastAPI) -> None:
    """Lifespan-owned task: poll opt-in state, retain cache, cancel on shutdown."""
    while True:
        state = await _state(app.state.db)
        if state.get('enabled') and time.time() >= state.get('next_attempt', 0):
            await _sync(app)
        await asyncio.sleep(60)
