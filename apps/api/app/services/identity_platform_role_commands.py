"""Bind platform-role previews and changes to the configured Identity owner."""

from __future__ import annotations

import asyncio
from typing import Protocol

from aap_identity.contract import IdentityUnavailable, Principal
from aap_identity.platform_role_contract import (
    PlatformRoleReply,
    PlatformRoleRequest,
    decode_platform_role_reply,
)
from aap_identity.platform_role_manager import PlatformRoleManager
from aap_identity.platform_role_postgres import PlatformRolePostgresData
from app.config import settings
from app.services.identity_account_commands import (
    _AccountAuthenticator,
    _remote_json,
    account_json,
)
from app.services.identity_platform_role_data import IdentityPlatformRoleCommandData
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway, identity_database_source


class PlatformRoleCommandsPort(Protocol):
    async def execute(self, request: PlatformRoleRequest) -> PlatformRoleReply: ...


class IdentityPlatformRoleCommands:
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

    async def execute(self, request: PlatformRoleRequest) -> PlatformRoleReply:
        try:
            async with asyncio.timeout(self.identity.timeout_seconds):
                if self.identity.identity_url is not None:
                    if self.expected_principal is None:
                        raise IdentityGatewayUnavailable()
                    raw = await _remote_json(
                        self.identity,
                        "/v1/identity/platform-roles/execute",
                        account_json(request.model_dump(mode="python", by_alias=True)),
                    )
                    return decode_platform_role_reply(
                        request, raw, expected_principal=self.expected_principal
                    )
                dsn = identity_database_source(
                    settings.identity_platform_role_database_url,
                    settings.identity_platform_role_database_url_file,
                )
                if dsn:
                    data = PlatformRolePostgresData(dsn)
                    await data.platform_role_policy()
                else:
                    data = IdentityPlatformRoleCommandData(self.db)
                secret = (
                    self.identity.signing_secret
                    if self.identity.signing_secret is not None
                    else settings.secret_key
                )
                if not secret:
                    raise IdentityGatewayUnavailable()
                return await PlatformRoleManager(
                    _AccountAuthenticator(self.identity, self.db), data, secret
                ).execute(request)
        except (IdentityUnavailable, TimeoutError, ValueError, TypeError, UnicodeError):
            raise IdentityGatewayUnavailable() from None
