"""Bind self-account and presence commands to the configured Identity owner."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Protocol

import httpx

from aap_identity.account_contract import (
    AccountReply,
    AccountRequest,
    decode_account_reply,
)
from aap_identity.account_manager import AccountManager
from aap_identity.account_postgres import AccountPostgresData
from aap_identity.contract import IdentityUnavailable, Principal
from aap_identity.presence_contract import (
    PresenceRequest,
    decode_presence_reply,
    decode_presence_request,
)
from aap_identity.presence_postgres import PresencePostgresData
from app.config import settings
from app.services.identity_account_data import (
    IdentityAccountCommandData,
    IdentityPresenceCommandData,
)
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import (
    IdentityGateway,
    identity_database_source,
    identity_endpoint,
)


class AccountCommandsPort(Protocol):
    async def execute(self, request: AccountRequest) -> AccountReply: ...


def account_json(value) -> str:
    """Encode metadata without Pydantic's lossy nonfinite-to-null conversion."""

    def encode(item):
        if isinstance(item, (datetime, uuid.UUID)):
            return item.isoformat() if isinstance(item, datetime) else str(item)
        raise TypeError("Unsupported Identity account value")

    return json.dumps(
        value,
        default=encode,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    )


class _AccountAuthenticator:
    def __init__(self, identity, db):
        self.identity = identity
        self.db = db

    async def authenticate_bound(self, token):
        verified = await self.identity.embedded_authenticator(
            self.db
        ).authenticate_bound(token)
        if (
            verified.principal.credential_type == "api_key"
            and self.identity.uses_caller_session(self.db)
        ):
            # Admission's last-used write cannot retain a key-before-User lock.
            await self.db.commit()
        return verified


async def _remote_json(identity, path: str, payload: str) -> bytes:
    if not identity.identity_token:
        raise IdentityGatewayUnavailable()
    endpoint = identity_endpoint(identity.identity_url, path)
    try:
        async with httpx.AsyncClient(
            timeout=identity.timeout_seconds, trust_env=False
        ) as client:
            async with client.stream(
                "POST",
                endpoint,
                content=payload,
                headers={
                    "Authorization": f"Bearer {identity.identity_token}",
                    "Content-Type": "application/json",
                },
            ) as response:
                if response.status_code != 200:
                    raise IdentityGatewayUnavailable()
                # Preferences have no global legacy byte ceiling. The existing
                # admission/key response cap cannot narrow this account contract.
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    body.extend(chunk)
                return bytes(body)
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        raise IdentityGatewayUnavailable() from None


class IdentityAccountCommands:
    def __init__(
        self,
        db,
        identity: IdentityGateway,
        *,
        expected_principal: Principal | None = None,
    ):
        self.db = db
        self.identity = identity
        self.expected_principal = expected_principal

    async def execute(self, request: AccountRequest) -> AccountReply:
        try:
            async with asyncio.timeout(self.identity.timeout_seconds):
                if self.identity.identity_url is not None:
                    raw = await _remote_json(
                        self.identity,
                        "/v1/identity/accounts/execute",
                        account_json(request.model_dump(mode="python", by_alias=True)),
                    )
                    return decode_account_reply(
                        request, raw, expected_principal=self.expected_principal
                    )
                dsn = identity_database_source(
                    settings.identity_account_database_url,
                    settings.identity_account_database_url_file,
                )
                data = (
                    AccountPostgresData(dsn)
                    if dsn
                    else IdentityAccountCommandData(self.db)
                )
                if dsn:
                    await data.account_policy()
                secret = (
                    self.identity.signing_secret
                    if self.identity.signing_secret is not None
                    else settings.secret_key
                )
                if not secret:
                    raise IdentityGatewayUnavailable()
                return await AccountManager(
                    _AccountAuthenticator(self.identity, self.db), data, secret
                ).execute(request)
        except (IdentityUnavailable, TimeoutError, ValueError, TypeError, UnicodeError):
            raise IdentityGatewayUnavailable() from None


class IdentityPresenceCommands:
    def __init__(self, db, identity: IdentityGateway):
        self.db = db
        self.identity = identity

    async def mark_inactive_accounts_offline(self, cutoff) -> int:
        try:
            request: PresenceRequest = decode_presence_request(
                {"schema": "aap-identity-presence/v1", "cutoff": cutoff.isoformat()}
            )
            async with asyncio.timeout(self.identity.timeout_seconds):
                if self.identity.identity_url is not None:
                    raw = await _remote_json(
                        self.identity,
                        "/v1/identity/presence/mark-inactive",
                        request.model_dump_json(by_alias=True),
                    )
                    return decode_presence_reply(raw).affected
                dsn = identity_database_source(
                    settings.identity_presence_database_url,
                    settings.identity_presence_database_url_file,
                )
                data = (
                    PresencePostgresData(dsn)
                    if dsn
                    else IdentityPresenceCommandData(self.db)
                )
                result = await data.mark_inactive_accounts_offline(cutoff)
                affected = result.affected if hasattr(result, "affected") else result
                return decode_presence_reply(
                    {"schema": "aap-identity-presence/v1", "affected": affected}
                ).affected
        except (IdentityUnavailable, TimeoutError, ValueError, TypeError):
            raise IdentityGatewayUnavailable() from None
