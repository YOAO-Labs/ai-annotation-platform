"""One admission client for embedded and remote Identity runtimes."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import httpx
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


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


class IdentityCredentialRejected(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class IdentityGatewayUnavailable(Exception):
    """Identity could not return a trustworthy admission result."""


def _secret_value(value) -> str | None:
    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    return getter() if callable(getter) else str(value)


def _postgres_dsn(value: str) -> str:
    try:
        return (
            make_url(value)
            .set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )
    except Exception:
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
        if self.identity_url is not None:
            principal = await self._authenticate_remote(token)
        else:
            principal = await self._authenticate_embedded(token, db=db)
        return principal

    async def _authenticate_remote(self, token: str):
        from aap_identity.contract import (
            IdentityFailure,
            IdentityUnavailable,
            decode_principal_reply,
        )

        if not self.identity_token:
            raise IdentityGatewayUnavailable()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.identity_url}/v1/identity/authenticate",
                    json={"token": token},
                    headers={"Authorization": f"Bearer {self.identity_token}"},
                )
        except httpx.HTTPError:
            raise IdentityGatewayUnavailable() from None
        if response.status_code != 200 or len(response.content) > 65536:
            raise IdentityGatewayUnavailable()
        try:
            principal = decode_principal_reply(response.content)
        except IdentityFailure as exc:
            self._raise_failure(exc.code)
        except IdentityUnavailable:
            raise IdentityGatewayUnavailable() from None
        return principal

    async def _authenticate_embedded(self, token: str, *, db: AsyncSession | None):
        from aap_identity.authenticator import Authenticator
        from aap_identity.contract import IdentityFailure, IdentityUnavailable
        from aap_identity.postgres import PostgresData

        secret = self.signing_secret or _secret_value(settings.secret_key)
        try:
            if self.uses_caller_session(db):
                from app.services.identity_session_data import IdentitySessionData

                data = IdentitySessionData(db)
            else:
                dsn = _secret_value(self.auth_database_url)
                if not dsn and self.auth_database_url_file:
                    dsn = (
                        Path(self.auth_database_url_file)
                        .read_text(encoding="utf-8")
                        .strip()
                    )
                    if not dsn:
                        raise IdentityGatewayUnavailable()
                if not dsn:
                    dsn = settings.database_url
                data = PostgresData(_postgres_dsn(dsn))
            return await Authenticator(data, secret=secret).authenticate(token)
        except IdentityFailure as exc:
            self._raise_failure(exc.code)
        except IdentityGatewayUnavailable:
            raise
        except IdentityUnavailable:
            raise IdentityGatewayUnavailable() from None
        except (OSError, UnicodeError):
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
