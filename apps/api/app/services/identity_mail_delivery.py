"""Optional embedded kick for the durable Identity credential-mail executor."""

from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aap_identity.contract import IdentityUnavailable
from aap_identity.mail_contract import (
    MAIL_NATIVE_PROTOCOL,
    MailFinishRequest,
    MailRenewRequest,
    decode_mail_accepted_reply,
    decode_mail_claim_reply,
    decode_mail_claim_request,
    decode_mail_delivery_policy,
    native_command_json,
)
from aap_identity.mail_postgres import MailPostgresData, MailSettingsPostgresData
from aap_identity_runtime.mail_worker import MailExecutor
from app.config import settings
from app.db.base import async_session
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import get_identity_gateway, identity_database_source
from app.services.identity_recovery_commands import recovery_mail_key
from app.services.identity_session_data import _json_value

log = logging.getLogger(__name__)


class IdentityMailCommandData:
    """Use short caller transactions; never keep a SQL session around SMTP."""

    def __init__(self, session_factory=async_session):
        self.session_factory = session_factory

    async def _execute(self, function, command, decode):
        async with self.session_factory() as db:
            try:
                raw = _json_value(
                    await db.scalar(
                        text(
                            f"SELECT aap_identity_recovery_v1.{function}(CAST(:command AS jsonb))"
                        ),
                        {"command": native_command_json(command)},
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
        if type(lease_seconds) is not int:
            raise IdentityUnavailable()
        request = decode_mail_claim_request(
            {
                "schema": MAIL_NATIVE_PROTOCOL,
                "worker_id": str(worker_id),
                "lease_seconds": max(1, min(300, lease_seconds)),
                "delivery_id": str(delivery_id) if delivery_id is not None else None,
            }
        )

        def decode(raw):
            delivery = decode_mail_claim_reply(raw).data.delivery
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
        request = MailRenewRequest(
            schema=MAIL_NATIVE_PROTOCOL,
            delivery_id=lease.id,
            worker_id=lease.worker_id,
            lease_token=lease.lease_token,
        )
        return await self._execute(
            "renew_mail",
            request,
            lambda raw: decode_mail_accepted_reply(raw).data.accepted,
        )

    async def finish_mail(self, lease, *, outcome, reason, retry_delay_seconds):
        request = MailFinishRequest(
            schema=MAIL_NATIVE_PROTOCOL,
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
            lambda raw: decode_mail_accepted_reply(raw).data.accepted,
        )


class IdentityMailSettingsData:
    def __init__(self, session_factory=async_session):
        self.session_factory = session_factory

    async def mail_delivery_policy(self, fallbacks):
        async with self.session_factory() as db:
            try:
                policy = decode_mail_delivery_policy(
                    _json_value(
                        await db.scalar(
                            text(
                                "SELECT aap_settings_v1.mail_delivery_policy(CAST(:fallbacks AS jsonb))"
                            ),
                            {
                                "fallbacks": json.dumps(
                                    fallbacks, ensure_ascii=True, allow_nan=False
                                )
                            },
                        )
                    )
                )
                await db.commit()
                return policy
            except SQLAlchemyError:
                await db.rollback()
                raise IdentityUnavailable() from None
            except BaseException:
                await db.rollback()
                raise


def _smtp_fallbacks():
    return {
        "smtp_host": settings.smtp_host or "",
        "smtp_port": settings.smtp_port if settings.smtp_port is not None else 0,
        "smtp_user": settings.smtp_user or "",
        "smtp_password": settings.smtp_password or "",
        "smtp_from": settings.smtp_from or "",
    }


async def run_credential_mail(delivery_id=None, *, session_factory=None):
    session_factory = session_factory or async_session
    identity = get_identity_gateway()
    if identity.identity_url is not None:
        # The selected remote owner is served by its own durable mail process.
        return {"state": "remote"}
    mail_dsn = identity_database_source(
        settings.identity_mail_database_url, settings.identity_mail_database_url_file
    )
    policy_dsn = identity_database_source(
        settings.identity_mail_settings_database_url,
        settings.identity_mail_settings_database_url_file,
    )
    recovery_dsn = identity_database_source(
        settings.identity_recovery_database_url,
        settings.identity_recovery_database_url_file,
    )
    if bool(mail_dsn) != bool(policy_dsn) or (recovery_dsn and not mail_dsn):
        raise IdentityGatewayUnavailable()
    data = (
        MailPostgresData(mail_dsn)
        if mail_dsn
        else IdentityMailCommandData(session_factory)
    )
    policy_data = (
        MailSettingsPostgresData(policy_dsn)
        if policy_dsn
        else IdentityMailSettingsData(session_factory)
    )

    async def policy():
        return await policy_data.mail_delivery_policy(_smtp_fallbacks())

    secret = identity.signing_secret or settings.secret_key
    if not secret:
        raise IdentityGatewayUnavailable()
    return await MailExecutor(data, policy, recovery_mail_key(secret)).run_once(
        delivery_id=delivery_id
    )


async def kick_credential_mail(delivery_id: uuid.UUID):
    """Send after acceptance; an interrupted kick leaves its durable intent."""
    try:
        return await run_credential_mail(delivery_id)
    except (IdentityUnavailable, IdentityGatewayUnavailable, ValueError, TypeError):
        log.warning("Credential mail deferred; durable delivery intent retained")
        return {"state": "unavailable"}
