"""Behavior fixtures for hashed challenges and committed credential mail."""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from aap_identity.recovery_crypto import token_hash
from aap_mail_transport import MailDeliveryResult, MailOutcome, MailReason
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
    messages: list = field(default_factory=list)

    def tokens(self):
        values = []
        for message in self.messages:
            for line in message.get_content().splitlines():
                query = parse_qs(urlsplit(line.strip()).query)
                values.extend(query.get("token", []))
        return values


@pytest.fixture
def credential_mail_capture(db_session, monkeypatch):
    import aap_mail_transport
    from app.services import identity_mail_delivery
    from app.config import settings

    for name in (
        "identity_url",
        "identity_recovery_database_url",
        "identity_recovery_database_url_file",
        "identity_mail_database_url",
        "identity_mail_database_url_file",
        "identity_mail_settings_database_url",
        "identity_mail_settings_database_url_file",
    ):
        monkeypatch.setattr(settings, name, "")

    @asynccontextmanager
    async def session():
        yield db_session

    capture = CredentialMailCapture()

    def send(config, message):
        capture.messages.append(message)
        return MailDeliveryResult(MailOutcome.DELIVERED, MailReason.SENT)

    monkeypatch.setattr(identity_mail_delivery, "async_session", session)
    monkeypatch.setattr(
        identity_mail_delivery,
        "_smtp_fallbacks",
        lambda: {
            "smtp_host": "synthetic-smtp.invalid",
            "smtp_port": 2525,
            "smtp_user": "",
            "smtp_password": "",
            "smtp_from": "noreply@example.test",
        },
    )
    monkeypatch.setattr(aap_mail_transport, "send_message", send)
    return capture
