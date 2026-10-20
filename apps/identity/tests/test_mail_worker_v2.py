from __future__ import annotations

import base64
import hashlib
import sys
import threading
import types
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient

from aap_identity.mail_contract import MailDeliveryPolicy
from aap_identity.mail_v2_contract import (
    MAIL_V2_POLICY,
    MailV2PolicyReply,
    InvitationMailV2Lease,
)
from aap_identity_runtime.mail_config_v2 import MailV2WorkerSettings
from aap_identity_runtime.mail_worker import MailExecutor
from aap_identity_runtime.mail_worker_v2 import create_app

TOKEN = "邀请-原始🔐proof"
KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
DELIVERY_ID = uuid.UUID("72000000-0000-4000-8000-000000000001")
WORKER_ID = uuid.UUID("72000000-0000-4000-8000-000000000002")
ISSUER_ID = uuid.UUID("72000000-0000-4000-8000-000000000003")
INVITATION_ID = uuid.UUID("72000000-0000-4000-8000-000000000004")


@dataclass
class FakeSMTPConfig:
    host: str
    port: int
    user: str
    password: str
    sender: str


@pytest.fixture(autouse=True)
def source_injected_crypto_and_smtp(monkeypatch):
    recovery_crypto = types.ModuleType("aap_identity.recovery_crypto")
    recovery_crypto.mail_encryption_id = lambda _key: "b" * 64
    recovery_crypto.decrypt_mail_token = lambda _ciphertext, _key, _key_id: TOKEN
    recovery_crypto.token_hash = lambda token: hashlib.sha256(
        token.encode()
    ).hexdigest()
    monkeypatch.setitem(sys.modules, "aap_identity.recovery_crypto", recovery_crypto)

    transport_module = types.ModuleType("aap_mail_transport")
    transport_module.SMTPConfig = FakeSMTPConfig
    transport_module.send_message = lambda config, message: SimpleNamespace(
        outcome="delivered", reason="sent"
    )
    monkeypatch.setitem(sys.modules, "aap_mail_transport", transport_module)


def invitation_lease(*, key_id: str = "b" * 64) -> InvitationMailV2Lease:
    now = datetime.now(UTC)
    return InvitationMailV2Lease(
        id=DELIVERY_ID,
        issuer_id=ISSUER_ID,
        invitation_id=INVITATION_ID,
        purpose="invitation",
        email="recipient@example.test",
        frontend_base_url="https://labels.example.test/app",
        token_hash=hashlib.sha256(TOKEN.encode()).hexdigest(),
        token_ciphertext="ciphertext-not-the-link",
        encryption_id=key_id,
        expires_at=now + timedelta(days=7),
        attempt=1,
        worker_id=WORKER_ID,
        lease_token="opaque-lease-token",
        lease_expires_at=now + timedelta(seconds=60),
        project_name="Vision project",
        project_member_role="annotator",
    )


def test_invitation_project_name_stays_plain_text_without_becoming_a_header():
    lease = invitation_lease().model_copy(
        update={"project_name": "Vision\r\nBcc: hidden@example.test"}
    )
    message = MailExecutor._message(lease, TOKEN, "noreply@example.test")
    assert "Bcc: hidden@example.test" in message.get_content()
    assert message.get("Bcc") is None
    assert message["To"] == lease.email


class FakeData:
    def __init__(self, delivery=None, *, accepted=True):
        self.delivery = delivery
        self.accepted = accepted
        self.claimed = []
        self.renewed = []
        self.finished = []
        self.policy_reads = 0

    async def claim_mail(self, worker_id, *, lease_seconds, delivery_id):
        self.claimed.append((worker_id, lease_seconds, delivery_id))
        return self.delivery

    async def renew_mail(self, lease):
        self.renewed.append(lease)
        return True

    async def finish_mail(self, lease, *, outcome, reason, retry_delay_seconds):
        self.finished.append((lease, outcome, reason, retry_delay_seconds))
        return self.accepted

    async def mail_policy(self):
        self.policy_reads += 1
        return MailV2PolicyReply(
            schema="aap-identity-mail-policy/v2",
            policy=MAIL_V2_POLICY,
        )


def smtp_policy(**values):
    return MailDeliveryPolicy(
        smtp_host=values.get("smtp_host", "smtp.example.test"),
        smtp_port=values.get("smtp_port", 587),
        smtp_user=values.get("smtp_user", "mailer"),
        smtp_password=values.get("smtp_password", "smtp-secret"),
        smtp_from=values.get("smtp_from", "noreply@example.test"),
    )


@pytest.mark.asyncio
async def test_v2_executor_sends_opaque_unicode_invitation_with_stable_message_id():
    lease = invitation_lease()
    data = FakeData(lease)
    messages = []

    def transport(config, message):
        assert config.password == "smtp-secret"
        messages.append(message)
        return SimpleNamespace(outcome="delivered", reason="sent")

    result = await MailExecutor(
        data,
        smtp_policy(),
        KEY,
        worker_id=WORKER_ID,
        transport=transport,
    ).run_once(DELIVERY_ID)

    message = messages[0]
    content = message.get_content()
    link = next(line for line in content.splitlines() if "/register?" in line)
    assert message["To"] == "recipient@example.test"
    assert message["Message-ID"] == f"<aap.{DELIVERY_ID}.invitation@aap.invalid>"
    assert parse_qs(urlsplit(link).query)["token"] == [TOKEN]
    assert "Vision project" in content
    assert "项目角色：annotator" in content
    assert TOKEN not in repr(result)
    assert "opaque-lease-token" not in repr(result)
    assert result == {
        "state": "finished",
        "delivery_id": str(DELIVERY_ID),
        "outcome": "delivered",
        "reason": "sent",
        "accepted": True,
    }
    assert data.finished == [(lease, "delivered", "sent", None)]


@pytest.mark.asyncio
async def test_v2_executor_retries_ambiguous_smtp_without_exposing_exception():
    lease = invitation_lease()
    data = FakeData(lease)

    def broken_transport(*_):
        raise RuntimeError("smtp password=private")

    result = await MailExecutor(
        data,
        smtp_policy(),
        KEY,
        worker_id=WORKER_ID,
        transport=broken_transport,
    ).run_once()

    assert result["outcome"] == "unknown"
    assert result["reason"] == "transport_failed"
    assert "private" not in repr(result)
    assert data.finished == [(lease, "unknown", "transport_failed", 30)]


@pytest.mark.asyncio
async def test_v2_readiness_checks_only_v2_and_settings_policies():
    settings = MailV2WorkerSettings(
        _env_file=None,
        database_url="postgresql://mail-v2/db",
        settings_database_url="postgresql://settings/db",
        encryption_key=KEY,
        jwt_signing_secret="unused-auth-secret",
    )
    data = FakeData()

    class SettingsData:
        async def mail_delivery_policy(self, fallbacks):
            assert set(fallbacks) == {
                "smtp_host",
                "smtp_port",
                "smtp_user",
                "smtp_password",
                "smtp_from",
            }
            return smtp_policy()

    app = create_app(
        settings,
        data=data,
        settings_data=SettingsData(),
        executor=MailExecutor(data, smtp_policy(), KEY, worker_id=WORKER_ID),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        health = await client.get("/health")
        ready = await client.get("/ready")
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert data.policy_reads == 1
    assert data.claimed == []
    assert data.finished == []


@pytest.mark.asyncio
async def test_v2_lost_renewal_does_not_acknowledge_a_late_send():
    lease = invitation_lease()
    release = threading.Event()
    data = FakeData(lease)

    async def renew(item):
        data.renewed.append(item)
        return len(data.renewed) == 1

    data.renew_mail = renew

    def blocked_transport(*_):
        release.wait(timeout=3)
        return SimpleNamespace(outcome="delivered", reason="sent")

    loop = __import__("asyncio").get_running_loop()
    loop.call_later(0.5, release.set)
    result = await MailExecutor(
        data,
        smtp_policy(),
        KEY,
        worker_id=WORKER_ID,
        lease_seconds=1,
        transport=blocked_transport,
    ).run_once()
    assert result["state"] == "stale"
    assert len(data.renewed) >= 2
    assert data.finished == []
