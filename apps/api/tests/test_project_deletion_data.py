from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import bindparam, delete, or_, text, update
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.annotation import Annotation
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.async_job import AsyncJob
from app.db.models.bug_report import BugReport
from app.db.models.dataset import ProjectDataset
from app.db.models.project import Project
from app.db.models.project_template import ProjectTemplate
from app.db.models.task import Task
from app.db.models.trained_model import ModelRelease, ProjectModelSelection
from app.db.models.user import User
from tests.test_project_owned_cleanup_contract import _cleanup as _cleanup_base
from tests.test_project_owned_cleanup_contract import _seed as _seed_base

pytestmark = pytest.mark.asyncio

_DELETE_PROJECT = text(
    "SELECT aap_workspace_v1.delete_project("
    ":operation_id, :actor_id, :project_id, CAST(:audit_context AS jsonb))"
).bindparams(
    bindparam("operation_id", type_=PGUUID(as_uuid=True)),
    bindparam("actor_id", type_=PGUUID(as_uuid=True)),
    bindparam("project_id", type_=PGUUID(as_uuid=True)),
)

_TARGET_COUNTS = text(
    """
    SELECT
      (SELECT count(*) FROM public.projects WHERE id=:project_id) AS projects,
      (SELECT count(*) FROM public.tasks WHERE project_id=:project_id) AS tasks,
      (SELECT count(*) FROM public.annotations WHERE project_id=:project_id) AS annotations,
      (SELECT count(*) FROM public.annotation_feedbacks WHERE project_id=:project_id) AS feedback,
      (SELECT count(*) FROM public.project_datasets WHERE project_id=:project_id) AS associations,
      (SELECT count(*) FROM public.storage_connections WHERE project_id=:project_id) AS storage,
      (SELECT count(*) FROM public.project_ml_backend_pool WHERE project_id=:project_id) AS backends,
      (SELECT count(*) FROM public.project_pipelines WHERE project_id=:project_id) AS pipelines,
      (SELECT count(*) FROM public.project_task_views WHERE project_id=:project_id) AS views,
      (SELECT count(*) FROM public.export_artifacts WHERE project_id=:project_id) AS exports,
      (SELECT count(*) FROM public.image_pyramid_assets WHERE task_id=:task_id) AS task_pyramids,
      (SELECT count(*) FROM public.async_jobs WHERE project_id=:project_id) AS jobs
    """
)


async def _seed_graph(test_engine, *, other_project: bool = True) -> dict[str, object]:
    fixture: dict[str, object] = await _seed_base(test_engine)
    fixture["operation_ids"] = []
    if not other_project:
        return fixture

    suffix = uuid.uuid4().hex[:10].upper()
    fixture.update(
        {
            "annotation_id": uuid.uuid4(),
            "feedback_id": uuid.uuid4(),
            "other_project_id": uuid.uuid4(),
            "other_task_id": uuid.uuid4(),
            "other_annotation_id": uuid.uuid4(),
            "other_feedback_id": uuid.uuid4(),
            "other_template_id": uuid.uuid4(),
            "target_template_id": uuid.uuid4(),
        }
    )
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
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
                Project(
                    id=fixture["other_project_id"],
                    display_id=f"PD-{suffix}",
                    name="Unrelated project",
                    type_label="Image",
                    type_key="image",
                    data_type="image",
                    owner_id=fixture["user_id"],
                    total_tasks=1,
                ),
                ProjectTemplate(
                    id=fixture["target_template_id"],
                    display_id=f"PT-{suffix}",
                    name="Template sourced from target",
                    type_label="Image",
                    type_key="image",
                    created_by=fixture["user_id"],
                    source_project_id=fixture["project_id"],
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Task(
                    id=fixture["other_task_id"],
                    project_id=fixture["other_project_id"],
                    dataset_item_id=fixture["item_id"],
                    display_id=f"TD-{suffix}",
                    file_name="source.jpg",
                    file_path=f"other/{suffix}/source.jpg",
                    file_type="image",
                ),
                ProjectDataset(
                    id=uuid.uuid4(),
                    project_id=fixture["other_project_id"],
                    dataset_id=fixture["dataset_id"],
                ),
                Annotation(
                    id=fixture["other_annotation_id"],
                    task_id=fixture["other_task_id"],
                    project_id=fixture["other_project_id"],
                    user_id=fixture["user_id"],
                    class_name="car",
                    geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                ),
                AnnotationFeedback(
                    id=fixture["feedback_id"],
                    kind="issue",
                    anchor_type="task",
                    project_id=fixture["project_id"],
                    task_id=fixture["task_id"],
                    annotation_id=fixture["annotation_id"],
                    body="Target feedback should be removed.",
                    author_id=fixture["user_id"],
                ),
                AnnotationFeedback(
                    id=fixture["other_feedback_id"],
                    kind="issue",
                    anchor_type="task",
                    project_id=fixture["other_project_id"],
                    task_id=fixture["other_task_id"],
                    annotation_id=fixture["other_annotation_id"],
                    body="Unrelated feedback should remain.",
                    author_id=fixture["user_id"],
                ),
                ProjectTemplate(
                    id=fixture["other_template_id"],
                    display_id=f"PTO-{suffix}",
                    name="Template sourced from unrelated project",
                    type_label="Image",
                    type_key="image",
                    created_by=fixture["user_id"],
                    source_project_id=fixture["other_project_id"],
                ),
            ]
        )
        await session.commit()
    return fixture


async def _cleanup_fixture(test_engine, fixture: dict[str, object]) -> None:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    project_ids = [fixture["project_id"]]
    task_ids = [fixture["task_id"]]
    if fixture.get("other_project_id"):
        project_ids.append(fixture["other_project_id"])
        task_ids.append(fixture["other_task_id"])
    async with maker.begin() as session:
        operation_ids = fixture.get("operation_ids", [])
        if operation_ids:
            await session.execute(
                text(
                    "DELETE FROM public.project_deletion_operations "
                    "WHERE operation_id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [str(value) for value in operation_ids]},
            )
            await session.execute(
                text(
                    "DELETE FROM public.audit_logs WHERE action='project.delete' "
                    "AND detail_json ->> 'operation_id' = ANY(CAST(:ids AS text[]))"
                ),
                {"ids": [str(value) for value in operation_ids]},
            )
        await session.execute(
            delete(ProjectModelSelection).where(
                ProjectModelSelection.project_id.in_(project_ids)
            )
        )
        await session.execute(
            delete(ModelRelease).where(ModelRelease.project_id.in_(project_ids))
        )
        await session.execute(
            delete(ProjectTemplate).where(
                ProjectTemplate.id.in_(
                    [
                        fixture[key]
                        for key in ("target_template_id", "other_template_id")
                        if key in fixture
                    ]
                )
            )
        )
        await session.execute(
            delete(AnnotationFeedback).where(
                or_(
                    AnnotationFeedback.author_id == fixture["user_id"],
                    AnnotationFeedback.task_id.in_(task_ids),
                )
            )
        )
        await session.execute(
            delete(Annotation).where(
                or_(
                    Annotation.user_id == fixture["user_id"],
                    Annotation.task_id.in_(task_ids),
                )
            )
        )
        await session.execute(
            delete(BugReport).where(BugReport.reporter_id == fixture["user_id"])
        )
        await session.execute(
            delete(AsyncJob).where(AsyncJob.user_id == fixture["user_id"])
        )
        if fixture.get("other_project_id"):
            await session.execute(
                delete(Task).where(Task.project_id == fixture["other_project_id"])
            )
            await session.execute(
                delete(Project).where(Project.id == fixture["other_project_id"])
            )
    await _cleanup_base(test_engine, fixture)
    successor_id = fixture.get("successor_id")
    if successor_id:
        async with maker.begin() as session:
            await session.execute(delete(User).where(User.id == successor_id))


async def _delete(
    session: AsyncSession,
    fixture: dict[str, object],
    operation_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    context: dict[str, str] | None = None,
) -> dict:
    audit_context = context or {
        "method": "DELETE",
        "path": f"/api/v1/projects/{fixture['project_id']}",
        "ip": "127.0.0.1",
        "request_id": str(uuid.uuid4()),
    }
    result = await session.scalar(
        _DELETE_PROJECT,
        {
            "operation_id": operation_id,
            "actor_id": actor_id or fixture["user_id"],
            "project_id": project_id or fixture["project_id"],
            "audit_context": json.dumps(audit_context),
        },
    )
    return json.loads(result) if isinstance(result, str) else result


async def _counts(session, fixture: dict[str, object]) -> dict[str, int]:
    row = (
        (
            await session.execute(
                _TARGET_COUNTS,
                {"project_id": fixture["project_id"], "task_id": fixture["task_id"]},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def _assert_graph_unchanged(session, fixture: dict[str, object]) -> None:
    assert await _counts(session, fixture) == {
        "projects": 1,
        "tasks": 1,
        "annotations": int("annotation_id" in fixture),
        "feedback": int("feedback_id" in fixture),
        "associations": 1,
        "storage": 1,
        "backends": 1,
        "pipelines": 1,
        "views": 1,
        "exports": 1,
        "task_pyramids": 1,
        "jobs": 4,
    }


async def _assert_no_receipt_or_audit(session, operation_id: uuid.UUID) -> None:
    assert (
        await session.scalar(
            text(
                "SELECT count(*) FROM public.project_deletion_operations WHERE operation_id=:id"
            ),
            {"id": operation_id},
        )
        == 0
    )
    assert (
        await session.scalar(
            text(
                "SELECT count(*) FROM public.audit_logs WHERE action='project.delete' "
                "AND detail_json ->> 'operation_id'=:id"
            ),
            {"id": str(operation_id)},
        )
        == 0
    )


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


async def test_delete_project_cleans_owner_data_audits_and_replays_frozen_receipt(
    test_engine,
):
    fixture = await _seed_graph(test_engine)
    operation_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            reply = await _delete(session, fixture, operation_id)
            assert set(reply) == {"schema", "ok", "data"}
            assert reply["schema"] == "aap-project-delete/v1" and reply["ok"] is True
            assert set(reply["data"]) == {
                "operation_id",
                "actor_id",
                "project_id",
                "deleted_tasks",
                "deleted_annotations",
                "audit_id",
            }
            assert reply["data"]["operation_id"] == str(operation_id)
            assert reply["data"]["actor_id"] == str(fixture["user_id"])
            assert reply["data"]["project_id"] == str(fixture["project_id"])
            assert reply["data"]["deleted_tasks"] == 1
            assert reply["data"]["deleted_annotations"] == 1
            assert reply["data"]["audit_id"] > 0
            await session.commit()

        async with maker() as verify:
            assert await _counts(verify, fixture) == {
                "projects": 0,
                "tasks": 0,
                "annotations": 0,
                "feedback": 0,
                "associations": 0,
                "storage": 0,
                "backends": 0,
                "pipelines": 0,
                "views": 0,
                "exports": 0,
                "task_pyramids": 0,
                "jobs": 0,
            }
            assert (
                await verify.scalar(
                    text("SELECT count(*) FROM public.datasets WHERE id=:id"),
                    {"id": fixture["dataset_id"]},
                )
                == 1
            )
            assert (
                await verify.scalar(
                    text("SELECT count(*) FROM public.dataset_items WHERE id=:id"),
                    {"id": fixture["item_id"]},
                )
                == 1
            )
            assert (
                await verify.scalar(
                    text(
                        "SELECT count(*) FROM public.image_pyramid_assets WHERE dataset_item_id=:id"
                    ),
                    {"id": fixture["item_id"]},
                )
                == 1
            )
            reports = (
                await verify.execute(
                    text(
                        "SELECT project_id, task_id FROM public.bug_reports WHERE reporter_id=:id"
                    ),
                    {"id": fixture["user_id"]},
                )
            ).all()
            assert len(reports) == 2 and all(
                p is None and t is None for p, t in reports
            )
            other = (
                await verify.execute(
                    text(
                        "SELECT project.id, task.id, association.id, annotation.id, feedback.id "
                        "FROM public.projects AS project "
                        "JOIN public.tasks AS task ON task.project_id=project.id "
                        "JOIN public.project_datasets AS association ON association.project_id=project.id "
                        "JOIN public.annotations AS annotation ON annotation.project_id=project.id "
                        "JOIN public.annotation_feedbacks AS feedback ON feedback.project_id=project.id "
                        "WHERE project.id=:id"
                    ),
                    {"id": fixture["other_project_id"]},
                )
            ).one()
            assert other[0] == fixture["other_project_id"]
            assert other[1] == fixture["other_task_id"]
            assert other[3] == fixture["other_annotation_id"]
            assert other[4] == fixture["other_feedback_id"]
            template_source = await verify.scalar(
                text(
                    "SELECT source_project_id FROM public.project_templates WHERE id=:id"
                ),
                {"id": fixture["target_template_id"]},
            )
            assert template_source is None
            assert (
                await verify.scalar(
                    text(
                        "SELECT source_project_id FROM public.project_templates WHERE id=:id"
                    ),
                    {"id": fixture["other_template_id"]},
                )
                == fixture["other_project_id"]
            )
            statuses = (
                await verify.execute(
                    text(
                        "SELECT kind, status, project_id, result, completed_at "
                        "FROM public.async_jobs WHERE user_id=:id"
                    ),
                    {"id": fixture["user_id"]},
                )
            ).all()
            creation_jobs = [row for row in statuses if row.kind == "create_tasks"]
            assert sorted(row.status for row in creation_jobs) == [
                "cancelled",
                "cancelled",
                "completed",
            ]
            assert all(row.project_id is None for row in creation_jobs)
            assert {
                row.result["retained"]
                for row in creation_jobs
                if row.status == "cancelled"
            } == {"pending result", "running result"}
            completed_job = next(
                row for row in creation_jobs if row.status == "completed"
            )
            assert completed_job.result == {"retained": "completed result"}
            assert completed_job.completed_at is not None
            import_job = next(row for row in statuses if row.kind == "dataset_import")
            assert import_job.status == "running" and import_job.project_id is None
            assert import_job.result == {"retained": "other result"}
            receipt = (
                (
                    await verify.execute(
                        text(
                            "SELECT actor_id, project_id, request, result "
                            "FROM public.project_deletion_operations WHERE operation_id=:id"
                        ),
                        {"id": operation_id},
                    )
                )
                .mappings()
                .one()
            )
            assert receipt["actor_id"] == fixture["user_id"]
            assert receipt["project_id"] == fixture["project_id"]
            assert receipt["request"] == {
                "operation_id": str(operation_id),
                "actor_id": str(fixture["user_id"]),
                "project_id": str(fixture["project_id"]),
            }
            assert receipt["result"] == reply
            audit = (
                (
                    await verify.execute(
                        text(
                            "SELECT id, actor_role, action, target_type, target_id, method, "
                            "status_code, ip::text, detail_json FROM public.audit_logs "
                            "WHERE action='project.delete' AND detail_json ->> 'operation_id'=:id"
                        ),
                        {"id": str(operation_id)},
                    )
                )
                .mappings()
                .one()
            )
            assert audit["id"] == reply["data"]["audit_id"]
            assert audit["actor_role"] == "super_admin"
            assert (audit["action"], audit["target_type"], audit["target_id"]) == (
                "project.delete",
                "project",
                str(fixture["project_id"]),
            )
            assert (audit["method"], audit["status_code"], audit["ip"]) == (
                "DELETE",
                204,
                "127.0.0.1",
            )
            assert audit["detail_json"]["deleted_tasks"] == 1
            assert audit["detail_json"]["deleted_annotations"] == 1

        async with maker() as replay:
            assert (
                await _delete(
                    replay,
                    fixture,
                    operation_id,
                    context={"method": "DELETE", "request_id": str(uuid.uuid4())},
                )
                == reply
            )
            assert await _delete(
                replay, fixture, operation_id, project_id=uuid.uuid4()
            ) == {
                "schema": "aap-project-delete/v1",
                "ok": False,
                "error": {"code": "operation_conflict"},
            }
            assert (
                await replay.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE action='project.delete' "
                        "AND detail_json ->> 'operation_id'=:id"
                    ),
                    {"id": str(operation_id)},
                )
                == 1
            )
    finally:
        await _cleanup_fixture(test_engine, fixture)


@pytest.mark.parametrize("end_transaction", ["rollback", "failed_commit"])
async def test_project_deletion_follows_outer_rollback_or_failed_commit(
    test_engine, end_transaction
):
    fixture = await _seed_graph(test_engine)
    operation_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            await session.begin()
            assert (await _delete(session, fixture, operation_id))["ok"] is True
            if end_transaction == "rollback":
                await session.rollback()
            else:
                await session.execute(
                    text(
                        "CREATE TEMP TABLE project_delete_commit_probe ("
                        "value integer UNIQUE DEFERRABLE INITIALLY DEFERRED) ON COMMIT DROP"
                    )
                )
                await session.execute(
                    text(
                        "INSERT INTO pg_temp.project_delete_commit_probe VALUES (1), (1)"
                    )
                )
                with pytest.raises(DBAPIError):
                    await session.commit()
                await session.rollback()
        async with maker() as verify:
            await _assert_graph_unchanged(verify, fixture)
            await _assert_no_receipt_or_audit(verify, operation_id)
            assert (
                await verify.scalar(
                    text(
                        "SELECT count(*) FROM public.annotation_feedbacks WHERE id=:id"
                    ),
                    {"id": fixture["other_feedback_id"]},
                )
                == 1
            )
    finally:
        await _cleanup_fixture(test_engine, fixture)


@pytest.mark.parametrize("blocker", ["selection", "empty_release"])
async def test_model_selection_and_empty_release_block_before_cleanup(
    test_engine, blocker
):
    fixture = await _seed_graph(test_engine, other_project=False)
    operation_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            if blocker == "selection":
                session.add(
                    ProjectModelSelection(
                        project_id=fixture["project_id"], generation=0, release_id=None
                    )
                )
            else:
                session.add(
                    ModelRelease(
                        id=uuid.uuid4(),
                        project_id=fixture["project_id"],
                        model_version_id=None,
                        deployment_id=None,
                        creator_id=fixture["user_id"],
                        request_id=uuid.uuid4(),
                        request_digest="a" * 64,
                        generation=1,
                        snapshot={},
                    )
                )
        async with maker() as session:
            assert await _delete(session, fixture, operation_id) == {
                "schema": "aap-project-delete/v1",
                "ok": False,
                "error": {"code": "retained_trained_models"},
            }
            await session.commit()
        async with maker() as verify:
            await _assert_graph_unchanged(verify, fixture)
            await _assert_no_receipt_or_audit(verify, operation_id)
    finally:
        await _cleanup_fixture(test_engine, fixture)


async def test_project_delete_rechecks_manager_after_owner_transfer(test_engine):
    fixture = await _seed_graph(test_engine, other_project=False)
    operation_id, successor_id = uuid.uuid4(), uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    fixture["successor_id"] = successor_id
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            actor = await session.get(User, fixture["user_id"])
            actor.role = "project_admin"
            session.add(
                User(
                    id=successor_id,
                    email=f"project-delete-{uuid.uuid4().hex}@example.test",
                    name="New owner",
                    password_hash="test-only",
                    role="project_admin",
                    is_active=True,
                )
            )
            await session.flush()
            await session.execute(
                update(Project)
                .where(Project.id == fixture["project_id"])
                .values(owner_id=successor_id)
            )
        async with maker() as session:
            assert await _delete(session, fixture, operation_id) == {
                "schema": "aap-project-delete/v1",
                "ok": False,
                "error": {"code": "project_missing"},
            }
            await session.commit()
        async with maker() as verify:
            await _assert_graph_unchanged(verify, fixture)
            await _assert_no_receipt_or_audit(verify, operation_id)
    finally:
        await _cleanup_fixture(test_engine, fixture)


async def test_project_delete_rejects_inactive_actor_without_partial_changes(
    test_engine,
):
    fixture = await _seed_graph(test_engine, other_project=False)
    operation_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            await session.execute(
                update(User)
                .where(User.id == fixture["user_id"])
                .values(is_active=False)
            )
        async with maker() as session:
            assert await _delete(session, fixture, operation_id) == {
                "schema": "aap-project-delete/v1",
                "ok": False,
                "error": {"code": "account_not_active"},
            }
            await session.commit()
        async with maker() as verify:
            await _assert_graph_unchanged(verify, fixture)
            await _assert_no_receipt_or_audit(verify, operation_id)
    finally:
        await _cleanup_fixture(test_engine, fixture)


async def test_project_delete_contention_rolls_back_owner_cleanup(test_engine):
    fixture = await _seed_graph(test_engine)
    operation_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    locker, caller = maker(), maker()
    try:
        await locker.begin()
        await locker.execute(
            text(
                "SELECT id FROM public.export_artifacts WHERE project_id=:id FOR UPDATE"
            ),
            {"id": fixture["project_id"]},
        )
        await caller.begin()
        await caller.execute(text("SET LOCAL lock_timeout='250ms'"))
        assert await _delete(caller, fixture, operation_id) == {
            "schema": "aap-project-delete/v1",
            "ok": False,
            "error": {"code": "project_busy"},
        }
        await _assert_graph_unchanged(caller, fixture)
        await _assert_no_receipt_or_audit(caller, operation_id)
        await caller.rollback()
    finally:
        await caller.close()
        await locker.rollback()
        await locker.close()
        await _cleanup_fixture(test_engine, fixture)


async def test_append_client_cannot_call_project_delete_coordinator(test_engine):
    role_url = os.environ.get("AAP_MODULE_DATA_APPEND_URL")
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1" or not role_url:
        pytest.skip("requires the disposable module-data role verifier")
    fixture = await _seed_graph(test_engine, other_project=False)
    operation_id = uuid.uuid4()
    fixture["operation_ids"].append(operation_id)
    role, target = make_url(role_url), test_engine.url
    if (
        role.drivername != "postgresql+asyncpg"
        or role.username != "append_client"
        or role.host != target.host
        or role.port != target.port
        or role.database != target.database
    ):
        pytest.fail("append-client URL does not match the disposable test database")
    engine = create_async_engine(
        role_url,
        hide_parameters=True,
        connect_args={"timeout": 5, "command_timeout": 10},
    )
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with engine.connect() as client:
            assert await client.scalar(text("SELECT current_user")) == "append_client"
            with pytest.raises(DBAPIError) as denied:
                await client.execute(
                    _DELETE_PROJECT,
                    {
                        "operation_id": operation_id,
                        "actor_id": fixture["user_id"],
                        "project_id": fixture["project_id"],
                        "audit_context": json.dumps({"method": "DELETE"}),
                    },
                )
            assert _sqlstate(denied.value) == "42501"
            await client.rollback()
        async with maker() as owner:
            await _assert_graph_unchanged(owner, fixture)
            await _assert_no_receipt_or_audit(owner, operation_id)
    finally:
        await engine.dispose()
        await _cleanup_fixture(test_engine, fixture)
