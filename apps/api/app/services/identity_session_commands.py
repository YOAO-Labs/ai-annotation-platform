"""Bind session commands to the independently running Identity owner."""

from __future__ import annotations

import asyncio
from typing import Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from aap_identity.contract import IdentityUnavailable, Principal
from aap_identity.session_contract import (
    SessionReply,
    SessionRequest,
    SessionFailure,
    decode_session_reply,
)
from aap_identity.session_manager import SessionManager
from aap_identity.session_postgres import SessionPostgresData
from app.config import settings
from app.services import login_failed_counter
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import (
    MAX_RESPONSE_BYTES,
    IdentityGateway,
    identity_database_source,
    identity_endpoint,
)
from app.services.identity_session_data import IdentitySessionCommandData


class SessionCommandsPort(Protocol):
    async def execute(self, request: SessionRequest) -> SessionReply: ...


class _SessionAuthenticator:
    def __init__(self, identity: IdentityGateway, db: AsyncSession):
        self.identity = identity
        self.db = db

    async def authenticate_bound(self, token: str, *, for_refresh: bool = False):
        bound = await self.identity.embedded_authenticator(self.db).authenticate_bound(
            token, for_refresh=for_refresh
        )
        if (
            bound.principal.credential_type == "api_key"
            and self.identity.uses_caller_session(self.db)
        ):
            await self.db.commit()
        return bound


class IdentitySessionCommands:
    def __init__(
        self,
        db: AsyncSession,
        identity: IdentityGateway,
        *,
        expected_principal: Principal | None = None,
    ):
        self.db = db
        self.identity = identity
        self.expected_principal = expected_principal

    async def execute(self, request: SessionRequest) -> SessionReply:
        try:
            async with asyncio.timeout(self.identity.timeout_seconds):
                if self.identity.identity_url is not None:
                    return await self._remote(request)
                dsn = identity_database_source(
                    settings.identity_session_database_url,
                    settings.identity_session_database_url_file,
                )
                data = (
                    SessionPostgresData(dsn)
                    if dsn
                    else IdentitySessionCommandData(self.db)
                )
                secret = (
                    self.identity.signing_secret
                    if self.identity.signing_secret is not None
                    else settings.secret_key
                )
                if not secret:
                    raise IdentityGatewayUnavailable()
                try:
                    result = await SessionManager(
                        _SessionAuthenticator(self.identity, self.db),
                        data,
                        secret,
                        counter=login_failed_counter,
                        expire_minutes=settings.access_token_expire_minutes,
                        email_verification_required=settings.email_verification_required,
                        login_captcha_threshold=settings.login_captcha_threshold,
                    ).execute(request)
                except SessionFailure:
                    if not dsn:
                        # Valid failed-login events are audited in this transaction.
                        await self.db.commit()
                    raise
                if not dsn:
                    await self.db.commit()
                return result
        except (IdentityUnavailable, TimeoutError):
            raise IdentityGatewayUnavailable() from None

    async def _remote(self, request: SessionRequest) -> SessionReply:
        if not self.identity.identity_token:
            raise IdentityGatewayUnavailable()
        endpoint = identity_endpoint(
            self.identity.identity_url, "/v1/identity/sessions/execute"
        )
        try:
            async with httpx.AsyncClient(
                timeout=self.identity.timeout_seconds, trust_env=False
            ) as client:
                async with client.stream(
                    "POST",
                    endpoint,
                    content=request.model_dump_json(by_alias=True, exclude_unset=True),
                    headers={
                        "Authorization": f"Bearer {self.identity.identity_token}",
                        "Content-Type": "application/json",
                    },
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
        return decode_session_reply(
            request, bytes(body), expected_principal=self.expected_principal
        )
