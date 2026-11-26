"""Fail-closed token encryption, SSRF IP pinning, and bounded HTTP client for format adapters."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken

from yoao_labeling.format_adapter_contract import (
    FormatAdapterExportRenderRequest,
    FormatAdapterExportRenderResponse,
    FormatAdapterImportParseRequest,
    FormatAdapterImportParseResponse,
    FormatAdapterSetupResponse,
    MAX_EXPORT_PAYLOAD_BYTES,
    MAX_EXPORT_RESPONSE_BYTES,
    MAX_HEALTH_RESPONSE_BYTES,
    MAX_IMPORT_PAYLOAD_BYTES,
    MAX_IMPORT_RESPONSE_BYTES,
    MAX_SETUP_RESPONSE_BYTES,
)

_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_ALLOWLIST_ENTRIES = 256
_MAX_ALLOWLIST_ENTRY_LENGTH = 253

DnsResolver = Callable[[str], list[str]]


class FormatAdapterKeyNotConfigured(RuntimeError):
    """FORMAT_ADAPTER_ENCRYPTION_KEY is absent or invalid."""


class FormatAdapterCryptoError(RuntimeError):
    """Format adapter token ciphertext is malformed or cannot be decrypted."""


class FormatAdapterHostDenied(Exception):
    """Format adapter endpoint violates scheme, allowlist, or SSRF guard."""


class FormatAdapterAllowlistInvalid(ValueError):
    """Format adapter host allowlist contains invalid syntax."""


class FormatAdapterHttpError(RuntimeError):
    """External format adapter returned an invalid or failed HTTP response."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def _fernet(key: str | bytes) -> Fernet:
    if not key:
        raise FormatAdapterKeyNotConfigured("FORMAT_ADAPTER_ENCRYPTION_KEY is required")
    try:
        return Fernet(key.encode("ascii") if isinstance(key, str) else key)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise FormatAdapterKeyNotConfigured(
            "FORMAT_ADAPTER_ENCRYPTION_KEY is invalid"
        ) from exc


def validate_adapter_token_plaintext(token: str) -> str:
    if (
        type(token) is not str
        or not token
        or len(token) > 4096
        or token != token.strip()
        or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in token)
    ):
        raise ValueError(
            "format adapter auth_token must be 1..4096 printable ASCII characters"
        )
    return token


def encrypt_adapter_token(plaintext: str, key: str | bytes) -> str:
    validated = validate_adapter_token_plaintext(plaintext)
    return _fernet(key).encrypt(validated.encode("ascii")).decode("ascii")


def decrypt_adapter_token(ciphertext: str, key: str | bytes) -> str:
    if type(ciphertext) is not str or not ciphertext:
        raise FormatAdapterCryptoError("format adapter auth token ciphertext is empty")
    try:
        raw = ciphertext.encode("ascii")
        plaintext = _fernet(key).decrypt(raw).decode("ascii")
    except FormatAdapterKeyNotConfigured:
        raise
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise FormatAdapterCryptoError(
            "format adapter auth token could not be decrypted"
        ) from exc
    try:
        return validate_adapter_token_plaintext(plaintext)
    except ValueError as exc:
        raise FormatAdapterCryptoError(
            "decrypted format adapter auth token is malformed"
        ) from exc


def adapter_token_sha256(plaintext: str) -> str:
    validated = validate_adapter_token_plaintext(plaintext)
    return hashlib.sha256(validated.encode("ascii")).hexdigest()


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
        raise FormatAdapterAllowlistInvalid("allowlist entry is too long")
    if "://" in value:
        raise FormatAdapterAllowlistInvalid(
            "allowlist entries cannot contain a URL scheme"
        )
    if "*" in value:
        raise FormatAdapterAllowlistInvalid("use a leading dot for suffix domains")
    try:
        return str(_unwrap_ip(ipaddress.ip_address(value)))
    except ValueError:
        pass
    if "/" in value:
        try:
            return str(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise FormatAdapterAllowlistInvalid("invalid CIDR entry") from exc
    if ":" in value or "\\" in value:
        raise FormatAdapterAllowlistInvalid(
            "domain entries cannot contain ports or paths"
        )
    suffix = value.startswith(".")
    domain = (value[1:] if suffix else value).rstrip(".")
    if not domain:
        raise FormatAdapterAllowlistInvalid("domain suffix cannot be empty")
    try:
        ascii_domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise FormatAdapterAllowlistInvalid("invalid domain entry") from exc
    if len(ascii_domain) > _MAX_ALLOWLIST_ENTRY_LENGTH or any(
        not _DOMAIN_LABEL.fullmatch(label) for label in ascii_domain.split(".")
    ):
        raise FormatAdapterAllowlistInvalid("invalid domain entry")
    return f".{ascii_domain}" if suffix else ascii_domain


def normalize_adapter_allowlist(entries: Sequence[str]) -> tuple[str, ...]:
    if len(entries) > _MAX_ALLOWLIST_ENTRIES:
        raise FormatAdapterAllowlistInvalid("allowlist has too many entries")
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
        raise FormatAdapterHostDenied("format adapter DNS lookup failed") from exc
    results: list[str] = []
    seen: set[str] = set()
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.add(addr)
            results.append(addr)
    if not results:
        raise FormatAdapterHostDenied("format adapter DNS returned no addresses")
    return results


def validate_adapter_endpoint_url(
    url: str,
    *,
    host_allowlist: Sequence[str] = (),
    allow_insecure_http_hosts: Sequence[str] = (),
    dns_resolver: DnsResolver | None = None,
) -> tuple[str, str, int, str, str]:
    if type(url) is not str or not url or url != url.strip() or len(url) > 512:
        raise FormatAdapterHostDenied(
            "endpoint_url must be a valid URL up to 512 chars"
        )
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in url):
        raise FormatAdapterHostDenied("endpoint_url contains invalid characters")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise FormatAdapterHostDenied("endpoint_url is malformed") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
    ):
        raise FormatAdapterHostDenied(
            "endpoint_url must be http(s) without credentials, query, or fragment"
        )
    if port is not None and not 1 <= port <= 65535:
        raise FormatAdapterHostDenied("endpoint_url port is out of range")

    allowlist = normalize_adapter_allowlist(host_allowlist)
    insecure_hosts = normalize_adapter_allowlist(allow_insecure_http_hosts)
    if parsed.scheme == "http" and not _host_matches_entries(hostname, insecure_hosts):
        raise FormatAdapterHostDenied(
            "http:// endpoint requires explicit insecure-http host allowlist"
        )

    resolver = dns_resolver or _default_dns_resolve
    try:
        literal = _unwrap_ip(ipaddress.ip_address(hostname))
        resolved = [str(literal)]
    except ValueError:
        resolved = resolver(hostname)

    pinned_ip: str | None = None
    host_allowed = _host_matches_entries(hostname, allowlist)
    for raw_ip in resolved:
        try:
            addr = _unwrap_ip(ipaddress.ip_address(raw_ip))
        except ValueError as exc:
            raise FormatAdapterHostDenied("DNS returned invalid IP") from exc
        if _hard_blocked(addr):
            if not (
                addr.is_loopback
                and (host_allowed or _address_matches_entries(addr, allowlist))
            ):
                raise FormatAdapterHostDenied(f"endpoint IP {addr} is blocked")
        elif addr.is_private or not addr.is_global:
            if not (host_allowed or _address_matches_entries(addr, allowlist)):
                raise FormatAdapterHostDenied(
                    "private endpoint IP requires FORMAT_ADAPTER_HOST_ALLOWLIST"
                )
        if pinned_ip is None:
            pinned_ip = str(addr)
    if pinned_ip is None:
        raise FormatAdapterHostDenied("no valid IP address resolved for endpoint")

    effective_port = port or (443 if parsed.scheme == "https" else 80)
    base_path = parsed.path.rstrip("/")
    return parsed.scheme, hostname, effective_port, base_path, pinned_ip


def _pinned_target(
    scheme: str,
    hostname: str,
    port: int,
    base_path: str,
    pinned_ip: str,
    suffix: str,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    ip_host = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    header_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    host_header = header_host if port == default_port else f"{header_host}:{port}"
    url = f"{scheme}://{ip_host}:{port}{base_path}{suffix}"
    headers = {"Host": host_header}
    extensions = {"sni_hostname": hostname} if scheme == "https" else {}
    return url, headers, extensions


async def _read_bounded_body(response: httpx.Response, *, max_bytes: int) -> bytes:
    buf = bytearray()
    async for chunk in response.aiter_bytes():
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise FormatAdapterHttpError(
                f"format adapter response exceeded {max_bytes} bytes",
                status_code=502,
            )
    return bytes(buf)


class FormatAdapterHttpClient:
    def __init__(
        self,
        *,
        host_allowlist: Sequence[str] = (),
        allow_insecure_http_hosts: Sequence[str] = (),
        dns_resolver: DnsResolver | None = None,
        connect_timeout: float = 3.0,
        setup_timeout: float = 10.0,
        execute_timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._host_allowlist = tuple(host_allowlist)
        self._allow_insecure_http_hosts = tuple(allow_insecure_http_hosts)
        self._dns_resolver = dns_resolver
        self._connect_timeout = connect_timeout
        self._setup_timeout = setup_timeout
        self._execute_timeout = execute_timeout
        self._transport = transport

    def _client(self, *, read_timeout: float) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=read_timeout,
            write=read_timeout,
            pool=self._connect_timeout,
        )
        return httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        )

    def _resolve(
        self, endpoint_url: str, suffix: str
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        scheme, hostname, port, base_path, pinned_ip = validate_adapter_endpoint_url(
            endpoint_url,
            host_allowlist=self._host_allowlist,
            allow_insecure_http_hosts=self._allow_insecure_http_hosts,
            dns_resolver=self._dns_resolver,
        )
        return _pinned_target(scheme, hostname, port, base_path, pinned_ip, suffix)

    async def check_health(self, endpoint_url: str) -> bool:
        url, headers, extensions = self._resolve(endpoint_url, "/health")
        try:
            async with self._client(read_timeout=self._setup_timeout) as client:
                async with client.stream(
                    "GET", url, headers=headers, extensions=extensions
                ) as response:
                    if response.status_code != 200:
                        return False
                    body = await _read_bounded_body(
                        response, max_bytes=MAX_HEALTH_RESPONSE_BYTES
                    )
            data = json.loads(body.decode("utf-8"))
            return isinstance(data, dict) and data.get("status") == "ok"
        except Exception:
            return False

    async def fetch_setup(
        self, endpoint_url: str, auth_token: str
    ) -> FormatAdapterSetupResponse:
        url, headers, extensions = self._resolve(endpoint_url, "/setup")
        headers["Authorization"] = f"Bearer {auth_token}"
        try:
            async with self._client(read_timeout=self._setup_timeout) as client:
                async with client.stream(
                    "GET", url, headers=headers, extensions=extensions
                ) as response:
                    body = await _read_bounded_body(
                        response, max_bytes=MAX_SETUP_RESPONSE_BYTES
                    )
                    status_code = response.status_code
        except FormatAdapterHttpError:
            raise
        except httpx.HTTPError as exc:
            raise FormatAdapterHttpError(
                f"format adapter /setup request failed: {exc}", status_code=502
            ) from exc
        if status_code in {401, 403}:
            raise FormatAdapterHttpError(
                "format adapter /setup rejected bearer token", status_code=502
            )
        if status_code != 200:
            raise FormatAdapterHttpError(
                f"format adapter /setup returned HTTP {status_code}", status_code=502
            )
        try:
            payload = json.loads(body.decode("utf-8"))
            return FormatAdapterSetupResponse.model_validate(payload)
        except Exception as exc:
            raise FormatAdapterHttpError(
                f"format adapter /setup returned invalid payload: {exc}",
                status_code=422,
            ) from exc

    async def parse_import(
        self,
        endpoint_url: str,
        auth_token: str,
        request: FormatAdapterImportParseRequest,
    ) -> FormatAdapterImportParseResponse:
        encoded = request.model_dump_json().encode("utf-8")
        if len(encoded) > MAX_IMPORT_PAYLOAD_BYTES:
            raise ValueError(
                "resource_budget_exceeded:max_external_import_payload_bytes"
            )
        url, headers, extensions = self._resolve(endpoint_url, "/import/parse")
        headers["Authorization"] = f"Bearer {auth_token}"
        headers["Content-Type"] = "application/json"
        try:
            async with self._client(read_timeout=self._execute_timeout) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    content=encoded,
                    extensions=extensions,
                ) as response:
                    body = await _read_bounded_body(
                        response, max_bytes=MAX_IMPORT_RESPONSE_BYTES
                    )
                    status_code = response.status_code
        except FormatAdapterHttpError:
            raise
        except httpx.HTTPError as exc:
            raise FormatAdapterHttpError(
                f"format adapter /import/parse request failed: {exc}", status_code=502
            ) from exc
        if status_code in {400, 422}:
            detail = ""
            try:
                parsed = json.loads(body.decode("utf-8"))
                if isinstance(parsed, dict):
                    detail = str(parsed.get("error") or parsed.get("detail") or "")
            except Exception:
                detail = ""
            raise ValueError(
                detail or f"format adapter rejected input (HTTP {status_code})"
            )
        if status_code != 200:
            raise FormatAdapterHttpError(
                f"format adapter /import/parse returned HTTP {status_code}",
                status_code=502,
            )
        try:
            payload = json.loads(body.decode("utf-8"))
            return FormatAdapterImportParseResponse.model_validate(payload)
        except Exception as exc:
            raise FormatAdapterHttpError(
                f"format adapter /import/parse returned invalid payload: {exc}",
                status_code=502,
            ) from exc

    async def render_export(
        self,
        endpoint_url: str,
        auth_token: str,
        request: FormatAdapterExportRenderRequest,
    ) -> FormatAdapterExportRenderResponse:
        encoded = request.model_dump_json().encode("utf-8")
        if len(encoded) > MAX_EXPORT_PAYLOAD_BYTES:
            raise ValueError(
                "resource_budget_exceeded:max_external_export_payload_bytes"
            )
        url, headers, extensions = self._resolve(endpoint_url, "/export/render")
        headers["Authorization"] = f"Bearer {auth_token}"
        headers["Content-Type"] = "application/json"
        try:
            async with self._client(read_timeout=self._execute_timeout) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    content=encoded,
                    extensions=extensions,
                ) as response:
                    body = await _read_bounded_body(
                        response, max_bytes=MAX_EXPORT_RESPONSE_BYTES
                    )
                    status_code = response.status_code
        except FormatAdapterHttpError:
            raise
        except httpx.HTTPError as exc:
            raise FormatAdapterHttpError(
                f"format adapter /export/render request failed: {exc}", status_code=502
            ) from exc
        if status_code != 200:
            raise FormatAdapterHttpError(
                f"format adapter /export/render returned HTTP {status_code}",
                status_code=502,
            )
        try:
            payload = json.loads(body.decode("utf-8"))
            return FormatAdapterExportRenderResponse.model_validate(payload)
        except Exception as exc:
            raise FormatAdapterHttpError(
                f"format adapter /export/render returned invalid payload: {exc}",
                status_code=502,
            ) from exc
