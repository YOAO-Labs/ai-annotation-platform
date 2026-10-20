from __future__ import annotations

import hashlib
import sys
import threading
import types
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aap_identity.mail_contract import (
    MailDeliveryPolicy,
    MailLease,
    MailPolicyReply,
)
from aap_identity_runtime.mail_config import MailWorkerSettings
from aap_identity_runtime.mail_worker import MailExecutor, create_app
from httpx import ASGITransport, AsyncClient

TOKEN = "0123456789abcdef" * 4
KEY = "dedicated-fernet-key"
DELIVERY_ID = uuid.UUID("40000000-0000-4000-8000-000000000001")
WORKER_ID = uuid.UUID("40000000-0000-4000-8000-000000000002")


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
    recovery_crypto.mail_encryption_id = lambda key: "b" * 64
    recovery_crypto.decrypt_mail_token = lambda ciphertext, key, encryption_id: TOKEN
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


def lease(*, purpose: str = "password_reset", key_id: str = "b" * 64) -> MailLease:
    now = datetime.now(UTC)
    return MailLease(
        id=DELIVERY_ID,
        actor_id=uuid.UUID("40000000-0000-4000-8000-000000000003"),
        purpose=purpose,
        email="user@example.test",
        frontend_base_url="https://labels.example.test/app",
        token_hash=hashlib.sha256(TOKEN.encode()).hexdigest(),
        token_ciphertext="ciphertext-not-a-token",
        encryption_id=key_id,
        expires_at=now + timedelta(minutes=55),
        attempt=1,
        worker_id=WORKER_ID,
        lease_token="lease-token-secret",
        lease_expires_at=now + timedelta(seconds=60),
    )


class FakeData:
    def __init__(self, delivery: MailLease | None = None):
        self.delivery = delivery
        self.claimed = []
        self.renewed = []
        self.finished = []
        self.policy_reads = 0

    async def claim_mail(self, worker_id, *, lease_seconds, delivery_id):
        self.claimed.append((worker_id, lease_seconds, delivery_id))
        return self.delivery

    async def renew_mail(self, item):
        self.renewed.append(item)
        return True

    async def finish_mail(self, item, *, outcome, reason, retry_delay_seconds):
        self.finished.append((item, outcome, reason, retry_delay_seconds))
        return True

    async def mail_policy(self):
        self.policy_reads += 1
        return MailPolicyReply(
            schema="aap-identity-recovery-mail-policy/v1",
            policy="aap-identity-recovery-mail/v1",
        )


class FakeSettingsData:
    async def mail_delivery_policy(self, fallbacks):
        assert set(fallbacks) == {
            "smtp_host",
            "smtp_port",
            "smtp_user",
            "smtp_password",
            "smtp_from",
        }
        return smtp_policy()


def smtp_policy(**values) -> MailDeliveryPolicy:
    return MailDeliveryPolicy(
        smtp_host=values.get("smtp_host", "smtp.example.test"),
        smtp_port=values.get("smtp_port", 587),
        smtp_user=values.get("smtp_user", "mailer"),
        smtp_password=values.get("smtp_password", "smtp-secret"),
        smtp_from=values.get("smtp_from", "noreply@example.test"),
    )


@pytest.mark.asyncio
async def test_executor_sends_link_with_stable_message_id_and_safe_summary():
    data = FakeData(lease(purpose="email_verification"))
    messages = []
    policies = []

    def transport(config, message):
        assert config.password == "smtp-secret"
        messages.append(message)
        return SimpleNamespace(outcome="delivered", reason="sent")

    async def read_policy():
        policies.append(True)
        return smtp_policy()

    result = await MailExecutor(
        data,
        read_policy,
        KEY,
        worker_id=WORKER_ID,
        transport=transport,
    ).run_once(DELIVERY_ID)

    assert result == {
        "state": "finished",
        "delivery_id": str(DELIVERY_ID),
        "outcome": "delivered",
        "reason": "sent",
        "accepted": True,
    }
    assert len(policies) == 1
    assert messages[0]["Message-ID"] == (
        f"<aap.{DELIVERY_ID}.email_verification@aap.invalid>"
    )
    assert (
        "https://labels.example.test/app/verify-email?token=" + TOKEN
        in messages[0].get_content()
    )
    assert data.finished[0][1:] == ("delivered", "sent", None)


@pytest.mark.asyncio
async def test_settings_policy_is_reloaded_for_each_poll():
    data = FakeData()
    reads = []

    async def read_policy():
        reads.append(len(reads))
        return smtp_policy(smtp_host=f"smtp-{len(reads)}.example.test")

    executor = MailExecutor(data, read_policy, KEY, worker_id=WORKER_ID)
    assert (await executor.run_once())["state"] == "idle"
    assert (await executor.run_once())["state"] == "idle"
    assert len(reads) == 2


@pytest.mark.asyncio
async def test_health_and_ready_are_read_only_and_do_not_need_auth_settings():
    settings = MailWorkerSettings(
        _env_file=None,
        database_url="postgresql://mail/db",
        settings_database_url="postgresql://settings/db",
        encryption_key=KEY,
        signing_secret="unused-auth-secret",
        token="unused-api-token",
    )
    data = FakeData()
    executor = MailExecutor(data, smtp_policy(), KEY, worker_id=WORKER_ID)
    app = create_app(
        settings,
        data=data,
        settings_data=FakeSettingsData(),
        executor=executor,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        health = await client.get("/health")
        ready = await client.get("/ready")

    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok"}
    assert data.policy_reads == 1
    assert data.claimed == []
    assert data.finished == []


@pytest.mark.asyncio
async def test_invalid_key_is_requeued_without_transport_or_raw_secret_summary():
    data = FakeData(lease(key_id="c" * 64))
    calls = []
    result = await MailExecutor(
        data,
        smtp_policy(),
        KEY,
        worker_id=WORKER_ID,
        transport=lambda *_: calls.append(True),
    ).run_once()

    assert result["outcome"] == "invalid_key"
    assert result["reason"] == "derivation_unavailable"
    assert calls == []
    assert data.finished[0][1:] == (
        "invalid_key",
        "derivation_unavailable",
        30,
    )
    assert TOKEN not in repr(result)
    assert "lease-token-secret" not in repr(result)


@pytest.mark.asyncio
async def test_missing_smtp_configuration_is_rejected_without_sending():
    data = FakeData(lease())
    sent = []
    result = await MailExecutor(
        data,
        smtp_policy(smtp_port=0),
        KEY,
        worker_id=WORKER_ID,
        transport=lambda *_: sent.append(True),
    ).run_once()

    assert result["outcome"] == "rejected"
    assert result["reason"] == "not_configured"
    assert data.finished[0][1:] == ("rejected", "not_configured", None)
    assert sent == []


@pytest.mark.asyncio
async def test_transport_exception_is_unknown_without_exposing_exception_text():
    data = FakeData(lease())

    def broken_transport(*_):
        raise RuntimeError("SMTP password=secret-token")

    result = await MailExecutor(
        data,
        smtp_policy(),
        KEY,
        worker_id=WORKER_ID,
        transport=broken_transport,
    ).run_once()

    assert result["outcome"] == "unknown"
    assert result["reason"] == "transport_failed"
    assert "secret-token" not in repr(result)
    assert data.finished[0][1:] == ("unknown", "transport_failed", 30)


@pytest.mark.asyncio
async def test_lost_lease_during_blocking_send_never_finishes_stale_delivery():
    release = threading.Event()
    data = FakeData(lease())

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
