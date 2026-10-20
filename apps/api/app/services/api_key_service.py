"""Bind API-key commands to the independent Identity owner."""

from __future__ import annotations

import asyncio

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from aap_identity.contract import IdentityUnavailable
from aap_identity.key_contract import KeyReply, KeyRequest, decode_key_reply
from aap_identity.key_manager import KeyManager
from aap_identity.key_postgres import KeyPostgresData
from app.config import settings
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import (
    MAX_RESPONSE_BYTES,
    IdentityGateway,
    identity_database_source,
    identity_endpoint,
)
from app.services.identity_session_data import IdentityKeySessionData


class _CommandAuthenticator:
    def __init__(self, identity: IdentityGateway, db: AsyncSession):
        self.identity = identity
        self.db = db

    async def authenticate_bound(self, token: str):
        bound = await self.identity.embedded_authenticator(self.db).authenticate_bound(
            token
        )
        if (
            bound.principal.credential_type == "api_key"
            and self.identity.uses_caller_session(self.db)
        ):
            # Record usage and release admission locks before bcrypt issuance.
            await self.db.commit()
        return bound


class IdentityKeyManagement:
    def __init__(self, db: AsyncSession, identity: IdentityGateway):
        self.db = db
        self.identity = identity

    async def execute(self, request: KeyRequest) -> KeyReply:
        try:
            async with asyncio.timeout(self.identity.timeout_seconds):
                if self.identity.identity_url is not None:
                    return await self._remote(request)
                dsn = identity_database_source(
                    settings.identity_key_database_url,
                    settings.identity_key_database_url_file,
                )
                data = KeyPostgresData(dsn) if dsn else IdentityKeySessionData(self.db)
                if dsn:
                    await data.key_policy()
                secret = (
                    self.identity.signing_secret
                    if self.identity.signing_secret is not None
                    else settings.secret_key
                )
                if not secret:
                    raise IdentityGatewayUnavailable()
                result = await KeyManager(
                    _CommandAuthenticator(self.identity, self.db), data, secret
                ).execute(request)
                if not dsn:
                    await self.db.commit()
                return result
        except (IdentityUnavailable, TimeoutError):
            raise IdentityGatewayUnavailable() from None

    async def _remote(self, request: KeyRequest) -> KeyReply:
        if not self.identity.identity_token:
            raise IdentityGatewayUnavailable()
        endpoint = identity_endpoint(
            self.identity.identity_url, "/v1/identity/api-keys/execute"
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
        return decode_key_reply(request, bytes(body))
