"""Select one invitation owner and await its durable mail result when requested."""

from __future__ import annotations

import asyncio
from typing import Protocol

from yoao_identity.contract import IdentityUnavailable, Principal
from yoao_identity.invitation_contract import (
    InvitationReply,
    InvitationRequest,
    decode_invitation_reply,
)
from yoao_identity.invitation_manager import InvitationManager
from yoao_identity.invitation_postgres import InvitationPostgresData
from app.config import settings
from app.services.identity_account_commands import (
    _AccountAuthenticator,
    _remote_json,
    account_json,
)
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway, identity_database_source
from app.services.identity_invitation_data import IdentityInvitationCommandData
from app.services.identity_recovery_commands import recovery_mail_key


class InvitationCommandsPort(Protocol):
    async def execute(self, request: InvitationRequest) -> InvitationReply: ...


class IdentityInvitationCommands:
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

    async def execute(self, request: InvitationRequest) -> InvitationReply:
        last_pending = None
        mail = request.operation == "send_invitation_email"
        timeout = (
            settings.identity_invitation_mail_timeout_seconds
            if mail
            else self.identity.timeout_seconds
        )
        try:
            async with asyncio.timeout(timeout):
                manager = None
                if self.identity.identity_url is None:
                    invitation_dsn = identity_database_source(
                        settings.identity_invitation_database_url,
                        settings.identity_invitation_database_url_file,
                    )
                    data = (
                        InvitationPostgresData(invitation_dsn)
                        if invitation_dsn
                        else IdentityInvitationCommandData(self.db)
                    )
                    if invitation_dsn:
                        await data.invitation_policy()
                    secret = self.identity.require_signing_secret()
                    manager = InvitationManager(
                        _AccountAuthenticator(self.identity, self.db),
                        data,
                        secret,
                        recovery_mail_key(secret),
                        invitation_ttl_days=settings.invitation_ttl_days,
                        max_invitations_per_day=settings.max_invitations_per_day,
                        frontend_base_url=settings.frontend_base_url,
                        expire_minutes=settings.access_token_expire_minutes,
                    )
                delay = 0.1
                while True:
                    if manager is None:
                        raw = await _remote_json(
                            self.identity,
                            "/v1/identity/invitations/execute",
                            account_json(
                                request.model_dump(mode="python", by_alias=True)
                            ),
                        )
                        reply = decode_invitation_reply(
                            request, raw, expected_principal=self.expected_principal
                        )
                    else:
                        reply = await manager.execute(request)
                    if not mail or reply.data.mail_status != "pending":
                        return reply
                    last_pending = reply
                    await asyncio.sleep(delay)
                    delay = min(1.0, delay * 2)
        except TimeoutError:
            if last_pending is not None:
                return last_pending
            raise IdentityGatewayUnavailable() from None
        except (IdentityUnavailable, ValueError, TypeError, UnicodeError):
            raise IdentityGatewayUnavailable() from None
