"""Owned acceptance checks for Workspace project-query foreign facts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from tests.factory import create_project, create_task, create_user

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data role grants and disposable test database",
    ),
]

_SCHEMA_PATH = (
    Path(__file__).parents[1]
    / "alembic/sql/0260_workspace_project_query_support.schema.json"
)
_FUNCTIONS = (
    ("aap_labeling_v1.workspace_project_query_counters(uuid[])", "labeling_code"),
    ("aap_labeling_v1.workspace_project_query_ai_counts(uuid[])", "labeling_code"),
    (
        "aap_labeling_v1.workspace_project_query_annotation_count(uuid[],boolean)",
        "labeling_code",
    ),
    (
        "aap_labeling_v1.workspace_project_query_task_series(uuid[],timestamptz[])",
        "labeling_code",
    ),
    (
        "aap_labeling_v1.workspace_project_query_annotation_series(uuid[],timestamptz[])",
        "labeling_code",
    ),
    ("aap_assets_v1.workspace_project_query_dataset_ids(uuid)", "assets_code"),
    (
        "aap_assets_v1.workspace_project_query_dataset_item_count(uuid[])",
        "assets_code",
    ),
    (
        "aap_assets_v1.workspace_project_query_dataset_item_ids(uuid[])",
        "assets_code",
    ),
    (
        "aap_labeling_v1.workspace_project_query_linked_task_count(uuid,uuid[])",
        "labeling_code",
    ),
    ("aap_labeling_v1.workspace_project_query_task_count(uuid)", "labeling_code"),
    ("aap_labeling_v1.workspace_project_query_batches(uuid)", "labeling_code"),
    (
        "aap_labeling_v1.workspace_project_query_batch_task_counts(uuid)",
        "labeling_code",
    ),
    ("aap_jobs_v1.workspace_project_query_job_status_counts(uuid)", "jobs_code"),
    ("aap_jobs_v1.workspace_project_query_latest_job_status(uuid)", "jobs_code"),
    ("aap_inference_v1.workspace_project_query_pool(uuid)", "inference_code"),
)


def _sqlstate(error: BaseException) -> str | None:
    return getattr(error, "sqlstate", None) or getattr(
        getattr(error, "orig", None), "sqlstate", None
    )


async def _rows(session, statement: str, parameters: dict | None = None):
    result = await session.execute(text(statement), parameters or {})
    return [dict(row) for row in result.mappings().all()]


async def _scalar(session, statement: str, parameters: dict | None = None):
    return await session.scalar(text(statement), parameters or {})


async def _invalid_calls(maker, statements: list[str]) -> None:
    for statement in statements:
        async with maker() as session:
            with pytest.raises(DBAPIError) as error:
                await session.execute(text(statement))
            assert _sqlstate(error.value) == "22023", statement
            await session.rollback()


async def _new_project(session, user_id: uuid.UUID, name: str):
    return await create_project(session, owner_id=user_id, name=name)


async def _new_annotation(
    session,
    *,
    task_id: uuid.UUID,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    active: bool = True,
    cancelled: bool = False,
    ai: bool = False,
    created_at: datetime | None = None,
):
    annotation = Annotation(
        task_id=task_id,
        project_id=project_id,
        user_id=user_id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 1, "y": 2, "w": 3, "h": 4},
        is_active=active,
        was_cancelled=cancelled,
        parent_prediction_id=uuid.uuid4() if ai else None,
        created_at=created_at,
    )
    session.add(annotation)
    await session.flush()
    return annotation


async def test_project_query_functions_have_fixed_domain_owners_and_grants(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    signatures = [signature for signature, _ in _FUNCTIONS]
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))

    async with maker() as session:
        rows = await _rows(
            session,
            """
            SELECT expected.signature,
                   namespace.nspname AS schema_name,
                   pg_catalog.pg_get_userbyid(function.proowner) AS owner_role,
                   function.prosecdef AS security_definer,
                   function.provolatile::text AS volatility,
                   function.proparallel::text AS parallel_mode,
                   function.proconfig AS function_settings,
                   pg_catalog.has_function_privilege(
                       'workspace_code', function.oid, 'EXECUTE'
                   ) AS workspace_can_execute,
                   pg_catalog.has_function_privilege(
                       'labeling_code', function.oid, 'EXECUTE'
                   ) AS labeling_can_execute,
                   EXISTS (
                       SELECT 1
                       FROM pg_catalog.aclexplode(COALESCE(
                           function.proacl,
                           pg_catalog.acldefault('f', function.proowner)
                       )) AS privilege
                       WHERE privilege.grantee = 0
                         AND privilege.privilege_type = 'EXECUTE'
                   ) AS public_can_execute
            FROM pg_catalog.unnest(CAST(:signatures AS text[]))
                AS expected(signature)
            JOIN pg_catalog.pg_proc AS function
              ON function.oid = pg_catalog.to_regprocedure(expected.signature)
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = function.pronamespace
            """,
            {"signatures": signatures},
        )

    assert schema["$id"] == "aap-workspace-project-query-support/v1"
    assert schema["function_defaults"] == {
        "volatility": "STABLE",
        "parallel": "UNSAFE",
        "security_definer": True,
        "search_path": ["pg_catalog", "pg_temp"],
    }
    assert schema["x-postgresql-functions"] == signatures
    assert [function["signature"] for function in schema["functions"]] == signatures
    assert {row["signature"] for row in rows} == set(signatures)
    expected_owners = dict(_FUNCTIONS)
    for row in rows:
        expected_schema = row["signature"].split(".", maxsplit=1)[0]
        assert row["schema_name"] == expected_schema
        assert row["owner_role"] == expected_owners[row["signature"]]
        assert row["security_definer"] is True
        assert row["volatility"] == "s"
        assert row["parallel_mode"] == "u"
        assert row["function_settings"] == ["search_path=pg_catalog, pg_temp"]
        if row["signature"] == (
            "aap_assets_v1.workspace_project_query_dataset_item_ids(uuid[])"
        ):
            assert row["workspace_can_execute"] is False
            assert row["labeling_can_execute"] is True
        else:
            assert row["workspace_can_execute"] is True
        assert row["public_can_execute"] is False


async def test_project_counters_and_annotation_count_semantics(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    user_id = None
    project_ids: list[uuid.UUID] = []
    try:
        async with maker.begin() as session:
            user = await create_user(
                session,
                "super_admin",
                f"workspace-project-query-counts-{suffix}@example.test",
                "Workspace project-query owner",
            )
            user_id = user.id
            first = await _new_project(session, user.id, f"Counters first {suffix}")
            project_ids.append(first.id)
            second = await _new_project(session, user.id, f"Counters second {suffix}")
            project_ids.append(second.id)
            first.total_tasks = -7
            first.completed_tasks = 13
            first.review_tasks = 4
            first.in_progress_tasks = 9
            first.batch_summary = {"raw": [None, "persisted"]}
            first_task = await create_task(session, project_id=first.id)
            second_task = await create_task(session, project_id=first.id)
            inactive_task = await create_task(session, project_id=first.id)
            await _new_annotation(
                session,
                task_id=first_task.id,
                project_id=first.id,
                user_id=user.id,
                active=True,
                cancelled=True,
                ai=True,
            )
            await _new_annotation(
                session,
                task_id=first_task.id,
                project_id=first.id,
                user_id=user.id,
                active=True,
                cancelled=False,
                ai=True,
            )
            await _new_annotation(
                session,
                task_id=second_task.id,
                project_id=first.id,
                user_id=user.id,
                active=True,
                cancelled=False,
                ai=False,
            )
            await _new_annotation(
                session,
                task_id=second_task.id,
                project_id=first.id,
                user_id=user.id,
                active=True,
                cancelled=True,
                ai=True,
            )
            await _new_annotation(
                session,
                task_id=inactive_task.id,
                project_id=first.id,
                user_id=user.id,
                active=False,
                cancelled=False,
                ai=True,
            )

        async with maker() as session:
            counters = await _rows(
                session,
                """
                SELECT project_id, total_tasks, completed_tasks, review_tasks,
                       in_progress_tasks, batch_summary
                FROM aap_labeling_v1.workspace_project_query_counters(
                    CAST(:project_ids AS uuid[])
                )
                """,
                {"project_ids": [project_ids[0], project_ids[1], project_ids[0]]},
            )
            ai_counts = await _rows(
                session,
                """
                SELECT project_id, task_count
                FROM aap_labeling_v1.workspace_project_query_ai_counts(
                    CAST(:project_ids AS uuid[])
                )
                """,
                {"project_ids": [project_ids[0], project_ids[1], project_ids[0]]},
            )
            total_annotations = await _scalar(
                session,
                """
                SELECT aap_labeling_v1.workspace_project_query_annotation_count(
                    CAST(:project_ids AS uuid[]), false
                )
                """,
                {"project_ids": [project_ids[0], project_ids[1], project_ids[0]]},
            )
            ai_annotations = await _scalar(
                session,
                """
                SELECT aap_labeling_v1.workspace_project_query_annotation_count(
                    CAST(:project_ids AS uuid[]), true
                )
                """,
                {"project_ids": [project_ids[0], project_ids[1], project_ids[0]]},
            )
            empty_counters = await _rows(
                session,
                """
                SELECT project_id FROM aap_labeling_v1.workspace_project_query_counters(
                    CAST(:project_ids AS uuid[])
                )
                """,
                {"project_ids": []},
            )

        counter_by_id = {row["project_id"]: row for row in counters}
        assert set(counter_by_id) == set(project_ids)
        assert len(counters) == 2
        assert counter_by_id[project_ids[0]] == {
            "project_id": project_ids[0],
            "total_tasks": -7,
            "completed_tasks": 13,
            "review_tasks": 4,
            "in_progress_tasks": 9,
            "batch_summary": {"raw": [None, "persisted"]},
        }
        assert {row["project_id"]: row["task_count"] for row in ai_counts} == {
            project_ids[0]: 2
        }
        assert total_annotations == 2
        assert ai_annotations == 1
        assert empty_counters == []
    finally:
        if project_ids:
            async with maker.begin() as session:
                await session.execute(
                    delete(Annotation).where(Annotation.project_id.in_(project_ids))
                )
                await session.execute(
                    delete(Task).where(Task.project_id.in_(project_ids))
                )
                await session.execute(
                    delete(Project).where(Project.id.in_(project_ids))
                )
        if user_id is not None:
            async with maker.begin() as session:
                await session.execute(delete(User).where(User.id == user_id))


async def test_project_query_series_keep_twelve_input_ordered_bucket_boundaries(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    user_id = None
    project_ids: list[uuid.UUID] = []
    try:
        async with maker.begin() as session:
            user = await create_user(
                session,
                "super_admin",
                f"workspace-project-query-series-{suffix}@example.test",
                "Workspace series owner",
            )
            user_id = user.id
            project = await _new_project(session, user.id, f"Series {suffix}")
            project_ids.append(project.id)
            other = await _new_project(session, user.id, f"Other series {suffix}")
            project_ids.append(other.id)
            first_end = datetime(2025, 1, 1, tzinfo=UTC)
            ends = [first_end + timedelta(days=index) for index in range(12)]
            before_first = ends[0] - timedelta(seconds=1)

            baseline = await create_task(session, project_id=project.id)
            baseline.created_at = before_first
            baseline.updated_at = before_first

            future = await create_task(session, project_id=project.id)
            future.created_at = ends[4]
            future.updated_at = ends[4]

            fallback_completed = await create_task(
                session, project_id=project.id, status="completed"
            )
            fallback_completed.created_at = before_first
            fallback_completed.updated_at = ends[6]
            fallback_completed.reviewed_at = None

            reviewed_completed = await create_task(
                session, project_id=project.id, status="completed"
            )
            reviewed_completed.created_at = before_first
            reviewed_completed.updated_at = ends[1]
            reviewed_completed.reviewed_at = ends[9]

            reviewed_submission = await create_task(
                session, project_id=project.id, status="reviewing"
            )
            reviewed_submission.created_at = before_first
            reviewed_submission.updated_at = before_first
            reviewed_submission.submitted_at = ends[3]
            reviewed_submission.reviewed_at = ends[5]

            open_submission = await create_task(session, project_id=project.id)
            open_submission.created_at = before_first
            open_submission.updated_at = before_first
            open_submission.submitted_at = ends[10]

            foreign_task = await create_task(
                session, project_id=other.id, status="completed"
            )
            foreign_task.created_at = before_first
            foreign_task.updated_at = before_first
            foreign_task.reviewed_at = before_first
            foreign_task.submitted_at = before_first
            await session.flush()

            await _new_annotation(
                session,
                task_id=baseline.id,
                project_id=project.id,
                user_id=user.id,
                created_at=ends[2],
            )
            await _new_annotation(
                session,
                task_id=baseline.id,
                project_id=project.id,
                user_id=user.id,
                ai=True,
                created_at=ends[4],
            )
            await _new_annotation(
                session,
                task_id=baseline.id,
                project_id=project.id,
                user_id=user.id,
                cancelled=True,
                ai=True,
                created_at=ends[0],
            )
            await _new_annotation(
                session,
                task_id=future.id,
                project_id=project.id,
                user_id=user.id,
                ai=True,
                created_at=ends[10],
            )
            await _new_annotation(
                session,
                task_id=foreign_task.id,
                project_id=other.id,
                user_id=user.id,
                ai=True,
                created_at=ends[0],
            )

        async with maker() as session:
            task_series = await _rows(
                session,
                """
                SELECT bucket_index, total_data, completed, pending_review
                FROM aap_labeling_v1.workspace_project_query_task_series(
                    CAST(:project_ids AS uuid[]), CAST(:ends AS timestamptz[])
                )
                ORDER BY bucket_index
                """,
                {"project_ids": [project_ids[0], project_ids[0]], "ends": ends},
            )
            annotation_series = await _rows(
                session,
                """
                SELECT bucket_index, total_annotations, ai_annotations
                FROM aap_labeling_v1.workspace_project_query_annotation_series(
                    CAST(:project_ids AS uuid[]), CAST(:ends AS timestamptz[])
                )
                ORDER BY bucket_index
                """,
                {"project_ids": [project_ids[0]], "ends": ends},
            )
            empty_series = await _rows(
                session,
                """
                SELECT bucket_index, total_data, completed, pending_review
                FROM aap_labeling_v1.workspace_project_query_task_series(
                    CAST(:project_ids AS uuid[]), CAST(:ends AS timestamptz[])
                )
                ORDER BY bucket_index
                """,
                {"project_ids": [], "ends": ends},
            )
            lower_bound_series = await _rows(
                session,
                """
                SELECT bucket_index, total_data, completed, pending_review
                FROM aap_labeling_v1.workspace_project_query_task_series(
                    CAST(:project_ids AS uuid[]),
                    array_fill(CAST(:last_end AS timestamptz), ARRAY[12], ARRAY[0])
                )
                ORDER BY bucket_index
                """,
                {"project_ids": [project_ids[0]], "last_end": ends[-1]},
            )

        assert [row["bucket_index"] for row in task_series] == list(range(1, 13))
        assert [row["total_data"] for row in task_series] == [5] * 4 + [6] * 8
        assert [row["completed"] for row in task_series] == [0] * 6 + [1] * 3 + [2] * 3
        assert [row["pending_review"] for row in task_series] == [
            0,
            0,
            0,
            1,
            1,
            0,
            0,
            0,
            0,
            0,
            1,
            1,
        ]
        assert [row["bucket_index"] for row in annotation_series] == list(range(1, 13))
        assert [row["total_annotations"] for row in annotation_series] == [
            0,
            0,
            1,
            1,
            2,
            2,
            2,
            2,
            2,
            2,
            3,
            3,
        ]
        assert [row["ai_annotations"] for row in annotation_series] == [
            0,
            0,
            0,
            0,
            1,
            1,
            1,
            1,
            1,
            1,
            2,
            2,
        ]
        assert len(empty_series) == 12
        assert all(
            row["total_data"] == row["completed"] == row["pending_review"] == 0
            for row in empty_series
        )
        assert lower_bound_series == [
            {
                "bucket_index": index,
                "total_data": 6,
                "completed": 2,
                "pending_review": 1,
            }
            for index in range(1, 13)
        ]
    finally:
        if project_ids:
            async with maker.begin() as session:
                await session.execute(
                    delete(Annotation).where(Annotation.project_id.in_(project_ids))
                )
                await session.execute(
                    delete(Task).where(Task.project_id.in_(project_ids))
                )
                await session.execute(
                    delete(Project).where(Project.id.in_(project_ids))
                )
        if user_id is not None:
            async with maker.begin() as session:
                await session.execute(delete(User).where(User.id == user_id))


async def test_project_query_assets_use_captured_dataset_ids_after_unlink(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    user_id = None
    project_ids: list[uuid.UUID] = []
    dataset_ids: list[uuid.UUID] = []
    item_ids: list[uuid.UUID] = []
    try:
        async with maker.begin() as session:
            user = await create_user(
                session,
                "super_admin",
                f"workspace-project-query-assets-{suffix}@example.test",
                "Workspace asset query owner",
            )
            user_id = user.id
            project = await _new_project(session, user.id, f"Assets {suffix}")
            project_ids.append(project.id)
            other_project = await _new_project(
                session, user.id, f"Other assets {suffix}"
            )
            project_ids.append(other_project.id)
            first_dataset = Dataset(
                display_id=f"PQ-{suffix[:12]}",
                name=f"Captured first {suffix}",
                data_type="image",
                created_by=user.id,
            )
            second_dataset = Dataset(
                display_id=f"PR-{suffix[:12]}",
                name=f"Captured second {suffix}",
                data_type="image",
                created_by=user.id,
            )
            outside_dataset = Dataset(
                display_id=f"PS-{suffix[:12]}",
                name=f"Outside {suffix}",
                data_type="image",
                created_by=user.id,
            )
            session.add_all([first_dataset, second_dataset, outside_dataset])
            await session.flush()
            dataset_ids = [first_dataset.id, second_dataset.id, outside_dataset.id]
            first_item = DatasetItem(
                dataset_id=first_dataset.id,
                file_name=f"first-{suffix}.jpg",
                file_path=f"project-query/{suffix}/first.jpg",
                file_type="image",
            )
            second_item = DatasetItem(
                dataset_id=second_dataset.id,
                file_name=f"second-{suffix}.jpg",
                file_path=f"project-query/{suffix}/second.jpg",
                file_type="image",
            )
            outside_item = DatasetItem(
                dataset_id=outside_dataset.id,
                file_name=f"outside-{suffix}.jpg",
                file_path=f"project-query/{suffix}/outside.jpg",
                file_type="image",
            )
            session.add_all([first_item, second_item, outside_item])
            await session.flush()
            item_ids = [first_item.id, second_item.id, outside_item.id]
            session.add_all(
                [
                    ProjectDataset(project_id=project.id, dataset_id=first_dataset.id),
                    ProjectDataset(project_id=project.id, dataset_id=second_dataset.id),
                ]
            )
            linked_task = await create_task(session, project_id=project.id)
            linked_task.dataset_item_id = first_item.id
            second_linked_task = await create_task(session, project_id=project.id)
            second_linked_task.dataset_item_id = second_item.id
            uploading_task = await create_task(
                session, project_id=project.id, status="uploading"
            )
            uploading_task.dataset_item_id = first_item.id
            await create_task(session, project_id=project.id)
            foreign_task = await create_task(session, project_id=other_project.id)
            foreign_task.dataset_item_id = first_item.id
            await session.flush()

            captured_dataset_ids = await _rows(
                session,
                """
                SELECT dataset_id
                FROM aap_assets_v1.workspace_project_query_dataset_ids(
                    CAST(:project_id AS uuid)
                )
                """,
                {"project_id": project.id},
            )
            assert {row["dataset_id"] for row in captured_dataset_ids} == set(
                dataset_ids[:2]
            )
            await session.execute(
                delete(ProjectDataset).where(ProjectDataset.project_id == project.id)
            )

            current_dataset_ids = await _rows(
                session,
                """
                SELECT dataset_id
                FROM aap_assets_v1.workspace_project_query_dataset_ids(
                    CAST(:project_id AS uuid)
                )
                """,
                {"project_id": project.id},
            )
            captured_ids = [row["dataset_id"] for row in captured_dataset_ids]
            item_count = await _scalar(
                session,
                """
                SELECT aap_assets_v1.workspace_project_query_dataset_item_count(
                    CAST(:dataset_ids AS uuid[])
                )
                """,
                {"dataset_ids": [*captured_ids, captured_ids[0]]},
            )
            existing_items = await _rows(
                session,
                """
                SELECT item_id
                FROM aap_assets_v1.workspace_project_query_dataset_item_ids(
                    CAST(:dataset_ids AS uuid[])
                )
                """,
                {"dataset_ids": captured_ids},
            )
            linked_count = await _scalar(
                session,
                """
                SELECT aap_labeling_v1.workspace_project_query_linked_task_count(
                    CAST(:project_id AS uuid), CAST(:dataset_ids AS uuid[])
                )
                """,
                {"project_id": project.id, "dataset_ids": captured_ids},
            )
            task_count = await _scalar(
                session,
                """
                SELECT aap_labeling_v1.workspace_project_query_task_count(
                    CAST(:project_id AS uuid)
                )
                """,
                {"project_id": project.id},
            )
            empty_item_count = await _scalar(
                session,
                """
                SELECT aap_assets_v1.workspace_project_query_dataset_item_count(
                    CAST(:dataset_ids AS uuid[])
                )
                """,
                {"dataset_ids": []},
            )

        assert current_dataset_ids == []
        assert item_count == 2
        assert {row["item_id"] for row in existing_items} == set(item_ids[:2])
        assert linked_count == 2
        assert task_count == 3
        assert empty_item_count == 0
    finally:
        if project_ids:
            async with maker.begin() as session:
                await session.execute(
                    delete(Task).where(Task.project_id.in_(project_ids))
                )
                await session.execute(
                    delete(ProjectDataset).where(
                        ProjectDataset.project_id.in_(project_ids)
                    )
                )
                if dataset_ids:
                    await session.execute(
                        delete(DatasetItem).where(
                            DatasetItem.dataset_id.in_(dataset_ids)
                        )
                    )
                    await session.execute(
                        delete(Dataset).where(Dataset.id.in_(dataset_ids))
                    )
                await session.execute(
                    delete(Project).where(Project.id.in_(project_ids))
                )
        if user_id is not None:
            async with maker.begin() as session:
                await session.execute(delete(User).where(User.id == user_id))


async def test_project_query_batches_jobs_and_pool_keep_legacy_rules(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    user_id = None
    project_ids: list[uuid.UUID] = []
    batch_ids: list[uuid.UUID] = []
    pool_id = None
    try:
        async with maker.begin() as session:
            user = await create_user(
                session,
                "super_admin",
                f"workspace-project-query-jobs-{suffix}@example.test",
                "Workspace job query owner",
            )
            user_id = user.id
            project = await _new_project(session, user.id, f"Batches and jobs {suffix}")
            project_ids.append(project.id)
            other = await _new_project(session, user.id, f"Other jobs {suffix}")
            project_ids.append(other.id)
            active_batch = TaskBatch(
                project_id=project.id,
                display_id=f"BQ-{suffix[:8]}-A",
                name=f"Draft batch {suffix}",
                status="draft",
                annotator_id=user.id,
                reviewer_id=user.id,
            )
            archived_batch = TaskBatch(
                project_id=project.id,
                display_id=f"BQ-{suffix[:8]}-B",
                name=f"Archived batch {suffix}",
                status="archived",
            )
            session.add_all([active_batch, archived_batch])
            await session.flush()
            batch_ids = [active_batch.id, archived_batch.id]

            active_first = await create_task(session, project_id=project.id)
            active_first.batch_id = active_batch.id
            active_second = await create_task(session, project_id=project.id)
            active_second.batch_id = active_batch.id
            archived_task = await create_task(session, project_id=project.id)
            archived_task.batch_id = archived_batch.id
            upload_task = await create_task(
                session, project_id=project.id, status="uploading"
            )
            upload_task.batch_id = active_batch.id
            await create_task(session, project_id=project.id)

            timestamp = datetime(2025, 5, 1, tzinfo=UTC)
            tie_ids = sorted([uuid.uuid4(), uuid.uuid4()])
            session.add_all(
                [
                    AsyncJob(
                        id=uuid.uuid4(),
                        project_id=project.id,
                        kind="create_tasks",
                        status="pending",
                        created_at=timestamp - timedelta(days=1),
                    ),
                    AsyncJob(
                        id=tie_ids[0],
                        project_id=project.id,
                        kind="create_tasks",
                        status="running",
                        created_at=timestamp,
                    ),
                    AsyncJob(
                        id=tie_ids[1],
                        project_id=project.id,
                        kind="create_tasks",
                        status="failed",
                        created_at=timestamp,
                    ),
                    AsyncJob(
                        project_id=project.id,
                        kind="batch_predict",
                        status="completed",
                        created_at=timestamp + timedelta(days=1),
                    ),
                    AsyncJob(
                        project_id=other.id,
                        kind="create_tasks",
                        status="completed",
                        created_at=timestamp + timedelta(days=2),
                    ),
                ]
            )
            pool = MLBackendServicePool(
                name=f"Disabled query pool {suffix}",
                enabled=False,
                legacy_instance_id=None,
            )
            session.add(pool)
            await session.flush()
            pool_id = pool.id

        async with maker() as session:
            batches = await _rows(
                session,
                """
                SELECT id, status, annotator_id, reviewer_id
                FROM aap_labeling_v1.workspace_project_query_batches(
                    CAST(:project_id AS uuid)
                )
                """,
                {"project_id": project_ids[0]},
            )
            batch_counts = await _rows(
                session,
                """
                SELECT batch_id, task_count
                FROM aap_labeling_v1.workspace_project_query_batch_task_counts(
                    CAST(:project_id AS uuid)
                )
                """,
                {"project_id": project_ids[0]},
            )
            job_counts = await _rows(
                session,
                """
                SELECT status, job_count
                FROM aap_jobs_v1.workspace_project_query_job_status_counts(
                    CAST(:project_id AS uuid)
                )
                """,
                {"project_id": project_ids[0]},
            )
            latest_status = await _rows(
                session,
                """
                SELECT status
                FROM aap_jobs_v1.workspace_project_query_latest_job_status(
                    CAST(:project_id AS uuid)
                )
                """,
                {"project_id": project_ids[0]},
            )
            empty_latest = await _rows(
                session,
                """
                SELECT status
                FROM aap_jobs_v1.workspace_project_query_latest_job_status(
                    CAST(:project_id AS uuid)
                )
                """,
                {"project_id": uuid.uuid4()},
            )
            task_count = await _scalar(
                session,
                """
                SELECT aap_labeling_v1.workspace_project_query_task_count(
                    CAST(:project_id AS uuid)
                )
                """,
                {"project_id": project_ids[0]},
            )
            pool_row = await _rows(
                session,
                """
                SELECT pool_id, legacy_instance_id
                FROM aap_inference_v1.workspace_project_query_pool(
                    CAST(:pool_id AS uuid)
                )
                """,
                {"pool_id": pool_id},
            )
            missing_pool = await _rows(
                session,
                """
                SELECT pool_id, legacy_instance_id
                FROM aap_inference_v1.workspace_project_query_pool(
                    CAST(:pool_id AS uuid)
                )
                """,
                {"pool_id": uuid.uuid4()},
            )

        assert {row["id"] for row in batches} == {batch_ids[0]}
        assert batches[0]["status"] == "draft"
        assert batches[0]["annotator_id"] == user_id
        assert batches[0]["reviewer_id"] == user_id
        assert {row["batch_id"]: row["task_count"] for row in batch_counts} == {
            batch_ids[0]: 2,
            batch_ids[1]: 1,
            None: 1,
        }
        assert {row["status"]: row["job_count"] for row in job_counts} == {
            "pending": 1,
            "running": 1,
            "failed": 1,
        }
        assert latest_status == [{"status": "failed"}]
        assert empty_latest == []
        assert task_count == 4
        assert pool_row == [{"pool_id": pool_id, "legacy_instance_id": None}]
        assert missing_pool == []
    finally:
        async with maker.begin() as session:
            if project_ids:
                await session.execute(
                    delete(Task).where(Task.project_id.in_(project_ids))
                )
                await session.execute(
                    delete(AsyncJob).where(AsyncJob.project_id.in_(project_ids))
                )
                if batch_ids:
                    await session.execute(
                        delete(TaskBatch).where(TaskBatch.id.in_(batch_ids))
                    )
                await session.execute(
                    delete(Project).where(Project.id.in_(project_ids))
                )
            if pool_id is not None:
                await session.execute(
                    delete(MLBackendServicePool).where(
                        MLBackendServicePool.id == pool_id
                    )
                )
            if user_id is not None:
                await session.execute(delete(User).where(User.id == user_id))


async def test_project_query_rejects_null_and_malformed_inputs_with_22023(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    statements = [
        "SELECT * FROM aap_labeling_v1.workspace_project_query_counters(NULL::uuid[])",
        "SELECT * FROM aap_labeling_v1.workspace_project_query_ai_counts(NULL::uuid[])",
        "SELECT aap_labeling_v1.workspace_project_query_annotation_count(NULL::uuid[], false)",
        "SELECT aap_labeling_v1.workspace_project_query_annotation_count(ARRAY[]::uuid[], NULL::boolean)",
        "SELECT * FROM aap_labeling_v1.workspace_project_query_task_series(NULL::uuid[], array_fill(TIMESTAMPTZ '2025-01-01', ARRAY[12]))",
        "SELECT * FROM aap_labeling_v1.workspace_project_query_annotation_series(NULL::uuid[], array_fill(TIMESTAMPTZ '2025-01-01', ARRAY[12]))",
        "SELECT * FROM aap_assets_v1.workspace_project_query_dataset_ids(NULL::uuid)",
        "SELECT aap_assets_v1.workspace_project_query_dataset_item_count(NULL::uuid[])",
        "SELECT * FROM aap_assets_v1.workspace_project_query_dataset_item_ids(NULL::uuid[])",
        "SELECT aap_labeling_v1.workspace_project_query_linked_task_count(NULL::uuid, ARRAY[]::uuid[])",
        "SELECT aap_labeling_v1.workspace_project_query_task_count(NULL::uuid)",
        "SELECT * FROM aap_labeling_v1.workspace_project_query_batches(NULL::uuid)",
        "SELECT * FROM aap_labeling_v1.workspace_project_query_batch_task_counts(NULL::uuid)",
        "SELECT * FROM aap_jobs_v1.workspace_project_query_job_status_counts(NULL::uuid)",
        "SELECT * FROM aap_jobs_v1.workspace_project_query_latest_job_status(NULL::uuid)",
        "SELECT * FROM aap_inference_v1.workspace_project_query_pool(NULL::uuid)",
        "SELECT * FROM aap_labeling_v1.workspace_project_query_counters(ARRAY[NULL::uuid])",
        "SELECT * FROM aap_labeling_v1.workspace_project_query_counters(ARRAY[[ '00000000-0000-0000-0000-000000000001'::uuid, '00000000-0000-0000-0000-000000000002'::uuid ]])",
        "SELECT * FROM aap_labeling_v1.workspace_project_query_task_series(ARRAY[]::uuid[], array_fill(TIMESTAMPTZ '2025-01-01', ARRAY[11]))",
        "SELECT * FROM aap_labeling_v1.workspace_project_query_task_series(ARRAY[]::uuid[], array_fill(TIMESTAMPTZ '2025-01-01', ARRAY[3, 4]))",
        "SELECT * FROM aap_labeling_v1.workspace_project_query_task_series(ARRAY[]::uuid[], ARRAY[NULL::timestamptz] || array_fill(TIMESTAMPTZ '2025-01-01', ARRAY[11]))",
    ]
    await _invalid_calls(maker, statements)
