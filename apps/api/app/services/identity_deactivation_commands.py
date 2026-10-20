"""Bind self-deactivation and scheduled execution to the selected Identity owner."""

from __future__ import annotations

import asyncio
from typing import Protocol

from aap_identity.contract import IdentityUnavailable, Principal
from aap_identity.deactivation_contract import (
    DeactivationRequest,
    DeactivationReply,
    DeactivationDueRequest,
    DeactivationDueReply,
    DeactivationDueFailure,
    decode_deactivation_reply,
    decode_deactivation_due_reply,
)
from aap_identity.deactivation_manager import DeactivationManager
from aap_identity.deactivation_postgres import (
    DeactivationPostgresData,
    DeactivationDuePostgresData,
)
from app.config import settings
from app.services.identity_account_commands import (
    _AccountAuthenticator,
    _remote_json,
    account_json,
)
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway, identity_database_source
from app.services.identity_deactivation_data import (
    IdentityDeactivationCommandData,
    IdentityDeactivationDueData,
)


class DeactivationCommandsPort(Protocol):
    async def execute(self, request: DeactivationRequest) -> DeactivationReply: ...


class DeactivationDuePort(Protocol):
    async def process_due_deactivations(
        self, request: DeactivationDueRequest
    ) -> DeactivationDueReply: ...


class DeactivationOperatorBusy(Exception):
    """The current reservation holder must commit before the operator retries."""


class IdentityDeactivationCommands:
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

    async def execute(self, request: DeactivationRequest) -> DeactivationReply:
        try:
            async with asyncio.timeout(self.identity.timeout_seconds):
                if self.identity.identity_url is not None:
                    raw = await _remote_json(
                        self.identity,
                        "/v1/identity/deactivation/execute",
                        account_json(request.model_dump(mode="python", by_alias=True)),
                    )
                    return decode_deactivation_reply(
                        request, raw, expected_principal=self.expected_principal
                    )
                dsn = identity_database_source(
                    settings.identity_deactivation_database_url,
                    settings.identity_deactivation_database_url_file,
                )
                data = (
                    DeactivationPostgresData(dsn)
                    if dsn
                    else IdentityDeactivationCommandData(self.db)
                )
                if dsn:
                    await data.deactivation_policy()
                secret = (
                    self.identity.signing_secret
                    if self.identity.signing_secret is not None
                    else settings.secret_key
                )
                if not secret:
                    raise IdentityGatewayUnavailable()
                return await DeactivationManager(
                    _AccountAuthenticator(self.identity, self.db), data, secret
                ).execute(request)
        except (IdentityUnavailable, TimeoutError, ValueError, TypeError, UnicodeError):
            raise IdentityGatewayUnavailable() from None


class IdentityDeactivationDueCommands:
    def __init__(self, db, identity: IdentityGateway):
        self.db = db
        self.identity = identity

    async def process_due_deactivations(
        self, request: DeactivationDueRequest
    ) -> DeactivationDueReply:
        try:
            async with asyncio.timeout(self.identity.timeout_seconds):
                if self.identity.identity_url is not None:
                    raw = await _remote_json(
                        self.identity,
                        "/v1/identity/deactivation/process-due",
                        request.model_dump_json(by_alias=True),
                    )
                    return decode_deactivation_due_reply(request, raw)
                dsn = identity_database_source(
                    settings.identity_deactivation_operator_database_url,
                    settings.identity_deactivation_operator_database_url_file,
                )
                data = (
                    DeactivationDuePostgresData(dsn)
                    if dsn
                    else IdentityDeactivationDueData(self.db)
                )
                if dsn:
                    await data.due_policy()
                return await data.process_due_deactivations(request)
        except DeactivationDueFailure as error:
            if error.code == "invariant_busy":
                raise DeactivationOperatorBusy() from None
            raise
        except (IdentityUnavailable, TimeoutError, ValueError, TypeError, UnicodeError):
            raise IdentityGatewayUnavailable() from None
