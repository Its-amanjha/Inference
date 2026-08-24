from __future__ import annotations

import asyncio
import ssl
from collections.abc import Iterable, Sequence
from typing import Any

import httpcore
import httpx
import pytest

import backend.net as net_module
from backend.net import (
    PinnedAsyncHTTPTransport,
    PinnedNetworkBackend,
    allowed_ip,
    create_http_client,
    validate_url,
)


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "0.0.0.1",
        "::",
        "169.254.169.254",
        "::ffff:169.254.169.254",
        "fe80::1",
        "224.0.0.1",
        "ff02::1",
        "192.0.2.10",
        "198.18.0.1",
        "100.100.100.200",
        "fd00:ec2::254",
    ],
)
def test_hard_denied_addresses_stay_denied_under_broad_allowlists(address: str) -> None:
    # Catches applying an allowlist before the non-overridable address checks.
    assert not allowed_ip(address, ("0.0.0.0/0", "::/0"))


def test_private_and_loopback_addresses_require_an_explicit_matching_allowlist() -> None:
    # Catches treating all non-reserved addresses as safe egress destinations.
    assert not allowed_ip("10.20.30.40")
    assert not allowed_ip("127.0.0.1")
    assert not allowed_ip("::1")
    assert allowed_ip("10.20.30.40", ("10.20.0.0/16",))
    assert allowed_ip("127.0.0.1", ("127.0.0.1/32",))
    assert allowed_ip("::ffff:127.0.0.1", ("127.0.0.1/32",))


def test_public_addresses_are_allowed_and_invalid_addresses_are_rejected() -> None:
    # Catches accidental default-deny of public egress and parser exceptions leaking out.
    assert allowed_ip("8.8.8.8")
    assert allowed_ip("2606:4700:4700::1111")
    assert not allowed_ip("not-an-ip")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "http://user:password@example.com/",
        "http:///missing-host",
        "http://example.com:99999/",
        "http://bad_host.example/",
        "http://-bad.example/",
        "http://example..com/",
        "http://[fe80::1%25eth0]/",
    ],
)
def test_validate_url_rejects_unsupported_or_ambiguous_authorities(url: str) -> None:
    # Catches authority parsing that could make validation and connection disagree.
    with pytest.raises(ValueError):
        validate_url(url)


def test_validate_url_returns_a_normalized_httpx_url() -> None:
    # Catches returning an unparsed string that downstream code could reinterpret.
    url = validate_url("HTTPS://Example.COM:443/a%20b?q=1")
    assert isinstance(url, httpx.URL)
    assert str(url) == "https://example.com:443/a%20b?q=1"


@pytest.mark.asyncio
async def test_stalled_first_address_leaves_time_for_reachable_second() -> None:
    attempted=[]
    stream=object()

    class Backend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, host, port, **kwargs):
            attempted.append(host)
            if host=='8.8.8.8':
                await asyncio.Event().wait()
            return stream

    async def resolve(host,port):
        return ['8.8.8.8','1.1.1.1']

    backend=PinnedNetworkBackend(resolver=resolve,network_backend=Backend())
    assert await backend.connect_tcp('example.test',443,timeout=0.1) is stream
    assert attempted==['8.8.8.8','1.1.1.1']


class RecordingStream(httpcore.AsyncNetworkStream):
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.server_hostnames: list[str | None] = []
        self._response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        response, self._response = self._response, b""
        return response

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.writes.append(buffer)

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.server_hostnames.append(server_hostname)
        assert ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert ssl_context.check_hostname
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


class RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.connect_hosts: list[str] = []
        self.streams: list[RecordingStream] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connect_hosts.append(host)
        stream = RecordingStream()
        self.streams.append(stream)
        return stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise AssertionError("Unix sockets must not be used for URL egress")

    async def sleep(self, seconds: float) -> None:
        return None


class StaticResolver:
    def __init__(self, answers: Sequence[str]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, host: str, port: int) -> Sequence[str]:
        self.calls.append((host, port))
        return self.answers


def make_transport(
    answers: Sequence[str], allow_cidrs: tuple[str, ...] = ()
) -> tuple[PinnedAsyncHTTPTransport, StaticResolver, RecordingBackend]:
    resolver = StaticResolver(answers)
    network = RecordingBackend()
    pinned = PinnedNetworkBackend(
        allow_cidrs=allow_cidrs,
        resolver=resolver,
        network_backend=network,
    )
    return PinnedAsyncHTTPTransport(network_backend=pinned), resolver, network


class FirstAddressHangsBackend(RecordingBackend):
    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connect_hosts.append(host)
        if len(self.connect_hosts) == 1:
            await asyncio.Event().wait()
        stream = RecordingStream()
        self.streams.append(stream)
        return stream


@pytest.mark.asyncio
async def test_mixed_dns_answer_is_rejected_before_any_connection() -> None:
    # Catches validating only the first DNS answer and leaving a rebinding path open.
    transport, resolver, network = make_transport(["93.184.216.34", "127.0.0.1"])
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("http://service.example/")

    assert resolver.calls == [("service.example", 80)]
    assert network.connect_hosts == []


@pytest.mark.asyncio
async def test_connection_uses_pinned_ip_while_preserving_host_and_tls_sni() -> None:
    # Catches validating a hostname and then resolving it again during TCP connect,
    # or replacing the hostname in TLS/HTTP while pinning the numeric address.
    transport, resolver, network = make_transport(
        ["127.0.0.1"], ("127.0.0.1/32",)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get("https://service.example/path?q=1")

    assert response.status_code == 200
    assert response.text == "OK"
    assert resolver.calls == [("service.example", 443)]
    assert network.connect_hosts == ["127.0.0.1"]
    assert network.streams[0].server_hostnames == ["service.example"]
    request_bytes = b"".join(network.streams[0].writes)
    assert b"Host: service.example\r\n" in request_bytes
    assert request_bytes.startswith(b"GET /path?q=1 HTTP/1.1\r\n")


@pytest.mark.asyncio
async def test_numeric_ip_bypasses_dns_and_remains_subject_to_policy() -> None:
    # Catches sending IP literals through a second resolver or bypassing policy checks.
    transport, resolver, network = make_transport([], ("127.0.0.1/32",))
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get("http://127.0.0.1/")

    assert response.status_code == 200
    assert resolver.calls == []
    assert network.connect_hosts == ["127.0.0.1"]


@pytest.mark.asyncio
async def test_connect_timeout_also_bounds_dns_resolution() -> None:
    # Catches placing custom DNS resolution outside HTTPX's connect deadline.
    async def resolver(host: str, port: int) -> Sequence[str]:
        await asyncio.Event().wait()
        return []

    backend = PinnedNetworkBackend(
        resolver=resolver,
        network_backend=RecordingBackend(),
    )
    with pytest.raises(httpcore.ConnectTimeout):
        await asyncio.wait_for(
            backend.connect_tcp("service.example", 443, timeout=0.01),
            timeout=0.2,
        )


@pytest.mark.asyncio
async def test_connect_deadline_leaves_time_for_a_later_dns_candidate() -> None:
    # Catches assigning the whole connect budget to a hanging first address.
    resolver = StaticResolver(["2606:4700:4700::1111", "8.8.8.8"])
    network = FirstAddressHangsBackend()
    backend = PinnedNetworkBackend(resolver=resolver, network_backend=network)

    stream = await asyncio.wait_for(
        backend.connect_tcp("service.example", 443, timeout=0.12),
        timeout=0.3,
    )

    assert isinstance(stream, RecordingStream)
    assert resolver.calls == [("service.example", 443)]
    assert network.connect_hosts == ["2606:4700:4700::1111", "8.8.8.8"]


@pytest.mark.asyncio
async def test_python310_asyncio_timeout_is_mapped_to_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Catches relying on the Python 3.11+ alias between the two timeout classes.
    class LegacyAsyncioTimeoutError(Exception):
        pass

    monkeypatch.setattr(net_module.asyncio, "TimeoutError", LegacyAsyncioTimeoutError)

    async def resolver(host: str, port: int) -> Sequence[str]:
        raise LegacyAsyncioTimeoutError

    backend = PinnedNetworkBackend(
        resolver=resolver,
        network_backend=RecordingBackend(),
    )
    with pytest.raises(httpcore.ConnectTimeout):
        await backend.connect_tcp("service.example", 443, timeout=0.1)


@pytest.mark.asyncio
async def test_create_http_client_uses_bounded_timeouts_and_disables_redirects() -> None:
    # Catches an unbounded lifecycle client or redirects that escape the checked origin.
    client = create_http_client()
    try:
        assert client.follow_redirects is False
        assert client.timeout.connect == 5.0
        assert client.timeout.read == 30.0
        assert client.timeout.write == 30.0
        assert client.timeout.pool == 5.0
    finally:
        await client.aclose()
