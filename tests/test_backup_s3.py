from datetime import datetime, timezone

import httpx
import pytest

from backend.modules.backups import s3


CONFIG = {'endpoint': 'https://store.example:9443', 'bucket': 'backups', 'region': 'us-east-1', 'prefix': 'daily'}
CREDS = {'access_key_id': 'EXAMPLE', 'secret_access_key': 'secret', 'session_token': 'session'}


@pytest.mark.asyncio
async def test_signature_and_exact_encoded_path(monkeypatch):
    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2026, 9, 9, 12, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(s3, 'datetime', Clock)
    requests = []
    async def transport(request):
        requests.append(request)
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), cookies={'secret': 'cookie'}, auth=('user', 'password'), headers={'X-Private': 'hidden'}) as client:
        await s3.upload(client, CONFIG, CREDS, 'résumé file.vob', b'encrypted')
    request, = requests
    assert request.url.raw_path == b'/backups/daily/r%C3%A9sum%C3%A9%20file.vob'
    assert request.headers['host'] == 'store.example:9443'
    assert request.headers['x-amz-date'] == '20260909T123000Z'
    assert request.headers['authorization'] == ('AWS4-HMAC-SHA256 Credential=EXAMPLE/20260909/us-east-1/s3/aws4_request, '
        'SignedHeaders=host;x-amz-content-sha256;x-amz-date;x-amz-security-token, '
        'Signature=a123e289305418dcd42f7c3ead7f2f7d2d14fccb991149a8e461dd1aebdb4257')
    assert 'cookie' not in request.headers and 'x-private' not in request.headers
    assert request.content == b'encrypted'


@pytest.mark.parametrize('endpoint', ['http://store.example', 'https://u:p@store.example', 'https://store.example/path', 'https://store.example?secret=x', 'https://store.example#fragment', 'https://store.example?', 'https://store.example/../', 'https://store.example\\bad', 'ftp://store.example'])
def test_invalid_endpoints(endpoint):
    with pytest.raises(s3.S3Error, match='configuration is invalid'):
        s3.validate_config({**CONFIG, 'endpoint': endpoint})


@pytest.mark.parametrize('key', ['../backup', '/backup', 'a//b', 'a/./b', 'a%2fb', 'a\\b', 'a?b', 'a#b'])
@pytest.mark.asyncio
async def test_invalid_keys_no_network(key):
    async def transport(request):
        pytest.fail('No request should be made')
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(s3.S3Error):
            await s3.upload(client, CONFIG, CREDS, key, b'archive')


@pytest.mark.parametrize(('status', 'uncertain'), [(301, False), (403, False), (408, True), (500, True)])
@pytest.mark.asyncio
async def test_errors_redacted_no_response_read_or_redirect(status, uncertain):
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail('Response bodies must not be read')
            yield b'private token'
    calls = []
    async def transport(request):
        calls.append(request)
        return httpx.Response(status, headers={'location': 'https://elsewhere.example'}, stream=Body())
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
        with pytest.raises(s3.S3Error) as error:
            await s3.upload(client, CONFIG, CREDS, 'archive', b'archive')
    assert error.value.uncertain is uncertain
    assert 'private' not in str(error.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_timeout_no_retry():
    calls = []
    async def transport(request):
        calls.append(request)
        raise httpx.ReadTimeout('provider private response secret')
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(s3.S3Error) as error:
            await s3.upload(client, CONFIG, CREDS, 'archive', b'archive')
    assert error.value.uncertain
    assert str(error.value) == 'S3 upload outcome is unknown.'
    assert len(calls) == 1


def test_http_loopback_fixture_and_canonical_prefix():
    assert s3.validate_config({**CONFIG, 'endpoint': 'http://127.0.0.1:9000/'})['endpoint'] == 'http://127.0.0.1:9000'
    with pytest.raises(s3.S3Error):
        s3.validate_config({**CONFIG, 'prefix': '../escape'})


def test_aws_published_put_signature_vector():
    # AWS's published PUT vector independently verifies HMAC key derivation.
    # https://docs.aws.amazon.com/AmazonS3/latest/developerguide/sig-v4-header-based-auth.html
    sign = s3._sign
    key = sign(sign(sign(sign(b'AWS4wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
                             '20130524'), 'us-east-1'), 's3'), 'aws4_request')
    string = ('AWS4-HMAC-SHA256\n20130524T000000Z\n20130524/us-east-1/s3/aws4_request\n'
              '9e0e90d9c76de8fa5b200d8c849cd5b8dc7a3be3951ddb7f6a76b4158342019d')
    assert sign(key, string).hex() == '98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd'
