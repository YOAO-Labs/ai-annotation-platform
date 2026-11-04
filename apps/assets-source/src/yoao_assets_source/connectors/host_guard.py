"""Framework-free connector host allowlist and SSRF checks."""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse


class ConnectorHostDenied(Exception):
    """A connector target is absent from the allowlist or resolves unsafely."""


class ConnectorAllowlistInvalid(ValueError):
    """An allowlist entry has invalid syntax."""


_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_ALLOWLIST_ENTRIES = 256
_MAX_ALLOWLIST_ENTRY_LENGTH = 253


def _normalize_allowlist_entry(entry: str) -> str:
    value = entry.strip()
    if not value:
        return ""
    if len(value) > _MAX_ALLOWLIST_ENTRY_LENGTH:
        raise ConnectorAllowlistInvalid("allowlist entry is too long")
    if "://" in value:
        raise ConnectorAllowlistInvalid("allowlist entries cannot contain a URL scheme")
    if "*" in value:
        raise ConnectorAllowlistInvalid("use a leading dot for suffix domains")

    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass

    if "/" in value:
        try:
            return str(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise ConnectorAllowlistInvalid("invalid CIDR entry") from exc

    if ":" in value or "\\" in value:
        raise ConnectorAllowlistInvalid("domain entries cannot contain ports or paths")
    suffix = value.startswith(".")
    domain = (value[1:] if suffix else value).rstrip(".")
    if not domain:
        raise ConnectorAllowlistInvalid("domain suffix cannot be empty")
    try:
        ascii_domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ConnectorAllowlistInvalid("invalid domain entry") from exc
    if len(ascii_domain) > _MAX_ALLOWLIST_ENTRY_LENGTH or any(
        not _DOMAIN_LABEL.fullmatch(label) for label in ascii_domain.split(".")
    ):
        raise ConnectorAllowlistInvalid("invalid domain entry")
    return f".{ascii_domain}" if suffix else ascii_domain


def normalize_allowlist(entries: list[str]) -> list[str]:
    """Validate, normalize, and de-duplicate allowlist entries in order."""
    if len(entries) > _MAX_ALLOWLIST_ENTRIES:
        raise ConnectorAllowlistInvalid("allowlist has too many entries")
    normalized: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        value = _normalize_allowlist_entry(entry)
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)
    return normalized


def extract_host(endpoint_or_host: str) -> str:
    """Extract a host from a URL or bare host[:port] value."""
    raw = (endpoint_or_host or "").strip()
    if not raw:
        raise ConnectorHostDenied("connector target is empty")
    if "://" in raw:
        host = urlparse(raw).hostname
        if not host:
            raise ConnectorHostDenied("connector target has no host")
        return host
    if raw.startswith("[") and "]" in raw:
        return raw[1 : raw.index("]")]
    return raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw


def _resolve_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ConnectorHostDenied("connector DNS lookup failed") from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.add(address)
            addresses.append(ipaddress.ip_address(address))
    if not addresses:
        raise ConnectorHostDenied("connector DNS returned no addresses")
    return addresses


def _hard_blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


def _host_matches_name(host: str, allowlist: list[str]) -> bool:
    candidate = host.lower().rstrip(".")
    for entry in allowlist:
        if not entry or "/" in entry:
            continue
        try:
            ipaddress.ip_address(entry)
        except ValueError:
            if entry.startswith("."):
                if candidate == entry[1:] or candidate.endswith(entry):
                    return True
            elif candidate == entry:
                return True
    return False


def _address_matches_network(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    allowlist: list[str],
) -> bool:
    for entry in allowlist:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if address.version == network.version and address in network:
            return True
    return False


def resolve_allowed_ips(host_or_endpoint: str, allowlist: list[str]) -> list[str]:
    """Return the exact validated addresses for clients that can pin a socket."""
    if not allowlist:
        raise ConnectorHostDenied("connector host allowlist is empty")
    host = extract_host(host_or_endpoint)
    name_allowed = _host_matches_name(host, allowlist)
    addresses = _resolve_ips(host)
    for address in addresses:
        if _hard_blocked(address):
            raise ConnectorHostDenied(
                "connector target resolves to a restricted address"
            )
        if _address_matches_network(address, allowlist):
            continue
        if name_allowed and address.is_global:
            continue
        raise ConnectorHostDenied("connector target is not allowlisted")
    return [str(address) for address in addresses]


def assert_host_allowed(host_or_endpoint: str, allowlist: list[str]) -> None:
    """Reject unlisted, loopback, link-local, multicast, or reserved targets."""
    resolve_allowed_ips(host_or_endpoint, allowlist)
