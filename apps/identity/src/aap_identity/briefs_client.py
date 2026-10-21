"""Public Identity Briefs client for selected local or HTTP owners."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from .briefs_contract import (
    BriefsFailure,
    BriefsReply,
    BriefsRequest,
    decode_briefs_request,
    decode_briefs_reply,
)
from .briefs_manager import BriefsData, BriefsManager
from .contract import IdentityUnavailable

_BRIEFS_PATH = "/v1/identity/briefs/execute"


class _UnusedBriefsData:
    """Sentinel for BriefsManager's no-I/O empty-request path."""

    def transaction(self):
        raise AssertionError("empty Briefs request must not open a transaction")


class BriefsClient:
    """Read validated UserBrief records from the explicitly selected owner.

    A nonempty ``identity_url`` selects HTTP ahead of injected SQL data. HTTP
    failures never fall back to SQL. When HTTP is not selected, ``data`` or
    ``data_factory`` supplies the portable BriefsManager data port. A data
    adapter may expose an async ``briefs_policy()`` method to validate a
    function-only PostgreSQL binding before its first command; borrowed-session
    adapters need not expose that method.

    Provider values are retained as supplied and inspected only for nonempty
    requests. In particular, a lazy data factory can resolve secret files at the
    profile-read phase without making empty Project lists depend on Identity.
    ``transport`` is an optional httpx transport injection point for tests and
    explicitly managed clients.
    """

    def __init__(
        self,
        data: BriefsData | None = None,
        *,
        data_factory: Callable[[], BriefsData | Awaitable[BriefsData]] | None = None,
        identity_url: str | None = None,
        identity_token: str | None = None,
        timeout_seconds: float = 2.0,
        transport: Any | None = None,
    ) -> None:
        self._data = data
        self._data_factory = data_factory
        self._identity_url = identity_url
        self._identity_token = identity_token
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def execute(self, request: BriefsRequest) -> BriefsReply:
        try:
            request = decode_briefs_request(
                request.model_dump(mode="json", by_alias=True)
            )
        except BriefsFailure:
            raise IdentityUnavailable() from None
        except (AttributeError, TypeError, ValueError, UnicodeError):
            raise IdentityUnavailable() from None

        if not request.payload.user_ids:
            return await BriefsManager(_UnusedBriefsData()).execute(request)

        try:
            async with asyncio.timeout(self._timeout_seconds):
                # Match IdentityGateway's current selector: an empty URL means no
                # remote owner; any nonempty URL selects HTTP and must not fall back.
                if self._identity_url:
                    return await self._execute_http(request)
                return await self._execute_data(request)
        except IdentityUnavailable:
            raise
        except (TimeoutError, TypeError, ValueError):
            raise IdentityUnavailable() from None

    async def _execute_data(self, request: BriefsRequest) -> BriefsReply:
        try:
            if self._data is not None and self._data_factory is not None:
                raise IdentityUnavailable()

            data = self._data
            if self._data_factory is not None:
                data = self._data_factory()
                if inspect.isawaitable(data):
                    data = await data
            if data is None:
                raise IdentityUnavailable()

            policy = getattr(data, "briefs_policy", None)
            if callable(policy):
                await policy()
            return await BriefsManager(data).execute(request)
        except IdentityUnavailable:
            raise
        except BriefsFailure:
            raise IdentityUnavailable() from None
        except (OSError, TimeoutError, ValueError, TypeError, UnicodeError):
            raise IdentityUnavailable() from None

    async def _execute_http(self, request: BriefsRequest) -> BriefsReply:
        try:
            import httpx
        except ImportError:
            raise IdentityUnavailable() from None

        try:
            endpoint = _identity_endpoint(self._identity_url)
            if not self._identity_token:
                raise IdentityUnavailable()
            payload = json.dumps(
                request.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )

            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                trust_env=False,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    "POST",
                    endpoint,
                    content=payload,
                    headers={
                        "Authorization": f"Bearer {self._identity_token}",
                        "Content-Type": "application/json",
                    },
                ) as response:
                    if response.status_code != 200:
                        raise IdentityUnavailable()
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        body.extend(chunk)

            return decode_briefs_reply(request, bytes(body))
        except IdentityUnavailable:
            raise
        except BriefsFailure:
            raise IdentityUnavailable() from None
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
            UnicodeError,
        ):
            raise IdentityUnavailable() from None


def _identity_endpoint(identity_url: str | None) -> str:
    """Validate an Identity base URL with the existing API gateway rules."""

    try:
        target = urlsplit(identity_url)
        if (
            target.scheme not in {"http", "https"}
            or not target.hostname
            or target.username is not None
            or target.password is not None
            or target.query
            or target.fragment
        ):
            raise IdentityUnavailable()
        _ = target.port
        return f"{identity_url.rstrip('/')}{_BRIEFS_PATH}"
    except IdentityUnavailable:
        raise
    except (AttributeError, TypeError, ValueError):
        raise IdentityUnavailable() from None
