"""Synchronous video ownership boundaries; physical providers are isolated seams."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy.dialects import postgresql

from app.services import video_sync_inference as sync
from app.services.content_snapshot import ContentError
from app.services.reference_bindings import task_binding
from tests.test_video_prediction_originals import PROBE, source


class Database:
    def __init__(self, events):
        self.events = events
        self.transaction = False
        self.sync_session = SimpleNamespace(in_nested_transaction=lambda: False)

    async def commit(self):
        self.events.append("commit")
        self.transaction = False

    async def rollback(self):
        self.events.append("rollback")
        self.transaction = False


class Source:
    def __init__(self, events):
        self.events = events
        self.stage = SimpleNamespace(_closed=False)
        self.probe = deepcopy(PROBE)
        self.closed = self.settled = self.unsettled = False
        self.close_gate = self.close_started = None
        self.close_failure = None
        self._closing = None

    async def _finish(self):
        self.events.append("source.close")
        if self.close_started:
            self.close_started.set()
        if self.close_gate:
            await self.close_gate.wait()
        if self.close_failure:
            self.unsettled = True
            raise sync.mark_unsettled(self.close_failure)
        self.stage._closed = self.closed = self.settled = True

    async def aclose(self, failure=None):
        if getattr(failure, "storage_operation_unsettled", False):
            self.unsettled = True
        if self.unsettled:
            raise sync.mark_unsettled(ContentError("storage_operation_unsettled", 503))
        if self._closing is None:
            self._closing = asyncio.create_task(self._finish())
        try:
            await asyncio.shield(self._closing)
        except BaseException:
            await sync.common.drain(self._closing)
            raise


@pytest.fixture
def world(monkeypatch):
    events = []
    original = source()
    task = SimpleNamespace(
        id=original.relation.task_id,
        project_id=original.relation.project_id,
        batch_id=None,
        dataset_item_id=original.item_id,
        file_path=original.object_key,
        file_name="original.mp4",
        file_type="video",
        version=1,
    )
    actor = SimpleNamespace(id=uuid.uuid4(), is_active=True)
    request = SimpleNamespace(state=SimpleNamespace(api_key_id=uuid.uuid4()))
    backend_id, pool_id = uuid.uuid4(), uuid.uuid4()
    transport = SimpleNamespace(
        backend_id=str(backend_id),
        base_url="http://selected.test",
        auth_method="none",
        auth_token=None,
        gpu_resource_id=None,
        vram_budget_mb=None,
        max_concurrency=4,
        _backend=SimpleNamespace(auth_revision=1),
    )
    selected = {
        "backend_id": str(backend_id),
        "pool_id": str(pool_id),
        "auth_revision": 1,
        "wire_digest": sync._wire(transport),
        "extra_digest": sync.common.digest({}),
    }
    db = Database(events)
    owned_source = Source(events)
    state = SimpleNamespace(
        original=original,
        route={**selected, "routing_generation": 1},
        selected=selected,
        credential_error=None,
        source_gate=None,
        source_started=None,
        input_failure=None,
        input_close_failure=None,
    )
    credentials = {
        "schema": 1,
        "actor_id": str(actor.id),
        "api_key_id": str(request.state.api_key_id),
        "key_fingerprint": "a" * 64,
        "key_expires_at": None,
        "jwt": None,
    }
    inputs = SimpleNamespace(
        record=SimpleNamespace(
            id=uuid.uuid4(), expires_at=datetime.now(timezone.utc) + timedelta(hours=2)
        ),
        _closed=False,
        _unsettled=False,
    )

    async def current(db_arg, _credentials, actor_id, key_id):
        events.append("credentials")
        assert actor_id == actor.id and key_id == request.state.api_key_id
        if state.credential_error:
            raise state.credential_error
        return actor

    async def task_lock(db_arg, task_id):
        assert task_id == task.id
        db_arg.transaction = True
        events.append("task")
        return task

    async def binding(db_arg, task_arg, *, lock):
        assert task_arg is task and lock
        return sync.originals.source_receipt(state.original)

    async def business(self, db_arg, task_arg, actor_arg):
        assert actor_arg is actor and task_arg is task

    async def get(model, identifier, **kwargs):
        return actor

    async def parents(db_arg, *args):
        db_arg.transaction = True

    async def route(db_arg, project_id, registry_id):
        assert project_id == task.project_id and registry_id == backend_id
        return deepcopy(state.route)

    async def backend(db_arg, identifier, **kwargs):
        assert str(identifier) == state.selected["backend_id"]
        return deepcopy(state.selected)

    @asynccontextmanager
    async def session(_db):
        fresh = Database(events)
        try:
            yield fresh
        finally:
            await fresh.rollback()

    async def create(factory, *, owner_id, owner_kind, task_id, guard, source_ref):
        assert not db.transaction
        assert owner_kind == "video_predict_test" and task_id == task.id
        assert source_ref == {
            **task_binding(task),
            "original": sync.originals.source_receipt(state.original),
        }
        assert owner_id != task.id
        events.append("inputs.create")
        inputs.guard = guard
        async with session(db) as fresh:
            await guard(fresh)
        return inputs

    async def open_source(db_arg, task_id, expected, *, guard, purpose, node_id):
        assert not db.transaction
        assert task_id == task.id
        assert expected == sync.originals.source_receipt(state.original)
        assert purpose == sync.prediction_inputs.purpose(inputs.record)
        events.append("source.open")
        if state.source_started:
            state.source_started.set()
        if state.source_gate:
            await state.source_gate.wait()
        async with session(db) as fresh:
            await guard(fresh)
        return owned_source

    async def upload(stage, *, probe):
        assert not db.transaction
        assert stage is owned_source.stage and not stage._closed
        assert probe == PROBE
        events.append("inputs.upload")
        if state.input_failure:
            raise state.input_failure
        return "http://delivery.test/video?versionId=fixed-immutable-version"

    async def live():
        events.append("inputs.live")
        async with session(db) as fresh:
            await inputs.guard(fresh)

    async def metadata(db_arg, inputs_arg):
        assert owned_source.settled and not owned_source.unsettled
        assert inputs_arg is inputs
        events.append("final.metadata")

    async def close_inputs():
        assert not db.transaction
        events.append("inputs.close")
        inputs._closed = True
        if state.input_close_failure:
            inputs._unsettled = True
            raise state.input_close_failure

    db.get = get
    inputs.upload_video, inputs.check_live, inputs.close = upload, live, close_inputs
    monkeypatch.setattr(
        sync.common, "capture_credentials", AsyncMock(return_value=credentials)
    )
    monkeypatch.setattr(sync.common, "reauthorize", current)
    monkeypatch.setattr(sync.common, "locked_task", task_lock)
    monkeypatch.setattr(sync.originals, "managed_binding", binding)
    monkeypatch.setattr(sync.originals, "_session", session)
    monkeypatch.setattr(sync.originals, "open_snapshot", open_source)
    monkeypatch.setattr(sync, "lock_actor_project_authority", parents)
    monkeypatch.setattr(sync, "lock_actor_scope", parents)
    monkeypatch.setattr(sync, "_route", route)
    monkeypatch.setattr(sync, "_backend", backend)
    monkeypatch.setattr(sync.VideoPredictTestOperation, "_business", business)
    monkeypatch.setattr(sync.prediction_inputs.PredictionInputs, "create", create)
    monkeypatch.setattr(sync.prediction_inputs, "check_metadata", metadata)
    return SimpleNamespace(
        db=db,
        task=task,
        actor=actor,
        request=request,
        backend_id=backend_id,
        transport=transport,
        source=owned_source,
        state=state,
        inputs=inputs,
        events=events,
        factory=object(),
    )


def context(world):
    return sync.video_predict_test_operation(
        world.db,
        world.task,
        world.actor,
        world.request,
        world.factory,
        backend_id=world.backend_id,
    )


async def test_video_dispatch_borrows_stage_until_model_then_closes_before_final_sql(
    world,
):
    async with context(world) as owner:
        assert owner.url.endswith("versionId=fixed-immutable-version")
        assert owner.probe == PROBE
        detached = owner.probe
        detached["video"]["width"] = 1
        assert owner.probe["video"]["width"] == 96
        world.db.transaction = True  # actual RoutedMLBackendClient selection
        await owner.before_dispatch(world.transport)
        assert not world.db.transaction
        assert not world.source.closed
        world.events.append("model.returned")
        assert await owner.final() is world.task
        assert world.source.settled and not world.source.unsettled
    assert world.inputs._closed
    assert world.events.index("inputs.upload") < world.events.index("model.returned")
    assert world.events.index("model.returned") < world.events.index("source.close")
    assert world.events.index("source.close") < world.events.index("final.metadata")


@pytest.mark.parametrize(
    "change",
    [
        "credentials",
        "task_version",
        "source_owner",
        "pool",
        "transport",
        "auth_revision",
    ],
)
async def test_changes_after_model_never_return_a_result(world, change):
    from dataclasses import replace

    with pytest.raises(HTTPException) as caught:
        async with context(world) as owner:
            await owner.before_dispatch(world.transport)
            if change == "credentials":
                world.state.credential_error = ContentError(
                    "api_key_authority_revoked", 403
                )
            elif change == "task_version":
                world.task.version += 1
            elif change == "source_owner":
                # Identical bytes on another owner are not the frozen original.
                current = world.state.original
                world.state.original = replace(current, content_id=uuid.uuid4())
            elif change == "pool":
                world.state.route["routing_generation"] += 1
            elif change == "auth_revision":
                world.state.selected["auth_revision"] += 1
            else:
                world.transport.base_url = "http://substituted.test"
            await owner.final()
    assert caught.value.status_code in {403, 409}
    assert "final.metadata" not in world.events
    assert world.source.settled and world.inputs._closed


async def test_dispatch_must_be_actual_routed_callback(world):
    with pytest.raises(HTTPException) as caught:
        async with context(world) as owner:
            await owner.final()
    assert caught.value.detail == {"code": "prediction_video_dispatch_required"}
    assert world.source.settled


async def test_missing_final_is_not_silently_qualified(world):
    with pytest.raises(HTTPException) as caught:
        async with context(world) as owner:
            await owner.before_dispatch(world.transport)
    assert caught.value.detail == {"code": "prediction_video_dispatch_required"}


async def test_selected_instance_in_another_pool_never_dispatches(world):
    with pytest.raises(HTTPException) as caught:
        async with context(world) as owner:
            world.state.selected["pool_id"] = str(uuid.uuid4())
            await owner.before_dispatch(world.transport)
    assert caught.value.detail == {"code": "prediction_video_backend_changed"}
    assert "final.metadata" not in world.events


async def test_an_equivalent_active_pool_member_may_be_selected(world):
    async with context(world) as owner:
        selected_id = uuid.uuid4()
        assert selected_id != world.backend_id
        world.transport.backend_id = str(selected_id)
        world.state.selected = {
            **world.state.selected,
            "backend_id": str(selected_id),
            "wire_digest": sync._wire(world.transport),
        }
        await owner.before_dispatch(world.transport)
        # Rollback can expire the ORM registry retained by MLBackendClient. The
        # transport's copied wire facts and captured revision remain usable.
        vars(world.transport._backend).clear()
        await owner.final()
    assert world.source.settled


async def test_same_auth_values_after_revision_rotation_cannot_use_stale_transport(
    world,
):
    with pytest.raises(HTTPException) as caught:
        async with context(world) as owner:
            world.state.selected["auth_revision"] += 1
            await owner.before_dispatch(world.transport)
    assert caught.value.detail == {"code": "prediction_video_backend_changed"}


async def test_revocation_during_actual_source_close_is_seen_by_final_caller_fence(
    world,
):
    world.source.close_started, world.source.close_gate = (
        asyncio.Event(),
        asyncio.Event(),
    )

    async def run():
        async with context(world) as owner:
            await owner.before_dispatch(world.transport)
            await owner.final()

    pending = asyncio.create_task(run())
    await world.source.close_started.wait()
    world.state.credential_error = ContentError("prediction_authority_revoked", 403)
    world.source.close_gate.set()
    with pytest.raises(HTTPException) as caught:
        await pending
    assert caught.value.status_code == 403
    assert world.source.settled
    assert "final.metadata" not in world.events


async def test_repeated_cancel_waits_for_late_source_and_closes_its_actual_stage(world):
    world.state.source_started, world.state.source_gate = (
        asyncio.Event(),
        asyncio.Event(),
    )

    async def run():
        async with context(world):
            pytest.fail("cancelled request must not dispatch")

    pending = asyncio.create_task(run())
    await world.state.source_started.wait()
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done() and not world.inputs._closed
    world.state.source_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert world.source.settled and world.inputs._closed
    assert world.events.count("source.close") == 1


async def test_repeated_cancel_during_final_waits_for_actual_source_close(world):
    world.source.close_started, world.source.close_gate = (
        asyncio.Event(),
        asyncio.Event(),
    )

    async def run():
        async with context(world) as owner:
            await owner.before_dispatch(world.transport)
            await owner.final()

    pending = asyncio.create_task(run())
    await world.source.close_started.wait()
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done() and not world.source.settled
    world.source.close_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert world.source.settled and world.inputs._closed
    assert "final.metadata" not in world.events


async def test_unknown_source_close_is_sticky_and_prevents_final_sql(world):
    world.source.close_failure = OSError("uncertain descriptor close")
    held = None
    with pytest.raises(BaseException) as caught:
        async with context(world) as owner:
            held = owner
            await owner.before_dispatch(world.transport)
            await owner.final()
    assert getattr(caught.value, "storage_operation_unsettled", False)
    assert held._unsettled and world.source.unsettled
    assert not world.source.settled
    with pytest.raises(ContentError) as repeated:
        await held.close()
    assert getattr(repeated.value, "storage_operation_unsettled", False)
    assert "final.metadata" not in world.events


async def test_unknown_input_upload_preserves_source_responsibility(world):
    world.state.input_failure = sync.mark_unsettled(OSError("provider close unknown"))
    with pytest.raises(BaseException) as caught:
        async with context(world):
            pytest.fail("unknown output must not dispatch")
    assert getattr(caught.value, "storage_operation_unsettled", False)
    assert world.source.unsettled and not world.source.settled


async def test_backend_query_bounds_json_and_does_not_hydrate_health():
    statements = []

    class DB:
        async def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(one_or_none=lambda: None)

    with pytest.raises(ContentError):
        await sync._backend(DB(), uuid.uuid4())
    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "CASE WHEN" in sql and "octet_length" in sql
    assert "health_meta" not in sql and "error_message" not in sql
    assert "FOR SHARE NOWAIT" in sql


@pytest.mark.parametrize("oversized,traffic", [(True, "active"), (False, "disabled")])
async def test_backend_refuses_oversize_or_disabled_physical_instance(
    oversized, traffic
):
    row = SimpleNamespace(oversized=oversized, traffic_state=traffic)

    class DB:
        async def execute(self, statement):
            return SimpleNamespace(one_or_none=lambda: row)

    with pytest.raises(ContentError, match="prediction_video_backend_changed"):
        await sync._backend(DB(), uuid.uuid4())


@pytest.mark.parametrize("manager", [True, False])
async def test_video_test_preserves_manager_read_policy_without_interactive_edit_requirements(
    monkeypatch, manager
):
    from app.api.v1.tasks import _shared

    task = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        file_type="video",
        file_path="saved/original",
        status="completed",
        batch_id=uuid.uuid4(),
    )
    actor = SimpleNamespace(id=uuid.uuid4(), is_active=True)
    project = SimpleNamespace(
        id=task.project_id, data_type="video", ai_interactive_enabled=False
    )
    db = SimpleNamespace(get=AsyncMock(return_value=project))
    access = SimpleNamespace(is_manager=manager)
    resolve = AsyncMock(return_value=access)
    visible = AsyncMock()
    monkeypatch.setattr(sync, "resolve_project_access", resolve)
    monkeypatch.setattr(_shared, "_assert_task_visible", visible)
    owner = sync.VideoPredictTestOperation(
        db,
        task,
        actor,
        SimpleNamespace(state=SimpleNamespace(api_key_id=None)),
        backend_id=uuid.uuid4(),
    )
    if manager:
        await owner._business(db, task, actor)
        visible.assert_awaited_once_with(db, task, actor, access=access)
    else:
        with pytest.raises(ContentError) as caught:
            await owner._business(db, task, actor)
        assert caught.value.status == 403
        visible.assert_not_awaited()


@pytest.mark.parametrize(
    "kind,path", [("image", "saved/image"), ("video", "__aap_reference__/invalid")]
)
async def test_video_business_does_not_grant_image_or_reference_qualification(
    monkeypatch, kind, path
):
    task = SimpleNamespace(
        id=uuid.uuid4(), project_id=uuid.uuid4(), file_type=kind, file_path=path
    )
    actor = SimpleNamespace(id=uuid.uuid4(), is_active=True)
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(data_type="video")))
    owner = sync.VideoPredictTestOperation(
        db,
        task,
        actor,
        SimpleNamespace(state=SimpleNamespace(api_key_id=None)),
        backend_id=uuid.uuid4(),
    )
    resolver = AsyncMock()
    monkeypatch.setattr(sync, "resolve_project_access", resolver)
    with pytest.raises(ContentError) as caught:
        await owner._business(db, task, actor)
    assert caught.value.status == 403
    resolver.assert_not_awaited()
