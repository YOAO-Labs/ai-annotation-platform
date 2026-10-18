from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import subprocess
import uuid

import pytest
from sqlalchemy import bindparam, delete, text, update
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.annotation import Annotation
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.db.models.asset_storage_deletion import AssetStorageDeletion
from tests.test_dataset_unlink_assets import _cleanup as _cleanup_base
from tests.test_dataset_unlink_assets import _seed as _seed_base

pytestmark = pytest.mark.asyncio

_UNLINK = text(
    "SELECT aap_workspace_v1.unlink_dataset("
    ":operation_id, :actor_id, :dataset_id, :project_id, :bucket, "
    "CAST(:audit_context AS jsonb))"
).bindparams(
    bindparam("operation_id", type_=PGUUID(as_uuid=True)),
    bindparam("actor_id", type_=PGUUID(as_uuid=True)),
    bindparam("dataset_id", type_=PGUUID(as_uuid=True)),
    bindparam("project_id", type_=PGUUID(as_uuid=True)),
)
_ASSOCIATE = text(
    "SELECT aap_assets_v1.associate_project("
    "CAST(:actor_id AS uuid), CAST(:dataset_id AS uuid), CAST(:project_id AS uuid))"
)
_RECEIPT_COUNT = text(
    "SELECT count(*) FROM public.dataset_unlink_operations WHERE operation_id=:id"
)
_AUDIT_COUNT = text(
    "SELECT count(*) FROM public.audit_logs WHERE action='dataset.unlink' "
    "AND detail_json ->> 'operation_id'=:id"
)
_DEFAULT_CONTEXT = object()


def _json(value: object) -> dict:
    result = json.loads(value) if isinstance(value, str) else value
    assert isinstance(result, dict)
    return result


def _creation_job(
    *,
    actor_id: uuid.UUID,
    dataset_id: uuid.UUID,
    project_id: uuid.UUID,
    association_id: uuid.UUID,
    status: str = "pending",
    kind: str = "create_tasks",
) -> AsyncJob:
    job_id = uuid.uuid4()
    job = AsyncJob(
        id=job_id,
        kind=kind,
        project_id=project_id,
        user_id=actor_id,
        status=status,
        progress_pct=37,
        payload={
            "schema": "aap-create-tasks/v1",
            "operation_id": str(job_id),
            "actor_id": str(actor_id),
            "dataset_id": str(dataset_id),
            "project_id": str(project_id),
            "association_id": str(association_id),
            "mode": "link",
        },
        result={"retained": str(job_id)},
    )
    if status in {"completed", "failed", "cancelled"}:
        job.completed_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    return job


async def _seed_complete_case(test_engine) -> dict[str, object]:
    fixture: dict[str, object] = await _seed_base(test_engine)
    suffix = uuid.uuid4().hex[:8].upper()
    fixture.update(
        {
            "operation_ids": [],
            "annotation_id": uuid.uuid4(),
            "batch_id": uuid.uuid4(),
            "feedback_id": None,
            "member_id": None,
            "extra_user_id": None,
        }
    )
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        batch = TaskBatch(
            id=fixture["batch_id"],
            project_id=fixture["project_id"],
            dataset_id=fixture["dataset_id"],
            display_id=f"DU-B-{suffix}",
            name="Dataset unlink batch",
            status="draft",
            assigned_user_ids=[],
            total_tasks=1,
            created_by=fixture["user_id"],
        )
        task = await session.get(Task, fixture["task_id"])
        project = await session.get(Project, fixture["project_id"])
        other_project = await session.get(Project, fixture["other_project_id"])
        assert task is not None and project is not None
        assert other_project is not None
        session.add(batch)
        await session.flush()
        task.batch_id = batch.id
        project.total_tasks = 1
        project.scene_mode = True
        other_project.scene_mode = True
        session.add_all(
            [
                Annotation(
                    id=fixture["annotation_id"],
                    task_id=fixture["task_id"],
                    project_id=fixture["project_id"],
                    user_id=fixture["user_id"],
                    class_name="car",
                    geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                ),
            ]
        )
        jobs = {
            "pending": _creation_job(
                actor_id=fixture["user_id"],
                dataset_id=fixture["dataset_id"],
                project_id=fixture["project_id"],
                association_id=fixture["association_id"],
                status="pending",
            ),
            "running": _creation_job(
                actor_id=fixture["user_id"],
                dataset_id=fixture["dataset_id"],
                project_id=fixture["project_id"],
                association_id=fixture["association_id"],
                status="running",
            ),
            "terminal": _creation_job(
                actor_id=fixture["user_id"],
                dataset_id=fixture["dataset_id"],
                project_id=fixture["project_id"],
                association_id=fixture["association_id"],
                status="completed",
            ),
            "other_generation": _creation_job(
                actor_id=fixture["user_id"],
                dataset_id=fixture["dataset_id"],
                project_id=fixture["project_id"],
                association_id=uuid.uuid4(),
                status="pending",
            ),
            "other_kind": _creation_job(
                actor_id=fixture["user_id"],
                dataset_id=fixture["dataset_id"],
                project_id=fixture["project_id"],
                association_id=fixture["association_id"],
                kind="dataset_import",
                status="pending",
            ),
        }
        session.add_all(jobs.values())
        fixture["jobs"] = {key: job.id for key, job in jobs.items()}
    return fixture


async def _add_feedback(test_engine, fixture: dict[str, object]) -> None:
    fixture["feedback_id"] = uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        session.add(
            AnnotationFeedback(
                id=fixture["feedback_id"],
                kind="comment",
                anchor_type="task",
                project_id=fixture["project_id"],
                task_id=fixture["task_id"],
                body="Retain authored feedback.",
                author_id=fixture["user_id"],
            )
        )


async def _cleanup_fixture(
    test_engine, fixture: dict[str, object], *, job_ids: list[uuid.UUID] | None = None
) -> None:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_ids = list(fixture.get("operation_ids", []))
    async with maker.begin() as session:
        database = await session.scalar(text("SELECT current_database()"))
        if not database or not database.endswith("_test"):
            pytest.fail("refusing fixture cleanup outside the disposable test database")
        if operation_ids:
            await session.execute(
                text(
                    "DELETE FROM public.dataset_unlink_operations "
                    "WHERE operation_id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [str(value) for value in operation_ids]},
            )
            await session.execute(
                delete(AssetStorageDeletion).where(
                    AssetStorageDeletion.operation_id.in_(operation_ids)
                )
            )
            await session.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
            await session.execute(
                text(
                    "DELETE FROM public.audit_logs WHERE action='dataset.unlink' "
                    "AND detail_json ->> 'operation_id' = ANY(CAST(:ids AS text[]))"
                ),
                {"ids": [str(value) for value in operation_ids]},
            )
        if fixture.get("feedback_id"):
            await session.execute(
                delete(AnnotationFeedback).where(
                    AnnotationFeedback.id == fixture["feedback_id"]
                )
            )
        await session.execute(
            delete(Annotation).where(Annotation.task_id == fixture["task_id"])
        )
        if fixture.get("batch_id"):
            await session.execute(
                delete(TaskBatch).where(TaskBatch.id == fixture["batch_id"])
            )
        owned_job_ids = list(fixture.get("jobs", {}).values())
        owned_job_ids.extend(job_ids or [])
        if owned_job_ids:
            await session.execute(
                delete(AsyncJob).where(AsyncJob.id.in_(owned_job_ids))
            )
        if fixture.get("member_id"):
            await session.execute(
                delete(ProjectMember).where(ProjectMember.id == fixture["member_id"])
            )
    await _cleanup_base(test_engine, fixture)
    if fixture.get("extra_user_id"):
        async with maker.begin() as session:
            await session.execute(
                delete(User).where(User.id == fixture["extra_user_id"])
            )


async def _unlink(
    session: AsyncSession,
    fixture: dict[str, object],
    operation_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    dataset_id: uuid.UUID | None = None,
    bucket: str = "media-cache-test",
    audit_context: dict[str, str] | None | object = _DEFAULT_CONTEXT,
) -> dict:
    context = (
        {
            "method": "DELETE",
            "path": f"/api/v1/datasets/{fixture['dataset_id']}/link/{fixture['project_id']}",
            "ip": "127.0.0.1",
            "request_id": str(uuid.uuid4()),
        }
        if audit_context is _DEFAULT_CONTEXT
        else audit_context
    )
    result = await session.scalar(
        _UNLINK,
        {
            "operation_id": operation_id,
            "actor_id": actor_id or fixture["user_id"],
            "dataset_id": dataset_id or fixture["dataset_id"],
            "project_id": project_id or fixture["project_id"],
            "bucket": bucket,
            "audit_context": json.dumps(context) if context is not None else None,
        },
    )
    return _json(result)


async def _count(session: AsyncSession, sql: str, **parameters) -> int:
    return await session.scalar(text(sql), parameters)


async def _assert_unlinked_graph(
    session: AsyncSession, fixture: dict[str, object]
) -> None:
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.tasks WHERE id=:id",
            id=fixture["task_id"],
        )
        == 0
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.tasks WHERE id=:id",
            id=fixture["other_task_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.annotations WHERE id=:id",
            id=fixture["annotation_id"],
        )
        == 0
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.project_datasets WHERE id=:id",
            id=fixture["association_id"],
        )
        == 0
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.project_datasets WHERE id=:id",
            id=fixture["other_association_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.datasets WHERE id=:id",
            id=fixture["dataset_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.dataset_items WHERE dataset_id=:id",
            id=fixture["dataset_id"],
        )
        == 3
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.image_pyramid_assets WHERE id=:id",
            id=fixture["item_asset_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.image_pyramid_generations WHERE asset_id=:id",
            id=fixture["item_asset_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.image_pyramid_assets WHERE id=:id",
            id=fixture["other_task_asset_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.image_pyramid_generations WHERE asset_id=:id",
            id=fixture["other_task_asset_id"],
        )
        == 1
    )


async def _assert_receipt_absent(
    session: AsyncSession, operation_id: uuid.UUID
) -> None:
    assert await session.scalar(_RECEIPT_COUNT, {"id": operation_id}) == 0
    assert await session.scalar(_AUDIT_COUNT, {"id": str(operation_id)}) == 0
    assert (
        await session.scalar(
            text(
                "SELECT count(*) FROM public.asset_storage_deletions WHERE operation_id=:id"
            ),
            {"id": operation_id},
        )
        == 0
    )


async def _assert_unmodified(
    session: AsyncSession, fixture: dict[str, object], operation_id: uuid.UUID
) -> None:
    project = await session.get(Project, fixture["project_id"], populate_existing=True)
    assert project is not None and project.total_tasks == 1
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.tasks WHERE id=:id",
            id=fixture["task_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.tasks WHERE id=:id",
            id=fixture["other_task_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.annotations WHERE id=:id",
            id=fixture["annotation_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.project_datasets WHERE id=:id",
            id=fixture["association_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.project_datasets WHERE id=:id",
            id=fixture["other_association_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.image_pyramid_assets WHERE id=:id",
            id=fixture["task_asset_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.image_pyramid_generations WHERE asset_id=:id",
            id=fixture["task_asset_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.task_batches WHERE id=:id",
            id=fixture["batch_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.datasets WHERE id=:id",
            id=fixture["dataset_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.image_pyramid_assets WHERE id=:id",
            id=fixture["item_asset_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.image_pyramid_assets WHERE id=:id",
            id=fixture["other_task_asset_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.image_pyramid_generations WHERE asset_id=:id",
            id=fixture["item_asset_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.image_pyramid_generations WHERE asset_id=:id",
            id=fixture["other_task_asset_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.bug_reports WHERE id=:id AND task_id=:task_id",
            id=fixture["report_target_id"],
            task_id=fixture["task_id"],
        )
        == 1
    )
    assert (
        await _count(
            session,
            "SELECT count(*) FROM public.bug_reports WHERE id=:id AND task_id=:task_id",
            id=fixture["report_unrelated_id"],
            task_id=fixture["other_task_id"],
        )
        == 1
    )
    await _assert_receipt_absent(session, operation_id)
    if fixture.get("feedback_id"):
        assert (
            await _count(
                session,
                "SELECT count(*) FROM public.annotation_feedbacks WHERE id=:id",
                id=fixture["feedback_id"],
            )
            == 1
        )
    for name, job_id in fixture.get("jobs", {}).items():
        job = await session.get(AsyncJob, job_id, populate_existing=True)
        assert job is not None
        assert job.status == (
            "completed"
            if name == "terminal"
            else "pending"
            if name in {"pending", "other_generation", "other_kind"}
            else "running"
        )
        assert job.project_id == fixture["project_id"]
        assert job.progress_pct == 37
        assert job.result == {"retained": str(job_id)}
        if name == "terminal":
            assert job.completed_at == datetime(2026, 1, 2, tzinfo=timezone.utc)
        else:
            assert job.completed_at is None


def _sqlstate(error: DBAPIError) -> str | None:
    pending = [error.orig]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        state = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if state:
            return state
        pending.extend(
            nested
            for nested in (
                getattr(current, "__cause__", None),
                getattr(current, "__context__", None),
            )
            if nested is not None
        )
    return None


def _role_url(test_engine, env_name: str, expected_user: str) -> URL:
    dsn = os.environ.get(env_name)
    if not dsn:
        pytest.fail("the disposable role verifier did not provide its database URL")
    url, target = make_url(dsn), test_engine.url
    if (
        url.drivername != "postgresql+asyncpg"
        or url.username != expected_user
        or url.host != "127.0.0.1"
        or url.port != target.port
        or url.database != target.database
        or not url.password
    ):
        pytest.fail("the role URL does not match the disposable test database")
    return url


async def _assert_permission_denied(
    connection, statement: str, parameters=None
) -> None:
    with pytest.raises(DBAPIError) as denied:
        await connection.execute(text(statement), parameters or {})
    assert _sqlstate(denied.value) == "42501"
    await connection.rollback()


def _native_psql_replay(
    append_url: URL,
    fixture: dict[str, object],
    operation_id: uuid.UUID,
) -> dict:
    from tests.test_task_creation_handoff import _inspect_owned_container

    container_id = _inspect_owned_container(append_url)
    database = append_url.database
    sql = """\
\\set ON_ERROR_STOP on
SELECT current_user || '|' || session_user || '|' || current_database();
BEGIN;
SELECT aap_workspace_v1.unlink_dataset(
    :'operation_id'::uuid,
    :'actor_id'::uuid,
    :'dataset_id'::uuid,
    :'project_id'::uuid,
    :'bucket'::text,
    NULL::jsonb
);
COMMIT;
"""
    child_environment = os.environ.copy()
    child_environment["PGPASSWORD"] = append_url.password
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "-e",
            "PGPASSWORD",
            container_id,
            "psql",
            "-X",
            "-q",
            "-A",
            "-t",
            "-h",
            "127.0.0.1",
            "-p",
            "5432",
            "-U",
            "append_client",
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            f"operation_id={operation_id}",
            "-v",
            f"actor_id={fixture['user_id']}",
            "-v",
            f"dataset_id={fixture['dataset_id']}",
            "-v",
            f"project_id={fixture['project_id']}",
            "-v",
            "bucket=media-cache-test",
            "-f",
            "-",
        ],
        input=sql,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
        env=child_environment,
    )
    if result.returncode != 0:
        pytest.fail("native psql dataset unlink replay failed")
    lines = result.stdout.splitlines()
    assert len(lines) == 2
    assert lines[0] == f"append_client|append_client|{database}"
    return _json(lines[1])


async def test_unlink_commits_exact_cleanup_projection_audit_and_receipt(test_engine):
    fixture = await _seed_complete_case(test_engine)
    operation_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        context = {
            "method": "DELETE",
            "path": f"/api/v1/datasets/{fixture['dataset_id']}/link/{fixture['project_id']}",
            "ip": "127.0.0.1",
            "request_id": str(uuid.uuid4()),
        }
        async with maker.begin() as caller:
            reply = await _unlink(caller, fixture, operation_id, audit_context=context)
        expected_data = {
            "deleted_tasks": 1,
            "deleted_annotations": 1,
            "deleted_batches": 1,
            "deleted_batch_ids": [str(fixture["batch_id"])],
            "soft": False,
            "operation_id": str(operation_id),
            "actor_id": str(fixture["user_id"]),
            "dataset_id": str(fixture["dataset_id"]),
            "project_id": str(fixture["project_id"]),
            "association_id": str(fixture["association_id"]),
            "cleanup_bucket": "media-cache-test",
            "audit_id": reply["data"]["audit_id"],
        }
        assert reply == {
            "schema": "aap-dataset-unlink/v1",
            "ok": True,
            "data": expected_data,
        }
        assert reply["data"]["audit_id"] > 0

        async with maker() as verify:
            await _assert_unlinked_graph(verify, fixture)
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.image_pyramid_assets WHERE id=:id",
                    id=fixture["task_asset_id"],
                )
                == 0
            )
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.image_pyramid_generations WHERE asset_id=:id",
                    id=fixture["task_asset_id"],
                )
                == 0
            )
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.bug_reports WHERE id=:id "
                    "AND task_id IS NOT NULL",
                    id=fixture["report_target_id"],
                )
                == 0
            )
            assert (
                await verify.scalar(
                    text("SELECT task_id FROM public.bug_reports WHERE id=:id"),
                    {"id": fixture["report_unrelated_id"]},
                )
                == fixture["other_task_id"]
            )
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.bug_reports WHERE reporter_id=:id",
                    id=fixture["user_id"],
                )
                == 2
            )
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.bug_comments WHERE bug_report_id=:id",
                    id=fixture["report_target_id"],
                )
                == 1
            )
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.task_batches WHERE id=:id",
                    id=fixture["batch_id"],
                )
                == 0
            )
            project = await verify.get(Project, fixture["project_id"])
            assert project is not None and project.total_tasks == 0
            assert project.completed_tasks == 0 and project.review_tasks == 0
            assert project.in_progress_tasks == 0
            assert (
                await _count(
                    verify,
                    "SELECT count(*) FROM public.dataset_unlink_operations WHERE operation_id=:id",
                    id=operation_id,
                )
                == 1
            )
            receipt = (
                (
                    await verify.execute(
                        text(
                            "SELECT actor_id, dataset_id, project_id, request, result "
                            "FROM public.dataset_unlink_operations WHERE operation_id=:id"
                        ),
                        {"id": operation_id},
                    )
                )
                .mappings()
                .one()
            )
            assert (
                receipt["actor_id"],
                receipt["dataset_id"],
                receipt["project_id"],
            ) == (fixture["user_id"], fixture["dataset_id"], fixture["project_id"])
            assert receipt["request"] == {
                "operation_id": str(operation_id),
                "actor_id": str(fixture["user_id"]),
                "dataset_id": str(fixture["dataset_id"]),
                "project_id": str(fixture["project_id"]),
                "cleanup_bucket": "media-cache-test",
            }
            assert receipt["result"] == reply
            audit = (
                (
                    await verify.execute(
                        text(
                            "SELECT id, actor_id, actor_email, actor_role, action, target_type, "
                            "target_id, method, path, status_code, host(ip) AS ip, request_id, detail_json "
                            "FROM public.audit_logs WHERE action='dataset.unlink' "
                            "AND detail_json ->> 'operation_id'=:id"
                        ),
                        {"id": str(operation_id)},
                    )
                )
                .mappings()
                .one()
            )
            assert audit["id"] == reply["data"]["audit_id"]
            assert audit["actor_id"] == fixture["user_id"]
            assert audit["actor_role"] == "super_admin"
            assert (audit["action"], audit["target_type"], audit["target_id"]) == (
                "dataset.unlink",
                "dataset",
                str(fixture["dataset_id"]),
            )
            assert (
                audit["method"],
                audit["path"],
                audit["status_code"],
                audit["ip"],
            ) == ("DELETE", context["path"], 200, "127.0.0.1")
            assert audit["detail_json"] == {
                "operation_id": str(operation_id),
                "project_id": str(fixture["project_id"]),
                "deleted_tasks": 1,
                "deleted_annotations": 1,
                "deleted_batches": 1,
                "deleted_batch_ids": [str(fixture["batch_id"])],
                "soft": False,
            }
            outbox = (
                (
                    await verify.execute(
                        text(
                            "SELECT operation_id, asset_id, bucket, prefix, completed_at "
                            "FROM public.asset_storage_deletions WHERE operation_id=:id"
                        ),
                        {"id": operation_id},
                    )
                )
                .mappings()
                .one()
            )
            assert outbox["asset_id"] == fixture["task_asset_id"]
            assert outbox["bucket"] == "media-cache-test"
            assert outbox["prefix"] == f"image-pyramids/{fixture['task_asset_id']}/"
            assert outbox["completed_at"] is None
            expected_status = {
                "pending": "cancelled",
                "running": "cancelled",
                "terminal": "completed",
                "other_generation": "pending",
                "other_kind": "pending",
            }
            for name, job_id in fixture["jobs"].items():
                job = await verify.get(AsyncJob, job_id, populate_existing=True)
                assert job is not None
                assert job.status == expected_status[name]
                assert job.project_id == fixture["project_id"]
                assert job.progress_pct == 37
                assert job.result == {"retained": str(job_id)}
                if name in {"pending", "running"}:
                    assert job.completed_at is not None
                elif name == "terminal":
                    assert job.completed_at == datetime(2026, 1, 2, tzinfo=timezone.utc)

        async with maker.begin() as relink:
            association = _json(
                await relink.scalar(
                    _ASSOCIATE,
                    {
                        "actor_id": fixture["user_id"],
                        "dataset_id": fixture["dataset_id"],
                        "project_id": fixture["project_id"],
                    },
                )
            )
            assert (
                association["ok"] is True and association["data"]["existing"] is False
            )
            new_association_id = uuid.UUID(association["data"]["link_id"])

        async with maker() as replay:
            assert (
                await _unlink(
                    replay,
                    fixture,
                    operation_id,
                    audit_context=None,
                )
                == reply
            )
            assert await _unlink(
                replay,
                fixture,
                operation_id,
                bucket="media-cache-other",
            ) == {
                "schema": "aap-dataset-unlink/v1",
                "ok": False,
                "error": {"code": "operation_conflict"},
            }
            assert (
                await _count(
                    replay,
                    "SELECT count(*) FROM public.project_datasets WHERE id=:id",
                    id=new_association_id,
                )
                == 1
            )
            assert (
                await _count(
                    replay,
                    "SELECT count(*) FROM public.tasks WHERE project_id=:id",
                    id=fixture["project_id"],
                )
                == 0
            )
            assert await replay.scalar(_AUDIT_COUNT, {"id": str(operation_id)}) == 1
            assert (
                await replay.scalar(
                    text(
                        "SELECT count(*) FROM public.asset_storage_deletions WHERE operation_id=:id"
                    ),
                    {"id": operation_id},
                )
                == 1
            )
    finally:
        await _cleanup_fixture(test_engine, fixture)


@pytest.mark.parametrize("end_transaction", ["rollback", "failed_commit"])
async def test_unlink_changes_audit_receipt_and_outbox_follow_outer_transaction(
    test_engine, end_transaction: str
):
    fixture = await _seed_complete_case(test_engine)
    operation_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as caller:
            await caller.begin()
            reply = await _unlink(caller, fixture, operation_id)
            assert reply["ok"] is True
            assert (
                await _count(
                    caller,
                    "SELECT count(*) FROM public.tasks WHERE id=:id",
                    id=fixture["task_id"],
                )
                == 0
            )
            assert await caller.scalar(_RECEIPT_COUNT, {"id": operation_id}) == 1
            assert await caller.scalar(_AUDIT_COUNT, {"id": str(operation_id)}) == 1
            assert (
                await _count(
                    caller,
                    "SELECT count(*) FROM public.asset_storage_deletions WHERE operation_id=:id",
                    id=operation_id,
                )
                == 1
            )
            if end_transaction == "rollback":
                await caller.rollback()
            else:
                await caller.execute(
                    text(
                        "CREATE TEMP TABLE dataset_unlink_commit_probe ("
                        "value integer UNIQUE DEFERRABLE INITIALLY DEFERRED) ON COMMIT DROP"
                    )
                )
                await caller.execute(
                    text(
                        "INSERT INTO pg_temp.dataset_unlink_commit_probe VALUES (1), (1)"
                    )
                )
                with pytest.raises(DBAPIError):
                    await caller.commit()
                await caller.rollback()
        async with maker() as verify:
            await _assert_unmodified(verify, fixture, operation_id)
    finally:
        await _cleanup_fixture(test_engine, fixture)


async def test_authored_task_feedback_blocks_unlink_before_cleanup(test_engine):
    fixture = await _seed_complete_case(test_engine)
    await _add_feedback(test_engine, fixture)
    operation_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as caller:
            reply = await _unlink(caller, fixture, operation_id)
            assert reply == {
                "schema": "aap-dataset-unlink/v1",
                "ok": False,
                "error": {"code": "task_feedback_retained"},
            }
            await caller.commit()
        async with maker() as verify:
            await _assert_unmodified(verify, fixture, operation_id)
    finally:
        await _cleanup_fixture(test_engine, fixture)


async def test_active_learning_reference_blocks_unlink_before_cleanup(
    db_session, super_admin, monkeypatch
):
    from tests.active_learning_fixtures import create as create_active_learning_round
    from tests.active_learning_fixtures import seed as seed_active_learning

    owner, _ = super_admin
    world = await seed_active_learning(db_session, owner, monkeypatch)
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"ALU-{uuid.uuid4().hex[:10].upper()}",
        name="Dataset retained by active learning",
        data_type="image",
        created_by=owner.id,
        file_count=len(world.tasks),
    )
    association = ProjectDataset(
        id=uuid.uuid4(),
        project_id=world.project.id,
        dataset_id=dataset.id,
    )
    db_session.add(dataset)
    await db_session.flush()
    items = [
        DatasetItem(
            id=uuid.uuid4(),
            dataset_id=dataset.id,
            file_name=task.file_name,
            file_path=task.file_path,
            file_type=task.file_type,
        )
        for task in world.tasks
    ]
    db_session.add_all([*items, association])
    await db_session.flush()
    for task, item in zip(world.tasks, items, strict=True):
        task.dataset_item_id = item.id
    await db_session.flush()
    await create_active_learning_round(db_session, owner, world)
    fixture = {
        "user_id": owner.id,
        "dataset_id": dataset.id,
        "project_id": world.project.id,
    }
    operation_id = uuid.uuid4()

    reply = await _unlink(db_session, fixture, operation_id)
    assert reply == {
        "schema": "aap-dataset-unlink/v1",
        "ok": False,
        "error": {"code": "active_learning_reference"},
    }
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.project_datasets WHERE id=:id"),
            {"id": association.id},
        )
        == 1
    )
    assert await db_session.scalar(_RECEIPT_COUNT, {"id": operation_id}) == 0
    assert await db_session.scalar(_AUDIT_COUNT, {"id": str(operation_id)}) == 0
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.asset_storage_deletions WHERE operation_id=:id"
            ),
            {"id": operation_id},
        )
        == 0
    )


@pytest.mark.parametrize(
    ("scenario", "actor_role", "membership_role", "expected"),
    [
        ("former_owner", "project_admin", None, "project_missing"),
        ("nonmanager_member", "employee", "annotator", "project_manager_required"),
    ],
)
async def test_unlink_rechecks_current_platform_role_and_project_manager(
    test_engine, scenario, actor_role, membership_role, expected
):
    fixture = await _seed_complete_case(test_engine)
    operation_id, successor_id = uuid.uuid4(), uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    fixture["extra_user_id"] = successor_id
    member_id = uuid.uuid4() if membership_role is not None else None
    fixture["member_id"] = member_id
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as owner:
            actor = await owner.get(User, fixture["user_id"])
            assert actor is not None
            actor.role = actor_role
            owner.add(
                User(
                    id=successor_id,
                    email=f"unlink-successor-{uuid.uuid4().hex}@example.test",
                    name="Successor owner",
                    password_hash="test-only",
                    role="project_admin",
                    is_active=True,
                )
            )
            await owner.flush()
            await owner.execute(
                update(Project)
                .where(Project.id == fixture["project_id"])
                .values(owner_id=successor_id)
            )
            if membership_role is not None:
                owner.add(
                    ProjectMember(
                        id=member_id,
                        project_id=fixture["project_id"],
                        user_id=fixture["user_id"],
                        role=membership_role,
                        assigned_by=successor_id,
                    )
                )
        async with maker() as caller:
            reply = await _unlink(caller, fixture, operation_id)
            assert reply == {
                "schema": "aap-dataset-unlink/v1",
                "ok": False,
                "error": {"code": expected},
            }
            await caller.commit()
        async with maker() as verify:
            await _assert_unmodified(verify, fixture, operation_id)
            assert (
                await verify.scalar(
                    text("SELECT owner_id FROM public.projects WHERE id=:id"),
                    {"id": fixture["project_id"]},
                )
                == successor_id
            )
    finally:
        await _cleanup_fixture(test_engine, fixture)


async def test_inactive_actor_cannot_unlink(test_engine):
    fixture = await _seed_complete_case(test_engine)
    operation_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as owner:
            await owner.execute(
                update(User)
                .where(User.id == fixture["user_id"])
                .values(is_active=False)
            )
        async with maker() as caller:
            reply = await _unlink(caller, fixture, operation_id)
            assert reply == {
                "schema": "aap-dataset-unlink/v1",
                "ok": False,
                "error": {"code": "account_not_active"},
            }
            await caller.commit()
        async with maker() as verify:
            await _assert_unmodified(verify, fixture, operation_id)
    finally:
        await _cleanup_fixture(test_engine, fixture)


async def test_audit_lock_contention_rolls_back_unlink_owner_changes(test_engine):
    fixture = await _seed_complete_case(test_engine)
    operation_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    locker, caller = maker(), maker()
    try:
        await locker.begin()
        await locker.execute(text("LOCK TABLE public.audit_logs IN SHARE MODE"))
        await caller.begin()
        await caller.execute(text("SET LOCAL lock_timeout='250ms'"))
        reply = await _unlink(caller, fixture, operation_id)
        assert reply == {
            "schema": "aap-dataset-unlink/v1",
            "ok": False,
            "error": {"code": "project_busy"},
        }
        await _assert_unmodified(caller, fixture, operation_id)
        await caller.rollback()
    finally:
        await caller.close()
        await locker.rollback()
        await locker.close()
        await _cleanup_fixture(test_engine, fixture)


@pytest.mark.parametrize(
    "locked_table", ["image_pyramid_assets", "image_pyramid_generations"]
)
async def test_pyramid_contention_returns_busy_before_cascade_and_retries(
    test_engine, locked_table
):
    fixture = await _seed_complete_case(test_engine)
    operation_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    locker, caller = maker(), maker()
    key = "id" if locked_table == "image_pyramid_assets" else "asset_id"
    try:
        await locker.execute(
            text(f"SELECT {key} FROM public.{locked_table} WHERE {key}=:id FOR UPDATE"),
            {"id": fixture["task_asset_id"]},
        )
        reply = await asyncio.wait_for(
            _unlink(caller, fixture, operation_id), timeout=3
        )
        assert reply == {
            "schema": "aap-dataset-unlink/v1",
            "ok": False,
            "error": {"code": "project_busy"},
        }
        await _assert_unmodified(caller, fixture, operation_id)
        await caller.rollback()
        await locker.rollback()
        reply = await _unlink(caller, fixture, operation_id)
        assert reply["ok"] is True
        await caller.commit()
        await _assert_unlinked_graph(caller, fixture)
    finally:
        await caller.close()
        await locker.close()
        await _cleanup_fixture(test_engine, fixture)


async def test_workflow_and_cleanup_logins_use_only_their_unlink_capabilities(
    test_engine,
):
    if (
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or not os.environ.get("AAP_MODULE_DATA_APPEND_URL")
        or not os.environ.get("AAP_MODULE_DATA_ASSET_CLEANUP_URL")
    ):
        pytest.skip("requires the disposable module-data role verifier")
    fixture = await _seed_complete_case(test_engine)
    operation_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    append_url = _role_url(test_engine, "AAP_MODULE_DATA_APPEND_URL", "append_client")
    cleanup_url = _role_url(
        test_engine, "AAP_MODULE_DATA_ASSET_CLEANUP_URL", "asset_cleanup_client"
    )
    workflow = create_async_engine(
        append_url,
        hide_parameters=True,
        connect_args={"timeout": 5, "command_timeout": 10},
    )
    cleanup = create_async_engine(
        cleanup_url,
        hide_parameters=True,
        connect_args={"timeout": 5, "command_timeout": 10},
    )
    try:
        async with workflow.begin() as caller:
            assert await caller.scalar(text("SELECT current_user")) == "append_client"
            assert await caller.scalar(text("SELECT session_user")) == "append_client"
            reply = await _unlink(caller, fixture, operation_id)
            assert reply["ok"] is True

        async with workflow.connect() as caller:
            await _assert_permission_denied(
                caller,
                "DELETE FROM public.tasks WHERE id=:id",
                {"id": fixture["other_task_id"]},
            )
            await _assert_permission_denied(
                caller,
                "DELETE FROM public.dataset_unlink_operations WHERE operation_id=:id",
                {"id": operation_id},
            )
            await _assert_permission_denied(
                caller,
                "SELECT aap_assets_v1.prepare_dataset_unlink_cleanup("
                "CAST(:operation_id AS uuid), CAST(:dataset_id AS uuid), "
                "CAST(:project_id AS uuid), CAST(:bucket AS text))",
                {
                    "operation_id": operation_id,
                    "dataset_id": fixture["dataset_id"],
                    "project_id": fixture["project_id"],
                    "bucket": "media-cache-test",
                },
            )
            await _assert_permission_denied(
                caller,
                "SELECT aap_labeling_v1.delete_dataset_tasks("
                "CAST(:dataset_id AS uuid), CAST(:project_id AS uuid))",
                {
                    "dataset_id": fixture["dataset_id"],
                    "project_id": fixture["project_id"],
                },
            )

        native_reply = _native_psql_replay(append_url, fixture, operation_id)
        assert native_reply == reply

        async with cleanup.connect() as worker:
            assert (
                await worker.scalar(text("SELECT current_user"))
                == "asset_cleanup_client"
            )
            assert (
                await worker.scalar(text("SELECT session_user"))
                == "asset_cleanup_client"
            )
            pending = (
                (
                    await worker.execute(
                        text(
                            "SELECT * FROM aap_assets_v1.pending_storage_deletions_v2("
                            "'media-cache-test',NULL::timestamptz,NULL::uuid,NULL::uuid,20)"
                        ),
                    )
                )
                .mappings()
                .all()
            )
            assert len(pending) == 1 and pending[0]["created_at"].tzinfo is not None
            assert [
                {key: value for key, value in row.items() if key != "created_at"}
                for row in pending
            ] == [
                {
                    "operation_id": operation_id,
                    "asset_id": fixture["task_asset_id"],
                    "bucket": "media-cache-test",
                    "prefix": f"image-pyramids/{fixture['task_asset_id']}/",
                }
            ]
            assert (
                await worker.scalar(
                    text(
                        "SELECT aap_assets_v1.complete_storage_deletion("
                        "CAST(:operation_id AS uuid), CAST(:asset_id AS uuid))"
                    ),
                    {
                        "operation_id": operation_id,
                        "asset_id": fixture["task_asset_id"],
                    },
                )
                is True
            )
            assert (
                await worker.execute(
                    text(
                        "SELECT * FROM aap_assets_v1.pending_storage_deletions_v2("
                        "'media-cache-test',NULL::timestamptz,NULL::uuid,NULL::uuid,20)"
                    ),
                )
            ).all() == []
            await worker.commit()
            await _assert_permission_denied(
                worker,
                "SELECT count(*) FROM public.asset_storage_deletions",
            )
            await _assert_permission_denied(
                worker,
                "SELECT aap_assets_v1.prepare_dataset_unlink_cleanup("
                "CAST(:operation_id AS uuid), CAST(:dataset_id AS uuid), "
                "CAST(:project_id AS uuid), CAST(:bucket AS text))",
                {
                    "operation_id": operation_id,
                    "dataset_id": fixture["dataset_id"],
                    "project_id": fixture["project_id"],
                    "bucket": "media-cache-test",
                },
            )

        async with async_sessionmaker(test_engine, class_=AsyncSession)() as verify:
            assert await verify.scalar(_RECEIPT_COUNT, {"id": operation_id}) == 1
            assert await verify.scalar(_AUDIT_COUNT, {"id": str(operation_id)}) == 1
            assert (
                await verify.scalar(
                    text(
                        "SELECT count(*) FROM public.asset_storage_deletions "
                        "WHERE operation_id=:id AND completed_at IS NOT NULL"
                    ),
                    {"id": operation_id},
                )
                == 1
            )
    finally:
        await workflow.dispose()
        await cleanup.dispose()
        await _cleanup_fixture(test_engine, fixture)
