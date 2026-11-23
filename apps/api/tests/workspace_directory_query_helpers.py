"""Directory API replay and portable owner protocol helpers."""

from __future__ import annotations

import httpx

from yoao_workspace.directory_query_contract import (
    DirectoryQueryReply,
    DirectoryQueryRequest,
)
from yoao_workspace.directory_query_values import decode_directory_query_reply
from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.factory import create_user
from tests.workspace_project_query_helpers import _dsn, query_with_pending_orm_work


def directory_query_dsn(test_engine, database=None):
    return _dsn(
        test_engine,
        "AAP_MODULE_DATA_WORKSPACE_DIRECTORY_QUERY_URL",
        "yoao_workspace_directory_query_client",
        database,
    )


async def execute_directory_query_contract(
    client: httpx.AsyncClient, token: str, request: DirectoryQueryRequest
) -> DirectoryQueryReply:
    response = await client.post(
        "/v1/workspace/directory-queries/execute",
        headers={"Authorization": f"Bearer {token}"},
        json=request.model_dump(mode="json", by_alias=True),
    )
    response.raise_for_status()
    return decode_directory_query_reply(request, response.content)


async def _borrowed_member_probe(
    *,
    runtime_maker,
    owner_maker,
    project_id,
    pending_user_id,
    actor_id,
    no_autoflush,
    pending_user_alias,
):
    from app.api.v1.projects import list_members
    from app.services.workspace_membership import add_member

    async with runtime_maker() as db:

        async def read_members():
            return await list_members(project_id, db=db, actor_id=actor_id)

        before = await read_members()
        assert all(row["user_id"] != pending_user_id for row in before)
        staged = await add_member(
            db,
            project_id=project_id,
            actor_id=actor_id,
            target_user_id=pending_user_id,
            project_role="annotator",
        )
        async with query_with_pending_orm_work(
            db, no_autoflush=no_autoflush
        ) as pending:
            rows = await read_members()
        included = any(
            row["id"] == staged.id and row["user_id"] == pending_user_id for row in rows
        )
        pending_in_new = pending in db.new
        transaction_open = db.in_transaction()
        async with owner_maker() as observer:
            committed = await observer.get(ProjectMember, staged.id) is not None
        await db.rollback()
        assert await read_members() == before
    async with owner_maker() as observer:
        assert await observer.get(ProjectMember, staged.id) is None
    return {
        "no_autoflush": no_autoflush,
        "pending_member_in_route_result": included,
        "pending_member_in_session_new_after_read": pending_in_new,
        "caller_transaction_open_after_read": transaction_open,
        "visible_from_owner_before_caller_rollback": committed,
        "pending_user_alias": pending_user_alias,
    }


async def capture_directory_scenario(
    test_engine, app_module, monkeypatch, *, member_ids_by_user_id
):
    """Seed the frozen world and compare HTTP without recapturing legacy evidence."""

    from datetime import datetime, timezone
    import uuid

    from sqlalchemy import delete, func, select, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.deps import get_db
    from tests import test_workspace_directory_reads_legacy as scenarios

    runtime_url = scenarios._legacy_runtime_url(test_engine)
    runtime_parsed = make_url(runtime_url)
    from tests.test_asset_storage_cleanup_runtime import _verify_postgres

    _verify_postgres(runtime_parsed)
    owner_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    runtime_engine = create_async_engine(runtime_url, hide_parameters=True)
    runtime_maker = async_sessionmaker(runtime_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:10]
    aliases = {suffix: "$capture_suffix"}
    actors: dict[str, User] = {}
    tokens: dict[str, str] = {}
    projects: dict[str, Project] = {}
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    capture = {
        "capture_kind": "current-owner-workspace-directory-http-replay",
        "source": {"current_owner": True},
        "runtime_identity": None,
        "samples": [],
        "observations": {},
    }
    previous_db_override = app_module.dependency_overrides.get(get_db)
    had_db_override = get_db in app_module.dependency_overrides

    async def request_db():
        async with runtime_maker() as db:
            yield db

    async def make_actor(db, alias: str, role: str, name: str, *, active=True):
        email = f"directory-{alias}-{suffix}@example.test"
        user = await create_user(db, role, email, name)
        user.is_active = active
        actor_ids.append(user.id)
        actors[alias] = user
        tokens[alias] = create_access_token(subject=str(user.id), role=role)
        aliases[str(user.id)] = f"$actor_{alias}"
        aliases[email] = f"$email_{alias}"
        return user

    async def make_project(db, alias: str, owner: User, name: str):
        project = Project(
            display_id=f"P-DIR-{suffix}-{len(projects)}",
            name=name,
            type_label="Directory capture",
            type_key="image-det",
            owner_id=owner.id,
            ai_enabled=False,
        )
        db.add(project)
        await db.flush()
        projects[alias] = project
        project_ids.append(project.id)
        aliases[str(project.id)] = f"$project_{alias}"
        aliases[project.display_id] = f"$display_{alias}"
        return project

    async def make_member(
        db, project: Project, user: User, role: str, day: int, *, member_id=None
    ):
        member = ProjectMember(
            id=member_id,
            project_id=project.id,
            user_id=user.id,
            assigned_by=actors["super_admin"].id,
            role=role,
            assigned_at=datetime(2026, 1, day, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, day, tzinfo=timezone.utc),
        )
        db.add(member)
        await db.flush()

    async def read(client, label: str, actor_alias: str, project_id, view: str):
        result = await scenarios._request(
            client,
            capture,
            aliases,
            actors,
            tokens,
            label=label,
            actor_alias=actor_alias,
            path=f"/api/v1/projects/{project_id}/{view}",
        )
        result[2]["observation"].pop("briefs_calls", None)
        return result

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
        }

        async with owner_maker.begin() as db:
            root = await make_actor(
                db, "super_admin", "super_admin", "Zeta Capture Admin"
            )
            await make_actor(
                db, "external_super_admin", "super_admin", "Alpha External Admin"
            )
            inactive_sa = await make_actor(
                db,
                "inactive_super_admin",
                "super_admin",
                "Inactive System Admin",
                active=False,
            )
            owner = await make_actor(db, "owner", "project_admin", "Project Owner")
            unicode_member = await make_actor(
                db, "unicode_member", "employee", "ßeta Annotator"
            )
            unicode_member.avatar_ref = "preset:blue"
            inactive_member = await make_actor(
                db, "inactive_member", "employee", "Inactive Annotator", active=False
            )
            viewer = await make_actor(db, "viewer", "viewer", "Project Viewer")
            await make_actor(db, "outsider", "employee", "Outside Employee")
            inactive_owner = await make_actor(
                db,
                "inactive_owner",
                "project_admin",
                "Inactive Project Owner",
                active=False,
            )
            pending_user = await make_actor(
                db, "pending_probe", "employee", "Pending Membership Probe"
            )

            target = await make_project(db, "target", owner, "Directory target")
            empty = await make_project(db, "empty", owner, "Directory empty")
            inactive_owner_project = await make_project(
                db, "inactive_owner", inactive_owner, "Inactive owner directory"
            )
            for project, user, role, day in (
                (target, owner, "reviewer", 10),
                (target, inactive_sa, "reviewer", 9),
                (target, root, "annotator", 8),
                (target, unicode_member, "annotator", 7),
                (target, inactive_member, "annotator", 6),
                (target, viewer, "viewer", 5),
                (inactive_owner_project, inactive_owner, "reviewer", 4),
            ):
                frozen_member_id = (
                    uuid.UUID(member_ids_by_user_id[aliases[str(user.id)]])
                    if project is target
                    else None
                )
                await make_member(
                    db, project, user, role, day, member_id=frozen_member_id
                )

            historical = User(
                id=uuid.uuid4(),
                email=f"directory-historical-role-{suffix}@example.test",
                name="Historical Role Member",
                password_hash="capture-only-not-a-credential",
                role="historical_staff_role",
                is_active=True,
            )
            try:
                async with db.begin_nested():
                    db.add(historical)
                    await db.flush()
            except IntegrityError:
                capture["observations"]["historical_role_seed"] = {
                    "seeded": False,
                    "reason": "database rejected an unknown platform role",
                }
            else:
                actor_ids.append(historical.id)
                aliases[str(historical.id)] = "$actor_historical_role"
                aliases[historical.email] = "$email_historical_role"
                historical_project = await make_project(
                    db, "historical_role", owner, "Historical role directory"
                )
                await make_member(db, historical_project, historical, "annotator", 3)
                capture["observations"]["historical_role_seed"] = {"seeded": True}

        if not capture["observations"]["historical_role_seed"]["seeded"]:
            raise AssertionError("frozen historical platform role could not be seeded")

        from app.config import settings as app_settings

        monkeypatch.setattr(app_settings, "identity_url", "")
        monkeypatch.setattr(app_settings, "identity_briefs_database_url", "")
        monkeypatch.setattr(app_settings, "identity_briefs_database_url_file", "")
        monkeypatch.setattr(app_settings, "audit_async", True)
        monkeypatch.setattr(
            "app.middleware.audit._enqueue_audit", lambda _payload: True
        )
        app_module.dependency_overrides[get_db] = request_db

        transport = httpx.ASGITransport(app=app_module, raise_app_exceptions=False)
        with scenarios._observe_runtime(runtime_engine)():
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                requests = (
                    ("target_members_super_admin", "super_admin", target, "members"),
                    (
                        "target_mention_candidates_super_admin",
                        "super_admin",
                        target,
                        "mention-candidates",
                    ),
                    (
                        "target_members_unicode_member",
                        "unicode_member",
                        target,
                        "members",
                    ),
                    (
                        "target_mention-candidates_unicode_member",
                        "unicode_member",
                        target,
                        "mention-candidates",
                    ),
                    ("target_members_viewer", "viewer", target, "members"),
                    (
                        "target_mention-candidates_viewer",
                        "viewer",
                        target,
                        "mention-candidates",
                    ),
                    ("empty_project_members", "super_admin", empty, "members"),
                    (
                        "empty_project_mention_candidates",
                        "super_admin",
                        empty,
                        "mention-candidates",
                    ),
                    (
                        "inactive_owner_member_fallback",
                        "super_admin",
                        inactive_owner_project,
                        "mention-candidates",
                    ),
                    ("out_of_scope_members", "outsider", target, "members"),
                    (
                        "out_of_scope_mention-candidates",
                        "outsider",
                        target,
                        "mention-candidates",
                    ),
                )
                for label, actor_alias, project, view in requests:
                    await read(client, label, actor_alias, project.id, view)

                missing_id = uuid.uuid4()
                aliases[str(missing_id)] = "$missing_project"
                await read(
                    client,
                    "missing_project_mention_candidates",
                    "super_admin",
                    missing_id,
                    "mention-candidates",
                )
                await read(
                    client,
                    "historical_platform_role_member_response",
                    "super_admin",
                    projects["historical_role"].id,
                    "members",
                )

        capture["observations"]["caller_autoflush"] = {
            mode: await _borrowed_member_probe(
                runtime_maker=runtime_maker,
                owner_maker=owner_maker,
                project_id=target.id,
                pending_user_id=pending_user.id,
                actor_id=root.id,
                no_autoflush=no_autoflush,
                pending_user_alias=aliases[str(pending_user.id)],
            )
            for mode, no_autoflush in (("default", False), ("no_autoflush", True))
        }
        return capture
    finally:
        if had_db_override:
            app_module.dependency_overrides[get_db] = previous_db_override
        else:
            app_module.dependency_overrides.pop(get_db, None)
        async with owner_maker.begin() as db:
            if project_ids:
                await db.execute(
                    delete(ProjectMember).where(
                        ProjectMember.project_id.in_(project_ids)
                    )
                )
                await db.execute(delete(Project).where(Project.id.in_(project_ids)))
            if actor_ids:
                await db.execute(delete(User).where(User.id.in_(actor_ids)))
        await runtime_engine.dispose()
