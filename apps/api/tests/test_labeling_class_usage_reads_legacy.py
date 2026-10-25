"""Capture actual legacy class-usage reads before any owner extraction."""

from __future__ import annotations

import base64
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
from sqlalchemy import delete, event, func, or_, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
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
        or os.environ.get("AAP_LABELING_CLASS_USAGE_LEGACY_CAPTURE") != "1",
        reason="requires an explicit owned legacy class-usage capture",
    ),
]

_ROOT = Path(__file__).resolve().parents[3]
_BASE_COMMIT = "ea3b1f87bb792d0ab55cd41828927929283ec421"
_PROJECTS_PATH = "/api/v1/projects"
_HEADERS = ("content-type", "location", "www-authenticate", "retry-after")
_SOURCE_FILES = """
apps/api/app/api/v1/projects.py
apps/api/app/deps.py
apps/api/app/services/project_access.py
apps/api/app/schemas/project.py
apps/api/app/db/models/annotation.py
apps/api/app/db/models/project.py
apps/api/app/db/models/project_member.py
apps/api/app/db/models/task.py
apps/api/app/db/models/user.py
apps/api/app/core/security.py
apps/api/app/config.py
apps/api/app/main.py
apps/api/app/services/identity_account_lock_data.py
apps/api/app/services/identity_contracts.py
apps/api/app/services/identity_gateway.py
apps/api/app/db/base.py
apps/api/tests/factory.py
apps/workspace/src/aap_workspace/access_contract.py
""".split()
_MISSING = object()


def _capture_path() -> Path:
    raw = os.environ.get("AAP_LABELING_CLASS_USAGE_LEGACY_OUTPUT")
    assert raw, "AAP_LABELING_CLASS_USAGE_LEGACY_OUTPUT is required"
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
    source_hashes = {}
    for relative in _SOURCE_FILES:
        baseline = subprocess.run(
            ["git", "show", f"{_BASE_COMMIT}:{relative}"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
        ).stdout
        current = (_ROOT / relative).read_bytes()
        assert baseline == current, relative
        source_hashes[relative] = hashlib.sha256(current).hexdigest()
    return {
        "captured_head": head,
        "pre_extraction_base": _BASE_COMMIT,
        "capture_driver_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "runtime_source_sha256": source_hashes,
    }


def _normalise(value, aliases: dict[str, str]):
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
        return {name: _normalise(item, aliases) for name, item in value.items()}
    return value


def _query_phase(statement: str) -> str:
    compact = " ".join(statement.casefold().split())
    if (
        "count(annotations.id)" in compact
        and "group by annotations.class_name" in compact
    ):
        return "annotation_class_counts"
    if "select annotations.attributes" in compact:
        return "annotation_attribute_rows"
    return "other"


def _is_class_count_query(statement) -> bool:
    compact = " ".join(str(statement).casefold().split())
    return (
        "count(annotations.id)" in compact
        and "group by annotations.class_name" in compact
    )


class _CaptureAsyncSession(AsyncSession):
    async def execute(self, statement, *args, **kwargs):
        result = await super().execute(statement, *args, **kwargs)
        capture = self.sync_session.info.get("labeling_class_usage_capture")
        if (
            capture is not None
            and not capture["class_query_completed"]
            and _is_class_count_query(statement)
        ):
            capture["class_query_completed"] = True
            interleave = capture.get("after_class_query")
            if interleave is not None:
                await interleave()
        return result


@contextmanager
def _observe_runtime(engine, state):
    def observe_sql(_connection, _cursor, statement, _parameters, _context, _many):
        current = state["current"]
        if current is None:
            return
        observation = current["observation"]
        normalized = " ".join(statement.split())
        observation["sql"].append(
            {
                "sequence": len(observation["sql"]) + 1,
                "phase": _query_phase(statement),
                "statement": normalized,
                "sha256": hashlib.sha256(normalized.encode()).hexdigest(),
            }
        )
        if normalized.lstrip().casefold().startswith(("insert", "update", "delete")):
            observation["dml_statements"].append(normalized)

    def after_commit(session):
        current = state["current"]
        if session.bind is engine.sync_engine and current is not None:
            current["observation"]["transaction_events"].append("commit")

    def after_rollback(session):
        current = state["current"]
        if session.bind is engine.sync_engine and current is not None:
            current["observation"]["transaction_events"].append("rollback")

    def handle_error(context):
        current = state["current"]
        if current is None:
            return
        statement = " ".join((context.statement or "").split())
        original = context.original_exception
        current["observation"]["db_errors"].append(
            {
                "exception_type": type(original).__name__,
                "sqlstate": getattr(original, "sqlstate", None)
                or getattr(original, "pgcode", None),
                "statement": statement,
                "statement_sha256": hashlib.sha256(statement.encode()).hexdigest(),
            }
        )

    event.listen(engine.sync_engine, "before_cursor_execute", observe_sql)
    event.listen(Session, "after_commit", after_commit)
    event.listen(Session, "after_rollback", after_rollback)
    event.listen(engine.sync_engine, "handle_error", handle_error)
    try:
        yield
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe_sql)
        event.remove(Session, "after_commit", after_commit)
        event.remove(Session, "after_rollback", after_rollback)
        event.remove(engine.sync_engine, "handle_error", handle_error)


async def _create_member(
    db, *, project: Project, user: User, assigner: User, role: str
):
    member = ProjectMember(
        project_id=project.id,
        user_id=user.id,
        assigned_by=assigner.id,
        role=role,
        version=1,
        assigned_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
    )
    db.add(member)
    await db.flush()
    return member


async def _create_annotation(
    db,
    *,
    project_id: uuid.UUID | None,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    class_name: str,
    attributes,
    source: str = "manual",
    active: bool = True,
    cancelled: bool = False,
    tool_unit_id: str = "bbox",
) -> Annotation:
    annotation = Annotation(
        project_id=project_id,
        task_id=task_id,
        user_id=user_id,
        source=source,
        annotation_type="bbox",
        tool_unit_id=tool_unit_id,
        class_name=class_name,
        geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        attributes={},
        is_active=active,
        was_cancelled=cancelled,
    )
    db.add(annotation)
    await db.flush()
    await db.execute(
        text("UPDATE annotations SET attributes=CAST(:payload AS jsonb) WHERE id=:id"),
        {
            "payload": json.dumps(
                attributes, ensure_ascii=False, separators=(",", ":")
            ),
            "id": annotation.id,
        },
    )
    return annotation


async def _cleanup_owned(maker, *, actor_ids, project_ids, task_ids) -> None:
    async with maker.begin() as db:
        if project_ids or task_ids:
            conditions = []
            if project_ids:
                conditions.append(Annotation.project_id.in_(project_ids))
            if task_ids:
                conditions.append(Annotation.task_id.in_(task_ids))
            await db.execute(delete(Annotation).where(or_(*conditions)))
        if task_ids:
            await db.execute(delete(Task).where(Task.id.in_(task_ids)))
        if project_ids:
            await db.execute(
                delete(ProjectMember).where(ProjectMember.project_id.in_(project_ids))
            )
            await db.execute(delete(Project).where(Project.id.in_(project_ids)))
        if actor_ids:
            await db.execute(delete(User).where(User.id.in_(actor_ids)))


def _write_capture(path: Path, capture: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(capture, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


async def _request(
    client: httpx.AsyncClient,
    capture: dict,
    state: dict,
    aliases: dict[str, str],
    actors: dict[str, User],
    tokens: dict[str, str],
    token_roles: dict[str, str],
    *,
    label: str,
    actor_alias: str,
    project_id: uuid.UUID,
    interleave=None,
):
    observation = {
        "sql": [],
        "dml_statements": [],
        "db_errors": [],
        "transaction_events": [],
    }
    if interleave is not None:
        observation["interleaving"] = interleave
    request_context = {
        "observation": observation,
        "class_query_completed": False,
        "after_class_query": state.pop("next_interleave", None),
    }
    request_path = f"{_PROJECTS_PATH}/{project_id}/class-usage"
    state["current"] = request_context
    try:
        response = await client.get(
            request_path,
            headers={"Authorization": f"Bearer {tokens[actor_alias]}"},
        )
    finally:
        state["current"] = None

    try:
        body = response.json() if response.content else None
    except ValueError:
        body = response.text
    actor = actors[actor_alias]
    sample = {
        "label": label,
        "observation": observation,
        "request": {
            "method": "GET",
            "path": _normalise(request_path, aliases),
            "actor": {
                "alias": actor_alias,
                "id": aliases[str(actor.id)],
                "current_role": actor.role,
                "token_role": token_roles[actor_alias],
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
            "body": _normalise(body, aliases),
            "raw_body": {
                "byte_length": len(response.content),
                "sha256": hashlib.sha256(response.content).hexdigest(),
                "base64": base64.b64encode(response.content).decode("ascii"),
            },
        },
    }
    capture["samples"].append(sample)
    assert observation["dml_statements"] == [], label
    assert "commit" not in observation["transaction_events"], label
    phases = [entry["phase"] for entry in observation["sql"]]
    if (
        response.status_code == 200
        and isinstance(body, dict)
        and {
            "classes",
            "attributes",
        }.issubset(body)
    ):
        class_position = phases.index("annotation_class_counts")
        attribute_position = phases.index("annotation_attribute_rows")
        assert class_position < attribute_position, label
        assert phases.count("annotation_class_counts") == 1, label
        assert phases.count("annotation_attribute_rows") == 1, label
    if label == "large_json_number_attribute_read":
        assert phases.count("annotation_class_counts") == 1, label
        assert phases.count("annotation_attribute_rows") == 1, label
        assert phases.index("annotation_class_counts") < phases.index(
            "annotation_attribute_rows"
        ), label
    if interleave is not None:
        assert request_context["class_query_completed"], label
        assert interleave["committed"] is True, label
        class_queries = [
            entry["sequence"]
            for entry in observation["sql"]
            if entry["phase"] == "annotation_class_counts"
        ]
        attribute_queries = [
            entry["sequence"]
            for entry in observation["sql"]
            if entry["phase"] == "annotation_attribute_rows"
        ]
        assert len(class_queries) == len(attribute_queries) == 1, label
        assert class_queries[0] < attribute_queries[0], label
        interleave["class_query_sequence"] = class_queries[0]
        interleave["attribute_query_sequence"] = attribute_queries[0]
    return sample


async def test_capture_legacy_labeling_class_usage(
    test_engine, app_module, monkeypatch
):
    from app.config import settings
    from app.deps import get_db

    capture_path = _capture_path()
    source = _source_metadata()
    runtime_url = _legacy_runtime_url(test_engine)
    runtime_engine = create_async_engine(runtime_url, hide_parameters=True)
    owner_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    runtime_maker = async_sessionmaker(
        runtime_engine, expire_on_commit=False, class_=_CaptureAsyncSession
    )
    suffix = uuid.uuid4().hex[:10]
    aliases: dict[str, str] = {suffix: "$capture_suffix"}
    actors: dict[str, User] = {}
    tokens: dict[str, str] = {}
    token_roles: dict[str, str] = {}
    projects: dict[str, Project] = {}
    tasks: dict[str, Task] = {}
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    task_ids: list[uuid.UUID] = []
    capture = {
        "capture_kind": "actual-legacy-labeling-class-usage-http",
        "source": source,
        "runtime_identity": None,
        "observations": {},
        "samples": [],
    }
    state = {"current": None}
    previous_db_override = app_module.dependency_overrides.get(get_db, _MISSING)
    previous_identity = {
        name: getattr(settings, name)
        for name in (
            "identity_url",
            "identity_auth_database_url",
            "identity_auth_database_url_file",
        )
    }
    capture_ready = False

    async def request_db():
        async with runtime_maker() as db:
            if state["current"] is not None:
                db.sync_session.info["labeling_class_usage_capture"] = state["current"]
            yield db

    async def make_actor(db, alias: str, role: str, name: str, *, active=True):
        email = f"class-usage-{alias}-{suffix}@example.test"
        user = await create_user(db, role, email, name)
        user.is_active = active
        actors[alias] = user
        tokens[alias] = create_access_token(subject=str(user.id), role=role)
        token_roles[alias] = role
        actor_ids.append(user.id)
        aliases[str(user.id)] = f"$actor_{alias}"
        aliases[email] = f"$email_{alias}"
        return user

    async def make_project(db, alias: str, owner: User, name: str):
        project = await create_project(db, owner_id=owner.id, name=name)
        project.name = name
        projects[alias] = project
        project_ids.append(project.id)
        aliases[str(project.id)] = f"$project_{alias}"
        aliases[project.display_id] = f"$display_{alias}"
        return project

    async def make_task(db, alias: str, project: Project):
        task = await create_task(db, project_id=project.id)
        tasks[alias] = task
        task_ids.append(task.id)
        aliases[str(task.id)] = f"$task_{alias}"
        aliases[task.display_id] = f"$task_display_{alias}"
        return task

    try:
        assert not capture_path.exists(), f"refusing to overwrite {capture_path}"
        async with owner_maker() as db:
            clean_counts = {
                "users": await db.scalar(select(func.count()).select_from(User)),
                "projects": await db.scalar(select(func.count()).select_from(Project)),
                "project_members": await db.scalar(
                    select(func.count()).select_from(ProjectMember)
                ),
                "tasks": await db.scalar(select(func.count()).select_from(Task)),
                "annotations": await db.scalar(
                    select(func.count()).select_from(Annotation)
                ),
            }
            assert clean_counts == {
                "users": 0,
                "projects": 0,
                "project_members": 0,
                "tasks": 0,
                "annotations": 0,
            }

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
            await make_actor(db, "super_admin", "super_admin", "Capture Super Admin")
            owner = await make_actor(db, "owner", "project_admin", "Project Owner")
            nonowner_admin = await make_actor(
                db, "nonowner_admin", "project_admin", "Non-owner Admin Member"
            )
            physical_owner = await make_actor(
                db, "physical_owner", "employee", "Employee Physical Owner"
            )
            visible_member = await make_actor(
                db, "visible_member", "employee", "Visible Employee Member"
            )
            await make_actor(db, "outsider", "employee", "Outside Employee")
            await make_actor(
                db, "inactive", "project_admin", "Inactive Admin Owner", active=False
            )
            stale_role = await make_actor(
                db, "stale_role", "employee", "Changed Platform Role"
            )
            stale_role.role = "project_admin"

            invalid_platform_seed = {"seeded": False, "reason": None}
            try:
                async with db.begin_nested():
                    invalid_platform = await make_actor(
                        db,
                        "invalid_platform_role",
                        "historic_platform_role",
                        "Historic Platform Role",
                    )
            except IntegrityError:
                invalid_platform_seed["reason"] = (
                    "database rejected an unknown platform role"
                )
            else:
                invalid_platform_seed["seeded"] = True

            invalid_member_role_actor = await make_actor(
                db, "invalid_member_role", "employee", "Invalid Project Role Member"
            )

            target = await make_project(db, "target", owner, "Class usage target")
            empty = await make_project(db, "empty", owner, "Class usage empty")
            foreign = await make_project(
                db, "foreign", owner, "Foreign annotation scope"
            )
            employee_owner_without = await make_project(
                db,
                "employee_owner_without_membership",
                physical_owner,
                "Employee owner without membership",
            )
            employee_owner_with = await make_project(
                db,
                "employee_owner_with_membership",
                physical_owner,
                "Employee owner with membership",
            )
            decode_failure_project = await make_project(
                db, "decode_failure", owner, "JSON decode failure"
            )
            target_task = await make_task(db, "target", target)
            foreign_task = await make_task(db, "foreign", foreign)
            decode_failure_task = await make_task(
                db, "decode_failure", decode_failure_project
            )

            await _create_member(
                db,
                project=target,
                user=nonowner_admin,
                assigner=owner,
                role="annotator",
            )
            await _create_member(
                db, project=target, user=visible_member, assigner=owner, role="reviewer"
            )
            await _create_member(
                db,
                project=employee_owner_with,
                user=physical_owner,
                assigner=physical_owner,
                role="annotator",
            )

            classes_and_attributes = (
                (
                    "car",
                    {
                        "color": "red",
                        "nested": {"child": "value"},
                        "_system": {"source": "capture"},
                        "雪": "white",
                    },
                    "manual",
                    True,
                    False,
                    "bbox",
                ),
                (
                    "car",
                    {"color": "blue", "model": {"id": "run-1"}},
                    "ai",
                    True,
                    False,
                    "bbox",
                ),
                (
                    "truck",
                    ["ignored", {"nested": "array item"}],
                    "manual",
                    True,
                    False,
                    "unconfigured-unit",
                ),
                (
                    "",
                    {"empty_class_key": "included", "color": "green"},
                    "manual",
                    True,
                    False,
                    "bbox",
                ),
                (
                    "inactive-only",
                    {"inactive_key": "ignored"},
                    "manual",
                    False,
                    False,
                    "bbox",
                ),
                (
                    "active-cancelled",
                    {"cancelled_key": "included"},
                    "ai",
                    True,
                    True,
                    "bbox",
                ),
                ("scalar-string", "not an object", "manual", True, False, "bbox"),
                ("scalar-number", 17, "manual", True, False, "bbox"),
                ("json-null", None, "manual", True, False, "bbox"),
            )
            for (
                class_name,
                attributes,
                source_name,
                active,
                cancelled,
                tool_unit_id,
            ) in classes_and_attributes:
                await _create_annotation(
                    db,
                    project_id=target.id,
                    task_id=target_task.id,
                    user_id=owner.id,
                    class_name=class_name,
                    attributes=attributes,
                    source=source_name,
                    active=active,
                    cancelled=cancelled,
                    tool_unit_id=tool_unit_id,
                )

            await _create_annotation(
                db,
                project_id=foreign.id,
                task_id=foreign_task.id,
                user_id=owner.id,
                class_name="foreign-project-only",
                attributes={"foreign_project_key": "excluded"},
            )
            await _create_annotation(
                db,
                project_id=None,
                task_id=target_task.id,
                user_id=owner.id,
                class_name="null-project-only",
                attributes={"null_project_key": "excluded"},
            )

            foreign_task_target_seed = {"seeded": False, "reason": None}
            try:
                async with db.begin_nested():
                    await _create_annotation(
                        db,
                        project_id=target.id,
                        task_id=foreign_task.id,
                        user_id=owner.id,
                        class_name="target-project-foreign-task",
                        attributes={"foreign_task_key": "scoped by annotation project"},
                        tool_unit_id="unconfigured-unit",
                    )
            except DBAPIError as exc:
                foreign_task_target_seed["reason"] = type(exc).__name__
            else:
                foreign_task_target_seed["seeded"] = True

            invalid_membership_seed = {"seeded": False, "reason": None}
            try:
                async with db.begin_nested():
                    await _create_member(
                        db,
                        project=target,
                        user=invalid_member_role_actor,
                        assigner=owner,
                        role="historic_project_role",
                    )
            except IntegrityError:
                invalid_membership_seed["reason"] = (
                    "database rejected an unknown project membership role"
                )
            else:
                invalid_membership_seed["seeded"] = True

            large_json_seed = {"seeded": False, "reason": None, "digits": 5000}
            try:
                async with db.begin_nested():
                    large_numeric = Annotation(
                        project_id=decode_failure_project.id,
                        task_id=decode_failure_task.id,
                        user_id=owner.id,
                        source="manual",
                        annotation_type="bbox",
                        tool_unit_id="bbox",
                        class_name="large-json-number",
                        geometry={
                            "type": "bbox",
                            "x": 0.1,
                            "y": 0.2,
                            "w": 0.3,
                            "h": 0.4,
                        },
                        attributes={},
                    )
                    db.add(large_numeric)
                    await db.flush()
                    await db.execute(
                        text(
                            "UPDATE annotations SET attributes=CAST(:payload AS jsonb) "
                            "WHERE id=:id"
                        ),
                        {
                            "payload": "9" * large_json_seed["digits"],
                            "id": large_numeric.id,
                        },
                    )
            except DBAPIError as exc:
                large_json_seed["reason"] = type(exc).__name__
            else:
                large_json_seed["seeded"] = True

            capture["observations"]["seed_eligibility"] = {
                "invalid_platform_role": invalid_platform_seed,
                "invalid_project_membership_role": invalid_membership_seed,
                "foreign_task_with_target_annotation_project": foreign_task_target_seed,
                "large_json_numeric_decode_case": large_json_seed,
            }
            if invalid_platform_seed["seeded"]:
                aliases[str(invalid_platform.id)] = "$actor_invalid_platform_role"
                aliases[invalid_platform.email] = "$email_invalid_platform_role"
            missing_project_id = uuid.uuid4()
            aliases[str(missing_project_id)] = "$missing_project"

        for name in previous_identity:
            monkeypatch.setattr(settings, name, "")
        monkeypatch.setattr(settings, "audit_async", True)
        monkeypatch.setattr(
            "app.middleware.audit._enqueue_audit", lambda _payload: True
        )
        app_module.dependency_overrides[get_db] = request_db

        transport = httpx.ASGITransport(app=app_module, raise_app_exceptions=False)
        with _observe_runtime(runtime_engine, state):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                aggregate = await _request(
                    client,
                    capture,
                    state,
                    aliases,
                    actors,
                    tokens,
                    token_roles,
                    label="target_project_owner_aggregation",
                    actor_alias="owner",
                    project_id=target.id,
                )
                await _request(
                    client,
                    capture,
                    state,
                    aliases,
                    actors,
                    tokens,
                    token_roles,
                    label="empty_project_owner",
                    actor_alias="owner",
                    project_id=empty.id,
                )

                race = {"requested": True, "committed": False}

                async def commit_between_aggregate_reads():
                    async with owner_maker.begin() as writer:
                        await _create_annotation(
                            writer,
                            project_id=target.id,
                            task_id=target_task.id,
                            user_id=owner.id,
                            class_name="",
                            attributes={"between_reads": "committed second session"},
                        )
                    race["committed"] = True
                    race["writer"] = "independent owner_maker session"
                    race["committed_after"] = "class_counts query completed"
                    race["attributes_key"] = "between_reads"

                # The dependency/session bridge reads this callback from the
                # active request context immediately before the first query.
                state["next_interleave"] = commit_between_aggregate_reads
                race_sample = await _request(
                    client,
                    capture,
                    state,
                    aliases,
                    actors,
                    tokens,
                    token_roles,
                    label="class_then_attributes_second_session_commit",
                    actor_alias="owner",
                    project_id=target.id,
                    interleave=race,
                )
                assert (
                    race_sample["response"]["status"] == aggregate["response"]["status"]
                )
                assert (
                    race_sample["response"]["body"]["classes"]
                    == aggregate["response"]["body"]["classes"]
                )
                assert (
                    race_sample["response"]["body"]["attributes"].get("between_reads")
                    == 1
                )
                state.pop("next_interleave", None)

                auth_cases = [
                    ("super_admin_aggregation", "super_admin", target.id),
                    ("project_admin_owner", "owner", target.id),
                    ("nonowner_project_admin_member", "nonowner_admin", target.id),
                    (
                        "employee_physical_owner_without_membership",
                        "physical_owner",
                        employee_owner_without.id,
                    ),
                    (
                        "employee_physical_owner_with_membership",
                        "physical_owner",
                        employee_owner_with.id,
                    ),
                    ("visible_employee_member", "visible_member", target.id),
                    ("outsider", "outsider", target.id),
                    ("inactive_account", "inactive", target.id),
                    ("stale_platform_role_token", "stale_role", target.id),
                    ("missing_project", "super_admin", missing_project_id),
                ]
                if invalid_platform_seed["seeded"]:
                    auth_cases.append(
                        ("unknown_platform_role", "invalid_platform_role", target.id)
                    )
                if invalid_membership_seed["seeded"]:
                    auth_cases.append(
                        (
                            "unknown_project_membership_role",
                            "invalid_member_role",
                            target.id,
                        )
                    )
                for label, actor_alias, project_id in auth_cases:
                    await _request(
                        client,
                        capture,
                        state,
                        aliases,
                        actors,
                        tokens,
                        token_roles,
                        label=label,
                        actor_alias=actor_alias,
                        project_id=project_id,
                    )

                if large_json_seed["seeded"]:
                    await _request(
                        client,
                        capture,
                        state,
                        aliases,
                        actors,
                        tokens,
                        token_roles,
                        label="large_json_number_attribute_read",
                        actor_alias="owner",
                        project_id=decode_failure_project.id,
                    )

        capture_ready = True
    finally:
        state["current"] = None
        state.pop("next_interleave", None)
        if previous_db_override is _MISSING:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous_db_override
        for name, value in previous_identity.items():
            setattr(settings, name, value)
        await _cleanup_owned(
            owner_maker,
            actor_ids=actor_ids,
            project_ids=project_ids,
            task_ids=task_ids,
        )
        await runtime_engine.dispose()

    if capture_ready:
        _write_capture(capture_path, capture)
