"""Synchronous video input ownership across the model dispatch boundary."""

import asyncio
from contextlib import asynccontextmanager, nullcontext
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from threading import BoundedSemaphore, Event
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy.dialects import postgresql
from yoao_identity.contract import Principal
from yoao_inference_contract.execution import ModelExecutionDispatch
from yoao_workspace_contract.access import project_manager_access, project_member_access

from app.services import comment_attachment_authority as credentials
from app.services import task_access
from app.services import video_sync_inference as sync
from app.services.content_snapshot import ContentError
from app.services.identity_contracts import (
    AcceptedApiKeyFacts,
    IdentityActorFacts,
    IdentityActorStatus,
)
from app.services.reference_bindings import task_binding
from tests.test_video_prediction_originals import (
    PROBE,
    RAW,
    source,
    stage,
    wait_started,
)


class Database:
    def __init__(self, events, *, actor_id, project_id):
        self.events = events
        self.actor_id = actor_id
        self.project_id = project_id
        self.transaction = False
        self.autoflush = False
        self.no_autoflush = nullcontext()
        self.sync_session = SimpleNamespace(in_nested_transaction=lambda: False)

    async def scalar(self, statement, parameters):
        assert parameters["project_id"] == self.project_id
        self.transaction = True
        if "user_id" in parameters:
            assert parameters["user_id"] == self.actor_id
            return None
        return self.project_id

    async def commit(self):
        self.events.append("commit")
        self.transaction = False

    async def rollback(self):
        self.events.append("rollback")
        self.transaction = False


class Descriptor:
    def __init__(self, file, events):
        self.file = file
        self.events = events
        self.started = Event()
        self.gate = None
        self.failure = None

    def fileno(self):
        return self.file.fileno()

    def close(self):
        self.events.append("source.close")
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(5)
        if self.failure is not None:
            raise self.failure
        self.file.close()


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
    actor = IdentityActorFacts(
        uuid.uuid4(), "Video manager", "manager@example.test", "super_admin", True
    )
    key_id, request_id = uuid.uuid4(), uuid.uuid4()
    principal = Principal(
        actor_id=actor.id,
        name=actor.name,
        email=actor.email,
        role="super_admin",
        is_active=True,
        credential_type="api_key",
        credential_id=key_id,
        generation=None,
        scopes=["*"],
        expires_at=None,
    )
    request = SimpleNamespace(
        state=SimpleNamespace(
            api_key_id=str(key_id), api_key_scopes=["*"], identity_principal=principal
        ),
    )
    backend_id, pool_id = uuid.uuid4(), uuid.uuid4()
    dispatch = ModelExecutionDispatch(
        request_id=request_id,
        operation="batch_predict",
        route_scope_id=uuid.uuid4(),
        attempt_id=request_id,
        requested_registry_id=backend_id,
        requested_pool_id=pool_id,
        registry_id=backend_id,
        instance_id=backend_id,
        pool_id=pool_id,
        member_id=uuid.uuid4(),
        topology_generation=1,
        endpoint_digest=sync.common.digest({"scope": "video-test-fixture"}),
    )
    member = {
        "backend_id": str(backend_id),
        "pool_id": str(pool_id),
        "auth_revision": 1,
    }
    db = Database(events, actor_id=actor.id, project_id=task.project_id)
    binary = stage()
    descriptor = Descriptor(binary.file, events)
    binary.file = descriptor
    monkeypatch.setattr(sync.originals, "_slots", BoundedSemaphore(1))
    admission = sync.originals._Admission()
    permit = SimpleNamespace(__aexit__=AsyncMock())
    owned_source = sync.originals.SourceInput(binary, PROBE, permit, admission)
    state = SimpleNamespace(
        original=original,
        route={**member, "routing_generation": 1},
        members={backend_id: member},
        api_key=AcceptedApiKeyFacts(("*",), None, "a" * 64),
        source_gate=None,
        source_started=None,
        input_failure=None,
    )
    inputs = SimpleNamespace(
        record=SimpleNamespace(
            id=uuid.uuid4(), expires_at=datetime.now(timezone.utc) + timedelta(hours=2)
        ),
        _closed=False,
        _unsettled=False,
    )

    async def actor_status(data, actor_id):
        assert data.db is db and actor_id == actor.id
        return IdentityActorStatus(actor.id, actor.role, actor.is_active)

    async def accounts(data, actor_ids, *, mode, nowait):
        assert actor_ids == [actor.id] and mode == "share" and nowait
        return {actor.id: actor}

    async def locked_actor(db_arg, actor_id, allowed_roles):
        assert db_arg.transaction and actor_id == actor.id
        return actor

    async def api_key(data, actor_id, identifier):
        events.append("credentials")
        assert actor_id == actor.id and identifier == key_id
        return state.api_key

    async def task_lock(db_arg, task_id):
        assert task_id == task.id
        db_arg.transaction = True
        if db_arg is db:
            events.append("caller.task")
        return task

    async def binding(db_arg, task_arg, *, lock):
        assert task_arg is task and lock
        return sync.originals.source_receipt(state.original)

    project = SimpleNamespace(id=task.project_id, data_type="video")
    access = project_manager_access(
        actor_id=actor.id,
        project_id=project.id,
        platform_role=actor.role,
        owner_id=actor.id,
    )

    async def read_project(db_arg, *, actor_id, project_id, populate_existing=False):
        assert actor_id == actor.id and project_id == task.project_id
        return project

    async def project_access(db_arg, *, actor_id, project):
        assert actor_id == actor.id and project.id == task.project_id
        return access

    async def route(db_arg, project_id, registry_id):
        assert project_id == task.project_id and registry_id == backend_id
        return deepcopy(state.route)

    async def backend(db_arg, identifier):
        events.append(("member", identifier))
        if identifier not in state.members:
            raise ContentError("prediction_video_backend_changed")
        return deepcopy(state.members[identifier])

    @asynccontextmanager
    async def session(_db):
        fresh = Database(events, actor_id=actor.id, project_id=task.project_id)
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
        assert os.pread(stage.file.fileno(), len(RAW), 0) == RAW
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
        assert db_arg is db and db_arg.transaction and descriptor.file.closed
        assert owned_source.settled and not owned_source.unsettled
        assert inputs_arg is inputs
        events.append("final.metadata")

    async def close_inputs():
        assert not db.transaction
        events.append("inputs.close")
        inputs._closed = True

    inputs.upload_video, inputs.check_live, inputs.close = upload, live, close_inputs
    monkeypatch.setattr(sync.IdentityAccountLockData, "read_actor_status", actor_status)
    monkeypatch.setattr(sync.IdentityAccountLockData, "lock_accounts", accounts)
    monkeypatch.setattr(credentials, "_actor", locked_actor)
    monkeypatch.setattr(
        sync.IdentityAccountLockData, "read_accepted_api_key_facts", api_key
    )
    monkeypatch.setattr(sync, "get_workspace_project", read_project)
    monkeypatch.setattr(task_access, "get_workspace_project", read_project)
    monkeypatch.setattr(sync, "resolve_project_access", project_access)
    monkeypatch.setattr(sync.common, "locked_task", task_lock)
    monkeypatch.setattr(sync.originals, "managed_binding", binding)
    monkeypatch.setattr(sync.originals, "_session", session)
    monkeypatch.setattr(sync.originals, "open_snapshot", open_source)
    monkeypatch.setattr(sync, "_route", route)
    monkeypatch.setattr(sync, "_backend", backend)
    monkeypatch.setattr(sync.prediction_inputs.PredictionInputs, "create", create)
    monkeypatch.setattr(sync.prediction_inputs, "check_metadata", metadata)
    yield SimpleNamespace(
        db=db,
        task=task,
        actor=actor,
        request=request,
        backend_id=backend_id,
        dispatch=dispatch,
        descriptor=descriptor,
        permit=permit,
        admission=admission,
        source=owned_source,
        state=state,
        inputs=inputs,
        events=events,
        factory=object(),
    )
    descriptor.file.close()


def context(world):
    return sync.video_predict_test_operation(
        world.db,
        world.task,
        world.actor,
        world.request,
        world.factory,
        backend_id=world.backend_id,
    )


async def wait_for_boundary(operation, started):
    boundary = asyncio.create_task(started)
    try:
        completed, _ = await asyncio.wait(
            (operation, boundary), timeout=5, return_when=asyncio.FIRST_COMPLETED
        )
        if operation in completed:
            await operation
            pytest.fail("video inference finished before reaching the I/O boundary")
        assert boundary in completed, "video inference did not reach the I/O boundary"
        await boundary
    finally:
        boundary.cancel()
        await asyncio.gather(boundary, return_exceptions=True)


async def test_video_dispatch_borrows_stage_until_model_then_closes_before_final_sql(
    world,
):
    async with context(world) as owner:
        assert owner.url.endswith("versionId=fixed-immutable-version")
        assert owner.probe == PROBE
        detached = owner.probe
        detached["video"]["width"] = 1
        assert owner.probe["video"]["width"] == 96
        world.db.transaction = True
        await owner.before_dispatch(world.dispatch)
        assert not world.db.transaction
        assert not world.source.closed and not world.descriptor.file.closed
        world.permit.__aexit__.assert_not_awaited()
        world.events.append("model.returned")
        assert await owner.final() is world.task
        assert world.source.settled and not world.source.unsettled
    assert world.inputs._closed
    assert world.descriptor.file.closed and world.admission.released
    world.permit.__aexit__.assert_awaited_once()
    assert world.events.index("inputs.upload") < world.events.index("model.returned")
    assert world.events.index("model.returned") < world.events.index("source.close")
    assert world.events.index("source.close") < world.events.index("final.metadata")
    _, final_task_lock = [
        index for index, event in enumerate(world.events) if event == "caller.task"
    ]
    assert world.events.index("source.close") < final_task_lock


@pytest.mark.parametrize(
    "change,status,code",
    [
        ("credentials", 403, "api_key_authority_revoked"),
        ("task_version", 409, "content_binding_changed"),
        ("source_owner", 409, "content_binding_changed"),
        ("pool", 409, "prediction_video_backend_changed"),
        ("auth_revision", 409, "prediction_video_backend_changed"),
    ],
    ids=["credentials", "task_version", "source_owner", "pool", "auth_revision"],
)
async def test_changes_after_model_never_return_a_result(world, change, status, code):
    with pytest.raises(HTTPException) as caught:
        async with context(world) as owner:
            await owner.before_dispatch(world.dispatch)
            if change == "credentials":
                world.state.api_key = None
            elif change == "task_version":
                world.task.version += 1
            elif change == "source_owner":
                current = world.state.original
                new_id = uuid.uuid4()
                world.state.original = replace(
                    current,
                    content_id=new_id,
                    ownership_facts=replace(current.ownership_facts, id=new_id),
                    replica_facts=replace(current.replica_facts, asset_id=new_id),
                )
            elif change == "pool":
                world.state.route["routing_generation"] += 1
            else:
                world.state.members[world.dispatch.instance_id]["auth_revision"] += 1
            await owner.final()
    assert caught.value.status_code == status
    assert caught.value.detail == {"code": code}
    assert "final.metadata" not in world.events
    assert world.source.settled and world.inputs._closed


async def test_dispatch_must_be_actual_routed_callback(world):
    with pytest.raises(HTTPException) as caught:
        async with context(world) as owner:
            await owner.final()
    assert caught.value.detail == {"code": "prediction_video_dispatch_required"}
    assert world.source.settled


@pytest.mark.parametrize("evidence", ["legacy_transport", "missing_endpoint"])
async def test_dispatch_refuses_evidence_without_a_sealed_endpoint(world, evidence):
    if evidence == "legacy_transport":
        dispatch = SimpleNamespace(backend_id=str(world.backend_id))
    else:
        dispatch = replace(world.dispatch, endpoint_digest=None)
    with pytest.raises(HTTPException) as caught:
        async with context(world) as owner:
            await owner.before_dispatch(dispatch)
    assert caught.value.detail == {"code": "prediction_video_backend_changed"}
    assert ("member", world.backend_id) not in world.events
    assert world.source.settled and "final.metadata" not in world.events


async def test_an_admitted_dispatch_cannot_be_replaced(world):
    with pytest.raises(HTTPException) as caught:
        async with context(world) as owner:
            await owner.before_dispatch(world.dispatch)
            replacement_id = uuid.uuid4()
            replacement = replace(
                world.dispatch,
                request_id=replacement_id,
                attempt_id=replacement_id,
                route_scope_id=uuid.uuid4(),
                endpoint_digest="f" * 64,
            )
            await owner.before_dispatch(replacement)
    assert caught.value.detail == {"code": "prediction_video_backend_changed"}
    assert world.source.settled and "final.metadata" not in world.events


async def test_missing_final_is_not_silently_qualified(world):
    with pytest.raises(HTTPException) as caught:
        async with context(world) as owner:
            await owner.before_dispatch(world.dispatch)
    assert caught.value.detail == {"code": "prediction_video_dispatch_required"}


@pytest.mark.parametrize("changed", ["member_pool", "dispatch_pool"])
async def test_selected_instance_in_another_pool_never_dispatches(
    world, monkeypatch, changed
):
    from app.api.v1 import ml_backends

    registry = SimpleNamespace(id=world.backend_id)
    monkeypatch.setattr(
        ml_backends.MLBackendService, "get_registry", AsyncMock(return_value=registry)
    )
    monkeypatch.setattr(
        ml_backends.MLBackendService, "is_enabled", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(ml_backends, "get_core_row", AsyncMock(return_value=world.task))
    other_pool_id = uuid.uuid4()

    async def execute(request, *, before_dispatch):
        world.events.append("model.handoff")
        assert request.operation == "batch_predict"
        assert request.route_operation == "predict_test"
        assert request.requested_registry_id == world.backend_id
        assert request.task_inputs == (
            {
                "id": str(world.task.id),
                "file_path": "http://delivery.test/video?versionId=fixed-immutable-version",
            },
        )
        assert not world.db.transaction and not world.descriptor.file.closed
        dispatch = replace(
            world.dispatch,
            request_id=request.request_id,
            attempt_id=request.request_id,
        )
        if changed == "member_pool":
            world.state.members[dispatch.instance_id]["pool_id"] = str(other_pool_id)
        else:
            dispatch = replace(dispatch, pool_id=other_pool_id)
        await before_dispatch(dispatch)
        pytest.fail("an unqualified instance must not reach the model")

    monkeypatch.setattr(
        ml_backends,
        "model_execution_port",
        lambda db: SimpleNamespace(execute=execute),
    )
    with pytest.raises(HTTPException) as caught:
        await ml_backends.predict_test(
            project_id=world.task.project_id,
            backend_id=world.backend_id,
            task_id=world.task.id,
            request=world.request,
            db=world.db,
            input_session_factory=world.factory,
            current_user=world.actor,
        )
    assert caught.value.detail == {"code": "prediction_video_backend_changed"}
    assert world.events.count("model.handoff") == 1
    assert world.source.settled and world.inputs._closed
    assert "final.metadata" not in world.events


@pytest.mark.parametrize("selected_changed", [False, True])
async def test_an_equivalent_active_pool_member_may_be_selected(
    world, selected_changed
):
    selected_id = uuid.uuid4()
    world.state.members[selected_id] = {
        **world.state.members[world.backend_id],
        "backend_id": str(selected_id),
    }
    dispatch = replace(world.dispatch, registry_id=selected_id, instance_id=selected_id)
    if selected_changed:
        with pytest.raises(HTTPException) as caught:
            async with context(world) as owner:
                await owner.before_dispatch(dispatch)
                world.state.members[selected_id]["auth_revision"] += 1
                await owner.final()
        assert caught.value.detail == {"code": "prediction_video_backend_changed"}
        assert "final.metadata" not in world.events
    else:
        async with context(world) as owner:
            await owner.before_dispatch(dispatch)
            assert await owner.final() is world.task
        assert "final.metadata" in world.events
    assert ("member", selected_id) in world.events
    assert ("member", world.backend_id) not in world.events
    assert world.source.settled


async def test_route_auth_revision_change_refuses_dispatch(world):
    with pytest.raises(HTTPException) as caught:
        async with context(world) as owner:
            world.state.route["auth_revision"] += 1
            await owner.before_dispatch(world.dispatch)
    assert caught.value.detail == {"code": "prediction_video_backend_changed"}


async def test_revocation_during_actual_source_close_is_seen_by_final_caller_fence(
    world,
):
    world.descriptor.gate = Event()

    async def run():
        async with context(world) as owner:
            await owner.before_dispatch(world.dispatch)
            await owner.final()

    pending = asyncio.create_task(run())
    try:
        await wait_for_boundary(pending, wait_started(world.descriptor.started))
        world.state.api_key = None
        world.descriptor.gate.set()
        with pytest.raises(HTTPException) as caught:
            await pending
        assert caught.value.status_code == 403
        assert caught.value.detail == {"code": "api_key_authority_revoked"}
    finally:
        world.descriptor.gate.set()
        await asyncio.gather(pending, return_exceptions=True)
    assert world.source.settled
    assert world.events.count("caller.task") == 1
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
    try:
        await wait_for_boundary(pending, world.state.source_started.wait())
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not world.inputs._closed
        assert not world.descriptor.file.closed
        world.state.source_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        world.state.source_gate.set()
        await asyncio.gather(pending, return_exceptions=True)
    assert world.source.settled and world.inputs._closed
    assert world.events.count("source.close") == 1
    assert world.descriptor.file.closed and world.admission.released
    world.permit.__aexit__.assert_awaited_once()


async def test_repeated_cancel_during_final_waits_for_actual_source_close(world):
    world.descriptor.gate = Event()

    async def run():
        async with context(world) as owner:
            await owner.before_dispatch(world.dispatch)
            await owner.final()

    pending = asyncio.create_task(run())
    try:
        await wait_for_boundary(pending, wait_started(world.descriptor.started))
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not world.source.settled
        assert not world.descriptor.file.closed
        world.permit.__aexit__.assert_not_awaited()
        world.descriptor.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        world.descriptor.gate.set()
        await asyncio.gather(pending, return_exceptions=True)
    assert world.source.settled and world.inputs._closed
    assert world.descriptor.file.closed and world.admission.released
    world.permit.__aexit__.assert_awaited_once()
    assert "final.metadata" not in world.events


async def test_unknown_source_close_is_sticky_and_prevents_final_sql(world):
    world.descriptor.failure = OSError("uncertain descriptor close")
    held = None
    with pytest.raises(ContentError, match="storage_operation_unsettled") as caught:
        async with context(world) as owner:
            held = owner
            await owner.before_dispatch(world.dispatch)
            await owner.final()
    assert caught.value.storage_operation_unsettled
    assert world.source.unsettled and not world.source.settled
    assert not world.descriptor.file.closed and world.admission.retained
    world.permit.__aexit__.assert_not_awaited()
    with pytest.raises(
        ContentError, match="prediction_original_close_unknown"
    ) as repeated:
        await held.close()
    assert repeated.value.storage_operation_unsettled
    assert world.events.count("source.close") == 1
    assert "final.metadata" not in world.events


async def test_unknown_input_upload_preserves_source_responsibility(world):
    world.state.input_failure = sync.mark_unsettled(OSError("provider close unknown"))
    with pytest.raises(ContentError, match="storage_operation_unsettled") as caught:
        async with context(world):
            pytest.fail("unknown output must not dispatch")
    assert caught.value.storage_operation_unsettled
    assert world.source.unsettled and not world.source.settled
    assert not world.descriptor.file.closed and world.admission.retained
    assert "source.close" not in world.events
    world.permit.__aexit__.assert_not_awaited()


async def test_backend_query_reads_only_nonsecret_fixed_size_member_facts():
    statements = []

    class DB:
        async def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(one_or_none=lambda: None)

    with pytest.raises(ContentError):
        await sync._backend(DB(), uuid.uuid4())
    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert list(statements[0].selected_columns.keys()) == [
        "id",
        "auth_revision",
        "pool_id",
        "traffic_state",
    ]
    assert "FOR SHARE NOWAIT" in sql


@pytest.mark.parametrize("traffic", ["draining", "disabled"])
async def test_backend_refuses_inactive_physical_instance(traffic):
    row = SimpleNamespace(
        id=uuid.uuid4(), pool_id=uuid.uuid4(), auth_revision=1, traffic_state=traffic
    )

    class DB:
        async def execute(self, statement):
            return SimpleNamespace(one_or_none=lambda: row)

    with pytest.raises(ContentError, match="prediction_video_backend_changed"):
        await sync._backend(DB(), row.id)


@pytest.mark.parametrize("manager", [True, False])
async def test_video_test_preserves_manager_read_policy_without_interactive_edit_requirements(
    monkeypatch, manager
):
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
    db = object()
    access = (
        project_manager_access(
            actor_id=actor.id,
            project_id=project.id,
            platform_role="super_admin",
            owner_id=actor.id,
        )
        if manager
        else project_member_access(
            actor_id=actor.id,
            project_id=project.id,
            platform_role="employee",
            membership_id=uuid.uuid4(),
            project_role="annotator",
        )
    )
    read_project = AsyncMock(return_value=project)
    visibility_reads = AsyncMock(return_value=project)
    monkeypatch.setattr(sync, "get_workspace_project", read_project)
    monkeypatch.setattr(sync, "resolve_project_access", AsyncMock(return_value=access))
    monkeypatch.setattr(task_access, "get_workspace_project", visibility_reads)
    owner = sync.VideoPredictTestOperation(
        db,
        task,
        actor,
        SimpleNamespace(state=SimpleNamespace(api_key_id=None)),
        backend_id=uuid.uuid4(),
    )
    if manager:
        await owner._business(db, task, actor)
        visibility_reads.assert_awaited_once_with(
            db, actor_id=actor.id, project_id=project.id
        )
    else:
        with pytest.raises(ContentError) as caught:
            await owner._business(db, task, actor)
        assert caught.value.status == 403
        visibility_reads.assert_not_awaited()


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
    db = object()
    monkeypatch.setattr(
        sync,
        "get_workspace_project",
        AsyncMock(return_value=SimpleNamespace(data_type="video")),
    )
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
