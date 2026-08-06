from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import socket
import ssl
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Sequence,
)

import httpcore
import httpx


Resolver = Callable[[str, int], Awaitable[Sequence[str]]]

_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)
_HARD_DENY_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("2001:db8::/32"),
)

_MAX_CONNECTIONS = 20
_MAX_KEEPALIVE_CONNECTIONS = 10
_KEEPALIVE_EXPIRY_SECONDS = 30.0


def _normalize_address(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    parsed = ipaddress.ip_address(address)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def _mapped_ipv4_network(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    if not isinstance(network, ipaddress.IPv6Network) or network.prefixlen < 96:
        return network
    first = network.network_address.ipv4_mapped
    last = network.broadcast_address.ipv4_mapped
    if first is None or last is None:
        return network
    return ipaddress.ip_network((first, network.prefixlen - 96), strict=False)


def _in_allowlist(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    allow_cidrs: tuple[str, ...],
) -> bool:
    for value in allow_cidrs:
        try:
            network = _mapped_ipv4_network(ipaddress.ip_network(value, strict=False))
        except ValueError:
            continue
        if address.version == network.version and address in network:
            return True
    return False


def allowed_ip(address: str, allow_cidrs: tuple[str, ...] = ()) -> bool:
    """Return whether an address is safe for outbound HTTP connections.

    Public unicast addresses are allowed. Private, loopback, and other
    non-global addresses require an explicit CIDR entry. Address classes that
    must never be HTTP egress targets are rejected before the allowlist is
    considered.
    """

    try:
        parsed = _normalize_address(address)
    except ValueError:
        return False

    if (
        parsed in _METADATA_ADDRESSES
        or any(
            parsed.version == network.version and parsed in network
            for network in _HARD_DENY_NETWORKS
        )
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_unspecified
        or parsed.is_reserved
    ):
        return False
    if parsed.is_global:
        return True
    return _in_allowlist(parsed, allow_cidrs)


def _validate_hostname(host: str) -> None:
    if "%" in host:
        raise ValueError("URL host must not contain an IPv6 zone identifier")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        ascii_host = host.rstrip(".")
        if not ascii_host or len(ascii_host) > 253:
            raise ValueError("URL hostname is malformed")
        if any(not _DNS_LABEL.fullmatch(label) for label in ascii_host.split(".")):
            raise ValueError("URL hostname is malformed")


def validate_url(url: str) -> httpx.URL:
    """Parse an outbound URL and reject ambiguous or unsupported authorities."""

    if not isinstance(url, str) or not url:
        raise ValueError("URL must be a non-empty string")
    try:
        parsed = httpx.URL(url)
        port = parsed.port
    except (httpx.InvalidURL, UnicodeError) as exc:
        raise ValueError("URL is malformed") from exc

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL scheme must be http or https")
    if not parsed.host:
        raise ValueError("URL must include a hostname")
    if parsed.userinfo:
        raise ValueError("URL user information is not permitted")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("URL port is invalid")

    raw_host = parsed.raw_host.decode("ascii")
    _validate_hostname(raw_host)
    return parsed


async def _resolve(host: str, port: int) -> Sequence[str]:
    try:
        answers = await asyncio.get_running_loop().getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise httpcore.ConnectError("Destination hostname could not be resolved") from exc

    unique: list[str] = []
    seen: set[str] = set()
    for _family, _socktype, _proto, _canonname, sockaddr in answers:
        address = str(sockaddr[0])
        try:
            normalized = str(_normalize_address(address))
        except ValueError as exc:
            raise httpcore.ConnectError("Resolver returned an invalid address") from exc
        if normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve, validate, and connect to a numeric address in one operation."""

    def __init__(
        self,
        allow_cidrs: tuple[str, ...] = (),
        *,
        resolver: Resolver | None = None,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._allow_cidrs = allow_cidrs
        self._resolver = resolver or _resolve
        self._network_backend = network_backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(timeout, 0.0)
        try:
            literal = str(_normalize_address(host))
        except ValueError:
            try:
                if deadline is None:
                    answers = list(await self._resolver(host, port))
                else:
                    answers = list(
                        await asyncio.wait_for(
                            self._resolver(host, port),
                            timeout=max(0.0, deadline - loop.time()),
                        )
                    )
            except (asyncio.TimeoutError,TimeoutError) as exc:
                raise httpcore.ConnectTimeout(
                    "Timed out while resolving destination hostname"
                ) from exc
        else:
            answers = [literal]

        if not answers:
            raise httpcore.ConnectError("Destination hostname returned no addresses")

        normalized: list[str] = []
        for answer in answers:
            try:
                numeric = str(_normalize_address(answer))
            except ValueError as exc:
                raise httpcore.ConnectError("Resolver returned an invalid address") from exc
            if not allowed_ip(numeric, self._allow_cidrs):
                raise httpcore.ConnectError("Destination address is blocked by egress policy")
            if numeric not in normalized:
                normalized.append(numeric)

        last_error: Exception | None = None
        for index,numeric in enumerate(normalized):
            remaining = None if deadline is None else max(0.0, deadline - loop.time())
            attempt_timeout=None if remaining is None else remaining/(len(normalized)-index)
            try:
                connect = self._network_backend.connect_tcp(
                    numeric,
                    port,
                    timeout=attempt_timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
                if remaining is None:
                    return await connect
                return await asyncio.wait_for(connect, timeout=attempt_timeout)
            except (asyncio.TimeoutError,TimeoutError):
                last_error=httpcore.ConnectTimeout('Timed out while connecting to destination')
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix socket egress is not permitted")

    async def sleep(self, seconds: float) -> None:
        await self._network_backend.sleep(seconds)


_HTTPCORE_EXCEPTIONS: dict[type[Exception], type[httpx.HTTPError]] = {
    httpcore.TimeoutException: httpx.TimeoutException,
    httpcore.ConnectTimeout: httpx.ConnectTimeout,
    httpcore.ReadTimeout: httpx.ReadTimeout,
    httpcore.WriteTimeout: httpx.WriteTimeout,
    httpcore.PoolTimeout: httpx.PoolTimeout,
    httpcore.NetworkError: httpx.NetworkError,
    httpcore.ConnectError: httpx.ConnectError,
    httpcore.ReadError: httpx.ReadError,
    httpcore.WriteError: httpx.WriteError,
    httpcore.ProxyError: httpx.ProxyError,
    httpcore.UnsupportedProtocol: httpx.UnsupportedProtocol,
    httpcore.ProtocolError: httpx.ProtocolError,
    httpcore.LocalProtocolError: httpx.LocalProtocolError,
    httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
}


@contextlib.contextmanager
def _map_httpcore_exceptions() -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        mapped: type[httpx.HTTPError] | None = None
        for source, target in _HTTPCORE_EXCEPTIONS.items():
            if isinstance(exc, source) and (mapped is None or issubclass(target, mapped)):
                mapped = target
        if mapped is None:
            raise
        raise mapped(str(exc)) from exc


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: AsyncIterable[bytes]) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        with _map_httpcore_exceptions():
            async for part in self._stream:
                yield part

    async def aclose(self) -> None:
        close = getattr(self._stream, "aclose", None)
        if close is not None:
            with _map_httpcore_exceptions():
                await close()


class PinnedAsyncHTTPTransport(httpx.AsyncBaseTransport):
    """HTTPX transport backed by a DNS-validating, address-pinned pool."""

    def __init__(self, *, network_backend: PinnedNetworkBackend) -> None:
        tls = ssl.create_default_context()
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=tls,
            max_connections=_MAX_CONNECTIONS,
            max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=_KEEPALIVE_EXPIRY_SECONDS,
            http1=True,
            http2=False,
            retries=0,
            network_backend=network_backend,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        validate_url(str(request.url))
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _map_httpcore_exceptions():
            response = await self._pool.handle_async_request(core_request)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_ResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        with _map_httpcore_exceptions():
            await self._pool.aclose()


def create_http_client(allow_cidrs: tuple[str, ...] = ()) -> httpx.AsyncClient:
    """Create the application's pooled, verified outbound HTTP client."""

    backend = PinnedNetworkBackend(allow_cidrs=allow_cidrs)
    transport = PinnedAsyncHTTPTransport(network_backend=backend)
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
        follow_redirects=False,
        trust_env=False,
    )
