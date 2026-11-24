"""SSRF host guard and connection-time IP pinning for external notification channels."""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_ALLOWLIST_ENTRIES = 256
_MAX_ALLOWLIST_ENTRY_LENGTH = 253

DnsResolver = Callable[[str], list[str]]


class ChannelHostDenied(Exception):
    """Notification channel endpoint violates scheme, allowlist, or SSRF guard."""


class ChannelAllowlistInvalid(ValueError):
    """Notification channel host allowlist contains invalid syntax."""


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
        raise ChannelAllowlistInvalid("allowlist entry is too long")
    if "://" in value:
        raise ChannelAllowlistInvalid("allowlist entries cannot contain a URL scheme")
    if "*" in value:
        raise ChannelAllowlistInvalid("use a leading dot for suffix domains")
    try:
        return str(_unwrap_ip(ipaddress.ip_address(value)))
    except ValueError:
        pass
    if "/" in value:
        try:
            return str(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise ChannelAllowlistInvalid("invalid CIDR entry") from exc
    if ":" in value or "\\" in value:
        raise ChannelAllowlistInvalid("domain entries cannot contain ports or paths")
    suffix = value.startswith(".")
    domain = (value[1:] if suffix else value).rstrip(".")
    if not domain:
        raise ChannelAllowlistInvalid("domain suffix cannot be empty")
    try:
        ascii_domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ChannelAllowlistInvalid("invalid domain entry") from exc
    if len(ascii_domain) > _MAX_ALLOWLIST_ENTRY_LENGTH or any(
        not _DOMAIN_LABEL.fullmatch(label) for label in ascii_domain.split(".")
    ):
        raise ChannelAllowlistInvalid("invalid domain entry")
    return f".{ascii_domain}" if suffix else ascii_domain


def normalize_channel_allowlist(entries: Sequence[str]) -> tuple[str, ...]:
    if len(entries) > _MAX_ALLOWLIST_ENTRIES:
        raise ChannelAllowlistInvalid("allowlist has too many entries")
    normalized: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        value = _normalize_entry(entry)
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


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
    return (
        unwrapped.is_loopback
        or unwrapped.is_link_local
        or unwrapped.is_multicast
        or unwrapped.is_unspecified
        or unwrapped.is_reserved
    )


def _default_dns_resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ChannelHostDenied("channel DNS lookup failed") from exc
    results: list[str] = []
    seen: set[str] = set()
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.add(addr)
            results.append(addr)
    if not results:
        raise ChannelHostDenied("channel DNS returned no addresses")
    return results


def validate_channel_endpoint_url(
    url: str,
    *,
    allow_insecure_http_hosts: Sequence[str] = (),
) -> str:
    """Validate URL shape and scheme policy (HTTPS by default; HTTP only on allowlist)."""
    if type(url) is not str or not url or url != url.strip() or len(url) > 512:
        raise ChannelHostDenied("invalid channel endpoint URL")
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in url):
        raise ChannelHostDenied("channel endpoint URL contains illegal characters")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ChannelHostDenied("invalid channel endpoint URL") from exc
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
        raise ChannelHostDenied(
            "channel endpoint must be an HTTP(S) base URL without credentials, query, or fragment"
        )
    if parsed.scheme == "http":
        insecure_allowed = normalize_channel_allowlist(allow_insecure_http_hosts)
        if not insecure_allowed or not _host_matches_entries(
            hostname, insecure_allowed
        ):
            raise ChannelHostDenied(
                "insecure http:// channel endpoints require explicit host allowlisting"
            )
    return url.rstrip("/")


def resolve_pinned_channel_ips(
    host_or_url: str,
    *,
    allow_insecure_http_hosts: Sequence[str] = (),
    host_allowlist: Sequence[str] = (),
    resolver: DnsResolver | None = None,
) -> tuple[str, list[str]]:
    """Resolve and validate every IP at connection time, returning (host, pinned_ips)."""
    if "://" in host_or_url:
        normalized_url = validate_channel_endpoint_url(
            host_or_url, allow_insecure_http_hosts=allow_insecure_http_hosts
        )
        host = urlsplit(normalized_url).hostname or ""
    else:
        host = host_or_url.strip()
        if not host:
            raise ChannelHostDenied("empty channel host")

    normalized_allowlist = normalize_channel_allowlist(host_allowlist)
    host_allowed = _host_matches_entries(host, normalized_allowlist)

    try:
        raw_ips = [str(_unwrap_ip(ipaddress.ip_address(host)))]
    except ValueError:
        raw_ips = (resolver or _default_dns_resolve)(host)

    pinned: list[str] = []
    for raw_ip in raw_ips:
        try:
            addr = _unwrap_ip(ipaddress.ip_address(raw_ip))
        except ValueError as exc:
            raise ChannelHostDenied("channel DNS returned an invalid address") from exc
        if _hard_blocked(addr):
            raise ChannelHostDenied(
                "channel target resolves to a forbidden address (loopback/link-local/reserved)"
            )
        if addr.is_private or not addr.is_global:
            if not (
                host_allowed or _address_matches_entries(addr, normalized_allowlist)
            ):
                raise ChannelHostDenied(
                    "channel target resolves to a private network not in NOTIFICATION_CHANNEL_HOST_ALLOWLIST"
                )
        pinned.append(str(addr))
    if not pinned:
        raise ChannelHostDenied("channel target resolved to no usable addresses")
    return host, pinned


def build_pinned_network_backend(
    *,
    host_allowlist: Sequence[str] = (),
    resolver: DnsResolver | None = None,
):
    """Build an httpcore AsyncNetworkBackend that pins TCP dials to validated IPs."""
    import httpcore

    class _PinnedChannelBackend(httpcore.AsyncNetworkBackend):
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
            _, ips = resolve_pinned_channel_ips(
                host,
                host_allowlist=host_allowlist,
                resolver=resolver,
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
            raise ChannelHostDenied("channel target could not be reached")

        async def connect_unix_socket(
            self,
            path: str,
            timeout: float | None = None,
            socket_options=None,
        ):
            raise ChannelHostDenied(
                "unix sockets are forbidden for notification channels"
            )

        async def sleep(self, seconds: float) -> None:
            await self._inner.sleep(seconds)

    return _PinnedChannelBackend()
