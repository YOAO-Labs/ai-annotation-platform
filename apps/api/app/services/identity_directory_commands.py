"""Bind directory reads to the configured Identity owner without table access."""

from __future__ import annotations

import asyncio
from typing import Protocol

from aap_identity.contract import IdentityUnavailable, Principal
from aap_identity.directory_contract import (
    DirectoryReply,
    DirectoryRequest,
    decode_directory_reply,
)
from aap_identity.directory_manager import DirectoryManager
from aap_identity.directory_postgres import DirectoryPostgresData
from app.config import settings
from app.services.identity_account_commands import (
    _AccountAuthenticator,
    _remote_json,
    account_json,
)
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway, identity_database_source
from app.services.identity_directory_data import IdentityDirectoryCommandData


class DirectoryCommandsPort(Protocol):
    async def execute(self, request: DirectoryRequest) -> DirectoryReply: ...


class IdentityDirectoryCommands:
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

    async def execute(self, request: DirectoryRequest) -> DirectoryReply:
        try:
            async with asyncio.timeout(self.identity.timeout_seconds):
                if self.identity.identity_url is not None:
                    raw = await _remote_json(
                        self.identity,
                        "/v1/identity/directory/execute",
                        account_json(request.model_dump(mode="python", by_alias=True)),
                    )
                    return decode_directory_reply(
                        request, raw, expected_principal=self.expected_principal
                    )
                dsn = identity_database_source(
                    settings.identity_directory_database_url,
                    settings.identity_directory_database_url_file,
                )
                data = (
                    DirectoryPostgresData(dsn)
                    if dsn
                    else IdentityDirectoryCommandData(self.db)
                )
                if dsn:
                    await data.directory_policy()
                secret = (
                    self.identity.signing_secret
                    if self.identity.signing_secret is not None
                    else settings.secret_key
                )
                if not secret:
                    raise IdentityGatewayUnavailable()
                return await DirectoryManager(
                    _AccountAuthenticator(self.identity, self.db), data, secret
                ).execute(request)
        except (IdentityUnavailable, TimeoutError, ValueError, TypeError, UnicodeError):
            raise IdentityGatewayUnavailable() from None
