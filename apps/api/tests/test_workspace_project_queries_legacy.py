"""Capture live legacy Project reads and response projections before extraction."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

import httpx
import pytest
from sqlalchemy import delete, event, or_, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.services import workspace_configuration
from app.services.identity_contracts import IdentityActorFacts
from app.api.v1.projects import list_projects
from tests.factory import (
    build_tool_bindings,
    create_batch,
    create_membership,
    create_project,
    create_task,
    create_user,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_WORKSPACE_PROJECT_QUERY_CAPTURE") != "1",
        reason="requires the explicit owned legacy Project-query capture",
    ),
]

_ROOT = Path(__file__).resolve().parents[3]
_BASE_COMMIT = "50fc1df9ac9f99fdbbed7ac0eda7205d7287156d"
_CAPTURE_PATH = _ROOT / ".worktree/workspace-project-query-legacy-samples.json"
_PROJECTS_PATH = "/api/v1/projects"
_PAST = datetime(2000, 1, 1, tzinfo=timezone.utc)
_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)
_FIXED_TIME_PREFIXES = ("2026-01-", "2000-01-", "2099-01-")
_TIME_FIELDS = {"created_at", "updated_at"}
_HEADERS = ("content-type", "location", "www-authenticate", "retry-after")
_SOURCE_FILES = """
apps/api/app/api/v1/projects.py
apps/api/app/schemas/project.py
apps/api/app/db/models/project.py
apps/api/app/db/models/project_member.py
apps/api/app/db/models/task.py
apps/api/app/db/models/task_batch.py
apps/api/app/db/models/annotation.py
apps/api/app/db/models/dataset.py
apps/api/app/db/models/async_job.py
apps/api/app/db/models/user.py
apps/api/app/deps.py
apps/api/app/core/security.py
apps/api/app/config.py
apps/api/app/services/identity_contracts.py
apps/api/app/services/project_access.py
apps/api/app/services/user_brief.py
apps/api/app/services/inference_limits.py
apps/api/app/services/workspace_configuration.py
apps/api/app/services/workspace_configuration_data.py
apps/api/app/schemas/user.py
apps/workspace/src/aap_workspace/configuration.py
apps/workspace/src/aap_workspace/configuration_contract.py
apps/workspace/src/aap_workspace/configuration_requests.py
apps/workspace/src/aap_workspace/configuration_values.py
apps/workspace/src/aap_workspace/mask_configuration_values.py
apps/workspace/src/aap_workspace/point_quality_configuration_values.py
apps/_shared/workspace_contract/src/aap_workspace_contract/__init__.py
apps/_shared/workspace_contract/src/aap_workspace_contract/projections.py
""".split()
_MISSING = object()


def _legacy_runtime_url(test_engine) -> str:
    value = os.environ.get("AAP_MODULE_DATA_LEGACY_RUNTIME_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.username == "legacy_runtime"
    assert parsed.database and parsed.database.endswith("_test")
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return parsed.set(drivername="postgresql+asyncpg").render_as_string(
        hide_password=False
    )


def _source_metadata() -> dict:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    for relative in _SOURCE_FILES:
        baseline = subprocess.run(
            ["git", "show", f"{_BASE_COMMIT}:{relative}"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
        ).stdout
        assert baseline == (_ROOT / relative).read_bytes(), relative
    return {
        "captured_head": head,
        "pre_extraction_base": _BASE_COMMIT,
        "capture_driver_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "runtime_source_sha256": {
            relative: hashlib.sha256((_ROOT / relative).read_bytes()).hexdigest()
            for relative in _SOURCE_FILES
        },
    }


def _normalise(value, aliases: dict[str, str], *, key: str | None = None):
    if key in _TIME_FIELDS and isinstance(value, str):
        if value.startswith(_FIXED_TIME_PREFIXES):
            return value
        return "<timestamp>"
    if isinstance(value, uuid.UUID):
        value = str(value)
    if isinstance(value, str):
        if value in aliases:
            return aliases[value]
        for actual, alias in aliases.items():
            value = value.replace(actual, alias)
        return value
    if isinstance(value, (list, tuple)):
        return [_normalise(item, aliases) for item in value]
    if isinstance(value, dict):
        return {
            name: _normalise(item, aliases, key=name) for name, item in value.items()
        }
    return value


async def _request(
    client: httpx.AsyncClient,
    capture: dict,
    aliases: dict[str, str],
    actors: dict[str, User],
    tokens: dict[str, str],
    *,
    actor_alias: str,
    method: str,
    path: str,
    params=None,
    body=None,
    new_resource_alias: str | None = None,
    active: bool = True,
):
    kwargs = {"headers": {"Authorization": f"Bearer {tokens[actor_alias]}"}}
    if params is not None:
        kwargs["params"] = params
    if body is not None:
        kwargs["json"] = body
    response = await client.request(method, path, **kwargs)
    try:
        response_body = response.json() if response.content else None
    except ValueError:
        response_body = response.text
    if new_resource_alias and isinstance(response_body, dict):
        resource_id = response_body.get("id")
        if resource_id:
            aliases[str(resource_id)] = f"${new_resource_alias}"
        display_id = response_body.get("display_id")
        if display_id:
            aliases[str(display_id)] = f"${new_resource_alias}_display_id"
    actor = actors[actor_alias]
    capture["samples"].append(
        {
            "request": {
                "method": method.upper(),
                "path": _normalise(path, aliases),
                "params": _normalise(params, aliases),
                "body": _normalise(body, aliases),
                "actor": {
                    "alias": actor_alias,
                    "id": aliases[str(actor.id)],
                    "role": actor.role,
                    "active": active,
                },
            },
            "response": {
                "status": response.status_code,
                "headers": {
                    name: response.headers[name]
                    for name in _HEADERS
                    if name in response.headers
                },
                "body": _normalise(response_body, aliases),
            },
        }
    )
    return response, response_body


@contextmanager
def _observe_sql(engine):
    statements: list[str] = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.casefold())

    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)


def _sql_observation(statements: list[str]) -> dict:
    selects = [
        statement for statement in statements if statement.lstrip().startswith("select")
    ]
    return {
        "asserted_as_contract": False,
        "statement_count": len(statements),
        "select_count": len(selects),
        "user_select_count": sum("users" in statement for statement in selects),
        "annotation_select_count": sum(
            "annotations" in statement for statement in selects
        ),
    }


async def _direct_read_probe(
    *, admin_maker, runtime_maker, actor: User, no_autoflush: bool
):
    facts = IdentityActorFacts(
        id=actor.id,
        name=actor.name,
        email=actor.email,
        role=actor.role,
        is_active=True,
    )
    pending = Project(
        id=uuid.uuid4(),
        display_id=f"P-PQ-{uuid.uuid4().hex[:10]}",
        name="Uncommitted project query probe",
        type_label="Project query baseline",
        type_key="image-det",
        owner_id=actor.id,
        created_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
    )
    async with runtime_maker() as db:
        db.add(pending)
        args = dict(
            status=None,
            search=None,
            type_key=None,
            data_type=None,
            member_id=None,
            project_role=None,
            created_from=None,
            created_to=None,
        )
        if no_autoflush:
            with db.no_autoflush:
                rows = await list_projects(**args, db=db, user=facts)
        else:
            rows = await list_projects(**args, db=db, user=facts)
        included = any(row["id"] == pending.id for row in rows)
        unflushed = pending in db.new
        transaction_open = db.in_transaction()
        async with admin_maker() as observer:
            committed = await observer.get(Project, pending.id) is not None
        await db.rollback()
    return {
        "no_autoflush": no_autoflush,
        "included_in_route_result": included,
        "pending_in_session_new_after_read": unflushed,
        "caller_transaction_open_after_read": transaction_open,
        "visible_from_owner_before_caller_rollback": committed,
    }


async def _cleanup_owned(
    maker,
    *,
    actor_ids: list[uuid.UUID],
    project_ids: list[uuid.UUID],
    dataset_ids: list[uuid.UUID],
    dataset_item_ids: list[uuid.UUID],
    project_dataset_ids: list[uuid.UUID],
    member_ids: list[uuid.UUID],
    batch_ids: list[uuid.UUID],
    task_ids: list[uuid.UUID],
    annotation_ids: list[uuid.UUID],
    job_ids: list[uuid.UUID],
    operation_ids: list[uuid.UUID],
) -> None:
    async with maker.begin() as db:
        for operation_id in operation_ids:
            params = {"operation_id": str(operation_id)}
            for table in (
                "workspace_configuration_operation_states",
                "workspace_configuration_operation_attempts",
                "workspace_configuration_operation_receipts",
            ):
                await db.execute(
                    text(
                        f"DELETE FROM public.{table} "
                        "WHERE operation_id=CAST(:operation_id AS uuid)"
                    ),
                    params,
                )
        for model, ids in (
            (Annotation, annotation_ids),
            (Task, task_ids),
            (AsyncJob, job_ids),
            (ProjectMember, member_ids),
            (ProjectDataset, project_dataset_ids),
            (TaskBatch, batch_ids),
            (DatasetItem, dataset_item_ids),
            (Dataset, dataset_ids),
        ):
            if ids:
                await db.execute(delete(model).where(model.id.in_(ids)))
        project_filters = []
        if project_ids:
            project_filters.append(Project.id.in_(project_ids))
        if actor_ids:
            project_filters.append(Project.owner_id.in_(actor_ids))
        if project_filters:
            await db.execute(delete(Project).where(or_(*project_filters)))
        if actor_ids:
            await db.execute(delete(User).where(User.id.in_(actor_ids)))


def _write_capture(capture: dict) -> None:
    _CAPTURE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _CAPTURE_PATH.open("x", encoding="utf-8") as stream:
        json.dump(capture, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


async def test_capture_legacy_project_query_and_projection_samples(
    test_engine, app_module, monkeypatch
):
    from app.config import settings
    from app.deps import get_db

    assert not _CAPTURE_PATH.exists(), f"refusing to overwrite {_CAPTURE_PATH}"
    runtime_url = _legacy_runtime_url(test_engine)
    owner_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    runtime_engine = create_async_engine(runtime_url, hide_parameters=True)
    runtime_maker = async_sessionmaker(runtime_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:10]
    aliases: dict[str, str] = {suffix: "$capture_suffix"}
    actors: dict[str, User] = {}
    tokens: dict[str, str] = {}
    project_by_alias: dict[str, Project] = {}
    project_ids: list[uuid.UUID] = []
    dataset_ids: list[uuid.UUID] = []
    dataset_item_ids: list[uuid.UUID] = []
    project_dataset_ids: list[uuid.UUID] = []
    member_ids: list[uuid.UUID] = []
    batch_ids: list[uuid.UUID] = []
    task_ids: list[uuid.UUID] = []
    annotation_ids: list[uuid.UUID] = []
    job_ids: list[uuid.UUID] = []
    operation_ids: list[uuid.UUID] = []
    capture = {
        "capture_kind": "actual-pre-extraction-project-query-and-response-projection",
        "source": _source_metadata(),
        "runtime_identity": None,
        "samples": [],
        "observations": {},
    }

    previous_db_override = app_module.dependency_overrides.get(get_db, _MISSING)
    original_configuration_request = workspace_configuration.configuration_request

    def capture_operation_id(**kwargs):
        request = original_configuration_request(**kwargs)
        operation_ids.append(request.operation_id)
        return request

    async def request_db():
        async with runtime_maker() as db:
            yield db

    monkeypatch.setattr(settings, "workspace_configuration_url", "")
    monkeypatch.setattr(settings, "workspace_configuration_database_url", "")
    monkeypatch.setattr(settings, "workspace_configuration_database_url_file", "")
    monkeypatch.setattr(settings, "audit_async", True)
    monkeypatch.setattr("app.middleware.audit._enqueue_audit", lambda _payload: True)
    monkeypatch.setattr(
        workspace_configuration, "configuration_request", capture_operation_id
    )
    app_module.dependency_overrides[get_db] = request_db

    try:
        async with runtime_engine.connect() as connection:
            runtime_role, runtime_database = (
                await connection.execute(
                    text("SELECT current_user, current_database()")
                )
            ).one()
        assert runtime_role == "legacy_runtime"
        assert runtime_database == test_engine.url.database
        capture["runtime_identity"] = {
            "database_role": runtime_role,
            "database": runtime_database,
        }

        async with owner_maker.begin() as db:
            for alias, role, name in (
                ("super_admin", "super_admin", "Baseline Super Admin"),
                ("project_admin", "project_admin", "Baseline Project Admin"),
                ("employee", "employee", "Baseline Employee"),
                ("reviewer", "employee", "Baseline Reviewer"),
                ("inactive_annotator", "employee", "Inactive Annotator"),
            ):
                actor = await create_user(
                    db,
                    role,
                    f"project-query-{alias}-{suffix}@example.test",
                    name,
                )
                actors[alias] = actor
                tokens[alias] = create_access_token(subject=str(actor.id), role=role)
                aliases[str(actor.id)] = f"$actor_{alias}"
            actors["inactive_annotator"].is_active = False

            bindings = build_tool_bindings(["car", "person"])
            project_specs = (
                (
                    "target",
                    "project_admin",
                    "Alpha target",
                    "image-det",
                    31,
                    "in_progress",
                ),
                (
                    "older",
                    "project_admin",
                    "Older segmentation",
                    "image-seg",
                    10,
                    "completed",
                ),
                (
                    "shared",
                    "super_admin",
                    "Shared Video 42%",
                    "video-track",
                    20,
                    "in_progress",
                ),
                ("hidden", "super_admin", "Hidden project", "lidar", 15, "archived"),
            )
            for alias, owner_alias, name, type_key, day, status in project_specs:
                created_at = datetime(2026, 1, day, tzinfo=timezone.utc)
                if alias == "target":
                    created_at = created_at.replace(
                        hour=23, minute=59, second=59, microsecond=999999
                    )
                project = await create_project(
                    db,
                    owner_id=actors[owner_alias].id,
                    name=f"{name} {suffix}",
                    type_key=type_key,
                    type_label="Project query baseline",
                    classes=["car", "person"],
                )
                project.data_type = (
                    "video"
                    if type_key.startswith("video")
                    else ("lidar" if type_key == "lidar" else "image")
                )
                project.status = status
                project.created_at = project.updated_at = created_at
                if alias == "target":
                    project.tool_bindings = bindings
                    project.annotation_guide = "# readiness guide"
                    (
                        project.total_tasks,
                        project.completed_tasks,
                        project.review_tasks,
                    ) = 4, 1, 1
                project_by_alias[alias] = project
                aliases[str(project.id)] = f"$project_{alias}"
                aliases[project.display_id] = f"$display_{alias}"
                project_ids.append(project.id)

            member_specs = (
                ("target", "employee", "annotator", "project_admin"),
                ("target", "reviewer", "reviewer", "project_admin"),
                ("target", "inactive_annotator", "annotator", "project_admin"),
                ("shared", "project_admin", "viewer", "super_admin"),
                ("shared", "employee", "reviewer", "super_admin"),
            )
            for project_alias, user_alias, role, assigned_by_alias in member_specs:
                member = await create_membership(
                    db,
                    project_id=project_by_alias[project_alias].id,
                    user_id=actors[user_alias].id,
                    role=role,
                    assigned_by=actors[assigned_by_alias].id,
                )
                member_ids.append(member.id)

            dataset = Dataset(
                id=uuid.uuid4(),
                display_id=f"D-PQ-{uuid.uuid4().hex[:8]}",
                name=f"Project query dataset {suffix}",
                data_type="image",
                created_by=actors["project_admin"].id,
            )
            db.add(dataset)
            await db.flush()
            item = DatasetItem(
                id=uuid.uuid4(),
                dataset_id=dataset.id,
                file_name="readiness-item.jpg",
                file_path=f"project-query/{suffix}/readiness-item.jpg",
                file_type="image",
            )
            link = ProjectDataset(
                id=uuid.uuid4(),
                project_id=project_by_alias["target"].id,
                dataset_id=dataset.id,
            )
            db.add_all([item, link])
            await db.flush()
            dataset_ids.append(dataset.id)
            dataset_item_ids.append(item.id)
            project_dataset_ids.append(link.id)

            batch_specs = (
                ("Assigned active batch", actors["employee"], actors["reviewer"]),
                (
                    "Inactive assignee batch",
                    actors["inactive_annotator"],
                    actors["reviewer"],
                ),
                ("Empty active batch", None, None),
            )
            batches = []
            for name, annotator, reviewer in batch_specs:
                batch = await create_batch(
                    db,
                    project_id=project_by_alias["target"].id,
                    name=name,
                    status="active",
                )
                batch.annotator_id = annotator.id if annotator else None
                batch.reviewer_id = reviewer.id if reviewer else None
                batches.append(batch)
            await db.flush()
            active_batch, inactive_batch, empty_batch = batches
            batch_ids.extend(batch.id for batch in batches)

            task_specs = (
                (
                    "pending-old",
                    "pending",
                    _PAST + timedelta(days=1),
                    active_batch.id,
                    item.id,
                    None,
                    None,
                ),
                (
                    "completed-old",
                    "completed",
                    _PAST + timedelta(days=2),
                    inactive_batch.id,
                    None,
                    None,
                    _PAST + timedelta(days=3),
                ),
                (
                    "review-old",
                    "review",
                    _PAST + timedelta(days=4),
                    inactive_batch.id,
                    None,
                    _PAST + timedelta(days=5),
                    None,
                ),
                ("future-pending", "pending", _FUTURE, None, None, None, None),
            )
            tasks = []
            for (
                name,
                status,
                created_at,
                batch_id,
                item_id,
                submitted_at,
                reviewed_at,
            ) in task_specs:
                task = await create_task(
                    db,
                    project_id=project_by_alias["target"].id,
                    display_id=f"T-PQ-{uuid.uuid4().hex[:10]}",
                    status=status,
                )
                task.file_name = f"{name}.jpg"
                task.file_path = f"project-query/{name}.jpg"
                task.batch_id = batch_id
                task.dataset_item_id = item_id
                task.created_at = created_at
                task.updated_at = created_at
                task.submitted_at = submitted_at
                task.reviewed_at = reviewed_at
                tasks.append(task)
            await db.flush()
            task_ids.extend(task.id for task in tasks)
            target = project_by_alias["target"]
            target.total_tasks = 4
            target.completed_tasks = 1
            target.review_tasks = 1
            target.updated_at = target.created_at

            annotations = [
                Annotation(
                    id=uuid.uuid4(),
                    task_id=task.id,
                    project_id=target.id,
                    user_id=actors["project_admin"].id,
                    class_name="car",
                    geometry={
                        "type": "rectangle",
                        "x": 0,
                        "y": 0,
                        "width": 1,
                        "height": 1,
                    },
                    parent_prediction_id=uuid.uuid4() if ai else None,
                    was_cancelled=cancelled,
                    created_at=_PAST + timedelta(days=1),
                )
                for task, ai, cancelled in (
                    (tasks[1], False, False),
                    (tasks[1], True, False),
                    (tasks[2], True, True),
                )
            ]
            db.add_all(annotations)
            jobs = []
            for index, status in enumerate(("pending", "running", "failed")):
                job = AsyncJob(
                    id=uuid.uuid4(),
                    kind="create_tasks",
                    project_id=target.id,
                    user_id=actors["project_admin"].id,
                    status=status,
                    payload={},
                    result={},
                    created_at=_PAST + timedelta(hours=index),
                    updated_at=_PAST + timedelta(hours=index),
                )
                db.add(job)
                jobs.append(job)
            await db.flush()
            annotation_ids.extend(annotation.id for annotation in annotations)
            job_ids.extend(job.id for job in jobs)

        transport = httpx.ASGITransport(app=app_module, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=20
        ) as client:

            async def request(actor_alias, method, path, **kwargs):
                return await _request(
                    client,
                    capture,
                    aliases,
                    actors,
                    tokens,
                    actor_alias=actor_alias,
                    method=method,
                    path=path,
                    **kwargs,
                )

            list_bodies = {}
            with _observe_sql(runtime_engine) as list_statements:
                for actor_alias in ("super_admin", "project_admin", "employee"):
                    response, body = await request(actor_alias, "GET", _PROJECTS_PATH)
                    assert response.status_code == 200, response.text
                    list_bodies[actor_alias] = body
            capture["observations"]["project_list_sql"] = _sql_observation(
                list_statements
            )
            capture["observations"]["project_list_sql"]["request_count"] = 3

            def ids(body):
                return {row["id"] for row in body}

            target_id = str(project_by_alias["target"].id)
            older_id = str(project_by_alias["older"].id)
            shared_id = str(project_by_alias["shared"].id)
            hidden_id = str(project_by_alias["hidden"].id)
            assert ids(list_bodies["super_admin"]) == {
                target_id,
                older_id,
                shared_id,
                hidden_id,
            }
            assert ids(list_bodies["project_admin"]) == {target_id, older_id, shared_id}
            assert ids(list_bodies["employee"]) == {target_id, shared_id}
            assert [row["id"] for row in list_bodies["super_admin"]] == [
                target_id,
                shared_id,
                hidden_id,
                older_id,
            ]
            target_projection = next(
                row for row in list_bodies["project_admin"] if row["id"] == target_id
            )
            shared_projection = next(
                row for row in list_bodies["project_admin"] if row["id"] == shared_id
            )
            employee_target = next(
                row for row in list_bodies["employee"] if row["id"] == target_id
            )
            assert target_projection["owner_name"] == actors["project_admin"].name
            assert target_projection["member_count"] == 3
            # The legacy project projection counts active cancelled AI rows;
            # ProjectStats excludes them from annotation totals and rates.
            assert target_projection["ai_completed_tasks"] == 2
            assert target_projection["my_project_role"] is None
            assert employee_target["my_project_role"] == "annotator"
            assert shared_projection["my_project_role"] == "viewer"

            page_one, page_one_body = await request(
                "super_admin",
                "GET",
                f"{_PROJECTS_PATH}/query",
                params=[("page", "1"), ("page_size", "2")],
            )
            page_two, page_two_body = await request(
                "super_admin",
                "GET",
                f"{_PROJECTS_PATH}/query",
                params=[("page", "2"), ("page_size", "2")],
            )
            assert page_one.status_code == page_two.status_code == 200
            assert (page_one_body["total"], page_one_body["pages"]) == (4, 2)
            assert [row["id"] for row in page_one_body["items"]] == [
                target_id,
                shared_id,
            ]
            assert [row["id"] for row in page_two_body["items"]] == [
                hidden_id,
                older_id,
            ]

            legacy_filtered, legacy_filtered_body = await request(
                "project_admin",
                "GET",
                _PROJECTS_PATH,
                params=[
                    ("status", "in_progress"),
                    ("search", "ALPHA"),
                    ("type_key", "image-det"),
                    ("data_type", "image"),
                    ("data_type", "video"),
                    ("created_from", "2026-01-31"),
                    ("created_to", "2026-01-31"),
                    ("member_id", str(actors["employee"].id)),
                ],
            )
            assert legacy_filtered.status_code == 200, legacy_filtered.text
            assert [row["id"] for row in legacy_filtered_body] == [target_id]

            employee_filtered, employee_filtered_body = await request(
                "employee",
                "GET",
                f"{_PROJECTS_PATH}/query",
                params=[
                    ("type_key", "image-det"),
                    ("type_key", "video-track"),
                    ("data_type", "image"),
                    ("data_type", "video"),
                    ("project_role", "annotator"),
                ],
            )
            assert employee_filtered.status_code == 200
            assert employee_filtered_body["total"] == 1
            assert employee_filtered_body["items"][0]["id"] == target_id
            query_errors = (
                (
                    "role_before_date_parse",
                    [("project_role", "bad-role"), ("created_from", "bad")],
                    "非法项目职责",
                ),
                (
                    "invalid_created_from",
                    [("created_from", "bad")],
                    "创建日期须为 ISO 日期或时间",
                ),
                (
                    "reversed_date_range",
                    [("created_from", "2026-02-01"), ("created_to", "2026-01-01")],
                    "创建开始日期不能晚于结束日期",
                ),
            )
            for alias, params, detail in query_errors:
                response, body = await request(
                    "project_admin",
                    "GET",
                    f"{_PROJECTS_PATH}/query",
                    params=params,
                )
                assert response.status_code == 422
                assert body["detail"] == detail
            for params in ({"page": "0"}, {"page_size": "101"}):
                response, _body = await request(
                    "super_admin",
                    "GET",
                    f"{_PROJECTS_PATH}/query",
                    params=params,
                )
                assert response.status_code == 422

            detail_cases = (
                ("project_admin", "target", 200, None),
                ("project_admin", "shared", 200, "viewer"),
                ("employee", "target", 200, "annotator"),
                ("employee", "shared", 200, "reviewer"),
                ("employee", "hidden", 404, None),
                ("super_admin", "hidden", 200, None),
            )
            for (
                actor_alias,
                project_alias,
                expected_status,
                expected_role,
            ) in detail_cases:
                response, body = await request(
                    actor_alias,
                    "GET",
                    f"{_PROJECTS_PATH}/{project_by_alias[project_alias].id}",
                )
                assert response.status_code == expected_status, response.text
                if expected_status == 200:
                    assert body["my_project_role"] == expected_role

            readiness, readiness_body = await request(
                "project_admin",
                "GET",
                f"{_PROJECTS_PATH}/{project_by_alias['target'].id}/readiness",
            )
            assert readiness.status_code == 200, readiness.text
            readiness_keys = (
                "project_id",
                "guide_ready",
                "classes_ready",
                "linked_dataset_count",
                "dataset_item_count",
                "task_count",
                "linked_task_count",
                "task_creation_active_jobs",
                "task_creation_failed_jobs",
                "latest_task_creation_status",
                "batch_count",
                "nonempty_batch_count",
                "executable_batch_count",
                "assigned_batch_count",
                "active_annotator_count",
                "active_reviewer_count",
                "activated_batch_count",
            )
            assert tuple(readiness_body[key] for key in readiness_keys) == (
                target_id,
                True,
                True,
                1,
                1,
                4,
                1,
                2,
                1,
                "failed",
                3,
                2,
                2,
                1,
                1,
                1,
                2,
            )
            for actor_alias, project_alias, expected_status in (
                ("employee", "target", 403),
                ("project_admin", "shared", 403),
                ("super_admin", "target", 200),
            ):
                response, _body = await request(
                    actor_alias,
                    "GET",
                    f"{_PROJECTS_PATH}/{project_by_alias[project_alias].id}/readiness",
                )
                assert response.status_code == expected_status

            stats, stats_body = await request(
                "project_admin", "GET", f"{_PROJECTS_PATH}/stats"
            )
            assert stats.status_code == 200, stats.text
            assert tuple(
                stats_body[key]
                for key in (
                    "total_data",
                    "completed",
                    "pending_review",
                    "total_annotations",
                    "ai_derived_annotations",
                    "ai_rate",
                    "total_data_series",
                    "completed_series",
                    "pending_review_series",
                    "ai_rate_series",
                )
            ) == (4, 1, 1, 2, 1, 50.0, [3] * 12, [1] * 12, [1] * 12, [50.0] * 12)
            create_body = {
                "name": f"Created project projection {suffix}",
                "type_label": "Image label",
                "type_key": "image-det",
                "tool_bindings": build_tool_bindings(
                    [{"name": "truck", "color": "#123456", "alias": "Vehicle"}]
                ),
                "label_config": {"nested": [1, {"unicode": "猫"}]},
                "annotation_guide": "# Created guide",
                "rendering_config": {"smoothImage": True},
            }
            created, created_body = await request(
                "project_admin",
                "POST",
                _PROJECTS_PATH,
                body=create_body,
                new_resource_alias="project_created",
            )
            assert created.status_code == 200, created.text
            created_project_id = uuid.UUID(created_body["id"])
            project_ids.append(created_project_id)
            assert created_body["owner_name"] == actors["project_admin"].name
            assert created_body["member_count"] == 0
            assert created_body["classes"] == ["truck"]
            assert created_body["classes_config"]["truck"]["color"] == "#123456"
            # ProjectCreate ignores these extra configuration fields.
            assert created_body["label_config"] == {}
            assert created_body["annotation_guide"] is None

            update_body = {
                "name": f"Updated project projection {suffix}",
                "annotation_guide": None,
                "tool_bindings": {},
            }
            updated, updated_body = await request(
                "project_admin",
                "PATCH",
                f"{_PROJECTS_PATH}/{created_project_id}",
                body=update_body,
            )
            assert updated.status_code == 200, updated.text
            assert updated_body["name"] == update_body["name"]
            assert updated_body["annotation_guide"] is None
            assert updated_body["tool_bindings"] == {}
            assert updated_body["classes"] == []
            assert updated_body["type_key"] == "image-det"

            null_write, _null_body = await request(
                "project_admin",
                "PATCH",
                f"{_PROJECTS_PATH}/{created_project_id}",
                body={"rendering_config": None},
            )
            assert null_write.status_code == 500
            async with owner_maker.begin() as db:
                stored = (
                    await db.execute(
                        text(
                            "SELECT rendering_config IS NULL, jsonb_typeof(rendering_config) "
                            "FROM public.projects WHERE id=:id"
                        ),
                        {"id": created_project_id},
                    )
                ).one()
                assert stored[0] is False and stored[1] == "null"
                await db.execute(
                    text(
                        "UPDATE public.projects SET rendering_config='{}'::jsonb WHERE id=:id"
                    ),
                    {"id": created_project_id},
                )
            reset, reset_body = await request(
                "project_admin",
                "GET",
                f"{_PROJECTS_PATH}/{created_project_id}",
            )
            assert reset.status_code == 200
            assert reset_body["rendering_config"] is not None

        capture["direct_read_probes"] = [
            await _direct_read_probe(
                admin_maker=owner_maker,
                runtime_maker=runtime_maker,
                actor=actors["project_admin"],
                no_autoflush=no_autoflush,
            )
            for no_autoflush in (False, True)
        ]
        assert [
            (
                row["included_in_route_result"],
                row["pending_in_session_new_after_read"],
                row["caller_transaction_open_after_read"],
                row["visible_from_owner_before_caller_rollback"],
            )
            for row in capture["direct_read_probes"]
        ] == [(True, False, True, False), (False, True, True, False)]

        async with owner_maker() as observer:
            persisted = await observer.get(Project, created_project_id)
            assert persisted is not None and persisted.rendering_config == {}
            assert (
                await observer.get(Project, project_by_alias["target"].id) is not None
            )
    finally:
        if previous_db_override is _MISSING:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous_db_override
        await runtime_engine.dispose()
        if actors:
            await _cleanup_owned(
                owner_maker,
                actor_ids=[actor.id for actor in actors.values()],
                project_ids=project_ids,
                dataset_ids=dataset_ids,
                dataset_item_ids=dataset_item_ids,
                project_dataset_ids=project_dataset_ids,
                member_ids=member_ids,
                batch_ids=batch_ids,
                task_ids=task_ids,
                annotation_ids=annotation_ids,
                job_ids=job_ids,
                operation_ids=operation_ids,
            )

    _write_capture(capture)
