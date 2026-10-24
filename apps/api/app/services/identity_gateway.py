"""One admission client for embedded and remote Identity runtimes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

import httpx
from aap_identity_admission.client import RemoteAdmissionClient
from aap_identity_admission.contract import (
    IdentityFailure,
    IdentityUnavailable,
    Principal,
)
from aap_identity.actor_contract import (
    ACTOR_PROTOCOL,
    ActorContext,
    ActorContextRequest,
    decode_actor_reply,
)
from aap_identity.actor_reader import ActorContextReader
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.identity_contracts import (
    IdentityCredentialRejected,
    IdentityGatewayUnavailable,
)


_REJECTED_CODES = frozenset(
    {
        "credential_invalid",
        "credential_revoked",
        "generation_outdated",
        "token_expired",
        "user_inactive",
        "invalid_account_role",
    }
)
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def _secret_value(value) -> str | None:
    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    return getter() if callable(getter) else str(value)


def identity_postgres_dsn(value: str) -> str:
    try:
        return (
            make_url(value)
            .set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )
    except Exception:
        raise IdentityGatewayUnavailable() from None


def identity_database_source(value: str | None, path: str | None) -> str | None:
    if value and path:
        raise IdentityGatewayUnavailable()
    if path:
        try:
            with Path(path).open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise IdentityGatewayUnavailable()
            value = raw.decode().strip()
            if not value:
                raise IdentityGatewayUnavailable()
        except (OSError, UnicodeError):
            raise IdentityGatewayUnavailable() from None
    return identity_postgres_dsn(value) if value else None


def identity_endpoint(origin: str | None, path: str) -> str:
    try:
        target = urlsplit(origin)
        if (
            target.scheme not in {"http", "https"}
            or not target.hostname
            or target.username is not None
            or target.password is not None
            or target.query
            or target.fragment
        ):
            raise IdentityGatewayUnavailable()
        _ = target.port
        return f"{origin.rstrip('/')}{path}"
    except (ValueError, AttributeError):
        raise IdentityGatewayUnavailable() from None


class IdentityGateway:
    def __init__(
        self,
        *,
        identity_url: str | None = None,
        identity_token: str | None = None,
        timeout_seconds: float = 2.0,
        auth_database_url: str | None = None,
        auth_database_url_file: str | None = None,
        signing_secret: str | None = None,
    ) -> None:
        self.identity_url = identity_url.rstrip("/") if identity_url else None
        self.identity_token = identity_token
        self.timeout_seconds = timeout_seconds
        self.auth_database_url = auth_database_url
        self.auth_database_url_file = auth_database_url_file
        self.signing_secret = signing_secret

    def uses_caller_session(self, db: AsyncSession | None) -> bool:
        return (
            self.identity_url is None
            and db is not None
            and not self.auth_database_url
            and not self.auth_database_url_file
        )

    async def authenticate(self, token: str, *, db: AsyncSession | None = None):
        if not isinstance(token, str) or not 1 <= len(token) <= 16384:
            raise IdentityCredentialRejected("credential_invalid")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                if self.identity_url is not None:
                    principal = await self._authenticate_remote(token)
                else:
                    principal = await self._authenticate_embedded(token, db=db)
        except TimeoutError:
            raise IdentityGatewayUnavailable() from None
        return principal

    async def _remote_reply(self, path: str, payload: dict) -> bytes:
        if not self.identity_token:
            raise IdentityGatewayUnavailable()
        try:
            endpoint = identity_endpoint(self.identity_url, path)
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, trust_env=False
            ) as client:
                async with client.stream(
                    "POST",
                    endpoint,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.identity_token}"},
                ) as response:
                    if response.status_code != 200:
                        raise IdentityGatewayUnavailable()
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise IdentityGatewayUnavailable()
        except (httpx.HTTPError, httpx.InvalidURL, ValueError):
            raise IdentityGatewayUnavailable() from None
        return bytes(body)

    async def _authenticate_remote(self, token: str):
        try:
            return await RemoteAdmissionClient(
                origin=self.identity_url or "",
                service_token=self.identity_token or "",
                timeout_seconds=self.timeout_seconds,
            ).authenticate(token)
        except IdentityFailure as exc:
            self._raise_failure(exc.code)
        except IdentityUnavailable:
            raise IdentityGatewayUnavailable() from None

    async def actor_context(
        self, principal: Principal, *, db: AsyncSession | None = None
    ) -> ActorContext:
        request = ActorContextRequest(
            schema=ACTOR_PROTOCOL,
            actor_id=principal.actor_id,
            admitted_role=principal.role,
        )
        try:
            async with asyncio.timeout(self.timeout_seconds):
                if self.identity_url is not None:
                    payload = await self._remote_reply(
                        "/v1/identity/actor-context",
                        request.model_dump(mode="json", by_alias=True),
                    )
                    return decode_actor_reply(payload, request)
                return await ActorContextReader(self.embedded_data(db)).read(request)
        except IdentityFailure as exc:
            self._raise_failure(exc.code)
        except (IdentityUnavailable, TimeoutError):
            raise IdentityGatewayUnavailable() from None

    def embedded_data(self, db: AsyncSession | None):
        from aap_identity.postgres import PostgresData

        try:
            if self.identity_url is not None:
                raise IdentityGatewayUnavailable()
            dsn = identity_database_source(
                self.auth_database_url, self.auth_database_url_file
            )
            if self.uses_caller_session(db):
                from app.services.identity_session_data import IdentitySessionData

                data = IdentitySessionData(db)
            else:
                if not dsn:
                    dsn = identity_postgres_dsn(settings.database_url)
                data = PostgresData(dsn)
            return data
        except (OSError, UnicodeError):
            raise IdentityGatewayUnavailable() from None

    def embedded_authenticator(self, db: AsyncSession | None):
        from aap_identity.authenticator import Authenticator

        secret = (
            self.signing_secret
            if self.signing_secret is not None
            else _secret_value(settings.secret_key)
        )
        if not secret:
            raise IdentityGatewayUnavailable()
        return Authenticator(self.embedded_data(db), secret=secret)

    async def _authenticate_embedded(self, token: str, *, db: AsyncSession | None):
        from aap_identity.contract import IdentityFailure, IdentityUnavailable

        try:
            return await self.embedded_authenticator(db).authenticate(token)
        except IdentityFailure as exc:
            self._raise_failure(exc.code)
        except IdentityGatewayUnavailable:
            raise
        except IdentityUnavailable:
            raise IdentityGatewayUnavailable() from None

    @staticmethod
    def _raise_failure(code: str) -> NoReturn:
        if code in _REJECTED_CODES:
            raise IdentityCredentialRejected(code)
        raise IdentityGatewayUnavailable()


def get_identity_gateway() -> IdentityGateway:
    """Build the configured gateway without opening a connection."""

    return IdentityGateway(
        identity_url=_secret_value(getattr(settings, "identity_url", None)),
        identity_token=_secret_value(getattr(settings, "identity_token", None)),
        timeout_seconds=float(getattr(settings, "identity_timeout_seconds", 2.0)),
        auth_database_url=_secret_value(
            getattr(settings, "identity_auth_database_url", None)
        ),
        auth_database_url_file=_secret_value(
            getattr(settings, "identity_auth_database_url_file", None)
        ),
        signing_secret=_secret_value(settings.secret_key),
    )
