"""Real consumers preserve the append data API's transaction and locking contract."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aap_assets_source.operator_contract import OperatorItem, OperatorSourceEntry
from pydantic import ValidationError
from sqlalchemy import delete, event, func, insert, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.responses import Response

from app.application.dataset_deletion import delete_dataset, unlink_dataset_from_project
from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_append_operation import TaskAppendOperation
from app.db.models.user import User
from app.services.assets_operator import AssetsOperatorGateway
from app.services.labeling_task_creation_contract import (
    AppendAssociation,
    AppendProjectTotal,
    TaskAppendResult,
)
from app.services.labeling_tasks import LabelingTaskService


async def _append_operator_item(
    db,
    dataset_id,
    file_name: str,
    file_path: str,
    file_type: str,
    file_size: int | None = None,
) -> OperatorItem:
    (item,) = await AssetsOperatorGateway(db).append_all_items(
        dataset_id,
        [
            OperatorSourceEntry(
                id=uuid4(),
                file_name=file_name,
                file_path=file_path,
                file_type=file_type,
                file_size=file_size,
                metadata={},
            )
        ],
    )
    return item


@pytest.fixture
async def append_state(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    owner_id, dataset_id, project_id, item_id = (uuid4() for _ in range(4))
    suffix = uuid4().hex[:12]
    async with maker.begin() as db:
        db.add(
            User(
                id=owner_id,
                email=f"append-consumer-{suffix}@example.test",
                name="Append consumer fixture",
                password_hash="unused-test-hash",
                role="project_admin",
            )
        )
        await db.flush()
        db.add_all(
            [
                Dataset(
                    id=dataset_id,
                    display_id=f"D-AC-{suffix}",
                    name=f"append-consumer-{suffix}",
                    data_type="image",
                    created_by=owner_id,
                    file_count=1,
                ),
                Project(
                    id=project_id,
                    display_id=f"P-AC-{suffix}",
                    name="Append consumer fixture",
                    type_label="Image detection",
                    type_key="image-det",
                    owner_id=owner_id,
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                DatasetItem(
                    id=item_id,
                    dataset_id=dataset_id,
                    file_name="first.jpg",
                    file_path=f"append-consumer-{suffix}/first.jpg",
                    file_type="image",
                ),
                ProjectDataset(dataset_id=dataset_id, project_id=project_id),
            ]
        )
    try:
        yield SimpleNamespace(
            maker=maker,
            owner_id=owner_id,
            dataset_id=dataset_id,
            project_id=project_id,
            item_id=item_id,
        )
    finally:
        async with maker.begin() as db:
            await db.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
            await db.execute(
                text(
                    "DELETE FROM public.audit_logs WHERE actor_id=:actor AND action IN ('dataset.unlink','dataset.item_delete','dataset.delete')"
                ),
                {"actor": owner_id},
            )
            await db.execute(
                text(
                    "DELETE FROM public.dataset_unlink_operations WHERE actor_id=:actor"
                ),
                {"actor": owner_id},
            )
            await db.execute(
                text(
                    "DELETE FROM public.dataset_source_deletion_operations WHERE actor_id=:actor"
                ),
                {"actor": owner_id},
            )
            await db.execute(delete(Task).where(Task.project_id == project_id))
            await db.execute(
                delete(ProjectDataset).where(ProjectDataset.dataset_id == dataset_id)
            )
            await db.execute(
                delete(TaskAppendOperation).where(
                    TaskAppendOperation.dataset_id == dataset_id
                )
            )
            await db.execute(
                delete(DatasetItem).where(DatasetItem.dataset_id == dataset_id)
            )
            await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(User).where(User.id == owner_id))


@pytest.mark.asyncio
async def test_legacy_append_flushes_and_rolls_back_sources_tasks_counters_and_receipts(
    append_state,
):
    state = append_state
    async with state.maker() as db:
        item = DatasetItem(
            id=uuid4(),
            dataset_id=state.dataset_id,
            file_name="uncommitted.jpg",
            file_path="append-consumer/uncommitted.jpg",
            file_type="image",
        )
        db.add(item)
        item_id = item.id
        assert (
            await DatasetTaskCreationWorkflow(db).append_items(
                state.dataset_id, [item_id, item_id]
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count(TaskAppendOperation.operation_id)).where(
                    TaskAppendOperation.dataset_id == state.dataset_id
                )
            )
            == 1
        )
        await db.rollback()

    async with state.maker() as observer:
        assert await observer.get(DatasetItem, item_id) is None
        assert (
            await observer.scalar(
                select(func.count(Task.id)).where(Task.project_id == state.project_id)
            )
            == 0
        )
        assert (
            await observer.scalar(
                select(func.count(TaskAppendOperation.operation_id)).where(
                    TaskAppendOperation.dataset_id == state.dataset_id
                )
            )
            == 0
        )
        assert (await observer.get(Project, state.project_id)).total_tasks == 0


@pytest.mark.asyncio
async def test_receipt_replay_does_not_replace_current_cached_total_with_historical_total(
    append_state,
):
    state = append_state
    operation_id = uuid4()
    async with state.maker() as db:
        project = await db.get(Project, state.project_id)
        service = LabelingTaskService(db)
        assert (
            await service.append_dataset_items(
                dataset_id=state.dataset_id,
                item_ids=(state.item_id,),
                operation_id=operation_id,
            )
            == 1
        )
        await db.commit()
        item = await _append_operator_item(
            db,
            state.dataset_id,
            "second.jpg",
            "append-consumer/second.jpg",
            "image",
        )
        workflow = DatasetTaskCreationWorkflow(db)
        assert await workflow.append_items(state.dataset_id, [item.id]) == 1
        assert await workflow.append_items(state.dataset_id, [item.id]) == 0
        await db.commit()
        await db.refresh(project)
        assert project.total_tasks == 2

        assert (
            await service.append_dataset_items(
                dataset_id=state.dataset_id,
                item_ids=(state.item_id,),
                operation_id=operation_id,
            )
            == 1
        )
        assert "total_tasks" in inspect(project).expired_attributes
        await db.refresh(project)
        assert project.total_tasks == 2


async def test_append_updates_project_timestamp_but_receipt_replay_does_not(
    append_state,
):
    state = append_state
    operation_id = uuid4()
    async with state.maker.begin() as owner:
        await owner.execute(
            text(
                "UPDATE public.projects SET updated_at = '2000-01-01 UTC' WHERE id = :id"
            ),
            {"id": state.project_id},
        )
    async with state.maker() as db:
        project = await db.get(Project, state.project_id)
        original = project.updated_at
        service = LabelingTaskService(db)
        await service.append_dataset_items(
            dataset_id=state.dataset_id,
            item_ids=(state.item_id,),
            operation_id=operation_id,
        )
        assert "updated_at" in inspect(project).expired_attributes
        await db.refresh(project)
        assert project.updated_at > original
        await db.commit()
    async with state.maker.begin() as owner:
        newer = await owner.scalar(
            text(
                "UPDATE public.projects SET updated_at = updated_at + interval '1 day' "
                "WHERE id = :id RETURNING updated_at"
            ),
            {"id": state.project_id},
        )
    async with state.maker() as db:
        assert (
            await LabelingTaskService(db).append_dataset_items(
                dataset_id=state.dataset_id,
                item_ids=(state.item_id,),
                operation_id=operation_id,
            )
            == 1
        )
        assert (await db.get(Project, state.project_id)).updated_at == newer


async def _wait_for_blocker(test_engine, waiter: int, holder: int):
    async def wait():
        async with test_engine.connect() as observer:
            while not await observer.scalar(
                text(
                    "SELECT CAST(:holder AS int) = ANY(pg_blocking_pids(CAST(:waiter AS int)))"
                ),
                {"holder": holder, "waiter": waiter},
            ):
                await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["append", "unlink"])
async def test_real_append_and_unlink_serialize_without_orphan_tasks(
    test_engine, append_state, first
):
    state = append_state
    async with state.maker() as holder, state.maker() as waiter:
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await waiter.scalar(text("SELECT pg_backend_pid()"))

        async def append(db):
            return await DatasetTaskCreationWorkflow(db).append_items(
                state.dataset_id, [state.item_id]
            )

        async def unlink(db):
            return await unlink_dataset_from_project(
                db,
                state.dataset_id,
                state.project_id,
                actor_id=state.owner_id,
                cleanup_bucket="append-consumer-cache",
            )

        leading, trailing = (append, unlink) if first == "append" else (unlink, append)
        leading_result = await leading(holder)
        pending = asyncio.create_task(trailing(waiter))
        try:
            await _wait_for_blocker(test_engine, waiter_pid, holder_pid)
            await holder.commit()
            trailing_result = await asyncio.wait_for(pending, timeout=5)
            await waiter.commit()
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if first == "append":
            assert leading_result == 1
            assert trailing_result["deleted_tasks"] == 1
        else:
            assert leading_result["deleted_tasks"] == 0
            assert trailing_result == 0

    async with state.maker() as db:
        assert (
            await db.scalar(
                select(func.count(Task.id)).where(Task.project_id == state.project_id)
            )
            == 0
        )
        assert (
            await db.scalar(
                select(ProjectDataset.id).where(
                    ProjectDataset.dataset_id == state.dataset_id
                )
            )
            is None
        )
        assert (await db.get(Project, state.project_id)).total_tasks == 0


@pytest.mark.asyncio
async def test_item_delete_uses_parent_lock_before_append_reference(
    test_engine, append_state
):
    state = append_state
    async with state.maker() as deleter, state.maker() as appender:
        deleter_pid = await deleter.scalar(text("SELECT pg_backend_pid()"))
        appender_pid = await appender.scalar(text("SELECT pg_backend_pid()"))
        assert (
            await delete_dataset(
                deleter,
                state.dataset_id,
                item_id=state.item_id,
                actor_id=state.owner_id,
                cleanup_bucket="append-test-cache",
            )
        ).deleted_items == 1
        pending = asyncio.create_task(
            DatasetTaskCreationWorkflow(appender).append_items(
                state.dataset_id, [state.item_id]
            )
        )
        try:
            await _wait_for_blocker(test_engine, appender_pid, deleter_pid)
            await deleter.commit()
            assert await asyncio.wait_for(pending, timeout=5) == 0
            await appender.commit()
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_cached_dataset_count_cannot_overwrite_another_source_writer(
    append_state,
):
    state = append_state
    async with state.maker() as stale, state.maker() as peer:
        cached = await stale.get(Dataset, state.dataset_id)
        assert cached.file_count == 1
        peer_item = await _append_operator_item(
            peer, state.dataset_id, "peer.jpg", "peer.jpg", "image"
        )
        await peer.commit()
        stale_item = await _append_operator_item(
            stale, state.dataset_id, "stale.jpg", "stale.jpg", "image"
        )
        assert (peer_item.dataset_id, stale_item.dataset_id) == (
            state.dataset_id,
            state.dataset_id,
        )
        assert cached.file_count == 1
        await stale.commit()
    async with state.maker() as observer:
        assert (
            await observer.scalar(
                select(Dataset.file_count).where(Dataset.id == state.dataset_id)
            )
            == 3
        )
        assert (
            await observer.scalar(
                select(func.count(DatasetItem.id)).where(
                    DatasetItem.dataset_id == state.dataset_id
                )
            )
            == 3
        )


@pytest.mark.asyncio
async def test_inflight_replay_cannot_invert_source_writer_parent_lock(
    test_engine, append_state
):
    state = append_state
    operation_id = uuid4()
    async with state.maker() as source_writer, state.maker() as retry:
        source_pid = await source_writer.scalar(text("SELECT pg_backend_pid()"))
        retry_pid = await retry.scalar(text("SELECT pg_backend_pid()"))
        item = await _append_operator_item(
            source_writer,
            state.dataset_id,
            "inflight.jpg",
            "append-consumer/inflight.jpg",
            "image",
        )
        arguments = {
            "dataset_id": state.dataset_id,
            "item_ids": (item.id,),
            "operation_id": operation_id,
        }
        pending = asyncio.create_task(
            LabelingTaskService(retry).append_dataset_items(**arguments)
        )
        try:
            await _wait_for_blocker(test_engine, retry_pid, source_pid)
            assert (
                await asyncio.wait_for(
                    LabelingTaskService(source_writer).append_dataset_items(
                        **arguments
                    ),
                    timeout=5,
                )
                == 1
            )
            await source_writer.commit()
            assert await asyncio.wait_for(pending, timeout=5) == 1
            await retry.commit()
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_busy_unlink_retries_after_other_dataset_append_without_losing_total(
    test_engine, append_state
):
    state = append_state
    other_dataset_id, other_item_id = uuid4(), uuid4()
    async with state.maker.begin() as db:
        await DatasetTaskCreationWorkflow(db).append_items(
            state.dataset_id, [state.item_id]
        )
        original = await db.get(Dataset, state.dataset_id)
        db.add(
            Dataset(
                id=other_dataset_id,
                display_id=f"D-AC-{uuid4().hex[:12]}",
                name="Other dataset append",
                created_by=original.created_by,
                data_type="image",
            )
        )
        await db.flush()
        db.add_all(
            [
                DatasetItem(
                    id=other_item_id,
                    dataset_id=other_dataset_id,
                    file_name="other.jpg",
                    file_path="append-consumer/other.jpg",
                    file_type="image",
                ),
                ProjectDataset(
                    dataset_id=other_dataset_id, project_id=state.project_id
                ),
            ]
        )
    try:
        async with state.maker() as appender, state.maker() as unlinker:
            assert (
                await DatasetTaskCreationWorkflow(appender).append_items(
                    other_dataset_id, [other_item_id]
                )
                == 1
            )
            from fastapi import HTTPException

            operation_id = uuid4()
            with pytest.raises(HTTPException) as busy:
                await unlink_dataset_from_project(
                    unlinker,
                    state.dataset_id,
                    state.project_id,
                    actor_id=state.owner_id,
                    cleanup_bucket="append-consumer-cache",
                    operation_id=operation_id,
                )
            assert busy.value.status_code == 409
            assert busy.value.detail == {"reason": "project_busy"}
            await unlinker.rollback()
            await appender.commit()
            assert (
                await unlink_dataset_from_project(
                    unlinker,
                    state.dataset_id,
                    state.project_id,
                    actor_id=state.owner_id,
                    cleanup_bucket="append-consumer-cache",
                    operation_id=operation_id,
                )
            )["deleted_tasks"] == 1
            await unlinker.commit()
        async with state.maker() as observer:
            assert (await observer.get(Project, state.project_id)).total_tasks == 1
            assert (
                await observer.scalar(
                    select(func.count(Task.id)).where(
                        Task.project_id == state.project_id
                    )
                )
                == 1
            )
    finally:
        async with state.maker.begin() as db:
            await db.execute(delete(Task).where(Task.dataset_item_id == other_item_id))
            await db.execute(
                delete(TaskAppendOperation).where(
                    TaskAppendOperation.dataset_id == other_dataset_id
                )
            )
            await db.execute(
                delete(ProjectDataset).where(
                    ProjectDataset.dataset_id == other_dataset_id
                )
            )
            await db.execute(delete(DatasetItem).where(DatasetItem.id == other_item_id))
            await db.execute(delete(Dataset).where(Dataset.id == other_dataset_id))


@pytest.mark.asyncio
async def test_upload_completion_locks_parent_before_item_updates_and_delete(
    test_engine, append_state, monkeypatch
):
    from app.api.v1.datasets import upload_complete
    from app.services.assets_source import _LegacyUploadData
    from app.services.storage import storage_service

    state = append_state
    async with state.maker.begin() as db:
        await db.execute(
            delete(ProjectDataset).where(ProjectDataset.dataset_id == state.dataset_id)
        )
    upload_locked, resume_upload = asyncio.Event(), asyncio.Event()

    complete_legacy_upload = _LegacyUploadData.complete

    async def pause_before_legacy_complete(service, request):
        upload_locked.set()
        await resume_upload.wait()
        return await complete_legacy_upload(service, request)

    monkeypatch.setattr(_LegacyUploadData, "complete", pause_before_legacy_complete)
    monkeypatch.setattr(
        storage_service,
        "verify_upload",
        lambda *args, **kwargs: {"ContentLength": 123, "ETag": "a" * 32},
    )
    monkeypatch.setattr(
        storage_service, "read_image_dimensions", lambda *args, **kwargs: None
    )
    dispatcher = SimpleNamespace(dispatch_thumbnail=lambda item_id: None)
    async with state.maker() as uploader, state.maker() as deleter:
        upload_pid = await uploader.scalar(text("SELECT pg_backend_pid()"))
        delete_pid = await deleter.scalar(text("SELECT pg_backend_pid()"))
        upload = asyncio.create_task(
            upload_complete(
                state.dataset_id,
                state.item_id,
                Response(),
                db=uploader,
                current_user=SimpleNamespace(id=state.owner_id),
                media_dispatcher=dispatcher,
                idempotency_key=uuid4(),
            )
        )
        deletion = None
        try:
            await asyncio.wait_for(upload_locked.wait(), timeout=5)
            deletion = asyncio.create_task(
                delete_dataset(
                    deleter,
                    state.dataset_id,
                    item_id=state.item_id,
                    actor_id=state.owner_id,
                    cleanup_bucket="append-test-cache",
                )
            )
            await _wait_for_blocker(test_engine, delete_pid, upload_pid)
            resume_upload.set()
            assert (await asyncio.wait_for(upload, timeout=5))["linked_tasks"] == 0
            assert (await asyncio.wait_for(deletion, timeout=5)).deleted_items == 1
            await deleter.commit()
        finally:
            resume_upload.set()
            tasks = [upload] + ([deletion] if deletion is not None else [])
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_zip_sized_append_uses_one_data_request_and_preserves_all_items(
    test_engine, append_state
):
    state = append_state
    item_ids = [state.item_id, *(uuid4() for _ in range(4999))]
    async with state.maker.begin() as db:
        await db.execute(
            insert(DatasetItem),
            [
                {
                    "id": item_id,
                    "dataset_id": state.dataset_id,
                    "file_name": f"bulk-{index}.jpg",
                    "file_path": f"append-consumer/bulk-{index}.jpg",
                    "file_type": "image",
                }
                for index, item_id in enumerate(item_ids[1:])
            ],
        )
    statements = []

    def count_statement(
        connection, cursor, statement, parameters, context, executemany
    ):
        statements.append(statement)

    async with state.maker() as db:
        event.listen(test_engine.sync_engine, "before_cursor_execute", count_statement)
        try:
            assert (
                await DatasetTaskCreationWorkflow(db).append_items(
                    state.dataset_id, item_ids
                )
                == 5000
            )
        finally:
            event.remove(
                test_engine.sync_engine, "before_cursor_execute", count_statement
            )
        assert len(statements) == 1
        await db.commit()
        assert (
            await db.scalar(
                select(func.count(func.distinct(Task.dataset_item_id))).where(
                    Task.project_id == state.project_id
                )
            )
            == 5000
        )
        assert (await db.get(Project, state.project_id)).total_tasks == 5000


@pytest.mark.parametrize(
    "invalid",
    [{"schema": "unknown"}, {"created": -1}, {"created": True}, {"extra": "private"}],
)
def test_unknown_or_invalid_append_receipt_is_rejected(invalid):
    valid = {
        "schema": "aap-task-append/v1",
        "operation_id": str(uuid4()),
        "dataset_id": str(uuid4()),
        "created": 0,
        "project_totals": [],
        "associations": [],
    }
    with pytest.raises(ValidationError):
        TaskAppendResult.model_validate({**valid, **invalid})


def test_python_receipt_binding_tracks_frozen_data_contract():
    contract = json.loads(
        (
            Path(__file__).parents[1] / "alembic/sql/0191_task_append.schema.json"
        ).read_text()
    )["$defs"]
    for name, binding in (
        ("result", TaskAppendResult),
        ("association", AppendAssociation),
        ("project_total", AppendProjectTotal),
    ):
        bound = binding.model_json_schema()
        assert set(bound["properties"]) == set(contract[name]["properties"])
        assert set(bound["required"]) == set(contract[name]["required"])
        assert bound["additionalProperties"] == contract[name]["additionalProperties"]
