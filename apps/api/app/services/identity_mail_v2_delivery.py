"""Assemble the versioned durable mail executor for API and scheduler adapters."""

from __future__ import annotations

import logging
import uuid

from aap_identity.contract import IdentityUnavailable
from aap_identity.mail_postgres import MailSettingsPostgresData
from aap_identity.mail_v2_contract import (
    MAIL_V2_NATIVE_PROTOCOL,
    MailV2FinishRequest,
    MailV2RenewRequest,
    decode_mail_v2_accepted_reply,
    decode_mail_v2_claim_reply,
    decode_mail_v2_claim_request,
    native_mail_v2_command_json,
)
from aap_identity.mail_v2_postgres import MailV2PostgresData
from aap_identity_runtime.mail_worker import MailExecutor
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.db.base import async_session
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import get_identity_gateway, identity_database_source
from app.services.identity_mail_delivery import (
    IdentityMailSettingsData,
    _smtp_fallbacks,
)
from app.services.identity_recovery_commands import recovery_mail_key
from app.services.identity_session_data import _json_value

log = logging.getLogger(__name__)


class IdentityMailV2CommandData:
    """Commit each validated owner call before SMTP or the next lease operation."""

    def __init__(self, session_factory=async_session):
        self.session_factory = session_factory

    async def _execute(self, function, command, decode):
        async with self.session_factory() as db:
            try:
                raw = _json_value(
                    await db.scalar(
                        text(
                            f"SELECT aap_identity_mail_v2.{function}(CAST(:command AS jsonb))"
                        ),
                        {"command": native_mail_v2_command_json(command)},
                    )
                )
                result = decode(raw)
                if result is False:
                    await db.rollback()
                else:
                    await db.commit()
                return result
            except SQLAlchemyError:
                await db.rollback()
                raise IdentityUnavailable() from None
            except BaseException:
                await db.rollback()
                raise

    async def claim_mail(self, worker_id, *, lease_seconds=60, delivery_id=None):
        request = decode_mail_v2_claim_request(
            {
                "schema": MAIL_V2_NATIVE_PROTOCOL,
                "worker_id": str(worker_id),
                "lease_seconds": lease_seconds,
                "delivery_id": str(delivery_id) if delivery_id is not None else None,
            }
        )

        def decode(raw):
            delivery = decode_mail_v2_claim_reply(raw).data.delivery
            if delivery is not None and (
                delivery.worker_id != request.worker_id
                or (
                    request.delivery_id is not None
                    and delivery.id != request.delivery_id
                )
            ):
                raise IdentityUnavailable()
            return delivery

        return await self._execute("claim_mail", request, decode)

    async def renew_mail(self, lease):
        request = MailV2RenewRequest(
            schema=MAIL_V2_NATIVE_PROTOCOL,
            delivery_id=lease.id,
            worker_id=lease.worker_id,
            lease_token=lease.lease_token,
        )
        return await self._execute(
            "renew_mail",
            request,
            lambda raw: decode_mail_v2_accepted_reply(raw).data.accepted,
        )

    async def finish_mail(self, lease, *, outcome, reason, retry_delay_seconds):
        request = MailV2FinishRequest(
            schema=MAIL_V2_NATIVE_PROTOCOL,
            delivery_id=lease.id,
            worker_id=lease.worker_id,
            lease_token=lease.lease_token,
            outcome=outcome,
            reason=reason,
            retry_delay_seconds=retry_delay_seconds,
        )
        return await self._execute(
            "finish_mail",
            request,
            lambda raw: decode_mail_v2_accepted_reply(raw).data.accepted,
        )


async def run_invitation_mail(delivery_id=None, *, session_factory=None):
    identity = get_identity_gateway()
    mail_dsn = identity_database_source(
        settings.identity_mail_v2_database_url,
        settings.identity_mail_v2_database_url_file,
    )
    policy_dsn = identity_database_source(
        settings.identity_mail_settings_database_url,
        settings.identity_mail_settings_database_url_file,
    )
    if identity.identity_url is not None and not mail_dsn:
        return {"state": "remote"}
    invitation_dsn = identity_database_source(
        settings.identity_invitation_database_url,
        settings.identity_invitation_database_url_file,
    )
    if bool(mail_dsn) != bool(policy_dsn) or (invitation_dsn and not mail_dsn):
        raise IdentityGatewayUnavailable()
    session_factory = session_factory or async_session
    data = (
        MailV2PostgresData(mail_dsn)
        if mail_dsn
        else IdentityMailV2CommandData(session_factory)
    )
    policy_data = (
        MailSettingsPostgresData(policy_dsn)
        if policy_dsn
        else IdentityMailSettingsData(session_factory)
    )

    async def policy():
        return await policy_data.mail_delivery_policy(_smtp_fallbacks())

    secret = identity.signing_secret or settings.secret_key
    if not secret and not (
        settings.identity_recovery_mail_key or settings.identity_recovery_mail_key_file
    ):
        raise IdentityGatewayUnavailable()
    return await MailExecutor(data, policy, recovery_mail_key(secret)).run_once(
        delivery_id=delivery_id
    )


async def kick_invitation_mail(delivery_id: uuid.UUID):
    """An interrupted kick leaves the same durable intent for the independent worker."""
    try:
        return await run_invitation_mail(delivery_id)
    except (IdentityUnavailable, IdentityGatewayUnavailable, ValueError, TypeError):
        log.warning("Invitation mail deferred; durable delivery intent retained")
        return {"state": "unavailable"}
