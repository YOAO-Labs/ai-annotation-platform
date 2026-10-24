"""HTTP client for API-to-Notifications immediate publication."""

from __future__ import annotations

import asyncio
import math
from urllib.parse import urlsplit

from pydantic import SecretStr

from .publication_contract import (
    ImmediatePublicationRequest,
    ImmediatePublicationUnavailable,
    decode_publication_reply,
    encode_publication_request,
)

_PUBLICATION_PATH = "/v1/notifications/publish"


def _service_token(value: str | SecretStr) -> str:
    token = value.get_secret_value() if isinstance(value, SecretStr) else value
    if (
        type(token) is not str
        or not token
        or len(token) > 4096
        or token != token.strip()
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in token)
    ):
        raise ValueError("Invalid notification publication service token")
    return token


def _root_origin(value: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("Invalid notification publication origin")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid notification publication origin") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
        or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= 65535)
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        raise ValueError("Invalid notification publication origin")
    return value.rstrip("/")


class RemotePublicationClient:
    """Send exactly one immediate publication request without proxy/retry fallback."""

    def __init__(
        self,
        origin: str,
        service_token: str | SecretStr,
        timeout_seconds: float = 10,
    ) -> None:
        if (
            type(timeout_seconds) not in (int, float)
            or timeout_seconds <= 0
            or timeout_seconds > 60
            or not math.isfinite(timeout_seconds)
        ):
            raise ValueError("Invalid notification publication timeout")
        self._endpoint = _root_origin(origin) + _PUBLICATION_PATH
        self._service_token = _service_token(service_token)
        self._timeout_seconds = float(timeout_seconds)

    async def publish(self, request: ImmediatePublicationRequest) -> bool:
        try:
            import httpx

            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    self._endpoint,
                    content=encode_publication_request(request),
                    headers={
                        "Authorization": f"Bearer {self._service_token}",
                        "Content-Type": "application/json",
                    },
                )
            if not 200 <= response.status_code < 300:
                raise ImmediatePublicationUnavailable()
            return decode_publication_reply(response.content).published
        except asyncio.CancelledError:
            raise
        except ImmediatePublicationUnavailable:
            raise
        except Exception:
            raise ImmediatePublicationUnavailable() from None
