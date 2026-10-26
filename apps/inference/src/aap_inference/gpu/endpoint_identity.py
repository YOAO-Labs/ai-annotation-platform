"""Private endpoint identity normalization for GPU lifecycle alias checks."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

_UNRESERVED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~"
)
_HEX = frozenset("0123456789abcdefABCDEF")


def _normalize_percent_escapes(value: str) -> str:
    normalized: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "%":
            normalized.append(char)
            index += 1
            continue
        if (
            index + 2 >= len(value)
            or value[index + 1] not in _HEX
            or value[index + 2] not in _HEX
        ):
            raise ValueError("invalid URL percent escape")
        byte = int(value[index + 1 : index + 3], 16)
        decoded = chr(byte)
        normalized.append(decoded if decoded in _UNRESERVED else f"%{byte:02X}")
        index += 3
    return "".join(normalized)


def _canonical_ipv4_host(host: str) -> str | None:
    parts = host.split(".")
    if parts and parts[-1] == "":
        parts.pop()
    if not parts or len(parts) > 4:
        return None

    numbers: list[int] = []
    for part in parts:
        if not part:
            return None
        base = 10
        digits = part
        if len(part) >= 2 and part[:2].lower() == "0x":
            base = 16
            digits = part[2:]
        elif len(part) > 1 and part[0] == "0":
            base = 8
            digits = part[1:]
        if not digits:
            digits = "0"
        allowed = (
            frozenset("0123456789")
            if base == 10
            else frozenset("01234567")
            if base == 8
            else _HEX
        )
        if any(char not in allowed for char in digits):
            return None
        numbers.append(int(digits, base))

    if any(number > 255 for number in numbers[:-1]):
        raise ValueError("invalid IPv4 URL host")
    final_bits = 8 * (5 - len(numbers))
    if numbers[-1] >= 1 << final_bits:
        raise ValueError("invalid IPv4 URL host")
    value = numbers[-1]
    for index, number in enumerate(numbers[:-1]):
        value += number << (8 * (3 - index))
    return str(ipaddress.IPv4Address(value))


def _canonical_host(host: str) -> str:
    if ":" in host:
        return ipaddress.IPv6Address(host).compressed
    numeric_host = _canonical_ipv4_host(host)
    if numeric_host is not None:
        return numeric_host
    stripped = host.rstrip(".")
    if not stripped:
        raise ValueError("URL host is empty")
    try:
        return stripped.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("URL host is invalid") from None


def canonical_gpu_backend_endpoint(url: str) -> str:
    """Return a stable HTTP endpoint identity without exposing it in diagnostics."""

    if type(url) is not str:
        raise ValueError("URL must be text")
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("GPU backend endpoint URL is invalid")
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("GPU backend endpoint URL is invalid") from None
    if not host:
        raise ValueError("GPU backend endpoint URL is invalid")
    canonical_host = _canonical_host(host.lower())
    host_display = f"[{canonical_host}]" if ":" in canonical_host else canonical_host
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("GPU backend endpoint port is invalid")
    if port == (80 if scheme == "http" else 443):
        port = None
    authority = host_display if port is None else f"{host_display}:{port}"
    if parsed.username is not None:
        userinfo = _normalize_percent_escapes(parsed.username)
        if parsed.password is not None:
            userinfo += ":" + _normalize_percent_escapes(parsed.password)
        authority = f"{userinfo}@{authority}"

    path = _normalize_percent_escapes(parsed.path or "/")
    if not path.startswith("/"):
        path = "/" + path
    segments = path.split("/")[1:]
    trailing = path.endswith("/") or (bool(segments) and segments[-1] in {".", ".."})
    normalized_segments: list[str] = []
    for segment in segments:
        if segment == ".":
            continue
        if segment == "..":
            if normalized_segments:
                normalized_segments.pop()
            continue
        normalized_segments.append(segment)
    normalized_path = "/" + "/".join(normalized_segments)
    if trailing and normalized_path != "/":
        normalized_path += "/"
    normalized_path = normalized_path.rstrip("/") or "/"
    query = _normalize_percent_escapes(parsed.query)
    return f"{scheme}://{authority}{normalized_path}" + (f"?{query}" if query else "")


__all__ = ["canonical_gpu_backend_endpoint"]
