from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import uuid

import pytest

from aap_assets_media import contract as c
from app.services import assets_media
from app.workers import _db
from app.workers import media as media_workers


def _install_task_factory(monkeypatch, factories, sessions, completed=None):
    @asynccontextmanager
    async def task_session_factory():
        @asynccontextmanager
        async def factory():
            session = object()
            sessions.append(session)
            yield session

        factories.append(factory)
        try:
            yield factory
        finally:
            if completed is not None:
                completed.append(factory)

    monkeypatch.setattr(
        _db, "task_session_factory", task_session_factory, raising=False
    )


def test_consecutive_video_celery_runs_use_distinct_task_session_factories(monkeypatch):
    monkeypatch.setattr(assets_media.settings, "assets_media_url", "")
    factories = []
    sessions = []
    adapter_sessions = []
    executor_factories = []
    completed = []
    executor_closures = []
    _install_task_factory(monkeypatch, factories, sessions, completed)

    class FakeSqlAlchemyMediaDatabase:
        def __init__(self, db, *, commit=True):
            self.db = db

        async def call(self, operation, request):
            assert operation == "schedule_video_media"
            assert type(request) is c.ScheduleWorkRequest
            adapter_sessions.append(self.db)
            return request

    class FakeExecutor:
        def __init__(self, database):
            self.database = database

        async def execute_once(self, *, job_id):
            await self.database.call(
                "schedule_video_media",
                c.ScheduleWorkRequest(
                    operation_id=uuid.uuid4(),
                    dataset_item_id=uuid.uuid4(),
                    spec=c.MetadataSpec(),
                ),
            )
            return False

        async def close(self):
            executor_closures.append(True)

    def fake_executor(*, session_factory=None):
        executor_factories.append(session_factory)
        return FakeExecutor(assets_media._IndependentMediaDatabase(session_factory))

    monkeypatch.setattr(
        assets_media, "SqlAlchemyMediaDatabase", FakeSqlAlchemyMediaDatabase
    )
    monkeypatch.setattr(assets_media, "_executor", fake_executor)
    monkeypatch.setattr(
        "app.db.base.async_session",
        lambda: pytest.fail("video Celery task used the API-global session pool"),
    )

    media_workers.execute_video_media.run(None)
    media_workers.execute_video_media.run(None)

    assert len(factories) == 2
    assert factories[0] is not factories[1]
    assert executor_factories == factories
    assert completed == factories
    assert len(sessions) == 2
    assert adapter_sessions == sessions
    assert executor_closures == [True, True]


def test_video_schedule_passes_task_scoped_factory_to_independent_gateway(monkeypatch):
    monkeypatch.setattr(assets_media.settings, "assets_media_url", "")
    factories = []
    sessions = []
    _install_task_factory(monkeypatch, factories, sessions)
    captured = []

    class FakeGateway:
        async def call(self, operation, request):
            captured.append((operation, request))
            return SimpleNamespace()

    @asynccontextmanager
    async def fake_media_gateway(*, session_factory=None):
        captured.append(session_factory)
        yield FakeGateway()

    monkeypatch.setattr(media_workers, "media_gateway", fake_media_gateway)
    item_id = uuid.uuid4()

    media_workers.generate_video_metadata.run(str(item_id))

    assert captured[0] is factories[0]
    operation, request = captured[1]
    assert operation == "schedule_video_media"
    assert request.dataset_item_id == item_id
    assert isinstance(request.spec, c.MetadataSpec)


@pytest.mark.parametrize("connection", [False, True])
def test_media_gateway_derives_independent_factory_from_bound_session(
    monkeypatch, connection
):
    monkeypatch.setattr(assets_media.settings, "assets_media_url", "")
    bind = object()
    if connection:

        class FakeConnection:
            engine = bind

        monkeypatch.setattr("sqlalchemy.ext.asyncio.AsyncConnection", FakeConnection)
        db = FakeConnection()
    else:
        db = SimpleNamespace(bind=bind)
    constructed = []

    def fake_sessionmaker(actual_bind, *, class_, expire_on_commit):
        factory = object()
        constructed.append((actual_bind, class_, expire_on_commit, factory))
        return factory

    class FakeControl:
        def __init__(self, database, *_args, **_kwargs):
            self.database = database

    class FakeExecutor:
        async def execute_once(self, *, job_id):
            return None

        async def close(self):
            return None

    executor_factories = []

    def fake_executor(*, session_factory=None):
        executor_factories.append(session_factory)
        return FakeExecutor()

    monkeypatch.setattr("sqlalchemy.ext.asyncio.async_sessionmaker", fake_sessionmaker)
    monkeypatch.setattr("aap_assets_media_runtime.control.MediaControl", FakeControl)
    monkeypatch.setattr(assets_media, "media_storage", lambda **_kwargs: object())
    monkeypatch.setattr(assets_media, "_executor", fake_executor)

    gateway = assets_media.media_gateway(db)

    assert len(constructed) == 1
    assert constructed[0][0] is bind
    assert constructed[0][2] is False
    assert gateway.provider.database.session_factory is constructed[0][3]

    assert asyncio.run(gateway._execute(None)) is False
    assert executor_factories == [constructed[0][3]]
    asyncio.run(gateway.close())


@pytest.mark.parametrize("remote,usage", [(False, True), (False, False), (True, True)])
def test_video_maintenance_wakes_local_pool_only_after_local_reconcile(
    monkeypatch, remote, usage
):
    monkeypatch.setattr(
        assets_media.settings,
        "assets_media_url",
        "https://assets-media.example" if remote else "",
    )
    factories = []
    sessions = []
    if not remote:
        _install_task_factory(monkeypatch, factories, sessions)
    wakeups = []
    metric_values = []

    class FakeMetric:
        def set(self, value):
            metric_values.append(value)

    class FakeGauge:
        def labels(self, *, asset_type):
            return FakeMetric()

    monkeypatch.setattr(media_workers, "VIDEO_FRAME_ASSET_BYTES", FakeGauge())
    monkeypatch.setattr(
        media_workers.execute_video_media,
        "delay",
        lambda value=None: wakeups.append(value),
    )
    received_factories = []

    async def fake_maintain(*, session_factory=None):
        received_factories.append(session_factory)
        if not usage:
            return None
        return SimpleNamespace(frame_bytes=11, chunk_bytes=13)

    monkeypatch.setattr(media_workers, "maintain_video_media", fake_maintain)

    media_workers.cleanup_video_frame_assets.run()

    if remote:
        assert received_factories == []
        assert factories == []
        assert wakeups == []
    elif usage:
        assert received_factories == factories
        assert wakeups == [None]
        assert metric_values == [11, 13]
    else:
        assert len(received_factories) == 1
        assert received_factories == factories
        assert wakeups == []
        assert metric_values == []
