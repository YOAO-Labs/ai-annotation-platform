"""Capture actual pre-extraction Project class-rename HTTP behavior."""

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
from sqlalchemy import delete, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.user import User
from tests.factory import create_project, create_task, create_user


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_PROJECT_CLASS_RENAME_LEGACY_CAPTURE") != "1",
        reason="requires the explicit owned legacy class-rename capture",
    ),
]

_ROOT = Path(__file__).resolve().parents[3]
_BASE_COMMIT = "e2019de02d821b2f202ab4bc1770b004af159084"
_ROUTE = "/api/v1/projects"
_FIXED_TIME = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
_HEADERS = ("content-type", "location", "www-authenticate", "retry-after")
_TIME_FIELDS = {"created_at", "updated_at", "project_updated_at"}
_SOURCE_FILES = """
apps/api/app/api/v1/projects.py
apps/api/app/application/project_class_rename.py
apps/api/app/application/project_workspace.py
apps/api/app/config.py
apps/api/app/core/security.py
apps/api/app/db/base.py
apps/api/app/db/enums.py
apps/api/app/db/models/annotation.py
apps/api/app/db/models/project.py
apps/api/app/db/models/project_member.py
apps/api/app/db/models/task.py
apps/api/app/db/models/user.py
apps/api/app/deps.py
apps/api/app/main.py
apps/api/app/schemas/project.py
apps/api/app/services/annotation.py
apps/api/app/services/identity_account_lock_data.py
apps/api/app/services/identity_contracts.py
apps/api/app/services/identity_gateway.py
apps/api/app/services/project_access.py
apps/api/app/services/project_workspace.py
apps/api/app/services/project_write_guard.py
apps/api/app/services/workspace_project_query.py
apps/api/app/services/workspace_project_query_data.py
apps/api/alembic/versions/0174_project_role_conversion.py
apps/api/tests/factory.py
apps/workspace/src/aap_workspace/project_queries.py
apps/workspace/src/aap_workspace/project_query_contract.py
apps/workspace/src/aap_workspace/project_query_values.py
apps/workspace/src/aap_workspace/project_query_postgres.py
apps/workspace/src/aap_workspace/access_contract.py
""".split()
_MISSING = object()


def _capture_path() -> Path:
    raw = os.environ.get("AAP_PROJECT_CLASS_RENAME_LEGACY_OUTPUT")
    assert raw, "AAP_PROJECT_CLASS_RENAME_LEGACY_OUTPUT is required"
    requested = Path(raw)
    assert requested.is_absolute(), "capture output must be an absolute path"
    worktree = (_ROOT / ".worktree").resolve()
    output = requested.resolve()
    assert worktree in output.parents, "capture output must be inside Root .worktree"
    assert output.parent.is_dir(), "capture output parent must already exist"
    assert not output.exists(), f"refusing to overwrite {output}"
    return output


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
    from tests.test_asset_storage_cleanup_runtime import _verify_postgres

    _verify_postgres(parsed)
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
    hashes = {}
    for relative in _SOURCE_FILES:
        baseline = subprocess.run(
            ["git", "show", f"{_BASE_COMMIT}:{relative}"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
        ).stdout
        current = (_ROOT / relative).read_bytes()
        assert baseline == current, relative
        hashes[relative] = hashlib.sha256(current).hexdigest()
    return {
        "captured_head": head,
        "pre_extraction_base": _BASE_COMMIT,
        "capture_driver_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "runtime_source_sha256": hashes,
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


def _sql_phase(statement: str) -> str:
    compact = " ".join(statement.casefold().split())
    if compact.startswith("update annotations set class_name"):
        return "annotation_class_rename"
    if "aap_identity_v1." in compact:
        return "identity_authority"
    if "project_members" in compact:
        return "membership_authority"
    if "for update nowait" in compact and "projects" in compact:
        return "project_exclusive_lock"
    if "projects" in compact and compact.lstrip().startswith("select"):
        return "project_read_or_projection"
    if compact.lstrip().startswith(("insert", "update", "delete")):
        return "other_write"
    return "other_read"


@contextmanager
def _observe_runtime(engine, state):
    activity = {
        "sql": [],
        "dml_statements": [],
        "transaction_events": [],
        "db_errors": [],
    }

    def observe_sql(_connection, _cursor, statement, _parameters, _context, _many):
        if state["current"] is None:
            return
        normalized = " ".join(statement.split())
        entry = {
            "sequence": len(activity["sql"]) + 1,
            "phase": _sql_phase(statement),
            "statement": normalized,
            "sha256": hashlib.sha256(normalized.encode()).hexdigest(),
        }
        activity["sql"].append(entry)
        if normalized.casefold().startswith(("insert", "update", "delete")):
            activity["dml_statements"].append(normalized)

    def after_commit(session):
        if session.bind is engine.sync_engine and state["current"] is not None:
            activity["transaction_events"].append("commit")

    def after_rollback(session):
        if session.bind is engine.sync_engine and state["current"] is not None:
            activity["transaction_events"].append("rollback")

    def handle_error(context):
        if state["current"] is None:
            return
        statement = " ".join((context.statement or "").split())
        error = context.original_exception
        activity["db_errors"].append(
            {
                "exception_type": type(error).__name__,
                "sqlstate": getattr(error, "sqlstate", None)
                or getattr(error, "pgcode", None),
                "statement": statement,
                "statement_sha256": hashlib.sha256(statement.encode()).hexdigest(),
            }
        )

    event.listen(engine.sync_engine, "before_cursor_execute", observe_sql)
    event.listen(Session, "after_commit", after_commit)
    event.listen(Session, "after_rollback", after_rollback)
    event.listen(engine.sync_engine, "handle_error", handle_error)
    try:
        yield activity
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe_sql)
        event.remove(Session, "after_commit", after_commit)
        event.remove(Session, "after_rollback", after_rollback)
        event.remove(engine.sync_engine, "handle_error", handle_error)


def _activity_summary(activity: dict) -> dict:
    kinds = Counter(
        entry["statement"].lstrip().split(None, 1)[0].casefold()
        for entry in activity["sql"]
        if entry["statement"].strip()
    )
    return {
        **activity,
        "sql_by_kind": dict(sorted(kinds.items())),
        "commit_count": activity["transaction_events"].count("commit"),
        "rollback_count": activity["transaction_events"].count("rollback"),
    }


async def _request(
    client,
    capture,
    state,
    aliases,
    actors,
    tokens,
    *,
    label,
    actor_alias,
    project_id,
    old_name,
    new_name,
    tool_unit_id=None,
    token_role=None,
    token_alias=None,
):
    path = f"{_ROUTE}/{project_id}/classes/rename"
    body = {"old_name": old_name, "new_name": new_name}
    if tool_unit_id is not None:
        body["tool_unit_id"] = tool_unit_id
    observation = {
        "sql": [],
        "dml_statements": [],
        "transaction_events": [],
        "db_errors": [],
        "session_at_dependency_end": None,
    }
    request_context = {"label": label, "observation": observation}
    state["current"] = request_context
    try:
        response = await client.post(
            path,
            headers={"Authorization": f"Bearer {tokens[token_alias or actor_alias]}"},
            json=body,
        )
    finally:
        state["current"] = None
    try:
        response_body = response.json() if response.content else None
    except ValueError:
        response_body = response.text
    actor = actors[actor_alias]
    sample = {
        "label": label,
        "request": {
            "method": "POST",
            "path": _normalise(path, aliases),
            "body": _normalise(body, aliases),
            "actor": {
                "alias": actor_alias,
                "id": aliases[str(actor.id)],
                "current_role": actor.role,
                "token_role": token_role or actor.role,
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
        "observation": _activity_summary(observation),
    }
    capture["samples"].append(sample)
    return response, response_body, sample


async def _snapshot(maker, project_id):
    async with maker() as db:
        project = await db.get(Project, project_id)
        if project is None:
            return None
        annotations = (
            await db.scalars(
                select(Annotation)
                .where(Annotation.project_id == project_id)
                .order_by(Annotation.tool_unit_id, Annotation.id)
            )
        ).all()
        return {
            "tool_bindings": project.tool_bindings,
            "project_updated_at": project.updated_at.isoformat()
            if project.updated_at
            else None,
            "annotations": [
                {
                    "id": str(row.id),
                    "tool_unit_id": row.tool_unit_id,
                    "class_name": row.class_name,
                    "source": row.source,
                    "active": row.is_active,
                    "cancelled": row.was_cancelled,
                    "version": row.version,
                    "updated_at": row.updated_at.isoformat()
                    if row.updated_at
                    else None,
                }
                for row in annotations
            ],
        }


async def _cleanup_owned(maker, *, actor_ids, project_ids, task_ids):
    async with maker.begin() as db:
        if project_ids:
            await db.execute(
                delete(Annotation).where(Annotation.project_id.in_(project_ids))
            )
        if task_ids:
            await db.execute(delete(Task).where(Task.id.in_(task_ids)))
        if project_ids:
            await db.execute(
                delete(ProjectMember).where(ProjectMember.project_id.in_(project_ids))
            )
            await db.execute(delete(Project).where(Project.id.in_(project_ids)))
        if actor_ids:
            await db.execute(delete(User).where(User.id.in_(actor_ids)))


def _binding(names, *, enabled=True):
    return {
        "enabled": enabled,
        "classes": [{"name": name, "color": "#6699cc"} for name in names],
        "attribute_schema": {"fields": []},
    }


async def test_capture_pre_extraction_project_class_rename(
    test_engine, app_module, monkeypatch
):
    from app.config import settings
    from app.deps import get_db

    capture_path = _capture_path()
    source = _source_metadata()
    runtime_url = _legacy_runtime_url(test_engine)
    runtime_engine = create_async_engine(runtime_url, hide_parameters=True)
    owner_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    runtime_maker = async_sessionmaker(runtime_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:10]
    aliases = {suffix: "$capture_suffix"}
    actors: dict[str, User] = {}
    tokens: dict[str, str] = {}
    projects: dict[str, Project] = {}
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    task_ids: list[uuid.UUID] = []
    state = {"current": None}
    capture = {
        "capture_kind": "actual-pre-extraction-project-class-rename-http",
        "source": source,
        "runtime_identity": None,
        "samples": [],
    }
    previous_db_override = app_module.dependency_overrides.get(get_db, _MISSING)
    setting_names = (
        "identity_url",
        "identity_auth_database_url",
        "identity_auth_database_url_file",
        "workspace_project_query_url",
        "workspace_project_query_database_url",
        "workspace_project_query_database_url_file",
        "workspace_membership_control_token",
        "workspace_membership_control_token_file",
    )
    for name in setting_names:
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "audit_async", True)
    monkeypatch.setattr("app.middleware.audit._enqueue_audit", lambda _payload: True)

    async def request_db():
        async with runtime_maker() as db:
            try:
                yield db
            finally:
                current = state["current"]
                if current is not None:
                    current["observation"]["session_at_dependency_end"] = {
                        "in_transaction": db.in_transaction(),
                        "in_nested_transaction": db.in_nested_transaction(),
                    }

    app_module.dependency_overrides[get_db] = request_db

    async def make_actor(db, alias, role, *, active=True):
        email = f"class-rename-{alias}-{suffix}@example.test"
        user = await create_user(db, role, email, f"Class Rename {alias}")
        user.is_active = active
        actors[alias] = user
        tokens[alias] = create_access_token(subject=str(user.id), role=role)
        actor_ids.append(user.id)
        aliases[str(user.id)] = f"$actor_{alias}"
        aliases[email] = f"$email_{alias}"
        return user

    async def make_project(db, alias, owner, bindings):
        project = await create_project(
            db,
            owner_id=owner.id,
            name=f"Class rename {alias} {suffix}",
            type_label="Class rename capture",
        )
        project.tool_bindings = bindings
        project.created_at = project.updated_at = _FIXED_TIME
        projects[alias] = project
        project_ids.append(project.id)
        aliases[str(project.id)] = f"$project_{alias}"
        aliases[project.display_id] = f"$display_{alias}"
        return project

    async def add_annotation(
        db,
        project,
        actor,
        *,
        unit,
        class_name="Car",
        source_name="manual",
        active=True,
        cancelled=False,
    ):
        task = await create_task(db, project_id=project.id)
        task_ids.append(task.id)
        aliases[str(task.id)] = f"$task_{len(task_ids)}"
        row = Annotation(
            id=uuid.uuid4(),
            task_id=task.id,
            project_id=project.id,
            user_id=actor.id,
            source=source_name,
            annotation_type="bbox",
            tool_unit_id=unit,
            class_name=class_name,
            geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
            attributes={},
            is_active=active,
            was_cancelled=cancelled,
            version=7,
            created_at=_FIXED_TIME,
            updated_at=_FIXED_TIME,
        )
        db.add(row)
        await db.flush()
        aliases[str(row.id)] = f"$annotation_{len(aliases)}"
        return row

    capture_ready = False
    try:
        assert not capture_path.exists(), f"refusing to overwrite {capture_path}"
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
            "container_id": os.environ["AAP_MODULE_DATA_PG_CONTAINER_ID"],
            "container_name": os.environ["AAP_MODULE_DATA_PG_CONTAINER_NAME"],
            "container_identity": os.environ["AAP_MODULE_DATA_PG_CONTAINER_IDENTITY"],
        }

        async with owner_maker.begin() as db:
            super_admin = await make_actor(db, "super_admin", "super_admin")
            owner = await make_actor(db, "owner", "project_admin")
            nonowner_admin = await make_actor(db, "nonowner_admin", "project_admin")
            employee_owner = await make_actor(db, "employee_owner", "employee")
            employee_member_owner = await make_actor(
                db, "employee_member_owner", "employee"
            )
            visible_member = await make_actor(db, "visible_member", "employee")
            await make_actor(db, "outsider", "employee")
            await make_actor(db, "legacy_reviewer", "reviewer")
            inactive_owner = await make_actor(
                db, "inactive_owner", "project_admin", active=False
            )

            scoped = await make_project(
                db,
                "scoped",
                owner,
                {
                    "bbox": _binding(["Car"]),
                    "region": _binding(["Car"]),
                    "disabled_unit": _binding(["Car"], enabled=False),
                },
            )
            all_units = await make_project(
                db,
                "all_units",
                owner,
                {
                    "bbox": _binding(["Car"]),
                    "region": _binding(["Car"]),
                    "disabled_unit": _binding(["Car"], enabled=False),
                },
            )
            await make_project(
                db, "conflict", owner, {"bbox": _binding(["Car", "Truck"])}
            )
            await make_project(db, "missing", owner, {"bbox": _binding(["Car"])})
            await make_project(
                db, "noop_unknown_unit", owner, {"bbox": _binding(["Car"])}
            )
            await make_project(
                db, "noop_missing_old", owner, {"bbox": _binding(["Car"])}
            )
            await make_project(
                db, "changed_unknown_unit", owner, {"bbox": _binding(["Car"])}
            )
            projection_failure = await make_project(
                db, "projection_failure", owner, {"bbox": _binding(["Car"])}
            )
            caller_rollback_project = await make_project(
                db, "caller_rollback", owner, {"bbox": _binding(["Car"])}
            )
            await make_project(db, "super_admin", owner, {"bbox": _binding(["Car"])})
            pa_member_project = await make_project(
                db, "nonowner_admin_member", owner, {"bbox": _binding(["Car"])}
            )
            await make_project(
                db,
                "employee_physical_owner",
                employee_owner,
                {"bbox": _binding(["Car"])},
            )
            employee_member_owner_project = await make_project(
                db,
                "employee_owner_with_membership",
                employee_member_owner,
                {"bbox": _binding(["Car"])},
            )
            visible_member_project = await make_project(
                db, "visible_member", owner, {"bbox": _binding(["Car"])}
            )
            await make_project(db, "outsider", owner, {"bbox": _binding(["Car"])})
            await make_project(
                db, "inactive_owner", inactive_owner, {"bbox": _binding(["Car"])}
            )
            await make_project(
                db, "stale_role_claim", owner, {"bbox": _binding(["Car"])}
            )

            for project, member, role in (
                (pa_member_project, nonowner_admin, "viewer"),
                (employee_member_owner_project, employee_member_owner, "annotator"),
                (visible_member_project, visible_member, "annotator"),
            ):
                db.add(
                    ProjectMember(
                        project_id=project.id,
                        user_id=member.id,
                        assigned_by=super_admin.id,
                        role=role,
                        version=1,
                        assigned_at=_FIXED_TIME,
                    )
                )
            # The employee owner with a membership also demonstrates that a
            # platform owner field alone does not confer the managed-write role.
            await add_annotation(db, scoped, owner, unit="bbox")
            await add_annotation(db, scoped, owner, unit="region", source_name="ai")
            await add_annotation(db, scoped, owner, unit="disabled_unit", active=False)
            await add_annotation(db, scoped, owner, unit="bbox", cancelled=True)
            await add_annotation(db, scoped, owner, unit="region", class_name="Dog")
            for unit in ("bbox", "region", "disabled_unit"):
                await add_annotation(db, all_units, owner, unit=unit)
            await add_annotation(
                db, projection_failure, owner, unit="bbox", source_name="ai"
            )
            await add_annotation(db, caller_rollback_project, owner, unit="bbox")

        tokens["owner_stale_role_claim"] = create_access_token(
            subject=str(owner.id), role="employee"
        )

        async def call(
            client,
            label,
            actor_alias,
            project_alias=None,
            *,
            project_id=None,
            old="Car",
            new="Car",
            unit=None,
            token_role=None,
            token_alias=None,
        ):
            target_id = (
                project_id if project_id is not None else projects[project_alias].id
            )
            if project_alias is None:
                aliases[str(target_id)] = f"$project_{label}"
            with _observe_runtime(runtime_engine, state) as activity:
                response, body, sample = await _request(
                    client,
                    capture,
                    state,
                    aliases,
                    actors,
                    tokens,
                    label=label,
                    actor_alias=actor_alias,
                    project_id=target_id,
                    old_name=old,
                    new_name=new,
                    tool_unit_id=unit,
                    token_role=token_role,
                    token_alias=token_alias,
                )
            dependency_session = sample["observation"]["session_at_dependency_end"]
            sample["observation"] = _activity_summary(activity)
            sample["observation"]["session_at_dependency_end"] = dependency_session
            if project_alias is not None:
                snapshot = await _snapshot(owner_maker, target_id)
                sample["post_response_state"] = _normalise(snapshot, aliases)
            return response, body, sample

        transport = httpx.ASGITransport(app=app_module, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://legacy-class-rename-capture",
            timeout=30,
        ) as client:
            response, body, sample = await call(
                client,
                "scoped_rename_cross_unit_annotations",
                "owner",
                "scoped",
                old="Car",
                new="Truck",
                unit="bbox",
            )
            assert response.status_code == 200, (response.status_code, body)
            assert sample["observation"]["commit_count"] == 1
            scoped_state = sample["post_response_state"]
            assert (
                scoped_state["tool_bindings"]["bbox"]["classes"][0]["name"] == "Truck"
            )
            assert (
                scoped_state["tool_bindings"]["region"]["classes"][0]["name"] == "Car"
            )
            assert {row["class_name"] for row in scoped_state["annotations"]} == {
                "Dog",
                "Truck",
            }
            assert all(row["version"] == 7 for row in scoped_state["annotations"])
            assert all(
                row["updated_at"].startswith("2026-01-15")
                for row in scoped_state["annotations"]
            )
            scoped_phases = [entry["phase"] for entry in sample["observation"]["sql"]]
            assert "project_exclusive_lock" in scoped_phases
            assert scoped_phases.index("project_exclusive_lock") < scoped_phases.index(
                "annotation_class_rename"
            )

            response, body, sample = await call(
                client,
                "all_units_including_disabled",
                "owner",
                "all_units",
                old="Car",
                new="Vehicle",
            )
            assert response.status_code == 200, (response.status_code, body)
            assert sample["observation"]["commit_count"] == 1
            assert all(
                sample["post_response_state"]["tool_bindings"][unit]["classes"][0][
                    "name"
                ]
                == "Vehicle"
                for unit in ("bbox", "region", "disabled_unit")
            )

            response, body, sample = await call(
                client,
                "target_name_conflict",
                "owner",
                "conflict",
                old="Car",
                new="Truck",
                unit="bbox",
            )
            assert response.status_code == 409, (response.status_code, body)
            assert sample["observation"]["commit_count"] == 0
            response, body, sample = await call(
                client,
                "missing_old_class",
                "owner",
                "missing",
                old="Missing",
                new="Truck",
            )
            assert response.status_code == 404, (response.status_code, body)
            response, body, _sample = await call(
                client, "blank_old_name", "owner", "missing", old="  ", new="Truck"
            )
            assert response.status_code == 400, (response.status_code, body)
            response, body, _sample = await call(
                client, "blank_new_name", "owner", "missing", old="Car", new=" \t "
            )
            assert response.status_code == 400, (response.status_code, body)
            response, body, _sample = await call(
                client,
                "changed_unknown_tool_unit",
                "owner",
                "changed_unknown_unit",
                old="Car",
                new="Truck",
                unit="missing_unit",
            )
            assert response.status_code == 404, (response.status_code, body)
            response, body, sample = await call(
                client,
                "noop_unknown_tool_unit",
                "owner",
                "noop_unknown_unit",
                old=" Car ",
                new="Car",
                unit="missing_unit",
            )
            assert response.status_code == 200, (response.status_code, body)
            assert sample["observation"]["commit_count"] == 0
            assert "project_exclusive_lock" not in [
                entry["phase"] for entry in sample["observation"]["sql"]
            ]
            assert sample["observation"]["dml_statements"] == []
            response, body, sample = await call(
                client,
                "noop_missing_old_class",
                "owner",
                "noop_missing_old",
                old="Missing",
                new="Missing",
            )
            assert response.status_code == 200, (response.status_code, body)
            assert sample["observation"]["commit_count"] == 0
            assert "project_exclusive_lock" not in [
                entry["phase"] for entry in sample["observation"]["sql"]
            ]
            assert sample["observation"]["dml_statements"] == []

            response, body, sample = await call(
                client,
                "super_admin_noop",
                "super_admin",
                "super_admin",
                old="Car",
                new="Car",
            )
            assert response.status_code == 200, (response.status_code, body)
            await call(
                client,
                "nonowner_admin_member",
                "nonowner_admin",
                "nonowner_admin_member",
            )
            await call(
                client,
                "employee_physical_owner_without_membership",
                "employee_owner",
                "employee_physical_owner",
            )
            await call(
                client,
                "employee_owner_with_membership",
                "employee_member_owner",
                "employee_owner_with_membership",
            )
            await call(
                client, "visible_employee_member", "visible_member", "visible_member"
            )
            await call(client, "outsider_employee", "outsider", "outsider")
            response, body, _sample = await call(
                client,
                "blank_name_unauthorized_actor",
                "outsider",
                "outsider",
                old="  ",
                new="Truck",
            )
            assert response.status_code == 404, (response.status_code, body)
            response, body, _sample = await call(
                client,
                "unknown_legacy_platform_role",
                "legacy_reviewer",
                "outsider",
            )
            assert response.status_code == 401, (response.status_code, body)
            await call(
                client,
                "missing_project",
                "owner",
                project_id=uuid.uuid4(),
            )
            await call(client, "inactive_owner", "inactive_owner", "inactive_owner")
            await call(
                client,
                "stale_role_claim",
                "owner",
                "stale_role_claim",
                token_role="employee",
                token_alias="owner_stale_role_claim",
            )

            from app.application.project_class_rename import rename_project_class
            from app.services.identity_contracts import IdentityActorFacts

            caller_actor = IdentityActorFacts(
                id=owner.id,
                name=owner.name,
                email=owner.email,
                role=owner.role,
                is_active=owner.is_active,
                weekly_target_default=owner.weekly_target_default,
            )
            direct_activity = {
                "sql": [],
                "dml_statements": [],
                "transaction_events": [],
                "db_errors": [],
            }
            state["current"] = {
                "label": "caller_rollback_probe",
                "observation": direct_activity,
            }
            try:
                with _observe_runtime(runtime_engine, state) as activity:
                    async with runtime_maker() as caller:
                        changed, evidence = await rename_project_class(
                            caller,
                            actor=caller_actor,
                            project_id=caller_rollback_project.id,
                            old_name="Car",
                            new_name="Truck",
                            tool_unit_id="bbox",
                        )
                        workflow_return_count = len(activity["sql"])
                        with caller.no_autoflush:
                            working_project = await caller.get(
                                Project, caller_rollback_project.id
                            )
                        assert working_project is not None
                        aliases[str(evidence.operation_id)] = (
                            "$operation_class_rename_direct"
                        )
                        workflow_return = {
                            "changed": changed,
                            "operation_id": aliases[str(evidence.operation_id)],
                            "sql_statement_count": workflow_return_count,
                            "sql_phases": [
                                entry["phase"]
                                for entry in activity["sql"][:workflow_return_count]
                            ],
                            "project_dirty_before_caller_flush": (
                                working_project in caller.dirty
                            ),
                            "tool_bindings_before_caller_flush": json.loads(
                                json.dumps(working_project.tool_bindings)
                            ),
                            "in_transaction": caller.in_transaction(),
                        }
                        with caller.no_autoflush:
                            same_session_annotations = (
                                await caller.scalars(
                                    select(Annotation)
                                    .where(
                                        Annotation.project_id
                                        == caller_rollback_project.id
                                    )
                                    .order_by(Annotation.id)
                                )
                            ).all()
                        before_flush_count = len(activity["sql"])
                        await caller.flush()
                        after_flush_count = len(activity["sql"])
                        same_session_state = {
                            "tool_bindings": json.loads(
                                json.dumps(working_project.tool_bindings)
                            ),
                            "annotation_class_names": [
                                row.class_name for row in same_session_annotations
                            ],
                            "in_transaction": caller.in_transaction(),
                        }
                        visible_before_rollback = await _snapshot(
                            owner_maker, caller_rollback_project.id
                        )
                        await caller.rollback()
                        visible_after_rollback = await _snapshot(
                            owner_maker, caller_rollback_project.id
                        )
                        capture["caller_rollback_probe"] = {
                            "workflow_return": workflow_return,
                            "same_session_after_explicit_caller_flush": {
                                **same_session_state,
                                "sql_statement_count": after_flush_count,
                                "same_session_read_sql_count": (
                                    before_flush_count - workflow_return_count
                                ),
                                "caller_flush_sql_count": (
                                    after_flush_count - before_flush_count
                                ),
                            },
                            "independent_owner_before_rollback": _normalise(
                                visible_before_rollback, aliases
                            ),
                            "independent_owner_after_rollback": _normalise(
                                visible_after_rollback, aliases
                            ),
                            "caller_rollback_transaction_events": list(
                                activity["transaction_events"]
                            ),
                            "ordered_sql": activity["sql"],
                            "dml_statements": activity["dml_statements"],
                        }
                        assert changed
                        assert (
                            same_session_state["tool_bindings"]["bbox"]["classes"][0][
                                "name"
                            ]
                            == "Truck"
                        )
                        assert same_session_state["annotation_class_names"] == ["Truck"]
                        assert caller.in_transaction() is False
                        assert (
                            visible_before_rollback["tool_bindings"]["bbox"]["classes"][
                                0
                            ]["name"]
                            == "Car"
                        )
                        assert (
                            visible_before_rollback["annotations"][0]["class_name"]
                            == "Car"
                        )
                        assert (
                            visible_after_rollback["tool_bindings"]["bbox"]["classes"][
                                0
                            ]["name"]
                            == "Car"
                        )
                        assert (
                            visible_after_rollback["annotations"][0]["class_name"]
                            == "Car"
                        )
            finally:
                state["current"] = None

            monkeypatch.setattr(
                settings, "workspace_project_query_url", "http://127.0.0.1:1"
            )
            monkeypatch.setattr(
                settings, "workspace_membership_control_token", "capture-token"
            )
            response, body, sample = await call(
                client,
                "commit_then_projection_unavailable",
                "owner",
                "projection_failure",
                old="Car",
                new="Truck",
                unit="bbox",
            )
            assert response.status_code == 503, (response.status_code, body)
            assert sample["observation"]["commit_count"] == 1
            assert (
                sample["post_response_state"]["tool_bindings"]["bbox"]["classes"][0][
                    "name"
                ]
                == "Truck"
            )

        capture_ready = True
    finally:
        app_module.dependency_overrides.pop(get_db, None)
        if previous_db_override is not _MISSING:
            app_module.dependency_overrides[get_db] = previous_db_override
        try:
            await _cleanup_owned(
                owner_maker,
                actor_ids=actor_ids,
                project_ids=project_ids,
                task_ids=task_ids,
            )
        finally:
            await runtime_engine.dispose()

    if capture_ready:
        with capture_path.open("x", encoding="utf-8") as stream:
            json.dump(capture, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
