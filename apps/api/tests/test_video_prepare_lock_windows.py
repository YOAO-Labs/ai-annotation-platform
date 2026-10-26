"""Committed prepare boundaries release Item locks without weakening batch fences.

All connections use the repository's disposable test engine. Physical delivery is
replaced at its boundary; canonical originals, authority, generations, attempts
and competing row locks use the real database and production contract helpers.
"""

from types import SimpleNamespace
import asyncio
import time
import uuid

from fastapi import HTTPException, Request
import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.task import Task
from app.db.models.user import User
from app.db.models.video_derivative import VideoDerivativeOutput as Output
from app.services import video_derivative_objects as objects
from app.services import video_derivative_originals as originals
from app.services import video_derivatives as core
from app.services import video_frame_service as service
from tests.factory import make_user_dict
from tests.video_derivative_fixtures import (
    owned_metadata,
    owned_output,
    saved_url,
    video_authority,
)


@pytest.fixture
async def committed_video(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    owner_data = make_user_dict(
        "super_admin", f"video-lock-owner-{uuid.uuid4()}@test.local", "Owner"
    )
    actor_data = make_user_dict(
        "employee", f"video-lock-reader-{uuid.uuid4()}@test.local", "Reader"
    )
    project_id, dataset_id, item_id, task_id = (uuid.uuid4() for _ in range(4))
    state = SimpleNamespace(
        factory=factory,
        owner_id=owner_data["id"],
        actor_id=actor_data["id"],
        project_id=project_id,
        dataset_id=dataset_id,
        item_id=item_id,
        task_id=task_id,
        dispatched=[],
        physical=[],
    )
    monkeypatch.setattr(core.settings, "video_chunk_size_frames", 60)

    async def look_ahead(*args, **kwargs):
        return 1

    async def verified(db, output, authority, *, relative=None, bytes_limit=None):
        assert not db.in_transaction()
        core.validate_result(output, output.result, output.inventory)
        async with factory() as guard:
            await core.check(guard, output, authority)
        await _item_update_available(state)
        state.physical.append(output.id)
        assert relative is not None and bytes_limit is None
        return saved_url(output, relative)

    def dispatch(identifiers):
        if identifiers:
            state.dispatched.append(list(identifiers))

    monkeypatch.setattr(service.SystemSettingsService, "get", look_ahead)
    monkeypatch.setattr(core, "verified", verified)
    monkeypatch.setattr(core, "dispatch", dispatch)
    try:
        async with factory() as db:
            owner, actor = User(**owner_data), User(**actor_data)
            db.add_all([owner, actor])
            await db.flush()
            project = Project(
                id=project_id,
                display_id="P-VL-" + uuid.uuid4().hex[:10],
                name="Video lock windows",
                type_key="video-track",
                type_label="Video",
                data_type="video",
                owner_id=owner.id,
            )
            dataset = Dataset(
                id=dataset_id,
                display_id="D-VL-" + uuid.uuid4().hex[:10],
                name="Video lock windows",
                data_type="video",
                created_by=owner.id,
            )
            db.add_all([project, dataset])
            await db.flush()
            item = DatasetItem(
                id=item_id,
                dataset_id=dataset.id,
                file_name="source.mp4",
                file_path=f"video-lock-tests/{uuid.uuid4()}/source.mp4",
                file_type="video",
                metadata_={"video": {"fps": 30, "frame_count": 180}},
            )
            db.add(item)
            await db.flush()
            db.add_all(
                [
                    Task(
                        id=task_id,
                        display_id="T-VL-" + uuid.uuid4().hex[:10],
                        project_id=project.id,
                        dataset_item_id=item.id,
                        file_name=item.file_name,
                        file_path=item.file_path,
                        file_type="video",
                        status="pending",
                        assignee_id=actor.id,
                    ),
                    ProjectMember(
                        project_id=project.id,
                        user_id=actor.id,
                        role="annotator",
                        assigned_by=owner.id,
                    ),
                ]
            )
            await db.flush()
            generation = await owned_metadata(db, item)
            token = create_access_token(str(actor.id), actor.role)
            request = Request(
                {
                    "type": "http",
                    "headers": [(b"authorization", f"Bearer {token}".encode())],
                }
            )
            state.authority = await originals.authorize(db, task_id, actor, request)
            state.generation_id = generation.id
            await db.commit()
        yield state
    finally:
        async with factory() as db:
            identifiers = list(
                await db.scalars(
                    select(Output.id).where(Output.dataset_item_id == item_id)
                )
            )
            if identifiers:
                assert not await db.scalar(
                    select(StorageOperation.id)
                    .where(
                        StorageOperation.purpose.in_(
                            [objects.purpose(value) for value in identifiers]
                        )
                    )
                    .limit(1)
                )
            await db.execute(delete(Task).where(Task.id == task_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
            if identifiers:
                await db.execute(
                    update(Output)
                    .where(
                        Output.id.in_(identifiers),
                        Output.state.in_(["building", "ready"]),
                    )
                    .values(state="retiring")
                )
                await db.execute(
                    update(Output)
                    .where(Output.id.in_(identifiers), Output.state == "retiring")
                    .values(state="deleted")
                )
                await db.execute(delete(Output).where(Output.id.in_(identifiers)))
            await db.execute(
                delete(User).where(User.id.in_([owner_data["id"], actor_data["id"]]))
            )
            await db.commit()


async def _item_update_available(state):
    """A second actual connection must see the row and acquire UPDATE immediately."""
    async with state.factory() as other:
        identifier = await other.scalar(
            select(DatasetItem.id)
            .where(DatasetItem.id == state.item_id)
            .with_for_update(nowait=True)
        )
        assert identifier == state.item_id


async def _context(db, state):
    task = await db.get(Task, state.task_id)
    return await service.build_context_from_task(db, task, authority=state.authority)


@pytest.mark.parametrize("kind", ["frame", "chunk"])
async def test_each_prepare_releases_item_before_next_slot_and_physical_delivery(
    committed_video, monkeypatch, kind
):
    state = committed_video
    async with state.factory() as db:
        item = await db.get(DatasetItem, state.item_id)
        ready = await owned_output(db, item, kind)
    prepare = service._prepare
    boundaries = []

    async def observe(db, ctx, prepared_kind, spec, **kwargs):
        if boundaries:
            await _item_update_available(state)
        boundaries.append(spec["frame_index"] if kind == "frame" else spec["chunk_id"])
        return await prepare(db, ctx, prepared_kind, spec, **kwargs)

    monkeypatch.setattr(service, "_prepare", observe)
    async with state.factory() as db:
        ctx = await _context(db, state)
        if kind == "frame":
            response = await service.prefetch_frames(db, ctx, [0, 1, 2], 512, "webp")
            delivered = response.frames
        else:
            response = await service.list_chunks(db, ctx, 0, 65)
            delivered = response.chunks
        assert not db.in_transaction()
    assert boundaries == [0, 1, 2]  # chunk 2 is the separately committed warmup.
    assert state.physical == [ready.id]
    assert [row.status for row in delivered] == ["ready", "pending"] + (
        ["pending"] if kind == "frame" else []
    )
    assert len(state.dispatched) == 1 and len(state.dispatched[0]) == 2
    async with state.factory() as db:
        pending = list(
            await db.scalars(select(Output).where(Output.id.in_(state.dispatched[0])))
        )
        assert len(pending) == 2
        assert all(
            output.kind == kind
            and output.state == "building"
            and output.lease_token is None
            for output in pending
        )
        assert all(
            output.metadata_output_id == state.generation_id for output in pending
        )
    await _item_update_available(state)


@pytest.mark.parametrize("kind", ["frame", "chunk"])
async def test_overlapping_reads_reuse_a_committed_slot_instead_of_reporting_busy(
    committed_video, kind
):
    """Chunk, frame and prefetch reads for one video must coexist.

    The first reader keeps its transaction open, as a slow delivery does. A
    second actual connection must still reuse the committed slot rather than
    failing with content_busy / storage_original_busy on the Item row.
    """
    state = committed_video
    async with state.factory() as setup:
        item = await setup.get(DatasetItem, state.item_id)
        authority = await video_authority(setup, item)
        ready_id = (await owned_output(setup, item, kind)).id
        generation = await core.row(setup, Output, state.generation_id)
        spec = (
            core.frame_spec(generation, 0, 512, "webp")
            if kind == "frame"
            else core.chunk_spec(generation, 0)
        )
    async with state.factory() as first, state.factory() as second:
        opened = await core.prepare(first, state.item_id, authority, kind, spec=spec)
        assert (opened[0].id, opened[1]) == (ready_id, False)
        overlapping = await core.prepare(
            second, state.item_id, authority, kind, spec=spec
        )
        assert (overlapping[0].id, overlapping[1]) == (ready_id, False)
        await first.rollback()


async def _await_lock_wait(state, pid, reader):
    """Prove the real reader is blocked on a row lock, not merely slow."""
    async with state.factory() as observer:
        for _ in range(400):
            row = (
                await observer.execute(
                    text(
                        "SELECT wait_event_type, wait_event FROM pg_stat_activity "
                        "WHERE pid = :pid"
                    ),
                    {"pid": pid},
                )
            ).one_or_none()
            await observer.rollback()
            if row is not None and row[0] == "Lock":
                return row
            if reader.done():
                raise AssertionError("reader finished before the writer committed")
            await asyncio.sleep(0.01)
    raise AssertionError("reader never waited on the blocked visibility row")


async def test_counter_writer_is_waited_out_but_a_held_identity_fence_fails_closed(
    committed_video,
):
    """Counters get a bounded wait; an identity fence still fails closed.

    Deleting an annotation commits `projects.in_progress_tasks` and
    `tasks.total_annotations` under short ordinary transactions. An authorized
    read must wait that writer out inside the existing authority bound and then
    decide on the committed values. A held `FOR UPDATE` on the Task row (a
    removal or key change) must still reject with content_busy in the same bound.
    """
    state = committed_video
    # The fixture pins video_chunk_size_frames=60, so chunk 0 owns frames 0..59
    # and its samples must address every one of them inside the chunk bytes.
    samples = [
        {
            "frame_index": frame,
            "pts_ms": frame * 33,
            "duration_ms": 33,
            "is_keyframe": frame == 0,
            "size_bytes": 4,
            "offset_in_chunk": frame * 4,
        }
        for frame in range(60)
    ]
    diagnostics = {
        "codec_string": "avc1.4d001e",
        "width": 640,
        "height": 360,
        "samples": samples,
    }
    async with state.factory() as setup:
        item = await setup.get(DatasetItem, state.item_id)
        ready = await owned_output(
            setup,
            item,
            "chunk",
            diagnostics=diagnostics,
            data=b"\x00" * 4096,
        )
        ready_id = ready.id
    assert ready.state == "ready"
    async with state.factory() as writer, state.factory() as db:
        await writer.execute(
            update(Project)
            .where(Project.id == state.project_id)
            .values(in_progress_tasks=11)
        )
        await writer.execute(
            update(Task)
            .where(Task.id == state.task_id)
            .values(status="in_progress", total_annotations=3, is_labeled=True)
        )
        pid = await db.scalar(text("SELECT pg_backend_pid()"))
        ctx = await _context(db, state)
        reader = asyncio.create_task(
            service.chunk_samples(db, ctx, 0, expected_output_id=ready_id)
        )
        try:
            waited = await _await_lock_wait(state, pid, reader)
            assert waited[0] == "Lock"
        finally:
            if writer.in_transaction():
                await writer.commit()
            await asyncio.gather(reader, return_exceptions=True)
        body = reader.result()
        assert [entry.model_dump() for entry in body.samples] == samples
        assert body.output_id == ready_id
        current = (
            await db.execute(
                select(Project.in_progress_tasks, Task.total_annotations)
                .select_from(Project)
                .join(Task, Task.project_id == Project.id)
                .where(Project.id == state.project_id)
                .execution_options(populate_existing=True)
            )
        ).one()
        assert tuple(current) == (11, 3)
        await db.rollback()
    async with state.factory() as blocker, state.factory() as db:
        await blocker.execute(
            select(Task.id).where(Task.id == state.task_id).with_for_update()
        )
        ctx = await _context(db, state)
        started = time.monotonic()
        with pytest.raises(HTTPException) as raised:
            await service.chunk_samples(db, ctx, 0, expected_output_id=ready_id)
        assert time.monotonic() - started < 5
        assert raised.value.status_code == 409
        assert raised.value.detail == {"code": "content_busy"}
        await db.rollback()
        await blocker.rollback()


async def _change_batch_fence(state, change):
    async with state.factory() as db:
        if change == "generation":
            item = await db.get(DatasetItem, state.item_id)
            replacement = await owned_metadata(db, item, force=True)
            assert replacement.id != state.generation_id
        elif change == "actor":
            await db.execute(
                update(User).where(User.id == state.actor_id).values(is_active=False)
            )
        else:
            await db.execute(
                update(DatasetItem)
                .where(DatasetItem.id == state.item_id)
                .values(file_path="changed/another-original.mp4")
            )
        await db.commit()


@pytest.mark.parametrize("change", ["generation", "actor", "source"])
@pytest.mark.parametrize("stage", ["next_prepare", "physical"])
async def test_mid_batch_fence_change_never_delivers_or_dispatches_committed_attempts(
    committed_video, monkeypatch, change, stage
):
    state = committed_video
    if stage == "physical":
        async with state.factory() as db:
            item = await db.get(DatasetItem, state.item_id)
            await owned_output(db, item, "frame", frame_index=0)
            await owned_output(db, item, "frame", frame_index=1)
    prepare = service._prepare
    verified = core.verified
    prepared = []

    async def observe_prepare(db, ctx, kind, spec, **kwargs):
        if stage == "next_prepare" and len(prepared) == 1:
            await _item_update_available(state)
            await _change_batch_fence(state, change)
        output, enqueue = await prepare(db, ctx, kind, spec, **kwargs)
        prepared.append(output.id)
        return output, enqueue

    async def observe_physical(db, output, authority, **kwargs):
        value = await verified(db, output, authority, **kwargs)
        if len(state.physical) == 2:
            # The later owner changes after an earlier verified response has
            # been staged. The final batch fence must still reject all delivery.
            await _change_batch_fence(state, change)
        return value

    monkeypatch.setattr(service, "_prepare", observe_prepare)
    monkeypatch.setattr(core, "verified", observe_physical)
    async with state.factory() as db:
        ctx = await _context(db, state)
        with pytest.raises(HTTPException) as raised:
            await service.prefetch_frames(db, ctx, [0, 1, 2], 512, "webp")
        assert raised.value.status_code in {401, 403, 409}
        await db.rollback()
    assert state.dispatched == []
    assert len(state.physical) == (2 if stage == "physical" else 0)
    async with state.factory() as db:
        responsibility = list(
            await db.scalars(
                select(Output).where(
                    Output.dataset_item_id == state.item_id,
                    Output.kind == "frame",
                    Output.state == "building",
                )
            )
        )
        assert responsibility
        assert all(
            output.id in prepared and output.lease_token is None
            for output in responsibility
        )
        assert all(
            output.inventory is None and output.result is None
            for output in responsibility
        )
        assert any(
            output.metadata_output_id == state.generation_id
            for output in responsibility
        )


async def test_seed_retirement_drains_a_reader_allocated_after_its_initial_snapshot(
    committed_video, monkeypatch
):
    from app.api.v1 import _test_seed as seed
    from app.api.v1._test_seed_attachments import FixtureScope
    from app.services import storage_access

    state = committed_video
    scope = FixtureScope(dataset_ids=(state.dataset_id,))
    original_outputs = seed._fixture_video_outputs
    late_output = permit = None

    async def snapshot_then_allocate(db, scope, *, lock=False, **kwargs):
        nonlocal late_output, permit
        result = await original_outputs(db, scope, lock=lock, **kwargs)
        if not lock and late_output is None:
            async with state.factory() as writer:
                item = await writer.get(DatasetItem, state.item_id)
                late_output = await owned_output(writer, item, "frame")
                permit = await storage_access.acquire(
                    writer,
                    uuid.UUID(late_output.storage_ref["location_id"]),
                    objects.purpose(late_output.id),
                )
        return result

    monkeypatch.setattr(seed, "_fixture_video_outputs", snapshot_then_allocate)
    try:
        async with state.factory() as db:
            commit = db.commit

            async def finish_read_after_retirement():
                await commit()
                async with state.factory() as final_read:
                    assert (
                        await final_read.get(Output, late_output.id)
                    ).state == "retiring"
                    # Its final read must be able to finish: the collector may not
                    # hold the Item write fence while waiting for the permit.
                    assert (
                        await final_read.scalar(
                            select(DatasetItem.id)
                            .where(DatasetItem.id == state.item_id)
                            .with_for_update(read=True, nowait=True)
                        )
                        == state.item_id
                    )
                await permit.__aexit__(None, None, None)

            monkeypatch.setattr(db, "commit", finish_read_after_retirement)
            plan = await seed._prepare_fixture_video_cleanup(db, scope)
        assert {receipt.id for receipt in plan.receipts} == {
            state.generation_id,
            late_output.id,
        }
        async with state.factory() as db:
            assert await db.get(StorageOperation, permit.id) is None
            assert (await db.get(Output, late_output.id)).state == "retiring"
    finally:
        if permit is not None:
            await permit.__aexit__(None, None, None)
