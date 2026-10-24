"""Small HTTP client for a separately deployed Identity admission owner."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from pydantic import ValidationError

from .actor_status_contract import (
    ActorStatus,
    ActorStatusRequest,
    decode_actor_status_reply,
)
from .contract import (
    AuthenticateRequest,
    IdentityFailure,
    IdentityUnavailable,
    Principal,
    decode_principal_reply,
)

try:
    import httpx
except ImportError:  # The HTTP client remains an optional package extra.
    httpx = None

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_CHUNK_SIZE = 65_536


def _endpoint(origin: str, path: str) -> str:
    try:
        parsed = urlsplit(origin)
        if (
            origin != origin.strip()
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "?" in origin
            or "#" in origin
        ):
            raise IdentityUnavailable()
        _ = parsed.port
        return f"{origin.rstrip('/')}{path}"
    except (ValueError, AttributeError):
        raise IdentityUnavailable() from None


class RemoteAdmissionClient:
    def __init__(
        self, *, origin: str, service_token: str, timeout_seconds: float = 2.0
    ) -> None:
        if (
            not isinstance(origin, str)
            or not isinstance(service_token, str)
            or not service_token
            or timeout_seconds <= 0
        ):
            raise IdentityUnavailable()
        self._origin = origin.rstrip("/")
        self._service_token = service_token
        self._timeout_seconds = timeout_seconds

    async def _post(self, path: str, payload: dict) -> bytes:
        if httpx is None:
            raise IdentityUnavailable() from None

        try:
            endpoint = _endpoint(self._origin, path)
            async with asyncio.timeout(self._timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=self._timeout_seconds, trust_env=False
                ) as client:
                    async with client.stream(
                        "POST",
                        endpoint,
                        json=payload,
                        headers={"Authorization": f"Bearer {self._service_token}"},
                    ) as response:
                        if response.status_code != 200:
                            raise IdentityUnavailable()
                        body = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=_CHUNK_SIZE):
                            body.extend(chunk)
                            if len(body) > MAX_RESPONSE_BYTES:
                                raise IdentityUnavailable()
                        return bytes(body)
        except IdentityUnavailable:
            raise
        except (httpx.HTTPError, httpx.InvalidURL, TimeoutError, ValueError):
            raise IdentityUnavailable() from None

    async def authenticate(self, token: str) -> Principal:
        try:
            request = AuthenticateRequest.model_validate({"token": token})
        except (ValidationError, TypeError):
            raise IdentityFailure("credential_invalid") from None
        payload = await self._post(
            "/v1/identity/authenticate", request.model_dump(mode="json")
        )
        return decode_principal_reply(payload)

    async def actor_status(self, actor_id) -> ActorStatus | None:
        try:
            request = ActorStatusRequest(
                schema="aap-identity-actor-status/v1", actor_id=actor_id
            )
        except (ValidationError, TypeError, ValueError):
            raise IdentityUnavailable() from None
        payload = await self._post(
            "/v1/identity/actor-status", request.model_dump(mode="json", by_alias=True)
        )
        return decode_actor_status_reply(payload, request)
