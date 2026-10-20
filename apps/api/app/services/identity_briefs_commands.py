"""Select the Identity profile owner; selected remote failures never fall back."""

from __future__ import annotations

import asyncio
from typing import Protocol

from aap_identity.briefs_contract import (
    BriefsFailure,
    BriefsReply,
    BriefsRequest,
    decode_briefs_reply,
)
from aap_identity.briefs_manager import BriefsManager
from aap_identity.briefs_postgres import BriefsPostgresData
from aap_identity.contract import IdentityUnavailable
from app.config import settings
from app.services.identity_account_commands import _remote_json, account_json
from app.services.identity_briefs_data import IdentityBriefsData
from app.services.identity_contracts import (
    IdentityBriefsUnavailable,
    IdentityGatewayUnavailable,
)
from app.services.identity_gateway import IdentityGateway, identity_database_source


class BriefsCommandsPort(Protocol):
    async def execute(self, request: BriefsRequest) -> BriefsReply: ...


class IdentityBriefsCommands:
    def __init__(self, db, identity: IdentityGateway):
        self.db = db
        self.identity = identity

    async def execute(self, request: BriefsRequest) -> BriefsReply:
        try:
            async with asyncio.timeout(self.identity.timeout_seconds):
                if self.identity.identity_url is not None:
                    raw = await _remote_json(
                        self.identity,
                        "/v1/identity/briefs/execute",
                        account_json(request.model_dump(mode="python", by_alias=True)),
                    )
                    return decode_briefs_reply(request, raw)
                dsn = identity_database_source(
                    settings.identity_briefs_database_url,
                    settings.identity_briefs_database_url_file,
                )
                data = BriefsPostgresData(dsn) if dsn else IdentityBriefsData(self.db)
                if dsn:
                    await data.briefs_policy()
                return await BriefsManager(data).execute(request)
        except (
            IdentityGatewayUnavailable,
            IdentityUnavailable,
            BriefsFailure,
            TimeoutError,
            ValueError,
            TypeError,
            UnicodeError,
        ):
            raise IdentityBriefsUnavailable() from None
