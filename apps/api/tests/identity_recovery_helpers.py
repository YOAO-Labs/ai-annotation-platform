"""Behavior fixtures for hashed challenges and committed credential mail."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from yoao_identity.mail_contract import MailDeliveryPolicy
from yoao_identity.mail_v2_postgres import MailV2PostgresData
from yoao_identity.recovery_crypto import token_hash
from yoao_identity_runtime.mail_worker import MailExecutor
from yoao_mail_transport import MailDeliveryResult, MailOutcome, MailReason
from app.db.models.email_verification_token import EmailVerificationToken
from app.db.models.password_reset_token import PasswordResetToken


async def seed_challenge(
    db, user, purpose="password_reset", *, token=None, expires_at=None
):
    raw = token or secrets.token_hex(32)
    model = (
        PasswordResetToken if purpose == "password_reset" else EmailVerificationToken
    )
    expiry = expires_at or datetime.now(UTC) + timedelta(
        hours=1 if purpose == "password_reset" else 24
    )
    db.add(
        model(
            user_id=user.id,
            token_hash=token_hash(raw),
            issued_email=user.email,
            expires_at=expiry,
        )
    )
    await db.flush()
    return raw


@dataclass
class CredentialMailCapture:
    executor: MailExecutor
    messages: list = field(default_factory=list)

    async def poll(self, delivery_id=None):
        return await self.executor.run_once(delivery_id)

    def tokens(self):
        values = []
        for message in self.messages:
            for line in message.get_content().splitlines():
                query = parse_qs(urlsplit(line.strip()).query)
                values.extend(query.get("token", []))
        return values


@pytest.fixture
def credential_mail_capture(db_session, monkeypatch):
    import yoao_mail_transport
    from app.config import settings
    from app.core.security import embedded_signing_secret
    from app.services.identity_recovery_commands import recovery_mail_key

    for name in (
        "identity_url",
        "identity_recovery_database_url",
        "identity_recovery_database_url_file",
    ):
        monkeypatch.setattr(settings, name, "")

    class TransactionMailV2Data(MailV2PostgresData):
        """Bind the real native client to this test's outer savepoint."""

        async def _open(self):
            connection = await db_session.connection()
            return (await connection.get_raw_connection()).driver_connection

        async def _close(self, connection):
            pass

    policy = MailDeliveryPolicy(
        smtp_host="synthetic-smtp.invalid",
        smtp_port=2525,
        smtp_user="",
        smtp_password="",
        smtp_from="noreply@example.test",
    )
    capture = CredentialMailCapture(
        MailExecutor(
            TransactionMailV2Data(""),
            policy,
            recovery_mail_key(embedded_signing_secret()),
        )
    )

    def send(config, message):
        capture.messages.append(message)
        return MailDeliveryResult(MailOutcome.DELIVERED, MailReason.SENT)

    monkeypatch.setattr(yoao_mail_transport, "send_message", send)
    return capture
