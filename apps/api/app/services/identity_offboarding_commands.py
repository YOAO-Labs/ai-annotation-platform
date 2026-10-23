"""Bind offboarding preview and commit to the Identity owner."""

from __future__ import annotations

import asyncio
from typing import Protocol

from aap_identity.contract import IdentityUnavailable, Principal
from aap_identity.offboarding_contract import (
    OffboardingReply,
    OffboardingRequest,
    decode_offboarding_reply,
)
from aap_identity.offboarding_manager import OffboardingManager
from aap_identity.offboarding_postgres import OffboardingPostgresData

from app.config import settings
from app.services.identity_account_commands import (
    _AccountAuthenticator,
    _remote_json,
    account_json,
)
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway, identity_database_source
from app.services.identity_offboarding_data import IdentityOffboardingCommandData


class OffboardingCommandsPort(Protocol):
    async def execute(self, request: OffboardingRequest) -> OffboardingReply: ...


class IdentityOffboardingCommands:
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

    async def execute(self, request: OffboardingRequest) -> OffboardingReply:
        try:
            async with asyncio.timeout(self.identity.timeout_seconds):
                if self.identity.identity_url is not None:
                    if self.expected_principal is None:
                        raise IdentityGatewayUnavailable()
                    raw = await _remote_json(
                        self.identity,
                        "/v1/identity/offboarding/execute",
                        account_json(request.model_dump(mode="python", by_alias=True)),
                    )
                    return decode_offboarding_reply(
                        request, raw, expected_principal=self.expected_principal
                    )
                dsn = identity_database_source(
                    settings.identity_offboarding_database_url,
                    settings.identity_offboarding_database_url_file,
                )
                if dsn:
                    data = OffboardingPostgresData(dsn)
                    await data.offboarding_policy()
                else:
                    data = IdentityOffboardingCommandData(self.db)
                secret = (
                    self.identity.signing_secret
                    if self.identity.signing_secret is not None
                    else settings.secret_key
                )
                if not secret:
                    raise IdentityGatewayUnavailable()
                return await OffboardingManager(
                    _AccountAuthenticator(self.identity, self.db), data, secret
                ).execute(request)
        except (IdentityUnavailable, TimeoutError, ValueError, TypeError, UnicodeError):
            raise IdentityGatewayUnavailable() from None
