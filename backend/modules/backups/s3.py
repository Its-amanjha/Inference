"""Single-attempt, path-style SigV4 PUT using the application's verified client."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import re
from urllib.parse import quote, urlsplit

import httpx

from backend.net import validate_url


class S3Error(ValueError):
    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


def _segments(value: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not empty) or len(value.encode('utf-8')) > 1024:
        raise ValueError('Invalid object path')
    if value and any(part in {'', '.', '..'} for part in value.split('/')):
        raise ValueError('Invalid object path')
    if any(ord(c) < 32 or ord(c) == 127 or c in '\\%?#' for c in value):
        raise ValueError('Invalid object path')
    return value


def validate_config(config: dict) -> dict:
    """Validate non-secret destination settings without performing network I/O."""
    try:
        endpoint = config['endpoint']
        if not isinstance(endpoint, str) or any(ord(c) <= 32 or c == '\\' for c in endpoint):
            raise ValueError('Invalid endpoint')
        raw = urlsplit(endpoint)
        url = validate_url(endpoint)
        if raw.query or raw.fragment or '?' in endpoint or '#' in endpoint or raw.path not in {'', '/'}:
            raise ValueError('Endpoint must be an origin')
        if url.scheme == 'http':
            try:
                local = ipaddress.ip_address(url.host).is_loopback
            except ValueError:
                local = url.host == 'localhost'
            if not local:
                raise ValueError('HTTPS is required')
        bucket = config['bucket']
        region = config['region']
        if not isinstance(bucket, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket) or '..' in bucket:
            raise ValueError('Invalid bucket')
        if not isinstance(region, str) or not re.fullmatch(r'[a-z0-9-]{1,64}', region):
            raise ValueError('Invalid region')
        prefix = _segments(config.get('prefix', ''), empty=True)
        return {'endpoint': str(url).rstrip('/'), 'bucket': bucket, 'region': region, 'prefix': prefix}
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise S3Error('S3 destination configuration is invalid.') from None


def _sign(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode('utf-8'), hashlib.sha256).digest()


async def upload(client: httpx.AsyncClient, config: dict, credentials: dict, key: str, data: bytes) -> None:
    """Upload encrypted bytes once. Uncertain outcomes must never be retried blindly.

    The supplied client must be the lifespan-owned verified egress client. HTTP
    loopback fixtures still require its explicit CIDR allowlist. Response bodies
    are never read; untrusted XML cannot allocate memory or escape in errors.
    """
    config = validate_config(config)
    try:
        key = _segments(key)
        access = credentials['access_key_id']
        secret = credentials['secret_access_key']
        token = credentials.get('session_token')
        if not isinstance(access, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', access):
            raise ValueError('Invalid credentials')
        if not isinstance(secret, str) or not 1 <= len(secret) <= 4096:
            raise ValueError('Invalid credentials')
        if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 8192 or any(ord(c) <= 32 or ord(c) >= 127 for c in token)):
            raise ValueError('Invalid token')
        if not isinstance(data, bytes) or len(data) > 192 * 1024 * 1024:
            raise ValueError('Invalid archive size')
        path = '/'.join(filter(None, (config['bucket'], config['prefix'], key)))
        url = httpx.URL(config['endpoint'] + '/' + quote(path, safe='/-_.~'))
        now = datetime.now(timezone.utc)
        timestamp, day = now.strftime('%Y%m%dT%H%M%SZ'), now.strftime('%Y%m%d')
        digest = hashlib.sha256(data).hexdigest()
        # Request construction avoids client cookies/default headers and auth.
        request = httpx.Request('PUT', url, content=data)
        headers = {'host': request.headers['host'], 'x-amz-content-sha256': digest, 'x-amz-date': timestamp}
        if token is not None:
            headers['x-amz-security-token'] = token
        names = ';'.join(sorted(headers))
        canonical = ''.join(f'{name}:{headers[name]}\n' for name in sorted(headers))
        canonical_request = '\n'.join(('PUT', url.raw_path.decode('ascii'), '', canonical, names, digest))
        scope = f"{day}/{config['region']}/s3/aws4_request"
        to_sign = '\n'.join(('AWS4-HMAC-SHA256', timestamp, scope, hashlib.sha256(canonical_request.encode()).hexdigest()))
        signing = _sign(_sign(_sign(_sign(('AWS4' + secret).encode(), day), config['region']), 's3'), 'aws4_request')
        signature = _sign(signing, to_sign).hex()
        request.headers.update(headers)
        request.headers['authorization'] = f'AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={names}, Signature={signature}'
        request.headers['content-type'] = 'application/octet-stream'
        request.extensions['timeout'] = dict.fromkeys(('connect', 'read', 'write', 'pool'), 30.0)
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise S3Error('S3 upload arguments are invalid.') from None

    async def send() -> None:
        response = await client.send(request, auth=None, follow_redirects=False, stream=True)
        try:
            if 200 <= response.status_code < 300:
                return
            uncertain = response.status_code >= 500 or response.status_code == 408
            raise S3Error('S3 upload outcome is unknown.' if uncertain else 'S3 upload was rejected.', uncertain=uncertain)
        finally:
            await response.aclose()

    try:
        await asyncio.wait_for(send(), timeout=30.0)
    except (httpx.HTTPError, asyncio.TimeoutError, OSError):
        raise S3Error('S3 upload outcome is unknown.', uncertain=True) from None
