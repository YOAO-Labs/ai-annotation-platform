"""Capture actual pre-extraction Project access responses and read phases."""

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
from fastapi import HTTPException
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.factory import create_project, create_user

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_WORKSPACE_ACCESS_CAPTURE") != "1",
        reason="requires the explicit owned legacy Project-access capture",
    ),
]

_ROOT = Path(__file__).resolve().parents[3]
_BASE_COMMIT = "bb23672cdb52ebdcc70fb5197761351052df9d5d"
_CAPTURE_PATH = _ROOT / ".worktree/workspace-access-reads-legacy.json"
_PROJECT_PATH = "/api/v1/projects"
_HEADERS = ("content-type", "location", "www-authenticate", "retry-after")
_SOURCE_FILES = """
apps/api/app/api/v1/projects.py
apps/api/app/api/v1/router.py
apps/api/app/main.py
apps/api/app/deps.py
apps/api/app/core/security.py
apps/api/app/config.py
apps/api/app/db/enums.py
apps/api/app/db/models/project.py
apps/api/app/db/models/project_member.py
apps/api/app/db/models/user.py
apps/api/app/schemas/project.py
apps/api/app/services/project_access.py
apps/api/app/services/identity_account_lock_data.py
apps/api/app/services/identity_contracts.py
apps/api/app/services/identity_gateway.py
apps/api/app/services/identity_session_data.py
apps/identity/src/aap_identity/actor_contract.py
apps/identity/src/aap_identity/actor_protocol.schema.json
apps/identity/src/aap_identity/actor_reader.py
apps/identity/src/aap_identity/authenticator.py
apps/identity/src/aap_identity/contract.py
apps/identity/src/aap_identity/security.py
apps/api/alembic/sql/0212_asset_association_reads.sql
apps/api/alembic/sql/0212_asset_association_reads.schema.json
apps/api/alembic/sql/0235_identity_admission.sql
apps/api/alembic/sql/0235_identity_admission.schema.json
apps/api/alembic/sql/0250_identity_actor_context.sql
apps/api/alembic/sql/0250_identity_actor_context.schema.json
apps/workspace/src/aap_workspace/access_contract.py
apps/api/tests/factory.py
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


def _datetime_observation(value):
    if value is None:
        return None
    if type(value) is not datetime:
        return {"type": type(value).__name__}
    if value == datetime.max:
        return {"sentinel": "max", "tzinfo": None}
    if value == datetime.min:
        return {"sentinel": "min", "tzinfo": None}
    return {"isoformat": value.isoformat(), "tzinfo": str(value.tzinfo)}


def _member_snapshot(member, aliases, *, phase: str) -> dict:
    return {
        "phase": phase,
        "id": aliases.get(str(member.id), str(member.id)),
        "project_id": aliases.get(str(member.project_id), str(member.project_id)),
        "user_id": aliases.get(str(member.user_id), str(member.user_id)),
        "role": member.role,
        "version": member.version,
        "assigned_at": _datetime_observation(member.assigned_at),
        "updated_at": _datetime_observation(member.updated_at),
    }


def _read_phase(statement: str, observation: dict) -> str:
    compact = " ".join(statement.casefold().split())
    direct = observation.get("phase_context") == "direct_resolver"
    if "aap_identity_v1.read_actor_context" in compact:
        return "identity_actor_context"
    if "aap_identity_v1.read_jwt_principal" in compact:
        return "identity_jwt_principal"
    if "aap_identity_v1.read_actor_facts" in compact:
        count = observation.setdefault("_actor_status_reads", 0) + 1
        observation["_actor_status_reads"] = count
        if direct:
            return "direct_actor_status"
        return "visibility_actor_status" if count == 1 else "endpoint_actor_status"
    if "from projects" in compact:
        count = observation.setdefault("_project_reads", 0) + 1
        observation["_project_reads"] = count
        if direct:
            return "direct_project_lookup" if count == 1 else "direct_project_refresh"
        return {
            1: "visibility_project_lookup",
            2: "visibility_project_refresh",
            3: "endpoint_project_refresh",
        }.get(count, f"project_select_{count}")
    if "from project_members" in compact:
        count = observation.setdefault("_member_reads", 0) + 1
        observation["_member_reads"] = count
        if direct:
            return "direct_membership_lookup"
        return (
            "visibility_membership_refresh"
            if count == 1
            else "endpoint_membership_refresh"
        )
    return "other"


@contextmanager
def _observe_runtime(engine, state):
    def observe_sql(_connection, _cursor, statement, _parameters, _context, _many):
        current = state["current"]
        if current is None:
            return
        normalized = " ".join(statement.split())
        current["sql"].append(
            {
                "sequence": len(current["sql"]) + 1,
                "phase": _read_phase(statement, current),
                "statement": normalized,
                "sha256": hashlib.sha256(normalized.encode()).hexdigest(),
            }
        )

    def after_commit(session):
        current = state["current"]
        if session.bind is engine.sync_engine and current is not None:
            current["transaction_events"].append("commit")

    def after_rollback(session):
        current = state["current"]
        if session.bind is engine.sync_engine and current is not None:
            current["transaction_events"].append("rollback")

    def handle_error(context):
        current = state["current"]
        if current is None:
            return
        statement = context.statement or ""
        original = context.original_exception
        current["db_errors"].append(
            {
                "exception_type": type(original).__name__,
                "sqlstate": getattr(original, "sqlstate", None)
                or getattr(original, "pgcode", None),
                "statement_sha256": hashlib.sha256(
                    " ".join(statement.split()).encode()
                ).hexdigest(),
            }
        )

    def member_loaded(session, instance):
        current = state["member_current"]
        if (
            session.bind is engine.sync_engine
            and isinstance(instance, ProjectMember)
            and current is not None
        ):
            current.append(_member_snapshot(instance, state["aliases"], phase="load"))

    def member_refreshed(instance, _context, _attrs):
        current = state["member_current"]
        session = Session.object_session(instance)
        if (
            session is not None
            and session.bind is engine.sync_engine
            and current is not None
        ):
            current.append(
                _member_snapshot(instance, state["aliases"], phase="refresh")
            )

    event.listen(engine.sync_engine, "before_cursor_execute", observe_sql)
    event.listen(Session, "after_commit", after_commit)
    event.listen(Session, "after_rollback", after_rollback)
    event.listen(engine.sync_engine, "handle_error", handle_error)
    event.listen(Session, "loaded_as_persistent", member_loaded)
    event.listen(ProjectMember, "refresh", member_refreshed)
    try:
        yield
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe_sql)
        event.remove(Session, "after_commit", after_commit)
        event.remove(Session, "after_rollback", after_rollback)
        event.remove(engine.sync_engine, "handle_error", handle_error)
        event.remove(Session, "loaded_as_persistent", member_loaded)
        event.remove(ProjectMember, "refresh", member_refreshed)


async def _request(
    client: httpx.AsyncClient,
    capture: dict,
    aliases: dict[str, str],
    actors: dict[str, User],
    tokens: dict[str, str],
    token_roles: dict[str, str],
    *,
    label: str,
    actor_alias: str,
    path: str,
    state: dict,
):
    sample = {
        "label": label,
        "observation": {"sql": [], "db_errors": [], "transaction_events": []},
    }
    state["current"] = sample["observation"]
    state["member_current"] = sample["observation"].setdefault("loaded_member_rows", [])
    response = await client.get(
        path,
        headers={"Authorization": f"Bearer {tokens[actor_alias]}"},
    )
    state["current"] = None
    state["member_current"] = None
    for internal_key in ("_actor_status_reads", "_project_reads", "_member_reads"):
        sample["observation"].pop(internal_key, None)
    try:
        body = response.json() if response.content else None
    except ValueError:
        body = response.text
    actor = actors[actor_alias]
    sample["request"] = {
        "method": "GET",
        "path": _normalise(path, aliases),
        "actor": {
            "alias": actor_alias,
            "id": aliases[str(actor.id)],
            "current_role": actor.role,
            "token_role": token_roles[actor_alias],
            "active": actor.is_active,
        },
    }
    sample["response"] = {
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
    }
    capture["samples"].append(sample)
    return response, body, sample


async def _create_member(
    db,
    *,
    project: Project,
    user: User,
    assigned_by: User,
    role: str,
    version: int,
    day: int,
    member_alias: str,
    aliases: dict[str, str],
    temporal_edge: str | None = None,
) -> ProjectMember:
    member = ProjectMember(
        project_id=project.id,
        user_id=user.id,
        role=role,
        assigned_by=assigned_by.id,
        version=version,
        assigned_at=datetime(2026, 1, day, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, day, tzinfo=timezone.utc),
    )
    db.add(member)
    await db.flush()
    if temporal_edge == "infinity":
        await db.execute(
            text(
                "UPDATE project_members "
                "SET assigned_at='infinity'::timestamptz, "
                "updated_at='-infinity'::timestamptz WHERE id=:member_id"
            ),
            {"member_id": member.id},
        )
    elif temporal_edge == "finite_assigned_at":
        await db.execute(
            text(
                "UPDATE project_members "
                "SET assigned_at=TIMESTAMPTZ '10000-01-01 00:00:00+00' "
                "WHERE id=:member_id"
            ),
            {"member_id": member.id},
        )
    elif temporal_edge == "finite_updated_at":
        await db.execute(
            text(
                "UPDATE project_members "
                "SET updated_at=TIMESTAMPTZ '10000-01-01 00:00:00+00' "
                "WHERE id=:member_id"
            ),
            {"member_id": member.id},
        )
    aliases[str(member.id)] = f"$membership_{member_alias}"
    return member


async def _cleanup_owned(maker, *, actor_ids, project_ids) -> None:
    async with maker.begin() as db:
        if project_ids:
            await db.execute(
                delete(ProjectMember).where(ProjectMember.project_id.in_(project_ids))
            )
            await db.execute(delete(Project).where(Project.id.in_(project_ids)))
        if actor_ids:
            await db.execute(delete(User).where(User.id.in_(actor_ids)))


def _write_capture(capture: dict) -> None:
    _CAPTURE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _CAPTURE_PATH.open("x", encoding="utf-8") as stream:
        json.dump(capture, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


async def _resolver_caller_probe(
    *,
    runtime_maker,
    owner_maker,
    actor: User,
    project: Project,
    pending_user: User,
    assigned_by: User,
    no_autoflush: bool,
    aliases: dict[str, str],
    state: dict,
):
    from app.services.project_access import resolve_project_access

    member = ProjectMember(
        id=uuid.uuid4(),
        project_id=project.id,
        user_id=pending_user.id,
        role="annotator",
        assigned_by=assigned_by.id,
        version=1,
        assigned_at=datetime(2026, 1, 20, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 20, tzinfo=timezone.utc),
    )
    aliases[str(member.id)] = "$membership_pending_probe"
    async with runtime_maker() as db:
        observation = {
            "sql": [],
            "db_errors": [],
            "transaction_events": [],
            "phase_context": "direct_resolver",
        }
        state["current"] = observation
        state["member_current"] = observation.setdefault("loaded_member_rows", [])
        project_row = await db.get(Project, project.id)
        db.add(member)
        try:
            if no_autoflush:
                with db.no_autoflush:
                    access = await resolve_project_access(
                        db, actor_id=actor.id, project=project_row
                    )
            else:
                access = await resolve_project_access(
                    db, actor_id=actor.id, project=project_row
                )
            result = {
                "status": 200,
                "access": {
                    "project_role": access.project_role,
                    "membership_id": aliases.get(
                        str(access.membership_id), str(access.membership_id)
                    ),
                    "membership_version": access.membership_version,
                    "access_kind": access.access_kind,
                    "is_manager": access.is_manager,
                },
            }
        except HTTPException as exc:
            result = {"status": exc.status_code, "detail": exc.detail}
        transaction_open = db.in_transaction()
        async with owner_maker() as observer:
            committed = await observer.get(ProjectMember, member.id) is not None
        await db.rollback()
        state["current"] = None
        state["member_current"] = None
        for internal_key in (
            "_actor_status_reads",
            "_project_reads",
            "_member_reads",
        ):
            observation.pop(internal_key, None)
    result.update(
        {
            "no_autoflush": no_autoflush,
            "caller_transaction_open_after_read": transaction_open,
            "visible_from_owner_before_caller_rollback": committed,
            "pending_user_id": aliases[str(pending_user.id)],
            "caller": "resolve_project_access direct invocation; require_project_visible bypassed",
            "observation": observation,
        }
    )
    return result


async def test_capture_legacy_project_access_reads(
    test_engine, app_module, monkeypatch
):
    from app.config import settings
    from app.deps import get_db

    assert not _CAPTURE_PATH.exists(), f"refusing to overwrite {_CAPTURE_PATH}"
    source = _source_metadata()
    runtime_url = _legacy_runtime_url(test_engine)
    runtime_parsed = make_url(runtime_url)
    from tests.test_asset_storage_cleanup_runtime import _verify_postgres

    _verify_postgres(runtime_parsed)
    owner_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    runtime_engine = create_async_engine(runtime_url, hide_parameters=True)
    runtime_maker = async_sessionmaker(runtime_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:10]
    aliases: dict[str, str] = {suffix: "$capture_suffix"}
    actors: dict[str, User] = {}
    tokens: dict[str, str] = {}
    token_roles: dict[str, str] = {}
    projects: dict[str, Project] = {}
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    capture = {
        "capture_kind": "actual-pre-extraction-project-access-http",
        "source": source,
        "runtime_identity": None,
        "samples": [],
        "observations": {},
    }
    state = {"current": None, "member_current": None, "aliases": aliases}
    previous_db_override = app_module.dependency_overrides.get(get_db, _MISSING)

    async def request_db():
        async with runtime_maker() as db:
            yield db

    async def make_actor(db, alias: str, role: str, name: str, *, active=True):
        email = f"access-{alias}-{suffix}@example.test"
        user = await create_user(db, role, email, name)
        actor_ids.append(user.id)
        actors[alias] = user
        tokens[alias] = create_access_token(subject=str(user.id), role=role)
        token_roles[alias] = role
        aliases[str(user.id)] = f"$actor_{alias}"
        aliases[email] = f"$email_{alias}"
        if not active:
            user.is_active = False
        return user

    async def make_project(db, alias: str, owner: User, name: str):
        project = await create_project(db, owner_id=owner.id, name=name)
        project.name = name
        projects[alias] = project
        project_ids.append(project.id)
        aliases[str(project.id)] = f"$project_{alias}"
        aliases[project.display_id] = f"$display_{alias}"
        return project

    previous_identity = {
        name: getattr(settings, name)
        for name in (
            "identity_url",
            "identity_auth_database_url",
            "identity_auth_database_url_file",
        )
    }
    capture_ready = False

    try:
        async with owner_maker() as db:
            clean_counts = {
                "users": await db.scalar(select(func.count()).select_from(User)),
                "projects": await db.scalar(select(func.count()).select_from(Project)),
                "project_members": await db.scalar(
                    select(func.count()).select_from(ProjectMember)
                ),
            }
            assert clean_counts == {"users": 0, "projects": 0, "project_members": 0}

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
            super_admin = await make_actor(
                db, "super_admin", "super_admin", "Capture Super Admin"
            )
            owner = await make_actor(db, "owner", "project_admin", "Project Owner")
            admin_member = await make_actor(
                db, "project_admin_member", "project_admin", "Project Admin Member"
            )
            employee_reviewer = await make_actor(
                db, "employee_reviewer", "employee", "Reviewer Member"
            )
            employee_annotator = await make_actor(
                db, "employee_annotator", "employee", "Annotator Member"
            )
            finite_assigned_at_actor = await make_actor(
                db,
                "finite_assigned_at",
                "employee",
                "Finite Assigned Timestamp",
            )
            finite_updated_at_actor = await make_actor(
                db,
                "finite_updated_at",
                "employee",
                "Finite Updated Timestamp",
            )
            viewer = await make_actor(db, "viewer", "viewer", "Viewer Member")
            await make_actor(db, "outsider", "employee", "Outside Employee")
            await make_actor(
                db, "inactive", "employee", "Inactive Employee", active=False
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
                        "invalid_platform",
                        "historic_platform_role",
                        "Historic Platform Role",
                    )
            except IntegrityError:
                invalid_platform_seed["reason"] = (
                    "database rejected an unknown platform role"
                )
            else:
                invalid_platform_seed["seeded"] = True

            invalid_member_actor = await make_actor(
                db, "invalid_member_role", "employee", "Invalid Membership Role"
            )
            viewer_work = await make_actor(
                db, "viewer_work_role", "viewer", "Viewer With Work Role"
            )
            pending_user = await make_actor(
                db, "pending_probe", "employee", "Pending Access Probe"
            )

            target = await make_project(db, "target", owner, "Target access project")
            sa_owned = await make_project(
                db, "super_admin_owned", super_admin, "Super-admin-owned project"
            )
            member_rows = {}
            for alias, user, role, version, day, temporal in (
                (
                    "employee_reviewer",
                    employee_reviewer,
                    "reviewer",
                    7,
                    10,
                    True,
                ),
                (
                    "employee_annotator",
                    employee_annotator,
                    "annotator",
                    2,
                    9,
                    False,
                ),
                ("viewer", viewer, "viewer", 4, 8, False),
                ("project_admin_member", admin_member, "annotator", 5, 7, False),
                ("viewer_work_role", viewer_work, "annotator", 1, 6, False),
            ):
                member_rows[alias] = await _create_member(
                    db,
                    project=target,
                    user=user,
                    assigned_by=super_admin,
                    role=role,
                    version=version,
                    day=day,
                    member_alias=alias,
                    aliases=aliases,
                    temporal_edge=temporal,
                )
            if invalid_platform_seed["seeded"]:
                aliases[str(invalid_platform.id)] = "$actor_invalid_platform"
                aliases[invalid_platform.email] = "$email_invalid_platform"

            invalid_membership_seed = {"seeded": False, "reason": None}
            try:
                async with db.begin_nested():
                    member_rows["invalid_member_role"] = await _create_member(
                        db,
                        project=target,
                        user=invalid_member_actor,
                        assigned_by=super_admin,
                        role="historic_project_role",
                        version=1,
                        day=5,
                        member_alias="invalid_member_role",
                        aliases=aliases,
                    )
            except IntegrityError:
                invalid_membership_seed["reason"] = (
                    "database rejected an unknown project membership role"
                )
            else:
                invalid_membership_seed["seeded"] = True

            finite_temporal_seeds = {}
            temporal_role = (
                "historic_project_role"
                if invalid_membership_seed["seeded"]
                else "reviewer"
            )
            for alias, actor, edge, day, role in (
                (
                    "finite_assigned_at",
                    finite_assigned_at_actor,
                    "finite_assigned_at",
                    3,
                    temporal_role,
                ),
                (
                    "finite_updated_at",
                    finite_updated_at_actor,
                    "finite_updated_at",
                    2,
                    "reviewer",
                ),
            ):
                try:
                    async with db.begin_nested():
                        member = await _create_member(
                            db,
                            project=target,
                            user=actor,
                            assigned_by=super_admin,
                            role=role,
                            version=1,
                            day=day,
                            member_alias=alias,
                            aliases=aliases,
                            temporal_edge=edge,
                        )
                        server_times = (
                            (
                                await db.execute(
                                    text(
                                        "SELECT assigned_at::text AS assigned_at, "
                                        "updated_at::text AS updated_at "
                                        "FROM project_members WHERE id=:member_id"
                                    ),
                                    {"member_id": member.id},
                                )
                            )
                            .mappings()
                            .one()
                        )
                except DBAPIError as exc:
                    finite_temporal_seeds[alias] = {
                        "seeded": False,
                        "error_type": type(exc).__name__,
                    }
                else:
                    member_rows[alias] = member
                    finite_temporal_seeds[alias] = {
                        "seeded": True,
                        "role": role,
                        "server_assigned_at_text": server_times["assigned_at"],
                        "server_updated_at_text": server_times["updated_at"],
                    }

            capture["observations"]["seed_eligibility"] = {
                "invalid_platform_role": invalid_platform_seed,
                "invalid_project_membership_role": invalid_membership_seed,
                "member_temporal_edge": {
                    "user": "$actor_employee_reviewer",
                    "assigned_at": "infinity",
                    "updated_at": "-infinity",
                },
                "finite_out_of_python_range_members": finite_temporal_seeds,
            }

        from app.config import settings as app_settings

        for name in previous_identity:
            monkeypatch.setattr(app_settings, name, "")
        monkeypatch.setattr(app_settings, "audit_async", True)
        monkeypatch.setattr(
            "app.middleware.audit._enqueue_audit", lambda _payload: True
        )
        app_module.dependency_overrides[get_db] = request_db

        transport = httpx.ASGITransport(app=app_module, raise_app_exceptions=False)
        with _observe_runtime(runtime_engine, state):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                cases = [
                    ("project_owner", "owner", target.id),
                    ("super_admin_nonowner", "super_admin", target.id),
                    ("super_admin_is_owner", "super_admin", sa_owned.id),
                    ("employee_reviewer_membership", "employee_reviewer", target.id),
                    ("employee_annotator_membership", "employee_annotator", target.id),
                    ("viewer_membership", "viewer", target.id),
                    (
                        "project_admin_nonowner_membership",
                        "project_admin_member",
                        target.id,
                    ),
                    ("out_of_scope_employee", "outsider", target.id),
                    ("inactive_account", "inactive", target.id),
                    ("stale_platform_role_token", "stale_role", target.id),
                    ("viewer_with_work_membership", "viewer_work_role", target.id),
                ]
                for alias, seed in finite_temporal_seeds.items():
                    if seed["seeded"]:
                        cases.append(
                            (
                                f"{alias}_out_of_python_range",
                                alias,
                                target.id,
                            )
                        )
                for label, actor_alias, project_id in cases:
                    await _request(
                        client,
                        capture,
                        aliases,
                        actors,
                        tokens,
                        token_roles,
                        label=label,
                        actor_alias=actor_alias,
                        path=f"{_PROJECT_PATH}/{project_id}/access",
                        state=state,
                    )

                missing_id = uuid.uuid4()
                aliases[str(missing_id)] = "$missing_project"
                await _request(
                    client,
                    capture,
                    aliases,
                    actors,
                    tokens,
                    token_roles,
                    label="missing_project",
                    actor_alias="super_admin",
                    path=f"{_PROJECT_PATH}/{missing_id}/access",
                    state=state,
                )
                if invalid_platform_seed["seeded"]:
                    await _request(
                        client,
                        capture,
                        aliases,
                        actors,
                        tokens,
                        token_roles,
                        label="invalid_platform_role",
                        actor_alias="invalid_platform",
                        path=f"{_PROJECT_PATH}/{target.id}/access",
                        state=state,
                    )
                if invalid_membership_seed["seeded"]:
                    await _request(
                        client,
                        capture,
                        aliases,
                        actors,
                        tokens,
                        token_roles,
                        label="invalid_project_membership_role",
                        actor_alias="invalid_member_role",
                        path=f"{_PROJECT_PATH}/{target.id}/access",
                        state=state,
                    )

        with _observe_runtime(runtime_engine, state):
            default_probe = await _resolver_caller_probe(
                runtime_maker=runtime_maker,
                owner_maker=owner_maker,
                actor=pending_user,
                project=target,
                pending_user=pending_user,
                assigned_by=super_admin,
                no_autoflush=False,
                aliases=aliases,
                state=state,
            )
            no_autoflush_probe = await _resolver_caller_probe(
                runtime_maker=runtime_maker,
                owner_maker=owner_maker,
                actor=pending_user,
                project=target,
                pending_user=pending_user,
                assigned_by=super_admin,
                no_autoflush=True,
                aliases=aliases,
                state=state,
            )
        direct = {
            "caller": "resolve_project_access direct invocation; visibility dependency bypassed",
            "default": default_probe,
            "no_autoflush": no_autoflush_probe,
            "observation": {
                "default": default_probe["observation"],
                "no_autoflush": no_autoflush_probe["observation"],
            },
        }
        state["current"] = None
        capture["observations"]["resolver_caller_autoflush"] = direct
        capture_ready = True

        responses = {
            sample["label"]: sample["response"] for sample in capture["samples"]
        }
        assert responses["project_owner"]["status"] == 200
        assert responses["project_owner"]["body"]["access_kind"] == "owner"
        assert responses["project_owner"]["body"]["membership_id"] is None
        assert responses["super_admin_is_owner"]["body"]["access_kind"] == "super_admin"
        assert responses["super_admin_is_owner"]["body"]["membership_id"] is None
        assert (
            responses["employee_reviewer_membership"]["body"]["membership_version"] == 7
        )
        assert (
            responses["employee_reviewer_membership"]["body"]["membership_id"]
            == "$membership_employee_reviewer"
        )
        assert (
            responses["employee_annotator_membership"]["body"]["membership_version"]
            == 2
        )
        assert responses["viewer_membership"]["body"]["membership_version"] == 4
        assert (
            responses["viewer_membership"]["body"]["membership_id"]
            == "$membership_viewer"
        )
        project_admin_member = responses["project_admin_nonowner_membership"]["body"]
        assert project_admin_member["platform_role"] == "project_admin"
        assert project_admin_member["access_kind"] == "member"
        assert project_admin_member["is_manager"] is False
        assert responses["out_of_scope_employee"]["status"] == 404
        assert responses["missing_project"]["status"] == 404
        assert responses["inactive_account"]["status"] == 401
        # The valid old-role token resolves current authority, then hides scope.
        assert responses["stale_platform_role_token"]["status"] == 404
        assert responses["viewer_with_work_membership"]["status"] == 403
        for sample in capture["samples"]:
            if sample["response"]["status"] == 200:
                capabilities = sample["response"]["body"]["capabilities"]
                assert capabilities == sorted(capabilities)
                phases = [entry["phase"] for entry in sample["observation"]["sql"]]
                assert phases.count("visibility_actor_status") == 1
                assert phases.count("endpoint_actor_status") == 1
                assert phases.count("visibility_project_lookup") == 1
                assert phases.count("visibility_project_refresh") == 1
                assert phases.count("endpoint_project_refresh") == 1
                if sample["label"] in {
                    "employee_reviewer_membership",
                    "employee_annotator_membership",
                    "viewer_membership",
                    "project_admin_nonowner_membership",
                }:
                    assert phases.count("visibility_membership_refresh") == 1
                    assert phases.count("endpoint_membership_refresh") == 1
        temporal_sample = next(
            sample
            for sample in capture["samples"]
            if sample["label"] == "employee_reviewer_membership"
        )
        temporal_rows = temporal_sample["observation"]["loaded_member_rows"]
        assert temporal_rows
        assert all(
            row["assigned_at"] is not None and row["updated_at"] is not None
            for row in temporal_rows
        )
        if invalid_membership_seed["seeded"]:
            assert responses["invalid_project_membership_role"]["status"] == 403
        for alias, seed in finite_temporal_seeds.items():
            if seed["seeded"]:
                sample = next(
                    item
                    for item in capture["samples"]
                    if item["label"] == f"{alias}_out_of_python_range"
                )
                assert sample["response"]["status"] == 500
                assert any(
                    entry["phase"] == "visibility_membership_refresh"
                    for entry in sample["observation"]["sql"]
                )
        assert direct["default"]["status"] == 200
        assert direct["default"]["visible_from_owner_before_caller_rollback"] is False
        assert direct["no_autoflush"]["status"] == 404
        assert (
            direct["no_autoflush"]["visible_from_owner_before_caller_rollback"] is False
        )
    finally:
        state["current"] = None
        state["member_current"] = None
        if previous_db_override is _MISSING:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous_db_override
        for name, value in previous_identity.items():
            setattr(settings, name, value)
        await _cleanup_owned(owner_maker, actor_ids=actor_ids, project_ids=project_ids)
        await runtime_engine.dispose()

    if capture_ready:
        _write_capture(capture)
