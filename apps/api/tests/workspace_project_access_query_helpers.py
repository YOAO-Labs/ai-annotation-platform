"""Replay frozen Project-access responses against current query providers."""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.factory import create_project, create_user
from tests.test_workspace_access_reads_legacy import (
    _cleanup_owned,
    _create_member,
    _legacy_runtime_url,
    _normalise,
    _resolver_caller_probe,
)
from tests.workspace_project_query_helpers import api_project_query_binding


_HEADERS = ("content-type", "location", "www-authenticate", "retry-after")
_MISSING = object()


async def replay_workspace_project_access(
    test_engine,
    app_module,
    monkeypatch,
    frozen_samples: list[dict],
    *,
    token: str,
) -> dict:
    """Seed one isolated world, replay its public requests and resolver probes."""
    from app.config import settings
    from app.deps import get_db

    owner_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    runtime_url = _legacy_runtime_url(test_engine)
    from tests.test_asset_storage_cleanup_runtime import _verify_postgres

    _verify_postgres(make_url(runtime_url))
    runtime_engine = create_async_engine(runtime_url, hide_parameters=True)
    runtime_maker = async_sessionmaker(runtime_engine, expire_on_commit=False)

    suffix = uuid.uuid4().hex[:10]
    aliases: dict[str, str] = {suffix: "$capture_suffix"}
    actors: dict[str, User] = {}
    tokens: dict[str, str] = {}
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    state = {"current": None, "member_current": None, "aliases": aliases}
    previous_db_override = app_module.dependency_overrides.get(get_db, _MISSING)
    previous_identity = {
        name: getattr(settings, name)
        for name in (
            "identity_url",
            "identity_auth_database_url",
            "identity_auth_database_url_file",
        )
    }

    async def request_db():
        async with runtime_maker() as db:
            yield db

    async def make_actor(db, alias: str, role: str, name: str, *, active=True):
        email = f"access-{alias}-{suffix}@example.test"
        user = await create_user(db, role, email, name)
        actor_ids.append(user.id)
        actors[alias] = user
        tokens[alias] = create_access_token(subject=str(user.id), role=role)
        aliases[str(user.id)] = f"$actor_{alias}"
        aliases[email] = f"$email_{alias}"
        if not active:
            user.is_active = False
        return user

    async def make_project(db, alias: str, owner: User, name: str):
        project = await create_project(db, owner_id=owner.id, name=name)
        project.name = name
        project_ids.append(project.id)
        aliases[str(project.id)] = f"$project_{alias}"
        aliases[project.display_id] = f"$display_{alias}"
        return project

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
            await make_project(
                db, "super_admin_owned", super_admin, "Super-admin-owned project"
            )
            for alias, user, role, version, day in (
                ("employee_reviewer", employee_reviewer, "reviewer", 7, 10),
                ("employee_annotator", employee_annotator, "annotator", 2, 9),
                ("viewer", viewer, "viewer", 4, 8),
                ("project_admin_member", admin_member, "annotator", 5, 7),
                ("viewer_work_role", viewer_work, "annotator", 1, 6),
            ):
                await _create_member(
                    db,
                    project=target,
                    user=user,
                    assigned_by=super_admin,
                    role=role,
                    version=version,
                    day=day,
                    member_alias=alias,
                    aliases=aliases,
                    temporal_edge="infinity" if alias == "employee_reviewer" else None,
                )
            if invalid_platform_seed["seeded"]:
                aliases[str(invalid_platform.id)] = "$actor_invalid_platform"
                aliases[invalid_platform.email] = "$email_invalid_platform"

            invalid_membership_seed = {"seeded": False, "reason": None}
            try:
                async with db.begin_nested():
                    await _create_member(
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
            for alias, actor, edge, day in (
                (
                    "finite_assigned_at",
                    finite_assigned_at_actor,
                    "finite_assigned_at",
                    3,
                ),
                ("finite_updated_at", finite_updated_at_actor, "finite_updated_at", 2),
            ):
                try:
                    async with db.begin_nested():
                        member = await _create_member(
                            db,
                            project=target,
                            user=actor,
                            assigned_by=super_admin,
                            role=temporal_role,
                            version=1,
                            day=day,
                            member_alias=alias,
                            aliases=aliases,
                            temporal_edge=edge,
                        )
                        times = (
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
                    finite_temporal_seeds[alias] = {
                        "seeded": True,
                        "role": temporal_role,
                        "server_assigned_at_text": times["assigned_at"],
                        "server_updated_at_text": times["updated_at"],
                    }

        expected_labels = {sample["label"] for sample in frozen_samples}
        if "invalid_platform_role" in expected_labels:
            assert invalid_platform_seed["seeded"], invalid_platform_seed
        if "invalid_project_membership_role" in expected_labels:
            assert invalid_membership_seed["seeded"], invalid_membership_seed
        for alias in ("finite_assigned_at", "finite_updated_at"):
            label = f"{alias}_out_of_python_range"
            if label in expected_labels:
                assert finite_temporal_seeds[alias]["seeded"], finite_temporal_seeds[
                    alias
                ]

        missing_id = uuid.uuid4()
        aliases[str(missing_id)] = "$missing_project"

        for name in previous_identity:
            monkeypatch.setattr(settings, name, "")
        monkeypatch.setattr(settings, "audit_async", True)
        monkeypatch.setattr(
            "app.middleware.audit._enqueue_audit", lambda _payload: True
        )
        app_module.dependency_overrides[get_db] = request_db

        transport = httpx.ASGITransport(app=app_module, raise_app_exceptions=False)
        samples = []
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            for frozen in frozen_samples:
                actor_alias = frozen["request"]["actor"]["alias"]
                assert actor_alias in actors, actor_alias
                path = frozen["request"]["path"]
                for actual, alias in aliases.items():
                    path = path.replace(alias, actual)
                response = await client.get(
                    path,
                    headers={"Authorization": f"Bearer {tokens[actor_alias]}"},
                )
                try:
                    body = response.json() if response.content else None
                except ValueError:
                    body = response.text
                samples.append(
                    {
                        "label": frozen["label"],
                        "request": {
                            "method": "GET",
                            "path": _normalise(path, aliases),
                            "actor_alias": actor_alias,
                        },
                        "response": {
                            "status": response.status_code,
                            "headers": {
                                name: response.headers[name]
                                for name in _HEADERS
                                if name in response.headers
                            },
                            "body": _normalise(body, aliases),
                            "raw_body": {"byte_length": len(response.content)},
                        },
                    }
                )

        async with api_project_query_binding(token=token):
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

        return {
            "samples": samples,
            "resolver_caller_autoflush": {
                "caller": (
                    "resolve_project_access direct invocation; "
                    "visibility dependency bypassed"
                ),
                "default": _resolver_probe_semantics(default_probe),
                "no_autoflush": _resolver_probe_semantics(no_autoflush_probe),
            },
        }
    finally:
        if previous_db_override is _MISSING:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous_db_override
        for name, value in previous_identity.items():
            setattr(settings, name, value)
        await _cleanup_owned(owner_maker, actor_ids=actor_ids, project_ids=project_ids)
        await runtime_engine.dispose()


def _resolver_probe_semantics(probe: dict) -> dict:
    """Keep behavioral caller facts and omit incidental SQL/ORM observations."""
    fields = (
        "status",
        "access",
        "detail",
        "no_autoflush",
        "caller_transaction_open_after_read",
        "visible_from_owner_before_caller_rollback",
        "pending_user_id",
        "caller",
    )
    return {name: probe[name] for name in fields if name in probe}
