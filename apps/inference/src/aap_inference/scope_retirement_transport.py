"""Bounded authenticated transport for backend scope-retirement commands."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from urllib.parse import urlsplit
from uuid import UUID

import httpx

_MAX_BACKEND_RETIREMENT_BYTES = 65_536
_TIMEOUT_SECONDS = 5.0
_CONTEXT_FIELDS = frozenset(
    {
        "retirement_id",
        "scope_id",
        "registry_id",
        "authority_epoch",
        "backend_incarnation",
        "challenge",
        "fence_until",
        "endpoint",
    }
)
_ENDPOINT_FIELDS = frozenset({"url", "auth_method", "auth_token"})


class BackendScopeRetirementUnavailable(Exception):
    """The selected backend did not provide a complete retirement reply."""

    def __init__(self) -> None:
        super().__init__("Backend scope-retirement endpoint unavailable")


def _private_context(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _CONTEXT_FIELDS:
        raise BackendScopeRetirementUnavailable()
    context = dict(value)
    for name in (
        "retirement_id",
        "scope_id",
        "registry_id",
        "authority_epoch",
        "backend_incarnation",
    ):
        raw = context[name]
        if type(raw) is not str:
            raise BackendScopeRetirementUnavailable()
        try:
            identifier = UUID(raw)
        except (ValueError, AttributeError):
            raise BackendScopeRetirementUnavailable() from None
        if str(identifier) != raw:
            raise BackendScopeRetirementUnavailable()
    challenge = context["challenge"]
    fence_until = context["fence_until"]
    endpoint = context["endpoint"]
    if (
        type(challenge) is not str
        or len(challenge) != 64
        or any(character not in "0123456789abcdef" for character in challenge)
        or type(fence_until) is not int
        or not 0 <= fence_until <= (1 << 63) - 1
        or not isinstance(endpoint, Mapping)
        or set(endpoint) != _ENDPOINT_FIELDS
    ):
        raise BackendScopeRetirementUnavailable()
    url = endpoint["url"]
    auth_method = endpoint["auth_method"]
    auth_token = endpoint["auth_token"]
    if (
        type(url) is not str
        or not url
        or any(character.isspace() or ord(character) < 0x20 for character in url)
        or type(auth_method) is not str
        or auth_method != "token"
        or type(auth_token) is not str
        or not auth_token
        or not auth_token.isascii()
        or any(ord(character) < 33 or ord(character) > 126 for character in auth_token)
    ):
        raise BackendScopeRetirementUnavailable()
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        raise BackendScopeRetirementUnavailable() from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise BackendScopeRetirementUnavailable()
    return context


def _command(context: Mapping[str, object]) -> tuple[bytes, str, str]:
    from aap_protocol_v2.scope_retirement import (
        SCOPE_RETIREMENT_PATH,
        SCOPE_RETIREMENT_PROTOCOL,
        decode_backend_scope_retirement_command,
    )

    payload = {
        "schema": SCOPE_RETIREMENT_PROTOCOL,
        "retirement_id": context["retirement_id"],
        "registry_id": context["registry_id"],
        "scope_id": context["scope_id"],
        "authority_epoch": context["authority_epoch"],
        "backend_incarnation": context["backend_incarnation"],
        "challenge": context["challenge"],
        "fence_until": context["fence_until"],
    }
    try:
        raw_body = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        decode_backend_scope_retirement_command(raw_body)
    except Exception:
        raise BackendScopeRetirementUnavailable() from None
    if len(raw_body) > _MAX_BACKEND_RETIREMENT_BYTES:
        raise BackendScopeRetirementUnavailable()
    endpoint = context["endpoint"]
    assert isinstance(endpoint, Mapping)
    return (
        raw_body,
        str(endpoint["url"]).rstrip("/") + SCOPE_RETIREMENT_PATH,
        str(endpoint["auth_token"]),
    )


async def _read_reply(response: httpx.Response) -> bytes:
    if response.status_code != 200:
        raise BackendScopeRetirementUnavailable()
    if (
        response.headers.get("content-encoding", "identity").strip().lower()
        != "identity"
    ):
        raise BackendScopeRetirementUnavailable()
    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if content_type != "application/json":
        raise BackendScopeRetirementUnavailable()
    declared_length = response.headers.get("content-length")
    if declared_length is not None:
        if not declared_length.isascii() or not declared_length.isdigit():
            raise BackendScopeRetirementUnavailable()
        significant_length = declared_length.lstrip("0") or "0"
        if len(significant_length) > len(str(_MAX_BACKEND_RETIREMENT_BYTES)):
            raise BackendScopeRetirementUnavailable()
        if int(significant_length) > _MAX_BACKEND_RETIREMENT_BYTES:
            raise BackendScopeRetirementUnavailable()
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > _MAX_BACKEND_RETIREMENT_BYTES:
            raise BackendScopeRetirementUnavailable()
        chunks.append(chunk)
    return b"".join(chunks)


async def execute_backend_scope_retirement(
    private_context: Mapping[str, object],
):
    """Submit one immutable control to its selected backend and validate reply."""
    context = _private_context(private_context)
    raw_body, url, auth_token = _command(context)
    from aap_protocol_v2.scope_retirement import (
        decode_backend_scope_retirement_reply,
    )

    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(_TIMEOUT_SECONDS),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    url,
                    content=raw_body,
                    headers={
                        "Authorization": f"Bearer {auth_token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    raw_reply = await _read_reply(response)
    except BackendScopeRetirementUnavailable:
        raise
    except (httpx.HTTPError, OSError, TimeoutError):
        raise BackendScopeRetirementUnavailable() from None
    try:
        return decode_backend_scope_retirement_reply(raw_reply)
    except Exception:
        raise BackendScopeRetirementUnavailable() from None


__all__ = ["BackendScopeRetirementUnavailable", "execute_backend_scope_retirement"]
