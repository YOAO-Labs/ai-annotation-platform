"""Real mail leases, expiry fences and uncertain SMTP outcomes on owned state."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
import secrets
import time
import uuid

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import text

from aap_identity.mail_postgres import MailPostgresData, MailSettingsPostgresData
from aap_identity.recovery_contract import decode_recovery_request
from aap_identity.recovery_crypto import token_hash
from aap_identity.recovery_manager import RecoveryManager
from aap_identity.recovery_postgres import RecoveryPostgresData
from aap_identity_runtime.mail_worker import MailExecutor
from tests.identity_recovery_live_helpers import SMTPInbox, cleanup, command, outbox
from tests.test_identity_admission_live import _dsn, _world
from tests.test_identity_admission_live import ROOT
from tests.test_identity_recovery import _postgres_tool

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data roles",
    ),
]


async def _issue(
    test_engine, actor, operation, key, *, purpose="request_password_reset"
):
    request = decode_recovery_request(
        command(
            purpose,
            {
                "email": f"identity-live-{actor}@example.test",
                **(
                    {"captcha_verified": True}
                    if purpose == "request_password_reset"
                    else {}
                ),
            },
            operation_id=operation,
        )
    )
    await RecoveryManager(
        RecoveryPostgresData(_dsn("RECOVERY", test_engine)),
        secrets.token_urlsafe(32),
        key,
    ).execute(request)


async def _wait_mail_lock(maker):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        async with maker() as db:
            blocked = await db.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name='aap-identity-credential-mail' AND wait_event_type='Lock'"
                )
            )
        if blocked:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Mail capability did not wait on the owned User lock")


@pytest.mark.parametrize("operation", ["renew", "finish"])
async def test_mail_cannot_revive_or_complete_a_lease_that_expires_during_lock_wait(
    test_engine, operation
):
    maker, actor = await _world(test_engine)
    operation_id = uuid.uuid4()
    mail = MailPostgresData(_dsn("MAIL", test_engine))
    task = None
    blocker = maker()
    try:
        await _issue(test_engine, actor, operation_id, Fernet.generate_key().decode())
        lease = await mail.claim_mail(
            uuid.uuid4(), lease_seconds=1, delivery_id=operation_id
        )
        assert lease is not None
        await blocker.execute(
            text("SELECT id FROM users WHERE id=:id FOR UPDATE"), {"id": actor}
        )
        action = (
            mail.renew_mail(lease)
            if operation == "renew"
            else mail.finish_mail(
                lease, outcome="delivered", reason="sent", retry_delay_seconds=None
            )
        )
        task = asyncio.create_task(action)
        await _wait_mail_lock(maker)
        remaining = (lease.lease_expires_at - datetime.now(UTC)).total_seconds()
        await asyncio.sleep(max(0, remaining) + 0.1)
        await blocker.commit()
        assert await task is False
        stale = await outbox(maker, operation_id)
        assert stale.status == "leased" and stale.attempt == 1
        assert stale.lease_expires_at == lease.lease_expires_at
        successor = await mail.claim_mail(
            uuid.uuid4(), lease_seconds=5, delivery_id=operation_id
        )
        assert successor is not None and successor.attempt == 2
        assert successor.lease_token != lease.lease_token
        assert not await mail.finish_mail(
            lease, outcome="delivered", reason="sent", retry_delay_seconds=None
        )
        assert await mail.finish_mail(
            successor, outcome="delivered", reason="sent", retry_delay_seconds=None
        )
    finally:
        await blocker.rollback()
        await blocker.close()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await cleanup(maker, [actor], [operation_id])


async def test_mail_claim_does_not_send_a_challenge_that_expires_while_waiting(
    test_engine,
):
    maker, actor = await _world(test_engine)
    operation_id = uuid.uuid4()
    mail = MailPostgresData(_dsn("MAIL", test_engine))
    blocker = maker()
    task = None
    try:
        await _issue(test_engine, actor, operation_id, Fernet.generate_key().decode())
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE password_reset_tokens SET expires_at=clock_timestamp()+interval '1 second' WHERE id=:id"
                ),
                {"id": operation_id},
            )
            expires = await db.scalar(
                text(
                    "UPDATE identity_credential_mail_outbox SET expires_at=clock_timestamp()+interval '1 second' WHERE id=:id RETURNING expires_at"
                ),
                {"id": operation_id},
            )
        await blocker.execute(
            text("SELECT id FROM users WHERE id=:id FOR UPDATE"), {"id": actor}
        )
        task = asyncio.create_task(
            mail.claim_mail(uuid.uuid4(), delivery_id=operation_id)
        )
        await _wait_mail_lock(maker)
        await asyncio.sleep(max(0, (expires - datetime.now(UTC)).total_seconds()) + 0.1)
        await blocker.commit()
        assert await task is None
        assert (await outbox(maker, operation_id)).status == "expired"
    finally:
        await blocker.rollback()
        await blocker.close()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await cleanup(maker, [actor], [operation_id])


async def test_uncertain_smtp_and_wrong_key_keep_the_same_valid_link_for_retry(
    test_engine, record_property
):
    maker, actor = await _world(test_engine)
    operation_id, key = uuid.uuid4(), Fernet.generate_key().decode()
    inbox = SMTPInbox(lose_ack=1)
    mail = MailPostgresData(_dsn("MAIL", test_engine))
    settings = MailSettingsPostgresData(_dsn("MAIL_SETTINGS", test_engine))

    async def policy():
        return await settings.mail_delivery_policy(
            {
                "smtp_host": "127.0.0.1",
                "smtp_port": inbox.port,
                "smtp_user": "",
                "smtp_password": "",
                "smtp_from": "noreply@example.test",
            }
        )

    async def available():
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE identity_credential_mail_outbox SET available_at=clock_timestamp() WHERE id=:id"
                ),
                {"id": operation_id},
            )

    try:
        await _issue(test_engine, actor, operation_id, key)
        before = await outbox(maker, operation_id)
        first = await MailExecutor(mail, policy, key, retry_delay_seconds=1).run_once(
            operation_id
        )
        assert first["outcome"] == "unknown" and first["accepted"] is True
        await inbox.wait(1)
        after_unknown = await outbox(maker, operation_id)
        assert after_unknown.status == "pending" and after_unknown.attempt == 1
        await available()
        wrong = await MailExecutor(
            mail, policy, Fernet.generate_key().decode(), retry_delay_seconds=1
        ).run_once(operation_id)
        assert wrong["outcome"] == "invalid_key" and wrong["accepted"] is True
        assert len(inbox.messages) == 1
        await available()
        final = await MailExecutor(mail, policy, key).run_once(operation_id)
        assert final["outcome"] == "delivered" and final["accepted"] is True
        await inbox.wait(2)
        tokens = inbox.tokens()
        assert len(tokens) == 2 and secrets.compare_digest(tokens[0], tokens[1])
        assert inbox.messages[0]["Message-ID"] == inbox.messages[1]["Message-ID"]
        after = await outbox(maker, operation_id)
        assert after.status == "delivered" and after.attempt == 3
        assert after.token_hash == before.token_hash == token_hash(tokens[0])
        assert after.token_ciphertext == before.token_ciphertext
        async with maker() as db:
            assert (
                await db.scalar(
                    text("SELECT used_at FROM password_reset_tokens WHERE id=:id"),
                    {"id": operation_id},
                )
                is None
            )
        record_property(
            "smtp_recovery",
            "lost-DATA-ack,wrong-key,unchanged-link,stable-message-id,restricted-settings",
        )
    finally:
        inbox.close()
        await cleanup(maker, [actor], [operation_id])


async def test_legacy_runtime_grants_keep_embedded_recovery_and_mail_working(
    test_engine,
):
    from sqlalchemy.engine import make_url

    maker, actor = await _world(test_engine)
    operation_id, key = uuid.uuid4(), Fernet.generate_key().decode()
    try:
        _postgres_tool(
            test_engine,
            [
                "psql",
                "-U",
                "owner",
                "-d",
                test_engine.url.database,
                "-v",
                "ON_ERROR_STOP=1",
                "-v",
                f"expected_database={test_engine.url.database}",
                "-v",
                "migration_role=owner",
                "-v",
                "application_role=legacy_runtime",
                "-v",
                "asset_cleanup_role=asset_cleanup_client",
            ],
            body=(ROOT / "infra/docker/lan-production-grants.sql").read_bytes(),
        )
        dsn = (
            make_url(os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"])
            .set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )
        await RecoveryManager(
            RecoveryPostgresData(dsn), "legacy-recovery-fixture", key
        ).execute(
            decode_recovery_request(
                command(
                    "request_password_reset",
                    {
                        "email": f"identity-live-{actor}@example.test",
                        "captcha_verified": True,
                    },
                    operation_id=operation_id,
                )
            )
        )
        policy = await MailSettingsPostgresData(dsn).mail_delivery_policy(
            {
                "smtp_host": "",
                "smtp_port": 0,
                "smtp_user": "",
                "smtp_password": "",
                "smtp_from": "",
            }
        )
        result = await MailExecutor(MailPostgresData(dsn), policy, key).run_once(
            operation_id
        )
        assert result["outcome"] == "rejected" and result["reason"] == "not_configured"
        assert (await outbox(maker, operation_id)).status == "rejected"
    finally:
        await cleanup(maker, [actor], [operation_id])


async def test_existing_frontend_url_above_2048_characters_still_delivers(test_engine):
    maker, actor = await _world(test_engine)
    operation_id, key = uuid.uuid4(), Fernet.generate_key().decode()
    inbox = SMTPInbox()
    frontend = "https://app.example.test/" + "mounted-path/" * 180
    try:
        await RecoveryManager(
            RecoveryPostgresData(_dsn("RECOVERY", test_engine)),
            "long-frontend-fixture",
            key,
            frontend_base_url=frontend,
        ).execute(
            decode_recovery_request(
                command(
                    "request_password_reset",
                    {
                        "email": f"identity-live-{actor}@example.test",
                        "captcha_verified": True,
                    },
                    operation_id=operation_id,
                )
            )
        )
        result = await MailExecutor(
            MailPostgresData(_dsn("MAIL", test_engine)),
            {
                "smtp_host": "127.0.0.1",
                "smtp_port": inbox.port,
                "smtp_user": "",
                "smtp_password": "",
                "smtp_from": "noreply@example.test",
            },
            key,
        ).run_once(operation_id)
        assert result["outcome"] == "delivered" and result["accepted"] is True
        await inbox.wait(1)
        assert frontend + "reset-password?token=" in inbox.messages[0].get_content()
        assert len(inbox.tokens()) == 1
    finally:
        inbox.close()
        await cleanup(maker, [actor], [operation_id])
