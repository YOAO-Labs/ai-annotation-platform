"""账户恢复：邮件投递、统一响应、单次消费和会话失效。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aap_identity.recovery_contract import (
    RecoveryFailure,
    decode_recovery_request,
)
from aap_identity.recovery_crypto import token_hash
from aap_identity.recovery_manager import RecoveryManager
from app.core.ratelimit import limiter
from app.core.security import (
    create_access_token,
    hash_password,
    verify_password,
)
from app.core.token_blacklist import get_user_generation
from app.db.models.password_reset_token import PasswordResetToken
from app.db.models.user import User
from app.services.identity_recovery_data import IdentityRecoveryCommandData
from tests.identity_recovery_helpers import credential_mail_capture, seed_challenge  # noqa: F401

pytestmark = pytest.mark.asyncio

_RECOVERY_SECRET = "identity-recovery-test-secret"


def _consume_request(operation_id: UUID, token: str, new_password: str):
    return decode_recovery_request(
        {
            "schema": "aap-identity-recovery/v1",
            "operation": "consume_password_reset",
            "operation_id": str(operation_id),
            "audit_context": {
                "method": "POST",
                "path": "/api/v1/auth/reset-password",
                "ip": "127.0.0.1",
                "request_id": str(operation_id),
            },
            "payload": {"token": token, "new_password": new_password},
        }
    )


def _recovery_manager(data):
    return RecoveryManager(data, _RECOVERY_SECRET, None)


class _RecoveryTransactionProxy:
    def __init__(
        self,
        db,
        transaction,
        *,
        prepared=None,
        release=None,
        before_prepare=None,
        backend_pid=None,
    ):
        self.db = db
        self.transaction = transaction
        self.prepared = prepared
        self.release = release
        self.before_prepare = before_prepare
        self.backend_pid = backend_pid

    async def prepare_recovery_command(self, payload):
        if self.before_prepare is not None:
            self.backend_pid["value"] = await self.db.scalar(
                text("SELECT pg_catalog.pg_backend_pid()")
            )
            self.before_prepare.set()
        result = await self.transaction.prepare_recovery_command(payload)
        if self.prepared is not None:
            self.prepared.set()
            await self.release.wait()
        return result

    async def apply_recovery_command(self, payload):
        return await self.transaction.apply_recovery_command(payload)


class _RecoveryDataProxy:
    def __init__(self, db, **gate):
        self.data = IdentityRecoveryCommandData(db)
        self.gate = gate

    @asynccontextmanager
    async def transaction(self):
        async with self.data.transaction() as transaction:
            yield _RecoveryTransactionProxy(self.data.db, transaction, **self.gate)


async def _wait_for_lock_wait(test_engine, backend_pid: int):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        async with maker() as observer:
            wait_event = await observer.scalar(
                text(
                    "SELECT wait_event_type FROM pg_catalog.pg_stat_activity "
                    "WHERE pid=:pid"
                ),
                {"pid": backend_pid},
            )
        if wait_event == "Lock":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Recovery peer did not wait on the held account row lock")


async def _cleanup_recovery(test_engine, actor_id: UUID, operation_ids: list[UUID]):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as cleanup:
        await cleanup.execute(text("SET LOCAL app.allow_audit_update='true'"))
        await cleanup.execute(
            text(
                "DELETE FROM public.identity_recovery_operation_receipts "
                "WHERE operation_id = ANY(CAST(:operation_ids AS uuid[]))"
            ),
            {"operation_ids": operation_ids},
        )
        await cleanup.execute(
            text("DELETE FROM public.audit_logs WHERE actor_id=:actor_id"),
            {"actor_id": actor_id},
        )
        await cleanup.execute(
            text(
                "DELETE FROM public.identity_credential_mail_outbox "
                "WHERE actor_id=:actor_id"
            ),
            {"actor_id": actor_id},
        )
        await cleanup.execute(
            text(
                "DELETE FROM public.email_verification_tokens WHERE user_id=:actor_id"
            ),
            {"actor_id": actor_id},
        )
        await cleanup.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == actor_id)
        )
        await cleanup.execute(
            text("DELETE FROM public.api_keys WHERE user_id=:actor_id"),
            {"actor_id": actor_id},
        )
        await cleanup.execute(delete(User).where(User.id == actor_id))


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    limiter.reset()
    yield
    limiter.reset()


async def test_forgot_password_sends_reset_email_and_keeps_response_uniform(
    httpx_client,
    annotator,
    db_session: AsyncSession,
    credential_mail_capture,  # noqa: F811
    caplog,
):
    user, _ = annotator
    caplog.set_level(logging.INFO)
    created_before = datetime.now(UTC)

    response = await httpx_client.post(
        "/api/v1/auth/forgot-password",
        json={"email": user.email, "captcha_token": None},
    )

    assert response.status_code == 202
    assert "如果该邮箱已注册" in response.json()["message"]
    tokens = credential_mail_capture.tokens()
    assert len(tokens) == 1
    token = tokens[0]
    row = await db_session.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash(token)
        )
    )
    assert row is not None
    assert row.used_at is None
    assert row.issued_email == user.email
    assert created_before < row.expires_at <= datetime.now(UTC) + timedelta(hours=1)
    assert token not in caplog.text

    missing = await httpx_client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "missing@example.com", "captcha_token": None},
    )
    assert missing.status_code == response.status_code
    assert missing.json() == response.json()


async def test_forgot_password_smtp_failure_keeps_uniform_response_and_invalidates_token(
    httpx_client,
    annotator,
    db_session: AsyncSession,
    credential_mail_capture,  # noqa: F811
    monkeypatch,
    caplog,
):
    user, _ = annotator
    caplog.set_level(logging.INFO)
    import aap_mail_transport

    from aap_mail_transport import MailDeliveryResult, MailOutcome, MailReason

    def reject(config, message):
        credential_mail_capture.messages.append(message)
        return MailDeliveryResult(MailOutcome.REJECTED, MailReason.NOT_CONFIGURED)

    monkeypatch.setattr(aap_mail_transport, "send_message", reject)

    response = await httpx_client.post(
        "/api/v1/auth/forgot-password",
        json={"email": user.email, "captcha_token": None},
    )

    assert response.status_code == 202
    tokens = credential_mail_capture.tokens()
    assert len(tokens) == 1
    token = tokens[0]
    row = await db_session.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash(token)
        )
    )
    assert row is not None
    assert row.used_at is not None
    assert token not in caplog.text

    missing = await httpx_client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "missing@example.com", "captcha_token": None},
    )
    assert missing.status_code == response.status_code
    assert missing.json() == response.json()


async def test_reset_password_is_single_use_and_invalidates_old_sessions(
    httpx_client,
    annotator,
    db_session: AsyncSession,
):
    user, _ = annotator
    reset_token = await seed_challenge(db_session, user)

    old_session = create_access_token(subject=str(user.id), role=user.role, gen=0)
    response = await httpx_client.post(
        "/api/v1/auth/reset-password",
        json={"token": reset_token, "new_password": "ResetStrong1"},
    )

    assert response.status_code == 200, response.text
    assert verify_password("ResetStrong1", user.password_hash)

    old_me = await httpx_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {old_session}"},
    )
    assert old_me.status_code == 401

    reused = await httpx_client.post(
        "/api/v1/auth/reset-password",
        json={"token": reset_token, "new_password": "ResetStrong2"},
    )
    assert reused.status_code == 400
    assert "无效或已过期" in reused.text

    login = await httpx_client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "ResetStrong1"},
    )
    assert login.status_code == 200, login.text


async def test_reset_password_rejects_expired_token(
    httpx_client,
    annotator,
    db_session: AsyncSession,
):
    user, _ = annotator
    raw_token = await seed_challenge(
        db_session,
        user,
        token="expired-token",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    response = await httpx_client.post(
        "/api/v1/auth/reset-password",
        json={"token": raw_token, "new_password": "ResetStrong1"},
    )

    assert response.status_code == 400
    assert "无效或已过期" in response.text


async def test_reset_password_rejects_token_for_deactivated_user(
    httpx_client,
    annotator,
    db_session: AsyncSession,
):
    user, _ = annotator
    reset_token = await seed_challenge(db_session, user)
    user.is_active = False
    await db_session.flush()

    response = await httpx_client.post(
        "/api/v1/auth/reset-password",
        json={"token": reset_token, "new_password": "ResetStrong1"},
    )

    assert response.status_code == 400
    row = await db_session.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash(reset_token)
        )
    )
    assert row is not None
    assert row.used_at is not None


async def test_concurrent_consumers_can_use_a_reset_token_only_once(test_engine):
    """Two owner transactions contend on the same challenge and only one completes."""
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    user_id = uuid4()
    email = f"concurrent-{uuid4().hex}@example.test"
    token_value = f"concurrent-{uuid4().hex}"
    operation_ids = [uuid4(), uuid4()]

    async with maker() as setup:
        user = User(
            id=user_id,
            email=email,
            name="Concurrent Reset",
            password_hash=hash_password("Test1234"),
            role="employee",
            is_active=True,
        )
        setup.add(user)
        await setup.flush()
        await seed_challenge(setup, user, token=token_value)
        await setup.commit()

    first_prepared = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    second_pid: dict[str, int] = {}
    first_session, second_session = maker(), maker()
    first_request = _consume_request(operation_ids[0], token_value, "ResetStrong1")
    second_request = _consume_request(operation_ids[1], token_value, "ResetStrong2")
    first = second = None

    async def consume(session, request, data):
        try:
            return await _recovery_manager(data).execute(request)
        finally:
            await session.close()

    try:
        first = asyncio.create_task(
            consume(
                first_session,
                first_request,
                _RecoveryDataProxy(
                    first_session,
                    prepared=first_prepared,
                    release=release_first,
                ),
            )
        )
        await asyncio.wait_for(first_prepared.wait(), timeout=5)
        second = asyncio.create_task(
            consume(
                second_session,
                second_request,
                _RecoveryDataProxy(
                    second_session,
                    before_prepare=second_started,
                    backend_pid=second_pid,
                ),
            )
        )
        await asyncio.wait_for(second_started.wait(), timeout=5)
        await _wait_for_lock_wait(test_engine, second_pid["value"])
        release_first.set()
        first_result = await first
        assert first_result.data.completed is True
        with pytest.raises(RecoveryFailure) as failed:
            await second
        assert failed.value.code == "invalid_request"
    finally:
        release_first.set()
        await asyncio.gather(
            *[task for task in (first, second) if task is not None],
            return_exceptions=True,
        )
        await first_session.close()
        await second_session.close()
        await _cleanup_recovery(test_engine, user_id, operation_ids)


async def test_reset_user_lock_serializes_old_password_login(test_engine):
    """The recovery owner holds the real User lock through password/generation commit."""
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    user_id = uuid4()
    token_value = f"login-race-{uuid4().hex}"
    email = f"login-race-{uuid4().hex}@example.test"
    operation_id = uuid4()

    async with maker() as setup:
        user = User(
            id=user_id,
            email=email,
            name="Login Race",
            password_hash=hash_password("OldStrong1"),
            role="employee",
            is_active=True,
        )
        setup.add(user)
        await setup.flush()
        await seed_challenge(setup, user, token=token_value)
        await setup.commit()

    reset_prepared = asyncio.Event()
    release_reset = asyncio.Event()
    login_started = asyncio.Event()
    login_pid: dict[str, int] = {}
    reset_session = maker()
    reset_request = _consume_request(operation_id, token_value, "NewStrong1")

    async def reset_password_in_transaction():
        try:
            return await _recovery_manager(
                _RecoveryDataProxy(
                    reset_session,
                    prepared=reset_prepared,
                    release=release_reset,
                )
            ).execute(reset_request)
        finally:
            await reset_session.close()

    async def login_with_old_password() -> tuple[bool, int]:
        async with maker() as session:
            async with session.begin():
                login_pid["value"] = await session.scalar(
                    text("SELECT pg_catalog.pg_backend_pid()")
                )
                login_started.set()
                user = await session.scalar(
                    select(User).where(User.email == email).with_for_update()
                )
                assert user is not None
                return (
                    verify_password("OldStrong1", user.password_hash),
                    await get_user_generation(str(user.id), db=session),
                )

    reset_task = login_task = None
    try:
        reset_task = asyncio.create_task(reset_password_in_transaction())
        await asyncio.wait_for(reset_prepared.wait(), timeout=5)
        login_task = asyncio.create_task(login_with_old_password())
        await asyncio.wait_for(login_started.wait(), timeout=5)
        await _wait_for_lock_wait(test_engine, login_pid["value"])
        release_reset.set()
        await reset_task
        old_password_valid, observed_generation = await login_task
        assert old_password_valid is False
        assert observed_generation >= 1
    finally:
        release_reset.set()
        await asyncio.gather(
            *[task for task in (reset_task, login_task) if task],
            return_exceptions=True,
        )
        await _cleanup_recovery(test_engine, user_id, [operation_id])
