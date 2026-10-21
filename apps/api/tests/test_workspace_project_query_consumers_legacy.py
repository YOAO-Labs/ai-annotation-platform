"""Capture remaining pre-extraction Project query consumers from the live API."""

from __future__ import annotations

import base64
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

import httpx
import pytest
from sqlalchemy import delete, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.services import workspace_configuration
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
        or os.environ.get("AAP_WORKSPACE_PROJECT_QUERY_CONSUMER_CAPTURE") != "1",
        reason="requires the explicit owned legacy Project-consumer capture",
    ),
]

_ROOT = Path(__file__).resolve().parents[3]
_BASE_COMMIT = "86f4ff527446a1932e585f6fda381520860161c9"
_CAPTURE_PATH = _ROOT / ".worktree/workspace-project-query-consumers-legacy.json"
_SEARCH_PATH = "/api/v1/search"
_FIXED_TIME = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
_TIME_FIELDS = {"created_at", "updated_at"}
_HEADERS = ("content-type", "location", "www-authenticate", "retry-after")
_TABLES = (
    "projects",
    "project_members",
    "tasks",
    "task_batches",
    "datasets",
    "users",
    "annotations",
    "workspace_configuration_operation_receipts",
    "workspace_owner_transfer_operation_receipts",
)
_SOURCE_FILES = """
apps/api/app/api/v1/search.py
apps/api/app/api/v1/projects.py
apps/api/app/schemas/project.py
apps/api/app/db/enums.py
apps/api/app/db/models/annotation.py
apps/api/app/db/models/dataset.py
apps/api/app/db/models/project.py
apps/api/app/db/models/project_member.py
apps/api/app/db/models/task.py
apps/api/app/db/models/task_batch.py
apps/api/app/db/models/user.py
apps/api/app/deps.py
apps/api/app/core/security.py
apps/api/app/config.py
apps/api/app/application/project_class_rename.py
apps/api/app/application/project_workspace.py
apps/api/app/services/annotation.py
apps/api/app/services/identity_contracts.py
apps/api/app/services/identity_gateway.py
apps/api/app/services/identity_account_lock_data.py
apps/api/app/services/project_access.py
apps/api/app/services/project_workspace.py
apps/api/app/services/scheduler.py
apps/api/app/services/workspace_configuration.py
apps/api/app/services/workspace_configuration_data.py
apps/api/app/services/workspace_owner_transfer.py
apps/api/app/services/workspace_owner_transfer_data.py
apps/api/alembic/sql/0254_workspace_owner_transfer.sql
apps/api/alembic/sql/0257_workspace_configuration.sql
apps/workspace/src/aap_workspace/configuration.py
apps/workspace/src/aap_workspace/configuration_contract.py
apps/workspace/src/aap_workspace/configuration_requests.py
apps/workspace/src/aap_workspace/configuration_values.py
apps/_shared/workspace_contract/src/aap_workspace_contract/__init__.py
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
        if value.startswith("2026-01-"):
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


@contextmanager
def _observe_activity(engine):
    activity = {"statements": [], "commits": []}

    def observe_sql(_connection, _cursor, statement, _parameters, _context, _many):
        activity["statements"].append(statement.casefold())

    def observe_commit(session):
        if session.get_bind() is engine.sync_engine:
            activity["commits"].append("commit")

    event.listen(engine.sync_engine, "before_cursor_execute", observe_sql)
    event.listen(Session, "after_commit", observe_commit)
    try:
        yield activity
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe_sql)
        event.remove(Session, "after_commit", observe_commit)


def _activity_summary(activity: dict) -> dict:
    statements = activity["statements"]
    kinds = Counter(
        statement.lstrip().split(None, 1)[0]
        for statement in statements
        if statement.strip()
    )
    table_reads = Counter()
    table_writes = Counter()
    for statement in statements:
        is_read = statement.lstrip().startswith("select")
        for table in _TABLES:
            if table in statement:
                (table_reads if is_read else table_writes)[table] += 1
    return {
        "sql_statement_count": len(statements),
        "sql_by_kind": dict(sorted(kinds.items())),
        "read_table_mentions": dict(sorted(table_reads.items())),
        "write_table_mentions": dict(sorted(table_writes.items())),
        "commit_count": len(activity["commits"]),
    }


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
                    "active": actor.is_active,
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
                "raw_body": {
                    "byte_length": len(response.content),
                    "sha256": hashlib.sha256(response.content).hexdigest(),
                    "base64": base64.b64encode(response.content).decode("ascii"),
                },
            },
        }
    )
    return response, response_body


async def _cleanup_owned(
    maker,
    *,
    actor_ids: list[uuid.UUID],
    project_ids: list[uuid.UUID],
    dataset_ids: list[uuid.UUID],
    batch_ids: list[uuid.UUID],
    task_ids: list[uuid.UUID],
    annotation_ids: list[uuid.UUID],
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
        if project_ids:
            await db.execute(
                text(
                    "DELETE FROM public.workspace_owner_transfer_operation_receipts "
                    "WHERE project_id=ANY(CAST(:project_ids AS uuid[]))"
                ),
                {"project_ids": [str(value) for value in project_ids]},
            )
        for model, ids in (
            (Annotation, annotation_ids),
            (Task, task_ids),
            (TaskBatch, batch_ids),
        ):
            if ids:
                await db.execute(delete(model).where(model.id.in_(ids)))
        if project_ids:
            await db.execute(
                delete(ProjectMember).where(ProjectMember.project_id.in_(project_ids))
            )
        if dataset_ids:
            await db.execute(delete(Dataset).where(Dataset.id.in_(dataset_ids)))
        if project_ids:
            await db.execute(delete(Project).where(Project.id.in_(project_ids)))
        if actor_ids:
            await db.execute(delete(User).where(User.id.in_(actor_ids)))


def _write_capture(capture: dict) -> None:
    _CAPTURE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _CAPTURE_PATH.open("x", encoding="utf-8") as stream:
        json.dump(capture, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


async def test_capture_legacy_project_query_consumers_and_postcommit_projections(
    test_engine, app_module, monkeypatch
):
    from app.api.v1 import projects as projects_api
    from app.config import settings
    from app.deps import get_db
    from tests.test_asset_storage_cleanup_runtime import _verify_postgres

    assert not _CAPTURE_PATH.exists(), f"refusing to overwrite {_CAPTURE_PATH}"
    _verify_postgres(test_engine.url)
    runtime_url = _legacy_runtime_url(test_engine)
    owner_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    runtime_engine = create_async_engine(runtime_url, hide_parameters=True)
    runtime_maker = async_sessionmaker(runtime_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:10]
    marker = f"PQX{suffix}"
    aliases: dict[str, str] = {suffix: "$capture_suffix", marker: "$search_marker"}
    actors: dict[str, User] = {}
    tokens: dict[str, str] = {}
    projects: dict[str, Project] = {}
    tasks: dict[str, Task] = {}
    datasets: list[Dataset] = []
    batches: list[TaskBatch] = []
    annotations: list[Annotation] = []
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    task_ids: list[uuid.UUID] = []
    batch_ids: list[uuid.UUID] = []
    annotation_ids: list[uuid.UUID] = []
    operation_ids: list[uuid.UUID] = []
    active_activity: list[dict | None] = [None]
    active_request: list[str | None] = [None]
    flipped_projects: set[uuid.UUID] = set()
    projection_observations: dict[str, dict] = {}
    capture = {
        "capture_kind": "actual-pre-extraction-project-query-consumers-and-postcommit-projections",
        "source": _source_metadata(),
        "runtime_identity": None,
        "samples": [],
        "observations": {},
    }

    previous_db_override = app_module.dependency_overrides.get(get_db, _MISSING)
    original_configuration_request = workspace_configuration.configuration_request
    original_serialize_project = projects_api._serialize_project

    def capture_operation_id(**kwargs):
        request = original_configuration_request(**kwargs)
        operation_ids.append(request.operation_id)
        aliases[str(request.operation_id)] = (
            f"$configuration_operation_{len(operation_ids)}"
        )
        return request

    async def request_db():
        async with runtime_maker() as db:
            yield db

    for field in (
        "identity_url",
        "identity_auth_database_url",
        "identity_auth_database_url_file",
        "identity_briefs_database_url",
        "identity_briefs_database_url_file",
        "workspace_configuration_url",
        "workspace_configuration_database_url",
        "workspace_configuration_database_url_file",
        "workspace_owner_transfer_url",
        "workspace_owner_transfer_database_url",
        "workspace_owner_transfer_database_url_file",
    ):
        monkeypatch.setattr(settings, field, "")
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
            for alias, role in (
                ("super_admin", "super_admin"),
                ("project_admin", "project_admin"),
                ("employee", "employee"),
                ("reviewer", "employee"),
                ("viewer", "viewer"),
                ("hidden_employee", "employee"),
                ("old_owner", "project_admin"),
            ):
                name = (
                    f"{alias} {marker}"
                    if alias
                    in {
                        "super_admin",
                        "project_admin",
                        "employee",
                        "reviewer",
                        "viewer",
                        "hidden_employee",
                    }
                    else alias
                )
                actor = await create_user(
                    db,
                    role,
                    f"consumer-{alias}-{suffix}@example.test",
                    name,
                )
                actors[alias] = actor
                actor_ids.append(actor.id)
                aliases[str(actor.id)] = f"$actor_{alias}"
                tokens[alias] = create_access_token(subject=str(actor.id), role=role)

            for alias, owner_alias, name in (
                ("search_owner", "project_admin", f"{marker} owned"),
                ("search_shared", "super_admin", f"{marker} shared"),
                ("search_hidden", "super_admin", f"{marker} hidden"),
                ("rename", "project_admin", "Class rename consumer"),
                ("configuration", "project_admin", "Configuration projection"),
                ("transfer", "old_owner", "Owner transfer projection"),
            ):
                project = await create_project(
                    db,
                    owner_id=actors[owner_alias].id,
                    name=name,
                    type_label="Project consumer capture",
                )
                project.created_at = project.updated_at = _FIXED_TIME
                projects[alias] = project
                project_ids.append(project.id)
                aliases[str(project.id)] = f"$project_{alias}"
                aliases[project.display_id] = f"$display_{alias}"

            for project_alias, actor_alias, project_role in (
                ("search_shared", "project_admin", "viewer"),
                ("search_shared", "employee", "annotator"),
                ("search_shared", "reviewer", "reviewer"),
                ("search_shared", "viewer", "viewer"),
                ("search_hidden", "hidden_employee", "annotator"),
            ):
                await create_membership(
                    db,
                    project_id=projects[project_alias].id,
                    user_id=actors[actor_alias].id,
                    role=project_role,
                    assigned_by=actors["super_admin"].id,
                )

            search_dataset = Dataset(
                id=uuid.uuid4(),
                display_id=f"D-PQX-{suffix}",
                name=f"{marker} public dataset",
                data_type="image",
                created_by=actors["project_admin"].id,
            )
            db.add(search_dataset)
            datasets.append(search_dataset)
            aliases[str(search_dataset.id)] = "$dataset_search"
            aliases[search_dataset.display_id] = "$display_dataset_search"

            for alias, project_alias, assignee_alias, reviewer_alias, batched in (
                ("owner_task", "search_owner", None, None, False),
                ("employee_task", "search_shared", "employee", None, False),
                ("reviewer_task", "search_shared", None, "reviewer", False),
                ("viewer_task", "search_shared", "viewer", None, True),
                ("unassigned_task", "search_shared", None, None, False),
                ("hidden_task", "search_hidden", "hidden_employee", None, False),
            ):
                batch_id = None
                if batched:
                    batch = await create_batch(
                        db,
                        project_id=projects[project_alias].id,
                        name=f"{marker} active batch",
                        status="active",
                    )
                    batch.annotator_id = actors[assignee_alias].id
                    batches.append(batch)
                    batch_ids.append(batch.id)
                    batch_id = batch.id
                task = await create_task(
                    db,
                    project_id=projects[project_alias].id,
                    display_id=f"T-{marker}-{alias[:9]}",
                )
                task.file_name = f"{marker}-{alias}.jpg"
                task.file_type = "image"
                task.assignee_id = actors[assignee_alias].id if assignee_alias else None
                task.reviewer_id = actors[reviewer_alias].id if reviewer_alias else None
                task.batch_id = batch_id
                task.created_at = task.updated_at = _FIXED_TIME
                tasks[alias] = task
                task_ids.append(task.id)
                aliases[str(task.id)] = f"$task_{alias}"
                aliases[task.display_id] = f"$display_task_{alias}"

            rename_project = projects["rename"]
            rename_project.tool_bindings = {
                "bbox": build_tool_bindings(["Car"], unit="bbox")["bbox"],
                "region": build_tool_bindings(["Car"], unit="region")["region"],
            }
            for unit, geometry, annotation_type in (
                (
                    "bbox",
                    {"type": "bbox", "x": 1, "y": 2, "width": 3, "height": 4},
                    "bbox",
                ),
                (
                    "region",
                    {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1]]},
                    "polygon",
                ),
            ):
                task = await create_task(
                    db,
                    project_id=rename_project.id,
                    display_id=f"T-CLASS-{unit}-{suffix}",
                )
                annotation = Annotation(
                    id=uuid.uuid4(),
                    task_id=task.id,
                    project_id=rename_project.id,
                    user_id=actors["project_admin"].id,
                    class_name="Car",
                    geometry=geometry,
                    annotation_type=annotation_type,
                    tool_unit_id=unit,
                    temporal_role="sample",
                )
                db.add(annotation)
                annotations.append(annotation)
                annotation_ids.append(annotation.id)
                task_ids.append(task.id)
                aliases[str(task.id)] = f"$task_class_{unit}"
                aliases[task.display_id] = f"$display_task_class_{unit}"
            rename_project.created_at = rename_project.updated_at = _FIXED_TIME

        async def observed_serialize(db, project, **kwargs):
            request_name = active_request[0]
            activity = active_activity[0]
            if request_name in {"class_rename_noop", "class_rename_changed"}:
                if project.id == projects["rename"].id:
                    projection_observations[request_name] = {
                        "commit_count_before_projection": len(activity["commits"]),
                        "projection_started_after_route_commit": bool(
                            activity["commits"]
                        ),
                    }
            flip_spec = {
                projects["transfer"].id: ("owner_transfer", "super_admin"),
                projects["configuration"].id: ("configuration_update", "project_admin"),
            }
            if project.id in flip_spec and project.id not in flipped_projects:
                label, actor_alias = flip_spec[project.id]
                assert request_name == label
                committed_before_projection = len(activity["commits"])
                async with owner_maker() as observer:
                    current_project = await observer.get(Project, project.id)
                    receipt = await observer.execute(
                        text(
                            "SELECT operation_id FROM public."
                            + (
                                "workspace_owner_transfer_operation_receipts "
                                "WHERE project_id=:project_id"
                                if label == "owner_transfer"
                                else "workspace_configuration_operation_receipts "
                                "WHERE operation_id=:operation_id"
                            )
                        ),
                        {
                            "project_id": str(project.id),
                            "operation_id": str(operation_ids[-1]),
                        }
                        if label == "configuration_update"
                        else {"project_id": str(project.id)},
                    )
                    receipt_row = receipt.first()
                    if label == "owner_transfer" and receipt_row is not None:
                        aliases[str(receipt_row[0])] = "$owner_transfer_operation"
                    actor = await observer.get(User, actors[actor_alias].id)
                    prior_role = actor.role
                    before = {
                        "owner_id": aliases[str(current_project.owner_id)],
                        "project_name": current_project.name,
                        "receipt_present": receipt_row is not None,
                        "receipt_operation_id": (
                            aliases.get(str(receipt_row[0]), str(receipt_row[0]))
                            if receipt_row is not None
                            else None
                        ),
                        "actor_role_before_projection": prior_role,
                    }
                async with owner_maker.begin() as changer:
                    actor = await changer.get(User, actors[actor_alias].id)
                    actor.role = "employee"
                flipped_projects.add(project.id)
                projection_observations[label] = {
                    **before,
                    "actor_role_at_projection": "employee",
                    "accepted_actor_role": prior_role,
                    "commits_before_projection": committed_before_projection,
                    "projection_started_after_route_commit": committed_before_projection
                    > 0,
                    "role_change_committed_before_projection": True,
                }
            return await original_serialize_project(db, project, **kwargs)

        monkeypatch.setattr(projects_api, "_serialize_project", observed_serialize)
        transport = httpx.ASGITransport(app=app_module, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://legacy-capture", timeout=30
        ) as client:

            async def call(actor_alias, method, path, *, label, params=None, body=None):
                with _observe_activity(runtime_engine) as activity:
                    active_activity[0] = activity
                    active_request[0] = label
                    try:
                        response, response_body = await _request(
                            client,
                            capture,
                            aliases,
                            actors,
                            tokens,
                            actor_alias=actor_alias,
                            method=method,
                            path=path,
                            params=params,
                            body=body,
                        )
                    finally:
                        active_activity[0] = None
                        active_request[0] = None
                capture["samples"][-1]["label"] = label
                capture["samples"][-1]["activity"] = _activity_summary(activity)
                return response, response_body, activity

            search_expected = {
                "super_admin": {
                    "projects": {"search_owner", "search_shared", "search_hidden"},
                    "tasks": {
                        "owner_task",
                        "employee_task",
                        "reviewer_task",
                        "viewer_task",
                        "unassigned_task",
                        "hidden_task",
                    },
                    "members": {
                        "super_admin",
                        "project_admin",
                        "employee",
                        "reviewer",
                        "viewer",
                        "hidden_employee",
                    },
                },
                "project_admin": {
                    "projects": {"search_owner", "search_shared"},
                    "tasks": {"owner_task"},
                    "members": {"project_admin", "employee", "reviewer", "viewer"},
                },
                "employee": {
                    "projects": {"search_shared"},
                    "tasks": {"employee_task"},
                    "members": {"project_admin", "employee", "reviewer", "viewer"},
                },
                "reviewer": {
                    "projects": {"search_shared"},
                    "tasks": {"reviewer_task", "viewer_task"},
                    "members": {"project_admin", "employee", "reviewer", "viewer"},
                },
                "viewer": {
                    "projects": {"search_shared"},
                    "tasks": {"viewer_task"},
                    "members": {"project_admin", "employee", "reviewer", "viewer"},
                },
                "hidden_employee": {
                    "projects": {"search_hidden"},
                    "tasks": {"hidden_task"},
                    "members": {"hidden_employee"},
                },
            }
            global_search = {}
            for actor_alias, expected in search_expected.items():
                response, body, activity = await call(
                    actor_alias,
                    "GET",
                    _SEARCH_PATH,
                    label=f"global_search_{actor_alias}",
                    params={"q": marker, "limit": 20},
                )
                assert response.status_code == 200, response.text
                actual = {
                    "projects": {
                        name
                        for row in body["projects"]
                        for name in projects
                        if str(projects[name].id) == row["id"]
                    },
                    "tasks": {
                        name
                        for row in body["tasks"]
                        for name in tasks
                        if str(tasks[name].id) == row["id"]
                    },
                    "members": {
                        name
                        for row in body["members"]
                        for name, actor in actors.items()
                        if str(actor.id) == row["id"]
                    },
                }
                assert actual == expected, {"actor": actor_alias, "actual": actual}
                assert {row["name"] for row in body["datasets"]} == {
                    f"{marker} public dataset"
                }
                global_search[actor_alias] = {
                    **{key: sorted(value) for key, value in actual.items()},
                    "datasets": ["dataset_search"],
                    "activity": _activity_summary(activity),
                }
            capture["observations"]["global_search_scopes"] = global_search

            async with owner_maker.begin() as db:
                shared = await db.get(Project, projects["search_shared"].id)
                shared.video_collaboration = {"enabled": True}
                video = await create_task(
                    db, project_id=shared.id, display_id=f"T-VID-{suffix}"
                )
                video.file_type = "video"
                video.file_name = f"{marker}-collaborative-video.mp4"
                video.created_at = video.updated_at = _FIXED_TIME
                task_ids.append(video.id)
                aliases[str(video.id)] = "$task_collaborative_video"
                aliases[video.display_id] = "$display_collaborative_video"
                video_id = video.id
            collaborative = {}
            for actor_alias in ("project_admin", "employee", "viewer"):
                response, body, activity = await call(
                    actor_alias,
                    "GET",
                    _SEARCH_PATH,
                    label=f"global_search_collaborative_video_{actor_alias}",
                    params={"q": marker, "limit": 20},
                )
                assert response.status_code == 200, response.text
                assert str(video_id) in {item["id"] for item in body["tasks"]}
                collaborative[actor_alias] = _activity_summary(activity)

            async with owner_maker.begin() as db:
                shared = await db.get(Project, projects["search_shared"].id)
                shared.video_collaboration = {"enabled": "invalid-boolean"}
            response, _body, invalid_activity = await call(
                "employee",
                "GET",
                _SEARCH_PATH,
                label="global_search_invalid_collaboration_employee",
                params={"q": marker, "limit": 20},
            )
            assert response.status_code == 500, response.text
            invalid_summary = _activity_summary(invalid_activity)
            assert invalid_summary["read_table_mentions"].get("datasets", 0) == 0
            response, _body, privileged_activity = await call(
                "super_admin",
                "GET",
                _SEARCH_PATH,
                label="global_search_invalid_collaboration_super_admin",
                params={"q": marker, "limit": 20},
            )
            assert response.status_code == 200, response.text
            capture["observations"]["collaborative_video"] = {
                "visible_unassigned_video": collaborative,
                "invalid_boolean_employee": invalid_summary,
                "invalid_boolean_super_admin": _activity_summary(privileged_activity),
            }
            async with owner_maker.begin() as db:
                shared = await db.get(Project, projects["search_shared"].id)
                shared.video_collaboration = {}

            class_path = f"/api/v1/projects/{projects['rename'].id}/classes/rename"
            response, body, noop_activity = await call(
                "project_admin",
                "POST",
                class_path,
                label="class_rename_noop",
                body={"old_name": "Car", "new_name": "Car", "tool_unit_id": "bbox"},
            )
            assert response.status_code == 200, response.text
            assert noop_activity["commits"] == []
            async with owner_maker() as observer:
                current = await observer.get(Project, projects["rename"].id)
                class_names = await observer.execute(
                    text(
                        "SELECT tool_unit_id, class_name FROM public.annotations "
                        "WHERE project_id=:project_id ORDER BY tool_unit_id"
                    ),
                    {"project_id": str(projects["rename"].id)},
                )
                noop_state = {
                    "bbox": [
                        item["name"]
                        for item in current.tool_bindings["bbox"]["classes"]
                    ],
                    "region": [
                        item["name"]
                        for item in current.tool_bindings["region"]["classes"]
                    ],
                    "annotation_classes": [list(row) for row in class_names.all()],
                }
            assert noop_state["bbox"] == ["Car"]
            assert noop_state["region"] == ["Car"]
            assert {row[1] for row in noop_state["annotation_classes"]} == {"Car"}

            response, body, changed_activity = await call(
                "project_admin",
                "POST",
                class_path,
                label="class_rename_changed",
                body={"old_name": "Car", "new_name": "Truck", "tool_unit_id": "bbox"},
            )
            assert response.status_code == 200, response.text
            assert len(changed_activity["commits"]) == 1
            async with owner_maker() as observer:
                current = await observer.get(Project, projects["rename"].id)
                class_names = await observer.execute(
                    text(
                        "SELECT tool_unit_id, class_name FROM public.annotations "
                        "WHERE project_id=:project_id ORDER BY tool_unit_id"
                    ),
                    {"project_id": str(projects["rename"].id)},
                )
                changed_state = {
                    "bbox": [
                        item["name"]
                        for item in current.tool_bindings["bbox"]["classes"]
                    ],
                    "region": [
                        item["name"]
                        for item in current.tool_bindings["region"]["classes"]
                    ],
                    "annotation_classes": [list(row) for row in class_names.all()],
                }
            assert changed_state["bbox"] == ["Truck"]
            assert changed_state["region"] == ["Car"]
            assert {row[1] for row in changed_state["annotation_classes"]} == {"Truck"}
            capture["observations"]["class_rename"] = {
                "no_op": {
                    "state": noop_state,
                    "activity": _activity_summary(noop_activity),
                    "projection": projection_observations["class_rename_noop"],
                },
                "changed": {
                    "state": changed_state,
                    "activity": _activity_summary(changed_activity),
                    "projection": projection_observations["class_rename_changed"],
                },
            }

            response, transfer_body, transfer_activity = await call(
                "super_admin",
                "POST",
                f"/api/v1/projects/{projects['transfer'].id}/transfer",
                label="owner_transfer",
                body={"new_owner_id": str(actors["project_admin"].id)},
            )
            assert response.status_code == 200, response.text
            assert transfer_body["owner_id"] == str(actors["project_admin"].id)
            transfer_observation = projection_observations["owner_transfer"]
            async with owner_maker() as observer:
                transfer_actor_after_response = await observer.get(
                    User, actors["super_admin"].id
                )
            assert transfer_observation["receipt_present"]
            assert transfer_observation["projection_started_after_route_commit"]
            assert transfer_observation["accepted_actor_role"] == "super_admin"
            assert transfer_observation["actor_role_at_projection"] == "employee"
            assert transfer_actor_after_response.role == "employee"
            transfer_observation["actor_role_after_response"] = (
                transfer_actor_after_response.role
            )
            transfer_observation["response_status_after_role_change"] = (
                response.status_code
            )
            capture["observations"]["owner_transfer_readback"] = {
                **transfer_observation,
                "activity": _activity_summary(transfer_activity),
                "response_owner_alias": "$actor_project_admin",
            }

            response, configuration_body, configuration_activity = await call(
                "project_admin",
                "PATCH",
                f"/api/v1/projects/{projects['configuration'].id}",
                label="configuration_update",
                body={"name": f"Configuration projection updated {marker}"},
            )
            assert response.status_code == 200, response.text
            assert (
                configuration_body["name"]
                == f"Configuration projection updated {marker}"
            )
            configuration_observation = projection_observations["configuration_update"]
            async with owner_maker() as observer:
                configuration_actor_after_response = await observer.get(
                    User, actors["project_admin"].id
                )
            assert configuration_observation["receipt_present"]
            assert configuration_observation["projection_started_after_route_commit"]
            assert configuration_observation["accepted_actor_role"] == "project_admin"
            assert configuration_observation["actor_role_at_projection"] == "employee"
            assert (
                configuration_observation["project_name"] == configuration_body["name"]
            )
            assert configuration_actor_after_response.role == "employee"
            configuration_observation["actor_role_after_response"] = (
                configuration_actor_after_response.role
            )
            configuration_observation["response_status_after_role_change"] = (
                response.status_code
            )
            capture["observations"]["configuration_postcommit_projection"] = {
                **configuration_observation,
                "operation_ids": [
                    aliases.get(str(value), str(value)) for value in operation_ids
                ],
                "activity": _activity_summary(configuration_activity),
                "response_project_alias": "$project_configuration",
            }

    finally:
        try:
            await _cleanup_owned(
                owner_maker,
                actor_ids=actor_ids,
                project_ids=project_ids,
                dataset_ids=[item.id for item in datasets],
                batch_ids=batch_ids,
                task_ids=task_ids,
                annotation_ids=annotation_ids,
                operation_ids=operation_ids,
            )
        finally:
            if previous_db_override is _MISSING:
                app_module.dependency_overrides.pop(get_db, None)
            else:
                app_module.dependency_overrides[get_db] = previous_db_override
            await runtime_engine.dispose()
    _write_capture(capture)
