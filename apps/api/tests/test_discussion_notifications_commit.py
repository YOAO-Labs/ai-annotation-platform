"""Real-commit HTTP regressions for discussion notification delivery.

The normal ``db_session`` fixture is SAVEPOINT-bound and cannot prove that a
second PostgreSQL connection sees a committed notification while the route's
post-commit publish hook runs. These tests therefore use an explicitly opted-in
disposable database and fresh sessions for setup, each HTTP request, publishing,
and assertions.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.security import create_access_token
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.audit_log import AuditLog
from app.db.models.notification import Notification
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.deps import get_db
from app.main import app
from tests.factory import create_batch, create_project, create_task, create_user


_REAL_COMMIT_URL_ENV = "DISCUSSION_REAL_COMMIT_TEST_DATABASE_URL"
_DISPOSABLE_DB_PREFIX = "annotation_discussion_"
_DISPOSABLE_DB_SUFFIX = "_notifications_commit_test"
_FORCE_COMMIT_FAILURE = "discussion_test_force_commit_failure"


@dataclass(frozen=True)
class _ReplyFixture:
    project_id: uuid.UUID
    batch_id: uuid.UUID
    task_id: uuid.UUID
    root_id: uuid.UUID
    root_author_id: uuid.UUID
    actor_id: uuid.UUID
    actor_token: str
    user_ids: tuple[uuid.UUID, ...]


def _real_commit_test_url(normal_url: str) -> str:
    """Return the explicitly opted-in URL without exposing credentials."""

    raw_url = os.environ.get(_REAL_COMMIT_URL_ENV)
    if not raw_url:
        pytest.skip(f"set {_REAL_COMMIT_URL_ENV} to run the disposable DB test")

    real = make_url(raw_url)
    normal = make_url(normal_url)
    database = real.database or ""
    if not (
        real.drivername == "postgresql+asyncpg"
        and database.startswith(_DISPOSABLE_DB_PREFIX)
        and database.endswith(_DISPOSABLE_DB_SUFFIX)
    ):
        pytest.fail(
            "the real-commit test URL must use postgresql+asyncpg and a clearly "
            "disposable annotation_discussion_*_notifications_commit_test database"
        )
    if database == normal.database or real.render_as_string(
        hide_password=True
    ) == normal.render_as_string(hide_password=True):
        pytest.fail("the real-commit test database must differ from TEST_DATABASE_URL")
    return real.render_as_string(hide_password=False)


async def _upgrade(url: str) -> None:
    """Alembic's async env must run outside pytest's active event loop."""

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")


async def _verify_database_identity(url: str) -> None:
    """Confirm the connection reaches the named disposable DB before migration."""

    expected = make_url(url).database
    engine = create_async_engine(url, echo=False)
    try:
        async with engine.connect() as connection:
            actual = await connection.scalar(text("SELECT current_database()"))
        if actual != expected:
            pytest.fail("the real-commit connection reached an unexpected database")
    finally:
        await engine.dispose()


async def _seed_committed_fixture(
    session_factory: async_sessionmaker[AsyncSession],
) -> _ReplyFixture:
    """Seed and commit all route prerequisites before opening the HTTP client."""

    async with session_factory() as db:
        owner = await create_user(
            db, "super_admin", f"n1-owner-{uuid.uuid4().hex}@test.local", "N1 owner"
        )
        root_author = await create_user(
            db,
            "employee",
            f"n1-author-{uuid.uuid4().hex}@test.local",
            "N1 author",
        )
        actor = await create_user(
            db,
            "employee",
            f"n1-actor-{uuid.uuid4().hex}@test.local",
            "N1 actor",
        )
        project = await create_project(db, owner_id=owner.id)
        batch = await create_batch(db, project_id=project.id, status="active")
        task = await create_task(db, project_id=project.id)
        batch.annotator_id = root_author.id
        task.batch_id = batch.id
        db.add_all(
            [
                ProjectMember(
                    project_id=project.id,
                    user_id=root_author.id,
                    role="annotator",
                    assigned_by=owner.id,
                ),
                ProjectMember(
                    project_id=project.id,
                    user_id=actor.id,
                    role="reviewer",
                    assigned_by=owner.id,
                ),
            ]
        )
        root = AnnotationFeedback(
            id=uuid.uuid4(),
            kind="issue",
            anchor_type="task",
            project_id=project.id,
            task_id=task.id,
            annotation_id=None,
            anchor_position=None,
            severity="medium",
            title="Committed root",
            body="root seeded before the HTTP request",
            author_id=root_author.id,
            attachments=[],
            thread_parent_id=None,
            status="open",
            is_active=True,
        )
        db.add(root)
        await db.commit()

    return _ReplyFixture(
        project_id=project.id,
        batch_id=batch.id,
        task_id=task.id,
        root_id=root.id,
        root_author_id=root_author.id,
        actor_id=actor.id,
        actor_token=create_access_token(subject=str(actor.id), role="reviewer"),
        user_ids=(owner.id, root_author.id, actor.id),
    )


async def _post_reply(
    client: httpx.AsyncClient,
    fixture: _ReplyFixture,
    route_kind: str,
    body: str,
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {fixture.actor_token}"}
    if route_kind == "generic":
        return await client.post(
            "/api/v1/feedbacks",
            json={
                "kind": "comment",
                "anchor_type": "task",
                "project_id": str(fixture.project_id),
                "task_id": str(fixture.task_id),
                "body": body,
                "thread_parent_id": str(fixture.root_id),
            },
            headers=headers,
        )
    return await client.post(
        f"/api/v1/feedbacks/{fixture.root_id}/replies",
        json={"body": body},
        headers=headers,
    )


async def _post_task_comment(
    client: httpx.AsyncClient,
    fixture: _ReplyFixture,
    body: str,
    mentioned_user_id: uuid.UUID,
) -> httpx.Response:
    display_name = "N1 author"
    return await client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "comment",
            "anchor_type": "task",
            "project_id": str(fixture.project_id),
            "task_id": str(fixture.task_id),
            "body": f"@{display_name} {body}",
            "mentions": [
                {
                    "userId": str(mentioned_user_id),
                    "displayName": display_name,
                    "offset": 0,
                    "length": len(display_name) + 1,
                }
            ],
        },
        headers={"Authorization": f"Bearer {fixture.actor_token}"},
    )


async def _cleanup_fixture(
    session_factory: async_sessionmaker[AsyncSession],
    fixture: _ReplyFixture,
    *,
    extra_feedback_ids: tuple[uuid.UUID, ...] = (),
) -> None:
    """Delete only rows owned by this test's explicit IDs."""

    async with session_factory() as db:
        # The audit table is append-only by default. Its documented GDPR cleanup
        # escape hatch is scoped to this cleanup transaction only.
        await db.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
        await db.execute(
            delete(Notification).where(Notification.target_id == fixture.root_id)
        )
        if extra_feedback_ids:
            await db.execute(
                delete(Notification).where(
                    Notification.target_id.in_(extra_feedback_ids)
                )
            )
            await db.execute(
                delete(AnnotationFeedback).where(
                    AnnotationFeedback.id.in_(extra_feedback_ids)
                )
            )
        await db.execute(
            delete(AuditLog).where(
                AuditLog.actor_id.in_(fixture.user_ids),
            )
        )
        # Reply IDs are generated inside the route. The root ID is the explicit
        # parent scope, and no other test can reference this freshly generated root.
        await db.execute(
            delete(AnnotationFeedback).where(
                AnnotationFeedback.thread_parent_id == fixture.root_id
            )
        )
        await db.execute(
            delete(AnnotationFeedback).where(AnnotationFeedback.id == fixture.root_id)
        )
        await db.execute(
            delete(ProjectMember).where(ProjectMember.project_id == fixture.project_id)
        )
        await db.execute(delete(Task).where(Task.id == fixture.task_id))
        await db.execute(delete(TaskBatch).where(TaskBatch.id == fixture.batch_id))
        from app.db.models.project import Project

        await db.execute(delete(Project).where(Project.id == fixture.project_id))
        await db.execute(delete(User).where(User.id.in_(fixture.user_ids)))
        await db.commit()


@asynccontextmanager
async def _http_client(
    session_factory: async_sessionmaker[AsyncSession],
    request_flags: dict[str, bool],
) -> AsyncIterator[httpx.AsyncClient]:
    """Route every dependency-injected request through a fresh DB session."""

    async def override_get_db():
        async with session_factory() as db:
            if request_flags.get("fail_commit"):
                db.info[_FORCE_COMMIT_FAILURE] = True
            try:
                yield db
            finally:
                await db.rollback()

    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("route_kind", ["generic", "dedicated"])
async def test_real_commit_http_reply_routes_publish_after_commit_and_survive_redis_failure(
    test_db_url, route_kind, monkeypatch
):
    """Both reply entry points commit business + notification rows before publish."""

    url = _real_commit_test_url(test_db_url)
    await _verify_database_identity(url)
    await _upgrade(url)
    engine = create_async_engine(url, echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    fixture = await _seed_committed_fixture(session_factory)
    request_flags: dict[str, bool] = {}
    publish_calls: list[dict] = []
    visibility_at_publish: list[tuple[bool, bool]] = []
    fail_redis = False

    async def fake_publish(*, user_id, message):
        nonlocal fail_redis
        publish_calls.append(message)
        payload = message["payload"]
        reply_id = uuid.UUID(payload["reply_id"])
        async with session_factory() as reader:
            notification = await reader.get(Notification, uuid.UUID(message["id"]))
            reply = await reader.get(AnnotationFeedback, reply_id)
            visibility_at_publish.append((notification is not None, reply is not None))
        if fail_redis:
            raise RuntimeError("redis unavailable after commit")

    monkeypatch.setattr("app.services.notification._publish", fake_publish)
    try:
        async with _http_client(session_factory, request_flags) as client:
            first = await _post_reply(
                client, fixture, route_kind, "first committed reply"
            )
            assert first.status_code == 200, first.text
            first_reply_id = uuid.UUID(first.json()["id"])
            assert len(publish_calls) == 1
            assert visibility_at_publish == [(True, True)]

            fail_redis = True
            second = await _post_reply(
                client, fixture, route_kind, "reply durable despite redis failure"
            )
            assert second.status_code == 200, second.text
            second_reply_id = uuid.UUID(second.json()["id"])
            assert len(publish_calls) == 2
            assert visibility_at_publish == [(True, True), (True, True)]

        async with session_factory() as reader:
            replies = list(
                (
                    await reader.execute(
                        select(AnnotationFeedback).where(
                            AnnotationFeedback.id.in_([first_reply_id, second_reply_id])
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert {reply.id for reply in replies} == {
                first_reply_id,
                second_reply_id,
            }
            notifications = list(
                (
                    await reader.execute(
                        select(Notification).where(
                            Notification.target_id == fixture.root_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(notifications) == 2
            assert {row.user_id for row in notifications} == {fixture.root_author_id}
    finally:
        await _cleanup_fixture(session_factory, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_commit_http_task_comment_mentions_publish_after_commit_and_survive_redis_failure(
    test_db_url, monkeypatch
):
    """Task-comment mentions are visible on a second connection during publish."""

    url = _real_commit_test_url(test_db_url)
    await _verify_database_identity(url)
    await _upgrade(url)
    engine = create_async_engine(url, echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    fixture = await _seed_committed_fixture(session_factory)
    request_flags: dict[str, bool] = {}
    publish_calls: list[dict] = []
    visibility_at_publish: list[tuple[bool, bool]] = []
    comment_ids: list[uuid.UUID] = []
    fail_redis = False

    async def fake_publish(*, user_id, message):
        nonlocal fail_redis
        publish_calls.append(message)
        comment_id = uuid.UUID(message["target_id"])
        async with session_factory() as reader:
            notification = await reader.get(Notification, uuid.UUID(message["id"]))
            comment = await reader.get(AnnotationFeedback, comment_id)
            visibility_at_publish.append(
                (notification is not None, comment is not None)
            )
        if fail_redis:
            raise RuntimeError("redis unavailable after commit")

    monkeypatch.setattr("app.services.notification._publish", fake_publish)
    try:
        async with _http_client(session_factory, request_flags) as client:
            first = await _post_task_comment(
                client, fixture, "first committed task comment", fixture.root_author_id
            )
            assert first.status_code == 200, first.text
            first_id = uuid.UUID(first.json()["id"])
            comment_ids.append(first_id)
            assert first.json()["mentions"][0]["userId"] == str(fixture.root_author_id)
            assert len(publish_calls) == 1
            assert visibility_at_publish == [(True, True)]

            fail_redis = True
            second = await _post_task_comment(
                client,
                fixture,
                "task comment durable despite redis failure",
                fixture.root_author_id,
            )
            assert second.status_code == 200, second.text
            second_id = uuid.UUID(second.json()["id"])
            comment_ids.append(second_id)
            assert len(publish_calls) == 2
            assert visibility_at_publish == [(True, True), (True, True)]

        async with session_factory() as reader:
            comments = list(
                (
                    await reader.execute(
                        select(AnnotationFeedback).where(
                            AnnotationFeedback.id.in_(comment_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert {comment.id for comment in comments} == set(comment_ids)
            notifications = list(
                (
                    await reader.execute(
                        select(Notification).where(
                            Notification.target_id.in_(comment_ids),
                            Notification.type == "feedback.comment_mentioned",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(notifications) == 2
            assert {row.user_id for row in notifications} == {fixture.root_author_id}
    finally:
        await _cleanup_fixture(
            session_factory, fixture, extra_feedback_ids=tuple(comment_ids)
        )
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_commit_http_task_comment_mention_rolls_back_without_publish(
    test_db_url, monkeypatch
):
    """A failed task-comment commit leaves neither feedback nor mention event."""

    url = _real_commit_test_url(test_db_url)
    await _verify_database_identity(url)
    await _upgrade(url)
    engine = create_async_engine(url, echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    fixture = await _seed_committed_fixture(session_factory)
    request_flags = {"fail_commit": True}
    publish_calls: list[dict] = []

    original_commit = AsyncSession.commit

    async def forced_commit_failure(self):
        if self.info.get(_FORCE_COMMIT_FAILURE):
            await self.rollback()
            raise RuntimeError("forced business commit failure")
        await original_commit(self)

    async def fake_publish(*, user_id, message):
        publish_calls.append(message)

    monkeypatch.setattr(AsyncSession, "commit", forced_commit_failure)
    monkeypatch.setattr("app.services.notification._publish", fake_publish)
    try:
        async with _http_client(session_factory, request_flags) as client:
            failed = await _post_task_comment(
                client, fixture, "task mention must roll back", fixture.root_author_id
            )
            assert failed.status_code == 500

        async with session_factory() as reader:
            comments = list(
                (
                    await reader.execute(
                        select(AnnotationFeedback).where(
                            AnnotationFeedback.task_id == fixture.task_id,
                            AnnotationFeedback.body
                            == "@N1 author task mention must roll back",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert comments == []
            notifications = list(
                (
                    await reader.execute(
                        select(Notification).where(
                            Notification.type == "feedback.comment_mentioned",
                            Notification.payload["task_id"].astext
                            == str(fixture.task_id),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert notifications == []
        assert publish_calls == []
    finally:
        await _cleanup_fixture(session_factory, fixture)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("route_kind", ["generic", "dedicated"])
async def test_real_commit_http_reply_route_rolls_back_without_publish(
    test_db_url, route_kind, monkeypatch
):
    """A failed business commit leaves neither reply nor deferred notification."""

    url = _real_commit_test_url(test_db_url)
    await _verify_database_identity(url)
    await _upgrade(url)
    engine = create_async_engine(url, echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    fixture = await _seed_committed_fixture(session_factory)
    request_flags = {"fail_commit": True}
    publish_calls: list[dict] = []

    original_commit = AsyncSession.commit

    async def forced_commit_failure(self):
        if self.info.get(_FORCE_COMMIT_FAILURE):
            await self.rollback()
            raise RuntimeError("forced business commit failure")
        await original_commit(self)

    async def fake_publish(*, user_id, message):
        publish_calls.append(message)

    monkeypatch.setattr(AsyncSession, "commit", forced_commit_failure)
    monkeypatch.setattr("app.services.notification._publish", fake_publish)
    try:
        async with _http_client(session_factory, request_flags) as client:
            failed = await _post_reply(
                client, fixture, route_kind, "reply must roll back"
            )
            assert failed.status_code == 500

        async with session_factory() as reader:
            replies = list(
                (
                    await reader.execute(
                        select(AnnotationFeedback).where(
                            AnnotationFeedback.thread_parent_id == fixture.root_id,
                            AnnotationFeedback.body == "reply must roll back",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert replies == []
            notifications = list(
                (
                    await reader.execute(
                        select(Notification).where(
                            Notification.target_id == fixture.root_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert notifications == []
        assert publish_calls == []
    finally:
        await _cleanup_fixture(session_factory, fixture)
        await engine.dispose()
