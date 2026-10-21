"""Caller-session binding checks for the fixed Native Registry ingress port."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
import inspect

import pytest
from sqlalchemy import event, select, text

from aap_inference.registry_ingress import endpoint_snapshot, get_or_create_seed
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.user import User
from app.services import ml_backend_env_sync, screenshot_seed_backends
from app.services.inference_registry_ingress_data import (
    InferenceRegistryIngressData,
    _STAGES,
)


pytestmark = pytest.mark.asyncio


def _registry(url: str, *, source: str = "env", extra_params=None):
    return MLBackendRegistry(
        id=uuid.uuid4(),
        name="ingress-binding",
        url=url,
        source=source,
        state="disconnected",
        is_interactive=False,
        auth_method="none",
        auth_token=None,
        extra_params=extra_params if extra_params is not None else {},
        health_meta=None,
    )


async def _capture_sql(db_session):
    connection = (await db_session.connection()).sync_connection
    statements = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.lower().split()))

    event.listen(connection, "before_cursor_execute", observe)
    return connection, statements, observe


async def test_native_by_url_keeps_same_identity_and_unflushed_values(db_session):
    row = _registry("http://identity-map.test")
    db_session.add(row)
    await db_session.flush()
    db_session.autoflush = False
    row.name = "pending caller value"

    loaded = await InferenceRegistryIngressData(db_session).by_url(row.url)

    assert loaded is row
    assert loaded.name == "pending caller value"
    persisted_name = await db_session.scalar(
        text("SELECT name FROM public.ml_backend_registry WHERE id=:id"),
        {"id": row.id},
    )
    assert persisted_name == "ingress-binding"


async def test_native_registry_insert_precedes_pending_unrelated_uow_flush(db_session):
    pending = User(
        id=uuid.uuid4(),
        email=f"registry-ingress-{uuid.uuid4().hex}@example.test",
        name="Pending",
        password_hash="fixture",
        role="employee",
    )
    db_session.add(pending)
    connection, statements, observe = await _capture_sql(db_session)
    try:
        row = await InferenceRegistryIngressData(db_session).create_registry(
            "Native-created",
            "http://native-create.test",
            "env",
        )
    finally:
        event.remove(connection, "before_cursor_execute", observe)

    native_index = next(
        index
        for index, statement in enumerate(statements)
        if "aap_inference_registry_ingress_v1.create_registry" in statement
    )
    user_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("insert into users")
    )
    assert native_index < user_index
    assert row.source == "env"
    assert pending not in db_session.new


async def test_observation_stage_calls_native_at_flush_without_orm_registry_update(
    db_session,
):
    row = _registry("http://observation-binding.test")
    db_session.add(row)
    await db_session.flush()
    data = InferenceRegistryIngressData(db_session)
    snapshot = endpoint_snapshot(row)
    checked_at = datetime(2026, 10, 22, tzinfo=UTC)
    row.name = "observed"
    row.state = "connected"
    row.is_interactive = True
    row.health_meta = {"capabilities": {"is_interactive": True}}
    row.last_checked_at = checked_at
    await data.stage_observation(
        row,
        profile="env",
        usable_caps=True,
        expected_snapshot=snapshot,
    )

    connection, statements, observe = await _capture_sql(db_session)
    try:
        await data.flush()
    finally:
        event.remove(connection, "before_cursor_execute", observe)

    assert any(
        "aap_inference_registry_ingress_v1.apply_observation" in statement
        for statement in statements
    )
    assert not any(
        statement.startswith("update public.ml_backend_registry")
        for statement in statements
    )
    assert row.state == "connected"
    assert row.name == "observed"
    assert row.last_checked_at == checked_at
    assert row.updated_at is not None


async def test_registry_refresh_discards_a_staged_observation(db_session):
    row = _registry("http://refresh-invalidation.test")
    db_session.add(row)
    await db_session.flush()
    data = InferenceRegistryIngressData(db_session)
    snapshot = endpoint_snapshot(row)
    row.state = "error"
    row.error_message = "staged failure"
    row.last_checked_at = datetime(2026, 10, 22, tzinfo=UTC)
    await data.stage_observation(
        row,
        profile="seed",
        usable_caps=False,
        expected_snapshot=snapshot,
    )
    url = row.url
    db_session.expire(row)
    connection, statements, observe = await _capture_sql(db_session)
    try:
        with db_session.no_autoflush:
            assert await data.by_url(url) is row
        await data.flush()
    finally:
        event.remove(connection, "before_cursor_execute", observe)

    assert _STAGES not in db_session.sync_session.info
    assert not any(
        "aap_inference_registry_ingress_v1.apply_observation" in statement
        for statement in statements
    )
    assert row.state == "disconnected"
    assert row.error_message is None


async def test_seed_marker_preserves_pending_and_later_extra_param_edits(db_session):
    old_marker = {
        "managed_by": "screenshot-seed",
        "profile": "screenshots",
        "revision": "old",
    }
    new_marker = {
        "managed_by": "screenshot-seed",
        "profile": "screenshots",
        "revision": "screenshots-2026-08-g",
    }
    row = _registry(
        "http://seed-marker-binding.test",
        source="seed",
        extra_params={"persisted": "kept", "seed": old_marker},
    )
    db_session.add(row)
    await db_session.flush()
    data = InferenceRegistryIngressData(db_session)
    await data.create_singleton_pool(row, None, None)

    db_session.autoflush = False
    row.extra_params = {
        "persisted": "kept",
        "caller_before": "before marker",
        "seed": old_marker,
    }
    updated = await get_or_create_seed(
        data,
        row.url,
        "ingress-binding",
        new_marker,
    )
    assert updated is row
    row.extra_params["caller_after"] = "after marker"

    await data.flush()

    stored = await db_session.scalar(
        select(MLBackendRegistry.extra_params).where(MLBackendRegistry.id == row.id)
    )
    assert stored == {
        "persisted": "kept",
        "caller_before": "before marker",
        "caller_after": "after marker",
        "seed": new_marker,
    }


async def test_equal_seed_marker_does_not_call_native_tag(db_session):
    marker = {
        "managed_by": "screenshot-seed",
        "profile": "screenshots",
        "revision": "screenshots-2026-08-g",
    }
    row = _registry(
        "http://equal-seed-marker.test",
        source="seed",
        extra_params={"other": "kept", "seed": marker},
    )
    db_session.add(row)
    await db_session.flush()
    data = InferenceRegistryIngressData(db_session)
    await data.create_singleton_pool(row, None, None)
    db_session.autoflush = False
    connection, statements, observe = await _capture_sql(db_session)
    try:
        await get_or_create_seed(data, row.url, "ingress-binding", marker)
        await data.flush()
    finally:
        event.remove(connection, "before_cursor_execute", observe)

    assert not any(
        "aap_inference_registry_ingress_v1.tag_seed_registry" in statement
        for statement in statements
    )
    assert row.extra_params == {"other": "kept", "seed": marker}


async def test_env_borrowed_factory_commits_once_after_success(monkeypatch):
    class _FakeSessionContext:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Session:
        def __init__(self):
            self.commits = 0

        async def commit(self):
            self.commits += 1

    session = _Session()
    marker = object()
    monkeypatch.setattr(
        ml_backend_env_sync, "async_session", lambda: _FakeSessionContext(session)
    )
    monkeypatch.setattr(
        ml_backend_env_sync,
        "InferenceRegistryIngressData",
        lambda db: marker if db is session else None,
    )

    async with ml_backend_env_sync._borrowed_registry_data() as data:
        assert data is marker
        assert session.commits == 0

    assert session.commits == 1


async def test_env_borrowed_factory_does_not_commit_after_failure(monkeypatch):
    class _FakeSessionContext:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Session:
        def __init__(self):
            self.commits = 0

        async def commit(self):
            self.commits += 1

    session = _Session()
    monkeypatch.setattr(
        ml_backend_env_sync, "async_session", lambda: _FakeSessionContext(session)
    )
    monkeypatch.setattr(
        ml_backend_env_sync,
        "InferenceRegistryIngressData",
        lambda db: object() if db is session else None,
    )

    with pytest.raises(RuntimeError, match="abort borrowed transaction"):
        async with ml_backend_env_sync._borrowed_registry_data():
            raise RuntimeError("abort borrowed transaction")

    assert session.commits == 0


async def test_seed_refresh_adapter_keeps_single_argument_empty_call(monkeypatch):
    class _Clock:
        calls = 0

        @classmethod
        def now(cls, _timezone):
            cls.calls += 1
            return datetime(2026, 10, 22, tzinfo=UTC)

    monkeypatch.setattr(screenshot_seed_backends, "datetime", _Clock)
    assert tuple(
        inspect.signature(screenshot_seed_backends._refresh_candidates).parameters
    ) == ("backends",)

    assert await screenshot_seed_backends._refresh_candidates([]) == []
    assert _Clock.calls == 1
