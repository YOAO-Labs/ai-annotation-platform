from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.async_job import AsyncJob
from app.db.models.notification import Notification
from app.db.models.notification_preference import NotificationPreference
from app.db.models.user import User
from app.services.async_job_notify import notify_job_terminal
from app.services.notification import NotificationService
from yoao_notifications.terminal_append import NotificationTerminalAppendManager
from yoao_notifications.terminal_append_contract import (
    TERMINAL_APPEND_PROTOCOL,
    NotificationTerminalAppendUnavailable,
    TerminalNotificationRequest,
)
from yoao_notifications.terminal_append_postgres import (
    NotificationTerminalAppendPostgresData,
)


@pytest.fixture
async def terminal_world(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    async with maker() as db:
        db.add(
            User(
                id=user_id,
                email=f"terminal-{user_id}@test.local",
                name="Terminal test",
                password_hash="unused-in-terminal-notification-test",
            )
        )
        await db.flush()
        db.add(
            AsyncJob(
                id=job_id,
                kind="batch_predict",
                user_id=user_id,
                status="completed",
                payload={},
                result={},
            )
        )
        await db.commit()

    yield maker, user_id, job_id

    async with maker() as db:
        await db.execute(delete(Notification).where(Notification.target_id == job_id))
        await db.execute(delete(AsyncJob).where(AsyncJob.id == job_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def _terminal_count(maker, job_id, notification_type="job.completed"):
    async with maker() as db:
        return await db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.target_type == "async_job",
                Notification.target_id == job_id,
                Notification.type == notification_type,
            )
        )


async def _outbox_ids(db, job_id):
    return list(
        await db.scalars(
            text(
                "SELECT notification_id FROM public.notification_delivery_outbox "
                "WHERE operation_id=:job_id"
            ),
            {"job_id": job_id},
        )
    )


async def _committed_outbox_ids(maker, job_id):
    async with maker() as db:
        return await _outbox_ids(db, job_id)


@pytest.mark.asyncio
async def test_terminal_dedupe_race_creates_one_row(terminal_world, monkeypatch):
    """Independent callers share one inbox row and one durable handoff."""
    maker, _user_id, job_id = terminal_world
    original_command = NotificationTerminalAppendPostgresData.command
    both_at_owner = asyncio.Event()
    owner_call_count = 0

    async def synchronized_command(self, request):
        nonlocal owner_call_count
        owner_call_count += 1
        if owner_call_count == 2:
            both_at_owner.set()
        await asyncio.wait_for(both_at_owner.wait(), timeout=5)
        return await original_command(self, request)

    monkeypatch.setattr(
        NotificationTerminalAppendPostgresData, "command", synchronized_command
    )

    async def run_once():
        async with maker() as db:
            await notify_job_terminal(db, job_id=job_id)
            await db.commit()

    await asyncio.wait_for(asyncio.gather(run_once(), run_once()), timeout=10)

    assert owner_call_count == 2
    assert await _terminal_count(maker, job_id) == 1
    assert len(await _committed_outbox_ids(maker, job_id)) == 1


@pytest.mark.asyncio
async def test_terminal_existing_row_does_not_create_another_handoff(terminal_world):
    maker, user_id, job_id = terminal_world
    async with maker() as db:
        db.add(
            Notification(
                id=uuid.uuid4(),
                user_id=user_id,
                type="job.completed",
                target_type="async_job",
                target_id=job_id,
                payload={"kind": "batch_predict", "status": "completed"},
            )
        )
        await db.commit()

    async with maker() as db:
        await notify_job_terminal(db, job_id=job_id)
        await db.commit()

    assert await _terminal_count(maker, job_id) == 1
    assert await _committed_outbox_ids(maker, job_id) == []


@pytest.mark.asyncio
async def test_terminal_manager_rehydrates_identity_and_respects_caller_rollback(
    db_session, annotator
):
    user, _ = annotator
    service = NotificationService(db_session)
    target_id = uuid.uuid4()
    savepoint = await db_session.begin_nested()
    row = await service.notify_terminal(
        user_id=user.id,
        type="job.completed",
        target_id=target_id,
        payload={"kind": "batch_predict", "status": "completed"},
    )
    assert row is not None
    assert row.created_at.tzinfo is not None
    assert await db_session.get(Notification, row.id) is row
    row_id = row.id
    assert await _outbox_ids(db_session, target_id) == [row_id]

    await savepoint.rollback()
    if row in db_session:
        db_session.expunge(row)
    assert await db_session.get(Notification, row_id) is None
    assert await _outbox_ids(db_session, target_id) == []


@pytest.mark.asyncio
async def test_terminal_handoff_is_invisible_until_caller_commit(
    terminal_world, monkeypatch
):
    maker, _user_id, job_id = terminal_world
    immediate_publish = AsyncMock()
    monkeypatch.setattr(NotificationService, "publish_committed", immediate_publish)
    async with maker() as db:
        await notify_job_terminal(db, job_id=job_id)
        assert len(await _outbox_ids(db, job_id)) == 1
        await asyncio.sleep(0.65)
        assert await _terminal_count(maker, job_id) == 0
        assert await _committed_outbox_ids(maker, job_id) == []
        await db.commit()

    assert await _terminal_count(maker, job_id) == 1
    assert len(await _committed_outbox_ids(maker, job_id)) == 1
    immediate_publish.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires verifier-owned PostgreSQL LOGINs and Redis",
)
async def test_scheduled_terminal_relay_uses_existing_restricted_bindings(
    test_engine, terminal_world, monkeypatch
):
    import asyncpg

    from app.config import settings
    from app.workers.celery_app import celery_app
    from app.workers.notification_delivery import relay_identity_notifications
    from tests.test_identity_admission_live import _dsn
    from tests.test_identity_module_assembly_live import _publisher_redis
    from tests.test_notifications_publisher_image_live import _subscribe
    from tests.test_workspace_project_authority_locking import _api_url

    maker, user_id, job_id = terminal_world
    strict_engine = create_async_engine(_api_url(test_engine))
    strict = async_sessionmaker(strict_engine, expire_on_commit=False)
    publisher_dsn = _dsn("NOTIFICATION_PUBLISH", test_engine)
    entry = celery_app.conf.beat_schedule["relay-notification-deliveries"]
    assert entry["task"] == relay_identity_notifications.name
    assert entry["schedule"].total_seconds() == 10
    try:
        async with strict() as db:
            assert (
                await db.scalar(text("SELECT session_user")) == "identity_api_consumer"
            )
            with pytest.raises(DBAPIError) as denied:
                async with db.begin_nested():
                    await _outbox_ids(db, job_id)
            assert denied.value.orig.sqlstate == "42501"
            await notify_job_terminal(db, job_id=job_id)
            await db.commit()
    finally:
        await strict_engine.dispose()
    [notification_id] = await _committed_outbox_ids(maker, job_id)

    reader = await asyncpg.connect(publisher_dsn)
    try:
        assert (
            await reader.fetchval("SELECT session_user")
            == "yoao_notification_publish_client"
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await reader.fetchval("SELECT id FROM public.notifications LIMIT 1")
        transaction = reader.transaction()
        await transaction.start()
        try:
            old_claim = json.loads(
                await reader.fetchval(
                    "SELECT yoao_notifications_v1.claim_identity_deliveries($1::jsonb)",
                    json.dumps(
                        {
                            "schema": "aap-notifications-delivery/v1",
                            "worker_id": str(uuid.uuid4()),
                            "limit": 100,
                            "lease_seconds": 1,
                        }
                    ),
                )
            )
            assert old_claim["ok"] is True
            assert all(
                item["target_type"] == "user" for item in old_claim["data"]["items"]
            )
        finally:
            await transaction.rollback()
    finally:
        await reader.close()

    monkeypatch.setattr(settings, "notification_publish_database_url", publisher_dsn)
    monkeypatch.setattr(settings, "notification_publish_database_url_file", "")
    async with _publisher_redis() as (redis_dsn, owner_redis):
        monkeypatch.setattr(settings, "redis_url", redis_dsn)
        async with owner_redis.pubsub() as subscriber:
            await _subscribe(subscriber, (f"notify:{user_id}",))
            result = await asyncio.to_thread(
                lambda: celery_app.tasks[entry["task"]].apply(throw=True).get()
            )
            assert result["published"] >= 1
            message = await subscriber.get_message(
                ignore_subscribe_messages=True, timeout=5
            )
            assert message is not None
            assert json.loads(message["data"])["id"] == str(notification_id)
    async with maker() as db:
        assert (
            await db.scalar(
                text(
                    "SELECT state FROM public.notification_delivery_outbox "
                    "WHERE notification_id=:id"
                ),
                {"id": notification_id},
            )
            == "done"
        )


@pytest.mark.asyncio
async def test_terminal_owner_refusal_keeps_job_transaction_usable(
    terminal_world, monkeypatch
):
    maker, _user_id, job_id = terminal_world

    async def refuse(_self, _request):
        return {
            "schema": "aap-notification-terminal-append/v1",
            "ok": False,
            "error": {"code": "invalid_request"},
        }

    monkeypatch.setattr(NotificationTerminalAppendPostgresData, "command", refuse)
    async with maker() as db:
        job = await db.get(AsyncJob, job_id)
        assert job is not None
        job.status = "failed"
        await notify_job_terminal(db, job_id=job_id)
        await db.commit()

    async with maker() as db:
        assert (
            await db.scalar(select(AsyncJob.status).where(AsyncJob.id == job_id))
            == "failed"
        )


@pytest.mark.asyncio
async def test_terminal_mute_uses_existing_preference_truthiness(terminal_world):
    maker, user_id, job_id = terminal_world
    async with maker() as db:
        db.add(
            NotificationPreference(
                user_id=user_id,
                type="job.completed",
                channels={"in_app": False},
            )
        )
        await db.commit()

    async with maker() as db:
        await notify_job_terminal(db, job_id=job_id)
        await db.commit()
    assert await _terminal_count(maker, job_id) == 0
    assert await _committed_outbox_ids(maker, job_id) == []

    async with maker() as db:
        preference = await db.get(NotificationPreference, (user_id, "job.completed"))
        assert preference is not None
        preference.channels = {"in_app": "false"}
        await db.commit()
    async with maker() as db:
        await notify_job_terminal(db, job_id=job_id)
        await db.commit()

    assert await _terminal_count(maker, job_id) == 1
    assert len(await _committed_outbox_ids(maker, job_id)) == 1


@pytest.mark.asyncio
async def test_terminal_scope_payload_has_no_frozen_0214_size_cap(terminal_world):
    maker, _user_id, job_id = terminal_world
    scope = "scope-value-" * 2_000
    async with maker() as db:
        job = await db.get(AsyncJob, job_id)
        assert job is not None
        job.payload = {"scope": scope}
        await notify_job_terminal(db, job_id=job_id)
        await db.commit()

    async with maker() as db:
        row = await db.scalar(
            select(Notification).where(
                Notification.target_type == "async_job",
                Notification.target_id == job_id,
                Notification.type == "job.completed",
            )
        )
    assert row is not None
    assert len(row.payload["scope"]) > 16_384
    assert row.payload["scope"] == scope
    assert await _committed_outbox_ids(maker, job_id) == [row.id]


@pytest.mark.asyncio
async def test_terminal_full_rollback_discards_inbox_and_handoff(terminal_world):
    maker, _user_id, job_id = terminal_world
    async with maker() as db:
        await notify_job_terminal(db, job_id=job_id)
        assert await _terminal_count(maker, job_id) == 0
        assert len(await _outbox_ids(db, job_id)) == 1
        await db.rollback()

    assert await _terminal_count(maker, job_id) == 0
    assert await _committed_outbox_ids(maker, job_id) == []


@pytest.mark.asyncio
async def test_terminal_no_autoflush_keeps_owner_before_full_job_flush(
    terminal_world, monkeypatch
):
    maker, _user_id, job_id = terminal_world
    owner_rows = []
    original_command = NotificationTerminalAppendPostgresData.command

    async def observe_append(self, request):
        result = await original_command(self, request)
        connection = await self.session.connection()
        row = await connection.execute(
            select(AsyncJob.status, Notification.type)
            .join(Notification, Notification.target_id == AsyncJob.id)
            .where(AsyncJob.id == job_id)
        )
        owner_rows.append(tuple(row.one()))
        return result

    monkeypatch.setattr(
        NotificationTerminalAppendPostgresData, "command", observe_append
    )
    async with maker() as db:
        job = await db.get(AsyncJob, job_id)
        assert job is not None
        job.status = "failed"
        with db.no_autoflush:
            await notify_job_terminal(db, job_id=job_id)
        assert owner_rows == [("completed", "job.failed")]
        await db.commit()
    async with maker() as db:
        assert (await db.get(AsyncJob, job_id)).status == "failed"
    assert await _terminal_count(maker, job_id, "job.failed") == 1
    assert len(await _committed_outbox_ids(maker, job_id)) == 1


@pytest.mark.asyncio
async def test_terminal_append_interoperates_when_frozen_0214_writer_wins(
    terminal_world, monkeypatch
):
    maker, user_id, job_id = terminal_world
    frozen = maker()
    owner_call_started = asyncio.Event()
    original_command = NotificationTerminalAppendPostgresData.command

    async def observe_owner_call(self, request):
        owner_call_started.set()
        return await original_command(self, request)

    monkeypatch.setattr(
        NotificationTerminalAppendPostgresData, "command", observe_owner_call
    )

    async def python_append():
        async with maker() as db:
            await notify_job_terminal(db, job_id=job_id)
            await db.commit()

    task = None
    try:
        await frozen.begin()
        frozen_id = await frozen.scalar(
            text(
                "SELECT yoao_notifications_v1.create_job_terminal_notification("
                ":user_id, :job_id, 'completed', "
                "CAST(:payload AS pg_catalog.jsonb))"
            ),
            {
                "user_id": user_id,
                "job_id": job_id,
                "payload": json.dumps({"kind": "batch_predict", "status": "completed"}),
            },
        )
        assert frozen_id is not None
        task = asyncio.create_task(python_append())
        await asyncio.wait_for(owner_call_started.wait(), timeout=5)
        await asyncio.sleep(0.05)
        assert not task.done()
        await frozen.commit()
        await asyncio.wait_for(task, timeout=5)
    finally:
        if frozen.in_transaction():
            await frozen.rollback()
        await frozen.close()
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    assert await _terminal_count(maker, job_id) == 1
    assert await _committed_outbox_ids(maker, job_id) == []


@pytest.mark.asyncio
async def test_terminal_append_interoperates_when_python_writer_wins(terminal_world):
    maker, user_id, job_id = terminal_world
    python_created = asyncio.Event()
    allow_python_commit = asyncio.Event()
    python_ids = []

    async def python_append():
        async with maker() as db:
            await notify_job_terminal(db, job_id=job_id)
            python_ids.extend(await _outbox_ids(db, job_id))
            python_created.set()
            await allow_python_commit.wait()
            await db.commit()

    python_task = asyncio.create_task(python_append())
    frozen_task = None
    try:
        await asyncio.wait_for(python_created.wait(), timeout=5)

        async def frozen_append():
            async with maker() as db:
                result = await db.scalar(
                    text(
                        "SELECT yoao_notifications_v1.create_job_terminal_notification("
                        ":user_id, :job_id, 'completed', "
                        "CAST(:payload AS pg_catalog.jsonb))"
                    ),
                    {
                        "user_id": user_id,
                        "job_id": job_id,
                        "payload": json.dumps(
                            {"kind": "batch_predict", "status": "completed"}
                        ),
                    },
                )
                await db.commit()
                return result

        frozen_task = asyncio.create_task(frozen_append())
        await asyncio.sleep(0.05)
        assert not frozen_task.done()
        allow_python_commit.set()
        await asyncio.wait_for(python_task, timeout=5)
        frozen_id = await asyncio.wait_for(frozen_task, timeout=5)
    finally:
        allow_python_commit.set()
        if not python_task.done():
            python_task.cancel()
            with suppress(asyncio.CancelledError):
                await python_task
        if frozen_task is not None and not frozen_task.done():
            frozen_task.cancel()
            with suppress(asyncio.CancelledError):
                await frozen_task

    assert len(python_ids) == 1
    assert frozen_id == python_ids[0]
    assert await _terminal_count(maker, job_id) == 1
    assert await _committed_outbox_ids(maker, job_id) == [frozen_id]


@pytest.mark.asyncio
async def test_terminal_append_rejects_non_read_committed_transactions(
    terminal_world,
):
    maker, user_id, job_id = terminal_world
    request = TerminalNotificationRequest(
        schema=TERMINAL_APPEND_PROTOCOL,
        operation="append_job_terminal",
        id=uuid.uuid4(),
        user_id=user_id,
        type="job.completed",
        target_id=job_id,
        payload={"kind": "batch_predict", "status": "completed"},
    )
    async with maker() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        with pytest.raises(NotificationTerminalAppendUnavailable):
            await NotificationTerminalAppendManager(
                NotificationTerminalAppendPostgresData(db)
            ).append(request)
        await db.commit()

    assert await _terminal_count(maker, job_id) == 0


@pytest.mark.asyncio
async def test_terminal_sql_failure_preserves_job_transition(
    terminal_world, monkeypatch
):
    """A caught owner SQL error must not roll back the enclosing job transition."""
    maker, _user_id, job_id = terminal_world
    sqlstates = []

    async def fail_in_sql(self, _request):
        try:
            return await self.session.scalar(
                text("SELECT yoao_notifications_v1.__missing_terminal_append_probe__()")
            )
        except DBAPIError as exc:
            sqlstates.append(getattr(exc.orig, "sqlstate", None))
            raise

    monkeypatch.setattr(NotificationTerminalAppendPostgresData, "command", fail_in_sql)
    async with maker() as db:
        job = await db.get(AsyncJob, job_id)
        assert job is not None
        job.status = "failed"
        await notify_job_terminal(db, job_id=job_id)
        await db.commit()

    assert sqlstates == ["42883"]

    async with maker() as db:
        assert (
            await db.scalar(select(AsyncJob.status).where(AsyncJob.id == job_id))
            == "failed"
        )
