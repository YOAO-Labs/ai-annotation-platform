from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import bindparam, delete, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_append_operation import TaskAppendOperation
from app.db.models.user import User


_APPEND_ITEMS = text(
    """
    SELECT aap_labeling_v1.append_dataset_items(
        :dataset_id,
        :item_ids,
        :operation_id
    )
    """
).bindparams(
    bindparam("dataset_id", type_=UUID(as_uuid=True)),
    bindparam("item_ids", type_=ARRAY(UUID(as_uuid=True))),
    bindparam("operation_id", type_=UUID(as_uuid=True)),
)


async def _append(
    db: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    item_ids: list[uuid.UUID],
    operation_id: uuid.UUID,
) -> dict:
    return (
        await db.execute(
            _APPEND_ITEMS,
            {
                "dataset_id": dataset_id,
                "item_ids": item_ids,
                "operation_id": operation_id,
            },
        )
    ).scalar_one()


async def _seed_uncommitted(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    dataset_count: int = 1,
    project_count: int = 1,
    items_per_dataset: int = 1,
) -> tuple[list[Dataset], list[Project], dict[uuid.UUID, list[DatasetItem]]]:
    suffix = uuid.uuid4().hex[:8].upper()
    datasets = [
        Dataset(
            id=uuid.uuid4(),
            display_id=f"DSA-{suffix}-{index}",
            name=f"task-append-{suffix}-{index}",
            data_type="image",
            created_by=owner_id,
            file_count=items_per_dataset,
        )
        for index in range(dataset_count)
    ]
    projects = [
        Project(
            id=uuid.uuid4(),
            display_id=f"PA-{suffix}-{index}",
            name=f"task append {suffix} {index}",
            type_label="图像检测",
            type_key="image-det",
            data_type="image",
            owner_id=owner_id,
            total_tasks=0,
        )
        for index in range(project_count)
    ]
    db.add_all([*datasets, *projects])
    await db.flush()

    items_by_dataset: dict[uuid.UUID, list[DatasetItem]] = {}
    for dataset_index, dataset in enumerate(datasets):
        items = [
            DatasetItem(
                id=uuid.uuid4(),
                dataset_id=dataset.id,
                file_name=f"image-{item_index}.jpg",
                file_path=f"{dataset.name}/image-{item_index}.jpg",
                file_type="image",
                file_size=123,
            )
            for item_index in range(items_per_dataset)
        ]
        items_by_dataset[dataset.id] = items
        db.add_all(items)
        db.add_all(
            ProjectDataset(
                id=uuid.uuid4(),
                dataset_id=dataset.id,
                project_id=project.id,
            )
            for project in projects
        )
    await db.flush()
    return datasets, projects, items_by_dataset


async def _seed_committed(
    test_engine,
    *,
    dataset_count: int,
    project_count: int,
    items_per_dataset: int,
) -> dict:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:10]
    owner_id = uuid.uuid4()
    user = User(
        id=owner_id,
        email=f"task-append-{suffix}@test.local",
        name=f"task append {suffix}",
        password_hash="test-only",
        role="super_admin",
        status="offline",
        is_active=True,
    )
    async with maker() as db:
        db.add(user)
        await db.flush()
        datasets, projects, items_by_dataset = await _seed_uncommitted(
            db,
            owner_id=owner_id,
            dataset_count=dataset_count,
            project_count=project_count,
            items_per_dataset=items_per_dataset,
        )
        await db.commit()

    return {
        "owner_id": owner_id,
        "dataset_ids": [dataset.id for dataset in datasets],
        "project_ids": [project.id for project in projects],
        "item_ids": {
            dataset_id: [item.id for item in items]
            for dataset_id, items in items_by_dataset.items()
        },
    }


async def _cleanup_committed(
    test_engine, fixture: dict, operation_ids: list[uuid.UUID]
):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as db:
        if operation_ids:
            await db.execute(
                delete(TaskAppendOperation).where(
                    TaskAppendOperation.operation_id.in_(operation_ids)
                )
            )
        await db.execute(
            delete(Task).where(Task.project_id.in_(fixture["project_ids"]))
        )
        await db.execute(
            delete(ProjectDataset).where(
                ProjectDataset.dataset_id.in_(fixture["dataset_ids"])
            )
        )
        await db.execute(delete(Dataset).where(Dataset.id.in_(fixture["dataset_ids"])))
        await db.execute(delete(Project).where(Project.id.in_(fixture["project_ids"])))
        await db.execute(delete(User).where(User.id == fixture["owner_id"]))
        await db.commit()


@pytest.mark.asyncio
async def test_append_receipt_replays_defaults_and_conflicts_on_changed_request(
    db_session: AsyncSession, super_admin
):
    owner, _ = super_admin
    datasets, projects, items_by_dataset = await _seed_uncommitted(
        db_session,
        owner_id=owner.id,
        dataset_count=1,
        project_count=1,
        items_per_dataset=2,
    )
    dataset = datasets[0]
    project = projects[0]
    first, second = items_by_dataset[dataset.id]
    operation_id = uuid.uuid4()

    result = await _append(
        db_session,
        dataset_id=dataset.id,
        item_ids=[second.id, first.id, second.id],
        operation_id=operation_id,
    )
    replay = await _append(
        db_session,
        dataset_id=dataset.id,
        item_ids=[first.id, second.id],
        operation_id=operation_id,
    )

    assert result == replay
    assert result["schema"] == "aap-task-append/v1"
    assert result["operation_id"] == str(operation_id)
    assert result["dataset_id"] == str(dataset.id)
    assert result["created"] == 2
    assert result["associations"] == [
        {
            "id": str(
                (
                    await db_session.scalar(
                        select(ProjectDataset.id).where(
                            ProjectDataset.dataset_id == dataset.id,
                            ProjectDataset.project_id == project.id,
                        )
                    )
                )
            ),
            "project_id": str(project.id),
        }
    ]
    assert result["project_totals"] == [
        {"project_id": str(project.id), "total_tasks": 2}
    ]

    tasks = list(
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.dataset_item_id.in_([first.id, second.id]),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(tasks) == 2
    for task in tasks:
        assert task.file_type == "image"
        assert task.tags == []
        assert task.status == "pending"
        assert task.is_labeled is False
        assert task.overlap == 1
        assert task.total_annotations == 0
        assert task.total_predictions == 0
        assert task.batch_id is None
        assert task.version == 1
        assert task.assignee_is_override is False
        assert task.reviewer_is_override is False
        assert task.first_review_eligible is True
        assert task.annotation_contributor_ids == []
        assert task.reopened_count == 0

    receipt = await db_session.get(TaskAppendOperation, operation_id)
    assert receipt is not None
    assert receipt.item_ids == sorted([first.id, second.id])
    assert receipt.result == result
    assert (
        await db_session.scalar(
            select(Project.total_tasks).where(Project.id == project.id)
        )
        == 2
    )

    with pytest.raises(DBAPIError) as conflict:
        async with db_session.begin_nested():
            await _append(
                db_session,
                dataset_id=dataset.id,
                item_ids=[first.id],
                operation_id=operation_id,
            )
    assert "operation_conflict" in str(conflict.value)


@pytest.mark.asyncio
async def test_append_is_atomic_with_caller_transaction(
    db_session: AsyncSession, super_admin
):
    owner, _ = super_admin
    datasets, projects, items_by_dataset = await _seed_uncommitted(
        db_session,
        owner_id=owner.id,
        dataset_count=1,
        project_count=1,
        items_per_dataset=1,
    )
    dataset = datasets[0]
    project = projects[0]
    item = items_by_dataset[dataset.id][0]
    operation_id = uuid.uuid4()

    savepoint = await db_session.begin_nested()
    result = await _append(
        db_session,
        dataset_id=dataset.id,
        item_ids=[item.id],
        operation_id=operation_id,
    )
    assert result["created"] == 1
    await savepoint.rollback()

    assert await db_session.get(TaskAppendOperation, operation_id) is None
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Task).where(Task.project_id == project.id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(Project.total_tasks).where(Project.id == project.id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_concurrent_same_operation_and_distinct_appends_do_not_duplicate_tasks(
    test_engine,
):
    fixture = await _seed_committed(
        test_engine,
        dataset_count=1,
        project_count=1,
        items_per_dataset=1,
    )
    dataset_id = fixture["dataset_ids"][0]
    item_id = fixture["item_ids"][dataset_id][0]
    project_id = fixture["project_ids"][0]
    operation_id = uuid.uuid4()
    second_operation_id = uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async def run(operation: uuid.UUID) -> dict:
        async with maker() as db:
            async with db.begin():
                return await _append(
                    db,
                    dataset_id=dataset_id,
                    item_ids=[item_id],
                    operation_id=operation,
                )

    try:
        same_operation = await asyncio.wait_for(
            asyncio.gather(run(operation_id), run(operation_id)), timeout=20
        )
        assert same_operation[0] == same_operation[1]
        assert same_operation[0]["created"] == 1

        distinct_operations = await asyncio.wait_for(
            asyncio.gather(run(operation_id), run(second_operation_id)), timeout=20
        )
        assert sorted(result["created"] for result in distinct_operations) == [0, 1]

        async with maker() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(
                        Task.project_id == project_id, Task.dataset_item_id == item_id
                    )
                )
                == 1
            )
            assert (
                await db.scalar(
                    select(Project.total_tasks).where(Project.id == project_id)
                )
                == 1
            )
    finally:
        await _cleanup_committed(
            test_engine,
            fixture,
            [operation_id, second_operation_id],
        )


@pytest.mark.asyncio
async def test_concurrent_two_dataset_two_project_counters_are_atomic(test_engine):
    fixture = await _seed_committed(
        test_engine,
        dataset_count=2,
        project_count=2,
        items_per_dataset=1,
    )
    dataset_ids = fixture["dataset_ids"]
    operation_ids = [uuid.uuid4(), uuid.uuid4()]
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async def run(index: int) -> dict:
        async with maker() as db:
            async with db.begin():
                return await _append(
                    db,
                    dataset_id=dataset_ids[index],
                    item_ids=fixture["item_ids"][dataset_ids[index]],
                    operation_id=operation_ids[index],
                )

    try:
        results = await asyncio.wait_for(asyncio.gather(run(0), run(1)), timeout=20)
        assert [result["created"] for result in results] == [2, 2]
        for project_id in fixture["project_ids"]:
            assert await _project_totals_for_results(results, project_id) == [1, 2]

        async with maker() as db:
            for project_id in fixture["project_ids"]:
                assert (
                    await db.scalar(
                        select(Project.total_tasks).where(Project.id == project_id)
                    )
                    == 2
                )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.project_id.in_(fixture["project_ids"]))
                )
                == 4
            )
    finally:
        await _cleanup_committed(test_engine, fixture, operation_ids)


async def _project_totals_for_results(
    results: list[dict], project_id: uuid.UUID
) -> list[int]:
    return sorted(
        total["total_tasks"]
        for result in results
        for total in result["project_totals"]
        if total["project_id"] == str(project_id)
    )


@pytest.mark.asyncio
async def test_failed_commit_leaves_no_receipt_tasks_or_counter(test_engine):
    fixture = await _seed_committed(
        test_engine,
        dataset_count=1,
        project_count=1,
        items_per_dataset=1,
    )
    dataset_id = fixture["dataset_ids"][0]
    project_id = fixture["project_ids"][0]
    item_id = fixture["item_ids"][dataset_id][0]
    operation_id = uuid.uuid4()
    suffix = uuid.uuid4().hex
    function_name = f"fail_task_append_commit_{suffix}"
    trigger_name = f"fail_task_append_commit_{suffix}"
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async with test_engine.begin() as connection:
        await connection.execute(
            text(
                f"""
                CREATE FUNCTION public.{function_name}() RETURNS trigger
                LANGUAGE plpgsql AS $test$
                BEGIN
                    IF current_setting('aap.test_task_append_fail_commit', true) = 'on' THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '23514',
                            MESSAGE = 'forced task append commit failure';
                    END IF;
                    RETURN NULL;
                END;
                $test$
                """
            )
        )
        await connection.execute(
            text(
                f"""
                CREATE CONSTRAINT TRIGGER {trigger_name}
                AFTER INSERT OR UPDATE ON public.task_append_operations
                DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION public.{function_name}()
                """
            )
        )

    try:
        async with maker() as db:
            await db.begin()
            await db.execute(
                text(
                    "SELECT pg_catalog.set_config("
                    "'aap.test_task_append_fail_commit', 'on', true)"
                )
            )
            assert (
                await _append(
                    db,
                    dataset_id=dataset_id,
                    item_ids=[item_id],
                    operation_id=operation_id,
                )
            )["created"] == 1
            with pytest.raises(DBAPIError, match="forced task append commit failure"):
                await db.commit()
            await db.rollback()

        async with maker() as observer:
            assert await observer.get(TaskAppendOperation, operation_id) is None
            assert (
                await observer.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.project_id == project_id)
                )
                == 0
            )
            assert (
                await observer.scalar(
                    select(Project.total_tasks).where(Project.id == project_id)
                )
                == 0
            )
    finally:
        async with test_engine.begin() as connection:
            await connection.execute(
                text(
                    f"DROP TRIGGER IF EXISTS {trigger_name} "
                    "ON public.task_append_operations"
                )
            )
            await connection.execute(
                text(f"DROP FUNCTION IF EXISTS public.{function_name}()")
            )
        await _cleanup_committed(test_engine, fixture, [operation_id])


def test_append_json_schema_freezes_request_and_result_shape():
    schema_path = (
        Path(__file__).parents[1] / "alembic" / "sql" / "0191_task_append.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    request = schema["$defs"]["request"]
    result = schema["$defs"]["result"]

    assert request["additionalProperties"] is False
    assert set(request["required"]) == {"dataset_id", "item_ids", "operation_id"}
    assert result["additionalProperties"] is False
    assert set(result["required"]) == {
        "schema",
        "operation_id",
        "dataset_id",
        "created",
        "project_totals",
        "associations",
    }
    assert result["properties"]["schema"]["const"] == "aap-task-append/v1"
    assert schema["$defs"]["association"]["additionalProperties"] is False
    assert schema["$defs"]["project_total"]["additionalProperties"] is False
    assert not TaskAppendOperation.__table__.foreign_keys


def test_append_sql_artifact_uses_single_statement_frozen_chunks():
    artifact_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "sql"
        / "0191_dataset_task_append_contract.sql"
    )
    artifact = artifact_path.read_text(encoding="utf-8")
    statements = [
        statement.strip()
        for statement in artifact.split("-- aap-statement-break")
        if statement.strip()
    ]

    assert statements[0].startswith("-- Persistent")
    assert any(
        statement.startswith("CREATE FUNCTION") and statement.endswith("$function$;")
        for statement in statements
    )
    assert all(
        statement.count(";") == 1
        for statement in statements
        if not statement.startswith("CREATE FUNCTION")
    )
