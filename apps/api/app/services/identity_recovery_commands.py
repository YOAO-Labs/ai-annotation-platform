"""Bind public credential workflows to their configured Identity owner."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

import httpx

from aap_identity.contract import IdentityUnavailable
from aap_identity.recovery_contract import (
    RecoveryReply,
    RecoveryRequest,
    decode_recovery_reply,
)
from aap_identity.recovery_crypto import mail_encryption_id, mail_key_from_secret
from aap_identity.recovery_manager import RecoveryManager
from aap_identity.recovery_postgres import RecoveryPostgresData
from aap_identity.registration_contract import (
    REGISTRATION_STATUS_PROTOCOL,
    RegistrationStatusRequest,
    decode_registration_status_reply,
)
from aap_identity.registration_reader import RegistrationStatusReader
from app.config import settings
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import (
    MAX_RESPONSE_BYTES,
    IdentityGateway,
    identity_database_source,
    identity_endpoint,
)
from app.services.identity_recovery_data import IdentityRecoveryCommandData


class RecoveryCommandsPort(Protocol):
    async def execute(self, request: RecoveryRequest) -> RecoveryReply: ...

    async def registration_status(
        self, *, deployment_default: bool = False
    ) -> bool: ...


def recovery_mail_key(secret: str) -> str:
    value = settings.identity_recovery_mail_key
    path = settings.identity_recovery_mail_key_file
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
    key = value if value else mail_key_from_secret(secret)
    try:
        mail_encryption_id(key)
    except (ValueError, TypeError, UnicodeError):
        raise IdentityGatewayUnavailable() from None
    return key


class IdentityRecoveryCommands:
    def __init__(self, db, identity: IdentityGateway):
        self.db = db
        self.identity = identity

    async def registration_status(self, *, deployment_default: bool = False) -> bool:
        request = RegistrationStatusRequest(
            schema=REGISTRATION_STATUS_PROTOCOL,
            operation="read_deployment_default"
            if deployment_default
            else "read_status",
        )
        try:
            async with asyncio.timeout(self.identity.timeout_seconds):
                if self.identity.identity_url is not None:
                    raw = await self.identity._remote_reply(
                        "/v1/identity/recovery/registration-status",
                        request.model_dump(mode="json", by_alias=True),
                    )
                    reply = decode_registration_status_reply(raw, request)
                else:
                    dsn = identity_database_source(
                        settings.identity_recovery_database_url,
                        settings.identity_recovery_database_url_file,
                    )
                    data = (
                        RecoveryPostgresData(dsn)
                        if dsn
                        else IdentityRecoveryCommandData(self.db)
                    )
                    reply = await RegistrationStatusReader(
                        data,
                        allow_open_registration=settings.allow_open_registration,
                        frontend_base_url=settings.frontend_base_url,
                    ).read(request)
                return reply.data.allow_open_registration
        except (IdentityUnavailable, TimeoutError, ValueError, TypeError, UnicodeError):
            raise IdentityGatewayUnavailable() from None

    async def execute(self, request: RecoveryRequest) -> RecoveryReply:
        try:
            async with asyncio.timeout(self.identity.timeout_seconds):
                if self.identity.identity_url is not None:
                    return await self._remote(request)
                dsn = identity_database_source(
                    settings.identity_recovery_database_url,
                    settings.identity_recovery_database_url_file,
                )
                data = (
                    RecoveryPostgresData(dsn)
                    if dsn
                    else IdentityRecoveryCommandData(self.db)
                )
                if dsn:
                    await data.recovery_policy()
                secret = (
                    self.identity.signing_secret
                    if self.identity.signing_secret is not None
                    else settings.secret_key
                )
                if not secret:
                    raise IdentityGatewayUnavailable()
                return await RecoveryManager(
                    data,
                    secret,
                    recovery_mail_key(secret),
                    email_verification_required=settings.email_verification_required,
                    allow_open_registration=settings.allow_open_registration,
                    frontend_base_url=settings.frontend_base_url,
                    expire_minutes=settings.access_token_expire_minutes,
                ).execute(request)
        except (IdentityUnavailable, TimeoutError, ValueError, TypeError, UnicodeError):
            raise IdentityGatewayUnavailable() from None

    async def _remote(self, request):
        if not self.identity.identity_token:
            raise IdentityGatewayUnavailable()
        endpoint = identity_endpoint(
            self.identity.identity_url, "/v1/identity/recovery/execute"
        )
        try:
            async with httpx.AsyncClient(
                timeout=self.identity.timeout_seconds, trust_env=False
            ) as client:
                async with client.stream(
                    "POST",
                    endpoint,
                    content=request.model_dump_json(by_alias=True),
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
        return decode_recovery_reply(request, bytes(body))
