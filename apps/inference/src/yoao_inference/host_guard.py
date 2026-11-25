"""SSRF host guard and connection-time IP pinning for external inference plugins."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from urllib.parse import urlsplit

import httpx

INFERENCE_PLUGIN_PRIVATE_HOST_ALLOWLIST_ENV = "INFERENCE_PLUGIN_PRIVATE_HOST_ALLOWLIST"
INFERENCE_PLUGIN_LOOPBACK_HOST_ALLOWLIST_ENV = (
    "INFERENCE_PLUGIN_LOOPBACK_HOST_ALLOWLIST"
)

_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_IPV4_THIS_NETWORK = ipaddress.IPv4Network("0.0.0.0/8")
_MAX_ALLOWLIST_ENTRIES = 256
_MAX_ALLOWLIST_ENTRY_LENGTH = 253

DnsResolver = Callable[[str], list[str]]


class InferenceHostDenied(Exception):
    """Inference plugin endpoint violates scheme, allowlist, or SSRF guard."""


class InferenceAllowlistInvalid(ValueError):
    """Inference plugin host allowlist contains invalid syntax."""


def _unwrap_ip(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _normalize_entry(entry: str) -> str:
    value = entry.strip()
    if not value:
        return ""
    if len(value) > _MAX_ALLOWLIST_ENTRY_LENGTH:
        raise InferenceAllowlistInvalid("allowlist entry is too long")
    if "://" in value:
        raise InferenceAllowlistInvalid("allowlist entries cannot contain a URL scheme")
    if "*" in value:
        raise InferenceAllowlistInvalid("use a leading dot for suffix domains")
    try:
        return str(_unwrap_ip(ipaddress.ip_address(value)))
    except ValueError:
        pass
    if "/" in value:
        try:
            return str(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise InferenceAllowlistInvalid("invalid CIDR entry") from exc
    if ":" in value or "\\" in value:
        raise InferenceAllowlistInvalid("domain entries cannot contain ports or paths")
    suffix = value.startswith(".")
    domain = (value[1:] if suffix else value).rstrip(".")
    if not domain:
        raise InferenceAllowlistInvalid("domain suffix cannot be empty")
    try:
        ascii_domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise InferenceAllowlistInvalid("invalid domain entry") from exc
    if len(ascii_domain) > _MAX_ALLOWLIST_ENTRY_LENGTH or any(
        not _DOMAIN_LABEL.fullmatch(label) for label in ascii_domain.split(".")
    ):
        raise InferenceAllowlistInvalid("invalid domain entry")
    return f".{ascii_domain}" if suffix else ascii_domain


def parse_allowlist_env(raw: str | Sequence[str] | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return ()
        if text.startswith("["):
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError as exc:
                raise InferenceAllowlistInvalid("invalid JSON allowlist") from exc
            if not isinstance(loaded, list) or any(
                not isinstance(item, str) for item in loaded
            ):
                raise InferenceAllowlistInvalid("allowlist JSON must be a string list")
            return normalize_inference_allowlist(loaded)
        return normalize_inference_allowlist(text.split(","))
    return normalize_inference_allowlist(raw)


def normalize_inference_allowlist(entries: Sequence[str]) -> tuple[str, ...]:
    if len(entries) > _MAX_ALLOWLIST_ENTRIES:
        raise InferenceAllowlistInvalid("allowlist has too many entries")
    normalized: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        value = _normalize_entry(entry)
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def env_host_allowlists() -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (
        parse_allowlist_env(
            os.environ.get(INFERENCE_PLUGIN_PRIVATE_HOST_ALLOWLIST_ENV)
        ),
        parse_allowlist_env(
            os.environ.get(INFERENCE_PLUGIN_LOOPBACK_HOST_ALLOWLIST_ENV)
        ),
    )


def is_direct_plugin_extra_params(extra_params: object) -> bool:
    if not isinstance(extra_params, Mapping):
        return False
    plugin = extra_params.get("plugin")
    if not isinstance(plugin, Mapping):
        return False
    manifest = plugin.get("manifest")
    return (
        plugin.get("execution_mode") == "direct"
        and isinstance(manifest, Mapping)
        and manifest.get("execution_mode") == "direct"
    )


def _host_matches_entries(host: str, entries: Sequence[str]) -> bool:
    candidate = host.lower().rstrip(".")
    try:
        candidate_ip = _unwrap_ip(ipaddress.ip_address(candidate))
    except ValueError:
        candidate_ip = None

    for entry in entries:
        if not entry:
            continue
        if candidate_ip is not None:
            try:
                net = ipaddress.ip_network(entry, strict=False)
            except ValueError:
                continue
            if candidate_ip.version == net.version and candidate_ip in net:
                return True
            continue
        if "/" in entry:
            continue
        try:
            ipaddress.ip_address(entry)
            continue
        except ValueError:
            pass
        if entry.startswith("."):
            if candidate == entry[1:] or candidate.endswith(entry):
                return True
        elif candidate == entry:
            return True
    return False


def _address_matches_entries(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    entries: Sequence[str],
) -> bool:
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if address.version == network.version and address in network:
            return True
    return False


def _hard_blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    unwrapped = _unwrap_ip(address)
    if unwrapped.is_loopback:
        return False
    if isinstance(unwrapped, ipaddress.IPv4Address) and unwrapped in _IPV4_THIS_NETWORK:
        return True
    return (
        unwrapped.is_link_local
        or unwrapped.is_multicast
        or unwrapped.is_unspecified
        or unwrapped.is_reserved
    )


def _is_loopback_hostname(host: str) -> bool:
    lowered = host.lower().rstrip(".")
    return lowered == "localhost" or lowered.endswith(".localhost")


def _default_dns_resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise InferenceHostDenied("inference endpoint DNS lookup failed") from exc
    results: list[str] = []
    seen: set[str] = set()
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.add(addr)
            results.append(addr)
    if not results:
        raise InferenceHostDenied("inference endpoint DNS returned no addresses")
    return results


def validate_inference_plugin_url(url: str) -> str:
    """Validate URL shape and scheme for an inference plugin endpoint."""
    if type(url) is not str or not url or url != url.strip() or len(url) > 512:
        raise InferenceHostDenied("invalid inference plugin endpoint URL")
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in url):
        raise InferenceHostDenied("inference plugin URL contains illegal characters")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise InferenceHostDenied("invalid inference plugin endpoint URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in url
        or "#" in url
        or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise InferenceHostDenied(
            "inference plugin endpoint must be an HTTP(S) base URL without credentials, query, or fragment"
        )
    return url.rstrip("/")


def resolve_pinned_inference_ips(
    host_or_url: str,
    *,
    private_host_allowlist: Sequence[str] = (),
    loopback_host_allowlist: Sequence[str] = (),
    resolver: DnsResolver | None = None,
    enforce_private_and_loopback: bool = True,
) -> tuple[str, list[str]]:
    """Resolve and validate every IP at connection time, returning (host, pinned_ips)."""
    if "://" in host_or_url:
        normalized_url = validate_inference_plugin_url(host_or_url)
        host = urlsplit(normalized_url).hostname or ""
    else:
        host = host_or_url.strip()
        if not host:
            raise InferenceHostDenied("empty inference endpoint host")

    normalized_private = normalize_inference_allowlist(private_host_allowlist)
    normalized_loopback = normalize_inference_allowlist(loopback_host_allowlist)
    private_host_allowed = _host_matches_entries(host, normalized_private)
    loopback_host_allowed = _host_matches_entries(host, normalized_loopback)

    if enforce_private_and_loopback and _is_loopback_hostname(host):
        if not loopback_host_allowed:
            raise InferenceHostDenied(
                "inference loopback host requires explicit INFERENCE_PLUGIN_LOOPBACK_HOST_ALLOWLIST entry"
            )

    try:
        raw_ips = [str(_unwrap_ip(ipaddress.ip_address(host)))]
    except ValueError:
        raw_ips = (resolver or _default_dns_resolve)(host)

    pinned: list[str] = []
    for raw_ip in raw_ips:
        try:
            addr = _unwrap_ip(ipaddress.ip_address(raw_ip))
        except ValueError as exc:
            raise InferenceHostDenied(
                "inference endpoint DNS returned an invalid address"
            ) from exc
        if _hard_blocked(addr):
            raise InferenceHostDenied(
                "inference target resolves to a forbidden address (link-local/metadata/multicast/reserved)"
            )
        if enforce_private_and_loopback:
            if addr.is_loopback:
                if not (
                    loopback_host_allowed
                    or _address_matches_entries(addr, normalized_loopback)
                ):
                    raise InferenceHostDenied(
                        "inference target resolves to a loopback address not in INFERENCE_PLUGIN_LOOPBACK_HOST_ALLOWLIST"
                    )
            elif addr.is_private or not addr.is_global:
                if not (
                    private_host_allowed
                    or _address_matches_entries(addr, normalized_private)
                ):
                    raise InferenceHostDenied(
                        "inference target resolves to a private network not in INFERENCE_PLUGIN_PRIVATE_HOST_ALLOWLIST"
                    )
        pinned.append(str(addr))
    if not pinned:
        raise InferenceHostDenied("inference target resolved to no usable addresses")
    return host, pinned


def build_pinned_network_backend(
    *,
    private_host_allowlist: Sequence[str] = (),
    loopback_host_allowlist: Sequence[str] = (),
    resolver: DnsResolver | None = None,
    enforce_private_and_loopback: bool = True,
):
    """Build an httpcore AsyncNetworkBackend that pins TCP dials to validated IPs."""
    import httpcore

    class _PinnedInferenceBackend(httpcore.AsyncNetworkBackend):
        def __init__(self) -> None:
            self._inner = httpcore.AnyIOBackend()

        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options=None,
        ):
            _, ips = resolve_pinned_inference_ips(
                host,
                private_host_allowlist=private_host_allowlist,
                loopback_host_allowlist=loopback_host_allowlist,
                resolver=resolver,
                enforce_private_and_loopback=enforce_private_and_loopback,
            )
            last_error: Exception | None = None
            for ip in ips:
                try:
                    return await self._inner.connect_tcp(
                        ip,
                        port,
                        timeout=timeout,
                        local_address=local_address,
                        socket_options=socket_options,
                    )
                except Exception as exc:
                    last_error = exc
            if last_error is not None:
                raise last_error
            raise InferenceHostDenied("inference target could not be reached")

        async def connect_unix_socket(
            self,
            path: str,
            timeout: float | None = None,
            socket_options=None,
        ):
            raise InferenceHostDenied(
                "unix sockets are forbidden for inference plugin endpoints"
            )

        async def sleep(self, seconds: float) -> None:
            await self._inner.sleep(seconds)

    return _PinnedInferenceBackend()


@contextmanager
def _map_httpcore_exceptions():
    import httpcore

    mapping = {
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
    try:
        yield
    except InferenceHostDenied:
        raise
    except Exception as exc:
        mapped_exc = None
        for from_exc, to_exc in mapping.items():
            if isinstance(exc, from_exc):
                mapped_exc = to_exc
        if mapped_exc is None:
            raise
        raise mapped_exc(str(exc)) from exc


def build_pinned_http_transport(
    *,
    private_host_allowlist: Sequence[str] = (),
    loopback_host_allowlist: Sequence[str] = (),
    resolver: DnsResolver | None = None,
    enforce_private_and_loopback: bool = True,
) -> httpx.AsyncBaseTransport:
    """Build an httpx AsyncBaseTransport backed by the IP-pinning network backend."""
    import httpcore

    backend = build_pinned_network_backend(
        private_host_allowlist=private_host_allowlist,
        loopback_host_allowlist=loopback_host_allowlist,
        resolver=resolver,
        enforce_private_and_loopback=enforce_private_and_loopback,
    )
    pool = httpcore.AsyncConnectionPool(
        network_backend=backend,
        retries=0,
    )

    class _PinnedResponseStream(httpx.AsyncByteStream):
        def __init__(self, stream) -> None:
            self._stream = stream

        async def __aiter__(self):
            with _map_httpcore_exceptions():
                async for part in self._stream:
                    yield part

        async def aclose(self) -> None:
            if hasattr(self._stream, "aclose"):
                with _map_httpcore_exceptions():
                    await self._stream.aclose()

    class _PinnedInferenceTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            req = httpcore.Request(
                method=request.method.encode("ascii"),
                url=httpcore.URL(
                    scheme=request.url.scheme.encode("ascii"),
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
            with _map_httpcore_exceptions():
                resp = await pool.handle_async_request(req)
            return httpx.Response(
                status_code=resp.status,
                headers=resp.headers,
                stream=_PinnedResponseStream(resp.stream),
                extensions=resp.extensions,
            )

        async def aclose(self) -> None:
            await pool.aclose()

    return _PinnedInferenceTransport()
