from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
import types
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.mail_v2_contract import (
    MAIL_V2_NATIVE_PROTOCOL,
    InvitationMailV2Lease,
)
from app.services.identity_contracts import IdentityGatewayUnavailable

from app.services import identity_mail_v2_delivery as gateway

WORKER_ID = uuid.UUID("74000000-0000-4000-8000-000000000001")
DELIVERY_ID = uuid.UUID("74000000-0000-4000-8000-000000000002")
INVITATION_ID = uuid.UUID("74000000-0000-4000-8000-000000000003")
ISSUER_ID = uuid.UUID("74000000-0000-4000-8000-000000000004")
TOKEN = "opaque-邀请-token"
KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")


def _lease(worker_id: uuid.UUID = WORKER_ID) -> InvitationMailV2Lease:
    now = datetime.now(UTC)
    worker_id = uuid.UUID(str(worker_id))
    return InvitationMailV2Lease(
        id=DELIVERY_ID,
        issuer_id=ISSUER_ID,
        invitation_id=INVITATION_ID,
        purpose="invitation",
        email="recipient@example.test",
        frontend_base_url="https://labels.example.test/app",
        token_hash=hashlib.sha256(TOKEN.encode()).hexdigest(),
        token_ciphertext="encrypted-link-only",
        encryption_id="b" * 64,
        expires_at=now + timedelta(days=7),
        attempt=1,
        worker_id=worker_id,
        lease_token="74000000-0000-4000-8000-000000000005",
        lease_expires_at=now + timedelta(seconds=60),
        project_name="Vision project",
        project_member_role="annotator",
    )


def _lease_reply(worker_id: uuid.UUID = WORKER_ID) -> dict:
    lease = _lease(worker_id)
    return {
        "schema": MAIL_V2_NATIVE_PROTOCOL,
        "ok": True,
        "data": {"delivery": lease.model_dump(mode="json")},
    }


class FakeSession:
    def __init__(self, responder, sessions):
        self.responder = responder
        self.sessions = sessions
        self.closed = False
        self.commits = 0
        self.rollbacks = 0
        sessions.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def scalar(self, statement, params):
        return self.responder(str(statement), params)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class FakeSessionFactory:
    def __init__(self, responder):
        self.responder = responder
        self.sessions = []

    def __call__(self):
        return FakeSession(self.responder, self.sessions)


def _command(params):
    return json.loads(params["command"])


def _mail_reply(command: dict, *, accepted=True):
    assert command.get("schema", MAIL_V2_NATIVE_PROTOCOL) == MAIL_V2_NATIVE_PROTOCOL
    return {
        "schema": MAIL_V2_NATIVE_PROTOCOL,
        "ok": True,
        "data": {"accepted": accepted},
    }


def _api_settings(**overrides):
    values = {
        "identity_mail_v2_database_url": "",
        "identity_mail_v2_database_url_file": "",
        "identity_mail_settings_database_url": "",
        "identity_mail_settings_database_url_file": "",
        "identity_invitation_database_url": "",
        "identity_invitation_database_url_file": "",
        "identity_recovery_mail_key": "",
        "identity_recovery_mail_key_file": "",
        "smtp_host": "smtp.example.test",
        "smtp_port": 587,
        "smtp_user": "mailer",
        "smtp_password": "smtp-secret",
        "smtp_from": "noreply@example.test",
        "secret_key": "api-signing-secret",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def source_injected_mail_crypto_and_smtp(monkeypatch):
    crypto = types.ModuleType("aap_identity.recovery_crypto")
    crypto.mail_encryption_id = lambda _key: "b" * 64
    crypto.decrypt_mail_token = lambda _ciphertext, _key, _epoch: TOKEN
    crypto.token_hash = lambda value: hashlib.sha256(value.encode()).hexdigest()
    monkeypatch.setitem(sys.modules, "aap_identity.recovery_crypto", crypto)


def test_malformed_claim_and_claim_binding_failures_rollback_before_commit():
    from app.services.identity_mail_v2_delivery import IdentityMailV2CommandData

    malformed = {
        "schema": MAIL_V2_NATIVE_PROTOCOL,
        "ok": True,
        "data": {"delivery": {"unexpected": True}},
    }
    factory = FakeSessionFactory(lambda _sql, _params: json.dumps(malformed))
    with pytest.raises(IdentityUnavailable):
        asyncio.run(IdentityMailV2CommandData(factory).claim_mail(WORKER_ID))
    assert factory.sessions[0].rollbacks == 1
    assert factory.sessions[0].commits == 0
    assert factory.sessions[0].closed

    for reply, delivery_id in (
        (_lease_reply(uuid.uuid4()), None),
        (_lease_reply(), uuid.uuid4()),
    ):
        factory = FakeSessionFactory(
            lambda _sql, _params, value=reply: json.dumps(value)
        )
        with pytest.raises(IdentityUnavailable):
            asyncio.run(
                IdentityMailV2CommandData(factory).claim_mail(
                    WORKER_ID, delivery_id=delivery_id
                )
            )
        assert factory.sessions[0].rollbacks == 1
        assert factory.sessions[0].commits == 0
        assert factory.sessions[0].closed


@pytest.mark.parametrize("operation", ["renew_mail", "finish_mail"])
def test_stale_owner_false_reply_rolls_back_before_commit(operation):
    from app.services.identity_mail_v2_delivery import IdentityMailV2CommandData

    reply = _mail_reply({}, accepted=False)
    factory = FakeSessionFactory(lambda _sql, _params: json.dumps(reply))
    data = IdentityMailV2CommandData(factory)
    lease = _lease()

    if operation == "renew_mail":
        accepted = asyncio.run(data.renew_mail(lease))
    else:
        accepted = asyncio.run(
            data.finish_mail(
                lease,
                outcome="delivered",
                reason="sent",
                retry_delay_seconds=None,
            )
        )

    assert accepted is False
    assert factory.sessions[0].rollbacks == 1
    assert factory.sessions[0].commits == 0
    assert factory.sessions[0].closed


def test_embedded_gateway_closes_each_short_session_before_smtp(monkeypatch):
    from aap_identity_runtime.mail_worker import MailExecutor

    settings = _api_settings()
    monkeypatch.setattr(gateway, "settings", settings)
    monkeypatch.setattr(
        gateway,
        "get_identity_gateway",
        lambda: SimpleNamespace(identity_url=None, signing_secret="api-signing-secret"),
    )
    monkeypatch.setattr(gateway, "recovery_mail_key", lambda _secret: KEY)
    monkeypatch.setattr(
        gateway,
        "_smtp_fallbacks",
        lambda: {
            "smtp_host": "smtp.example.test",
            "smtp_port": 587,
            "smtp_user": "mailer",
            "smtp_password": "smtp-secret",
            "smtp_from": "noreply@example.test",
        },
    )

    settings_payload = {
        "smtp_host": "smtp.example.test",
        "smtp_port": 587,
        "smtp_user": "mailer",
        "smtp_password": "smtp-secret",
        "smtp_from": "noreply@example.test",
    }
    factory = FakeSessionFactory(
        lambda sql, params: (
            json.dumps(settings_payload)
            if "aap_settings_v1.mail_delivery_policy" in sql
            else json.dumps(_lease_reply(_command(params)["worker_id"]))
            if "aap_identity_mail_v2.claim_mail" in sql
            else json.dumps(_mail_reply(_command(params)))
        )
    )
    sent = []

    def send_sync(self, policy, message):
        assert factory.sessions
        assert all(session.closed for session in factory.sessions)
        sent.append((policy.smtp_host, message["Message-ID"]))
        return SimpleNamespace(outcome="delivered", reason="sent")

    monkeypatch.setattr(MailExecutor, "_send_sync", send_sync)
    result = asyncio.run(gateway.run_invitation_mail(session_factory=factory))

    assert result["state"] == "finished"
    assert sent[0][0] == "smtp.example.test"
    assert sent[0][1] == f"<aap.{DELIVERY_ID}.invitation@aap.invalid>"
    assert len(factory.sessions) == 4  # settings, claim, renew, finish
    assert all(session.closed for session in factory.sessions)
    assert all(
        session.commits == 1 and session.rollbacks == 0 for session in factory.sessions
    )


def test_gateway_requires_mail_v2_and_settings_dsn_pair_and_never_falls_back_for_bad_file(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        gateway,
        "get_identity_gateway",
        lambda: SimpleNamespace(identity_url=None, signing_secret="api-signing-secret"),
    )
    factory = FakeSessionFactory(lambda *_args: None)
    monkeypatch.setattr(
        gateway,
        "settings",
        _api_settings(identity_mail_v2_database_url="postgresql://mail-v2/db"),
    )
    with pytest.raises(IdentityGatewayUnavailable):
        asyncio.run(gateway.run_invitation_mail(session_factory=factory))
    assert factory.sessions == []

    factory = FakeSessionFactory(lambda *_args: None)
    monkeypatch.setattr(
        gateway,
        "settings",
        _api_settings(
            identity_mail_v2_database_url_file=str(tmp_path / "missing-mail-dsn")
        ),
    )
    with pytest.raises(IdentityGatewayUnavailable):
        asyncio.run(gateway.run_invitation_mail(session_factory=factory))
    assert factory.sessions == []


def test_remote_owner_skips_api_kick_unless_narrow_mail_sender_is_selected(monkeypatch):
    import app.services.identity_mail_v2_delivery as delivery

    remote = SimpleNamespace(
        identity_url="https://identity.example.test", signing_secret="remote-secret"
    )
    monkeypatch.setattr(delivery, "get_identity_gateway", lambda: remote)
    monkeypatch.setattr(delivery, "settings", _api_settings())
    factory = FakeSessionFactory(lambda *_args: None)
    monkeypatch.setattr(delivery, "async_session", factory)
    assert asyncio.run(delivery.kick_invitation_mail(DELIVERY_ID)) == {
        "state": "remote"
    }
    assert factory.sessions == []

    constructed = {}

    class FakeV2Data:
        def __init__(self, dsn):
            constructed["mail_dsn"] = dsn

    class FakePolicyData:
        def __init__(self, dsn):
            constructed["policy_dsn"] = dsn

    class FakeExecutor:
        def __init__(self, data, policy, key):
            constructed["data"] = data
            constructed["policy"] = policy
            constructed["key"] = key

        async def run_once(self, *, delivery_id=None):
            constructed["delivery_id"] = delivery_id
            return {"state": "idle"}

    monkeypatch.setattr(delivery, "MailV2PostgresData", FakeV2Data)
    monkeypatch.setattr(delivery, "MailSettingsPostgresData", FakePolicyData)
    monkeypatch.setattr(delivery, "MailExecutor", FakeExecutor)
    monkeypatch.setattr(delivery, "recovery_mail_key", lambda _secret: KEY)
    monkeypatch.setattr(
        delivery,
        "settings",
        _api_settings(
            identity_mail_v2_database_url="postgresql://narrow-mail-role/db",
            identity_mail_settings_database_url="postgresql://settings-role/db",
        ),
    )
    assert asyncio.run(delivery.kick_invitation_mail(DELIVERY_ID)) == {"state": "idle"}
    assert constructed["mail_dsn"] == "postgresql://narrow-mail-role/db"
    assert constructed["policy_dsn"] == "postgresql://settings-role/db"
    assert constructed["delivery_id"] == DELIVERY_ID
    assert factory.sessions == []


def test_celery_mail_relay_uses_fresh_loops_and_stops_after_sixteen(monkeypatch):
    import app.workers.identity_mail_v2_delivery as worker

    loops = []
    calls = []

    @asynccontextmanager
    async def session_factory_context():
        yield object()

    async def run_mail(*, session_factory):
        assert session_factory is not None
        loops.append(asyncio.get_running_loop())
        calls.append(True)
        return {"state": "processed"}

    monkeypatch.setattr(worker, "task_session_factory", session_factory_context)
    monkeypatch.setattr(worker, "run_invitation_mail", run_mail)

    first = worker.relay_invitation_mail.run()
    first_loop_ids = [id(loop) for loop in loops]
    second = worker.relay_invitation_mail.run()

    assert first == {"processed": 16, "state": "bounded"}
    assert second == {"processed": 16, "state": "bounded"}
    assert len(calls) == 32
    assert len(set(first_loop_ids)) == 1
    assert id(loops[0]) != id(loops[16])
    assert all(loop.is_closed() for loop in loops)
