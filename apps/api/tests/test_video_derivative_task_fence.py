import asyncio
import hashlib
import uuid

from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1.video_tracker_jobs import _lock_task_for_job_mutation
from app.core.security import create_access_token
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.user import User
from app.deps import get_current_principal, get_current_user
from app.services import storage_access, video_derivative_originals as originals
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID, register_item_asset
from app.services.identity_gateway import IdentityGateway
from app.services.labeling_core_read_relations import lock_task_for_core_read
from app.services.labeling_write_guard import lock_labeling_write
from app.services.labeling_write_scope import capture_labeling_write_scope
from tests.factory import create_project, create_user


@pytest.fixture
async def video_task(test_engine):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    async with sessions.begin() as db:
        owner = await create_user(
            db, "project_admin", f"owner-{suffix}@test.local", "Owner"
        )
        actor = await create_user(
            db, "employee", f"reader-{suffix}@test.local", "Reader"
        )
        project = await create_project(db, owner_id=owner.id, type_key="video-track")
        dataset = Dataset(
            display_id=f"D-FENCE-{suffix}",
            name="Video",
            data_type="video",
            created_by=owner.id,
        )
        db.add(dataset)
        await db.flush()
        items = [
            DatasetItem(
                dataset_id=dataset.id,
                file_name=f"{index}.mp4",
                file_path=f"video-task-fence/{suffix}/{index}.mp4",
                file_type="video",
                file_size=8,
            )
            for index in range(2)
        ]
        db.add_all(items)
        await db.flush()
        for index, item in enumerate(items):
            await register_item_asset(
                db,
                item,
                sha256=hashlib.sha256(bytes([index]) * 8).hexdigest(),
                size_bytes=8,
            )
        task = Task(
            display_id=f"T-FENCE-{suffix}",
            project_id=project.id,
            dataset_item_id=items[0].id,
            file_name=items[0].file_name,
            file_path=items[0].file_path,
            file_type="video",
            status="in_progress",
            assignee_id=actor.id,
        )
        member = ProjectMember(
            project_id=project.id, user_id=actor.id, role="annotator"
        )
        db.add_all([task, member])
    try:
        async with sessions() as db:
            await storage_access.ensure(db, LEGACY_LOCATION_ID)
        yield sessions, actor, task, member.id, items[1]
    finally:
        async with sessions.begin() as db:
            await db.execute(delete(Task).where(Task.id == task.id))
            await db.execute(delete(Project).where(Project.id == project.id))
            await db.execute(delete(Dataset).where(Dataset.id == dataset.id))
            await db.execute(delete(User).where(User.id.in_((actor.id, owner.id))))


async def _http_authority(db, task, actor):
    token = create_access_token(str(actor.id), actor.role)
    request = Request(
        {"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]}
    )
    identity = IdentityGateway()
    principal = await get_current_principal(
        request,
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
        db,
        identity,
    )
    admitted = await get_current_user(request, principal, db, identity)
    return await originals.authorize(db, task.id, admitted, request)


async def _delegated_authority(db, task, actor, mode):
    if mode == "http":
        value = originals.authorization(await _http_authority(db, task, actor))
        assert value["actor_id"] == str(actor.id)
    else:
        source = await originals.pin(db, "task", task.id)
        value = originals.system_authorization("task", task.id, source)
        assert "actor_id" not in value
    authority = await originals.delegated(db, value)
    assert authority.mode == mode
    return authority


async def _wait_for_blocking(holder, holder_pid, waiting_pid, future):
    while not future.done():
        if await holder.scalar(
            text("SELECT :holder = ANY(pg_blocking_pids(:waiting))"),
            {"holder": holder_pid, "waiting": waiting_pid},
        ):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("The expected Task lock wait was not observed")


async def _release_observed_wait(holder, waiting, call):
    holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
    waiting_pid = await waiting.scalar(text("SELECT pg_backend_pid()"))
    future = asyncio.create_task(call)
    try:
        async with asyncio.timeout(3):
            await _wait_for_blocking(holder, holder_pid, waiting_pid, future)
            await holder.commit()
            return await future
    finally:
        if not future.done():
            future.cancel()
        await asyncio.gather(future, return_exceptions=True)


@pytest.mark.parametrize("mode", ["request", "http", "system"])
async def test_task_source_fence_waits_for_short_editor_without_losing_identity(
    video_task, mode
):
    sessions, actor, task, _, _ = video_task
    async with sessions() as writer, sessions() as reader:
        authority = (
            None
            if mode == "request"
            else await _delegated_authority(reader, task, actor, mode)
        )
        await reader.commit()
        await reader.execute(text("SET LOCAL lock_timeout = '2s'"))
        locked = await lock_task_for_core_read(writer, task.id, nowait=True)
        assert locked.id == task.id
        await writer.execute(
            update(Task).where(Task.id == task.id).values(total_annotations=1)
        )
        if authority is None:
            call = _http_authority(reader, task, actor)
        else:
            call = originals.check(reader, authority, lock=True)
        result = await _release_observed_wait(writer, reader, call)
        if authority is None:
            assert result.mode == "http" and result.actor_id == actor.id
            result = await originals.check(reader, result, lock=True)
        assert result.relation.task_id == task.id
        assert result.item_id == task.dataset_item_id
        assert await reader.scalar(text("SHOW lock_timeout")) == "2s"


@pytest.mark.parametrize("mode", ["http", "system"])
async def test_task_source_fence_rejects_committed_rebinding_after_wait(
    video_task, mode
):
    sessions, actor, task, _, replacement = video_task
    async with sessions() as writer, sessions() as reader:
        authority = await _delegated_authority(reader, task, actor, mode)
        await reader.commit()
        await writer.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(dataset_item_id=replacement.id, file_path=replacement.file_path)
        )
        with pytest.raises(ContentError, match="content_binding_changed"):
            await _release_observed_wait(
                writer, reader, originals.check(reader, authority, lock=True)
            )
        assert await reader.scalar(text("SELECT 1")) == 1


@pytest.mark.parametrize(
    "mode,busy", [("http", "content_busy"), ("system", "storage_original_busy")]
)
async def test_task_source_fence_stays_bounded_and_preserves_caller_transaction(
    video_task, mode, busy
):
    sessions, actor, task, _, _ = video_task
    async with sessions() as writer, sessions() as reader:
        authority = await _delegated_authority(reader, task, actor, mode)
        await reader.commit()
        await lock_task_for_core_read(writer, task.id, nowait=True)
        with pytest.raises(ContentError, match=busy):
            await asyncio.wait_for(
                originals.check(reader, authority, lock=True), timeout=3
            )
        assert await reader.scalar(text("SELECT 1")) == 1
        await writer.rollback()
        source = await originals.check(reader, authority, lock=True)
        assert source.relation.task_id == task.id


async def test_http_source_fence_holds_task_and_membership_until_release(video_task):
    sessions, actor, task, member_id, _ = video_task
    async with sessions() as writer, sessions() as reader:
        http = await _delegated_authority(reader, task, actor, "http")
        system = await _delegated_authority(reader, task, actor, "system")
        await reader.commit()
        assert (
            await originals.check(reader, http, lock=True)
        ).relation.task_id == task.id
        unassign = update(Task).where(Task.id == task.id).values(assignee_id=None)
        revoke = delete(ProjectMember).where(ProjectMember.id == member_id)
        for change in (unassign, revoke):
            await writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
            with pytest.raises(DBAPIError) as blocked:
                await writer.execute(change)
            assert blocked.value.orig.sqlstate == "55P03"
            await writer.rollback()
        await reader.commit()
        await writer.execute(unassign)
        await writer.commit()
        with pytest.raises(HTTPException) as denied:
            await originals.check(reader, http, lock=True)
        assert denied.value.status_code == 404
        await reader.rollback()
        await writer.execute(
            update(Task).where(Task.id == task.id).values(assignee_id=actor.id)
        )
        await writer.execute(revoke)
        await writer.commit()
        with pytest.raises(HTTPException) as denied:
            await originals.check(reader, http, lock=True)
        assert denied.value.status_code == 404
        assert (
            await originals.check(reader, system, lock=True)
        ).relation.task_id == task.id


def _candidate_scope(task, actor):
    return capture_labeling_write_scope(
        task,
        actor_id=actor.id,
        actor_known=True,
        operation="candidate_accept",
        execution_scope=None,
        execution_scope_known=True,
        job_id=None,
        job_id_known=True,
    )


async def test_only_one_video_decision_waits_out_the_http_source_fence(video_task):
    sessions, actor, task, _, _ = video_task
    async with sessions() as reader, sessions() as writer, sessions() as competitor:
        authority = await _http_authority(reader, task, actor)
        assert authority.mode == "http"
        holder_pid = await reader.scalar(text("SELECT pg_backend_pid()"))
        waiting_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
        await competitor.execute(text("SET LOCAL statement_timeout = '300ms'"))
        decision = asyncio.create_task(
            lock_labeling_write(
                writer, _candidate_scope(task, actor), operations=("candidate_accept",)
            )
        )
        try:
            async with asyncio.timeout(3):
                await _wait_for_blocking(reader, holder_pid, waiting_pid, decision)
                with pytest.raises(HTTPException) as busy:
                    await _lock_task_for_job_mutation(
                        competitor, task.id, decision_project_id=task.project_id
                    )
                assert (busy.value.status_code, busy.value.detail) == (
                    409,
                    {"reason": "task_busy"},
                )
                assert not decision.done()
                await reader.commit()
                target, _, _ = await decision
                assert (target.id, target.status) == (task.id, task.status)
            with pytest.raises(DBAPIError) as busy:
                await lock_task_for_core_read(competitor, task.id, nowait=True)
            assert busy.value.orig.sqlstate == "55P03"
        finally:
            if not decision.done():
                decision.cancel()
            await asyncio.gather(decision, return_exceptions=True)


@pytest.mark.parametrize("legacy_mode", ["update", "no_key_update"])
async def test_video_decision_refuses_existing_editor_without_waiting(
    video_task, legacy_mode
):
    sessions, actor, task, _, _ = video_task
    async with sessions() as holder, sessions() as contender:
        if legacy_mode == "update":
            await lock_task_for_core_read(holder, task.id, nowait=True)
        else:
            await holder.execute(
                update(Task).where(Task.id == task.id).values(total_annotations=1)
            )
        await contender.execute(text("SET LOCAL statement_timeout = '300ms'"))
        with pytest.raises(HTTPException) as busy:
            await lock_labeling_write(
                contender,
                _candidate_scope(task, actor),
                operations=("candidate_accept",),
            )
        assert (busy.value.status_code, busy.value.detail) == (
            409,
            {"reason": "task_busy"},
        )


async def test_frame_prediction_refuses_sustained_http_video_reader(video_task):
    from app.api.v1.ml_backends import _reacquire_write_boundary

    sessions, actor, task, _, _ = video_task
    async with sessions() as reader, sessions() as writer:
        await _http_authority(reader, task, actor)
        await writer.execute(text("SET LOCAL lock_timeout = '0'"))
        with pytest.raises(HTTPException) as busy:
            async with asyncio.timeout(3):
                await _reacquire_write_boundary(
                    writer, task.id, actor, video_decision_project_id=task.project_id
                )
        assert (busy.value.status_code, busy.value.detail) == (
            409,
            {"reason": "task_busy"},
        )


async def test_video_decision_rejects_changed_task_after_media_reader_releases_it(
    video_task,
):
    sessions, actor, task, _, _ = video_task
    scope = _candidate_scope(task, actor)
    async with sessions() as reader, sessions() as writer:
        await writer.execute(
            update(Task).where(Task.id == task.id).values(status="pending")
        )
        await writer.commit()
        await _http_authority(reader, task, actor)
        with pytest.raises(HTTPException) as changed:
            await _release_observed_wait(
                reader,
                writer,
                lock_labeling_write(writer, scope, operations=("candidate_accept",)),
            )
        assert (changed.value.status_code, changed.value.detail) == (
            409,
            {"reason": "prediction_task_scope_changed"},
        )
