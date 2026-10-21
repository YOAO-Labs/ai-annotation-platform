"""Capture the live pre-extraction Workspace member-directory responses."""

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
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.factory import create_user

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_WORKSPACE_DIRECTORY_CAPTURE") != "1",
        reason="requires the explicit owned legacy Workspace-directory capture",
    ),
]

_ROOT = Path(__file__).resolve().parents[3]
_BASE_COMMIT = "9ec01c682fb7f3de265feb655e411e9458edb2d6"
_CAPTURE_PATH = _ROOT / ".worktree/workspace-directory-reads-legacy.json"
_PROJECT_PATH = "/api/v1/projects"
_HEADERS = ("content-type", "location", "www-authenticate", "retry-after")
_TIME_FIELDS = {"assigned_at", "updated_at"}
_FIXED_TIME_PREFIX = "2026-01-"
_CANDIDATE_PHASES = {
    "mention_owner_lookup",
    "mention_active_superadmins",
    "mention_project_members",
}
_SOURCE_FILES = """
apps/api/app/api/v1/projects.py
apps/api/app/api/v1/router.py
apps/api/app/main.py
apps/api/app/deps.py
apps/api/app/core/security.py
apps/api/app/config.py
apps/api/app/db/models/project.py
apps/api/app/db/models/project_member.py
apps/api/app/db/models/user.py
apps/api/app/db/enums.py
apps/api/app/schemas/project.py
apps/api/app/schemas/user.py
apps/api/app/services/project_access.py
apps/api/app/services/identity_account_lock_data.py
apps/api/app/services/identity_contracts.py
apps/api/app/services/identity_gateway.py
apps/api/app/services/user_brief.py
apps/api/app/services/identity_briefs_commands.py
apps/api/app/services/identity_briefs_data.py
apps/api/app/services/identity_session_data.py
apps/identity/src/aap_identity/briefs_contract.py
apps/identity/src/aap_identity/briefs_manager.py
apps/identity/src/aap_identity/briefs_postgres.py
apps/api/alembic/sql/0249_identity_briefs.sql
apps/api/alembic/sql/0249_identity_briefs_support.sql
apps/api/alembic/sql/0253_workspace_membership_support.sql
apps/web/src/api/projects.ts
apps/web/src/hooks/useProjects.ts
apps/web/src/pages/Workbench/shell/CommentsPanel.tsx
apps/api/tests/factory.py
""".split()
_MISSING = object()
_active_observation: dict | None = None


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
        return value if value.startswith(_FIXED_TIME_PREFIX) else "<dynamic_timestamp>"
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
    if "aap_identity_briefs_v1.execute_briefs_command" in compact:
        return "identity_briefs_command"
    if "from project_members" in compact and "join users" in compact:
        return "mention_project_members"
    if "from project_members" in compact:
        if "order by" in compact and "assigned_at" in compact:
            return "project_member_list"
        return "project_access_membership"
    if (
        "from users" in compact
        and "users.role" in compact
        and "users.is_active" in compact
        and "order by users.name" in compact
    ):
        return "mention_active_superadmins"
    if (
        "from users" in compact
        and "users.name" in compact
        and "users.email" in compact
        and "users.is_active" in compact
        and "users.id =" in compact
    ):
        return "mention_owner_lookup"
    if "from projects" in compact:
        return "project_visibility_or_snapshot"
    return "other"


def _observe_runtime(engine):
    @contextmanager
    def observation():
        def observe_sql(_connection, _cursor, statement, _parameters, _context, _many):
            if _active_observation is None:
                return
            normalized = " ".join(statement.split())
            _active_observation["sql"].append(
                {
                    "phase": _sql_phase(statement),
                    "statement": normalized,
                    "sha256": hashlib.sha256(normalized.encode()).hexdigest(),
                }
            )

        def after_commit(session):
            if session.bind is engine.sync_engine and _active_observation is not None:
                _active_observation["transaction_events"].append("commit")

        def after_rollback(session):
            if session.bind is engine.sync_engine and _active_observation is not None:
                _active_observation["transaction_events"].append("rollback")

        event.listen(engine.sync_engine, "before_cursor_execute", observe_sql)
        event.listen(Session, "after_commit", after_commit)
        event.listen(Session, "after_rollback", after_rollback)
        try:
            yield
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", observe_sql)
            event.remove(Session, "after_commit", after_commit)
            event.remove(Session, "after_rollback", after_rollback)

    return observation


async def _request(
    client: httpx.AsyncClient,
    capture: dict,
    aliases: dict[str, str],
    actors: dict[str, User],
    tokens: dict[str, str],
    *,
    label: str,
    actor_alias: str,
    path: str,
):
    global _active_observation
    sample = {
        "label": label,
        "observation": {"sql": [], "briefs_calls": [], "transaction_events": []},
    }
    _active_observation = sample["observation"]
    try:
        response = await client.get(
            path,
            headers={"Authorization": f"Bearer {tokens[actor_alias]}"},
        )
    finally:
        _active_observation = None
    try:
        raw_body = response.json() if response.content else None
    except ValueError:
        raw_body = response.text
    actor = actors[actor_alias]
    sample["request"] = {
        "method": "GET",
        "path": _normalise(path, aliases),
        "actor": {
            "alias": actor_alias,
            "id": aliases[str(actor.id)],
            "role": actor.role,
            "active": actor.is_active,
        },
    }
    normalized_body = _normalise(raw_body, aliases)
    sample["response"] = {
        "status": response.status_code,
        "headers": {
            name: response.headers[name]
            for name in _HEADERS
            if name in response.headers
        },
        "body": normalized_body,
        "raw_body": {
            "byte_length": len(response.content),
            "sha256": hashlib.sha256(response.content).hexdigest(),
            "base64": base64.b64encode(response.content).decode("ascii"),
        },
    }
    capture["samples"].append(sample)
    return response, normalized_body, sample


def _ids(rows: list[dict]) -> list[str]:
    return [str(row["user_id"]) for row in rows]


async def _direct_member_list_probe(
    *,
    runtime_maker,
    owner_maker,
    project: Project,
    pending_user: User,
    assigner: User,
    no_autoflush: bool,
    aliases: dict[str, str],
):
    from app.api.v1.projects import list_members

    pending = ProjectMember(
        id=uuid.uuid4(),
        project_id=project.id,
        user_id=pending_user.id,
        assigned_by=assigner.id,
        role="annotator",
        assigned_at=datetime(2026, 1, 12, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 12, tzinfo=timezone.utc),
    )
    async with runtime_maker() as db:
        project_row = await db.get(Project, project.id)
        db.add(pending)
        if no_autoflush:
            with db.no_autoflush:
                rows = await list_members(project=project_row, db=db)
        else:
            rows = await list_members(project=project_row, db=db)
        included = any(row.user_id == pending_user.id for row in rows)
        pending_in_new = pending in db.new
        transaction_open = db.in_transaction()
        async with owner_maker() as observer:
            committed = await observer.get(ProjectMember, pending.id) is not None
        await db.rollback()
    return {
        "no_autoflush": no_autoflush,
        "pending_member_in_route_result": included,
        "pending_member_in_session_new_after_read": pending_in_new,
        "caller_transaction_open_after_read": transaction_open,
        "visible_from_owner_before_caller_rollback": committed,
        "pending_user_alias": aliases[str(pending_user.id)],
    }


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


async def test_capture_legacy_workspace_directory_reads(
    test_engine, app_module, monkeypatch
):
    from app.deps import get_db
    from app.services.identity_briefs_commands import IdentityBriefsCommands as Commands

    assert not _CAPTURE_PATH.exists(), f"refusing to overwrite {_CAPTURE_PATH}"
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
    projects: dict[str, Project] = {}
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    capture = {
        "capture_kind": "actual-pre-extraction-workspace-directory-http",
        "source": _source_metadata(),
        "runtime_identity": None,
        "samples": [],
        "observations": {},
    }
    previous_db_override = app_module.dependency_overrides.get(get_db, _MISSING)
    original_briefs_execute = Commands.execute
    capture_ready = False

    async def request_db():
        async with runtime_maker() as db:
            yield db

    async def capture_briefs_execute(self, request):
        global _active_observation
        try:
            reply = await original_briefs_execute(self, request)
        except Exception as exc:
            if _active_observation is not None:
                _active_observation["briefs_calls"].append(
                    {
                        "request": _normalise(
                            request.model_dump(mode="json", by_alias=True), aliases
                        ),
                        "error_type": type(exc).__name__,
                    }
                )
            raise
        if _active_observation is not None:
            _active_observation["briefs_calls"].append(
                {
                    "request": _normalise(
                        request.model_dump(mode="json", by_alias=True), aliases
                    ),
                    "returned_users": _normalise(
                        [
                            {
                                "id": str(user.id),
                                "name": user.name,
                                "email": user.email,
                                "role": user.role,
                                "avatar_initial": user.avatar_initial,
                                "avatar_ref": user.avatar_ref,
                            }
                            for user in reply.data.users
                        ],
                        aliases,
                    ),
                }
            )
        return reply

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
            display_id=f"P-DIR-{suffix[:10]}-{len(projects)}",
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

    async def make_member(db, project: Project, user: User, role: str, day: int):
        member = ProjectMember(
            project_id=project.id,
            user_id=user.id,
            assigned_by=actors["super_admin"].id,
            role=role,
            assigned_at=datetime(2026, 1, day, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, day, tzinfo=timezone.utc),
        )
        db.add(member)
        await db.flush()
        return member

    async def read(client, label: str, actor_alias: str, project_id, view: str):
        return await _request(
            client,
            capture,
            aliases,
            actors,
            tokens,
            label=label,
            actor_alias=actor_alias,
            path=f"{_PROJECT_PATH}/{project_id}/{view}",
        )

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
            await make_member(db, target, owner, "reviewer", 10)
            await make_member(db, target, inactive_sa, "reviewer", 9)
            await make_member(db, target, root, "annotator", 8)
            await make_member(db, target, unicode_member, "annotator", 7)
            await make_member(db, target, inactive_member, "annotator", 6)
            await make_member(db, target, viewer, "viewer", 5)
            await make_member(db, inactive_owner_project, inactive_owner, "reviewer", 4)

            historical_role_seed = {"seeded": False, "reason": None}
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
                historical_role_seed["reason"] = (
                    "database rejected an unknown platform role"
                )
            else:
                actor_ids.append(historical.id)
                aliases[str(historical.id)] = "$actor_historical_role"
                aliases[historical.email] = "$email_historical_role"
                malformed_project = await make_project(
                    db, "historical_role", owner, "Historical role directory"
                )
                await make_member(db, malformed_project, historical, "annotator", 3)
                historical_role_seed["seeded"] = True
            capture["observations"]["historical_role_seed"] = historical_role_seed

        from app.config import settings as app_settings

        monkeypatch.setattr(app_settings, "identity_url", "")
        monkeypatch.setattr(app_settings, "identity_briefs_database_url", "")
        monkeypatch.setattr(app_settings, "identity_briefs_database_url_file", "")
        monkeypatch.setattr(app_settings, "audit_async", True)
        monkeypatch.setattr(
            "app.middleware.audit._enqueue_audit", lambda _payload: True
        )
        monkeypatch.setattr(Commands, "execute", capture_briefs_execute)
        app_module.dependency_overrides[get_db] = request_db

        transport = httpx.ASGITransport(app=app_module, raise_app_exceptions=False)
        observe_runtime = _observe_runtime(runtime_engine)
        with observe_runtime():
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response, members, member_sample = await read(
                    client,
                    "target_members_super_admin",
                    "super_admin",
                    target.id,
                    "members",
                )
                assert response.status_code == 200
                assert _ids(members) == [
                    "$actor_owner",
                    "$actor_inactive_super_admin",
                    "$actor_super_admin",
                    "$actor_unicode_member",
                    "$actor_inactive_member",
                    "$actor_viewer",
                ]
                profiles = member_sample["observation"]["briefs_calls"][0][
                    "returned_users"
                ]
                unicode_profile = next(
                    item for item in profiles if item["id"] == "$actor_unicode_member"
                )
                assert (
                    unicode_profile["avatar_initial"],
                    unicode_profile["avatar_ref"],
                ) == (
                    "SS",
                    "preset:blue",
                )

                response, candidates, _ = await read(
                    client,
                    "target_mention_candidates_super_admin",
                    "super_admin",
                    target.id,
                    "mention-candidates",
                )
                assert response.status_code == 200
                assert _ids(candidates) == [
                    "$actor_owner",
                    "$actor_external_super_admin",
                    "$actor_super_admin",
                    "$actor_inactive_super_admin",
                    "$actor_unicode_member",
                    "$actor_inactive_member",
                    "$actor_viewer",
                ]
                assert [item["kind"] for item in candidates] == [
                    "owner",
                    "super_admin",
                    "super_admin",
                    "member",
                    "member",
                    "member",
                    "member",
                ]

                for actor_alias, actor_id in (
                    ("unicode_member", "$actor_unicode_member"),
                    ("viewer", "$actor_viewer"),
                ):
                    for view in ("members", "mention-candidates"):
                        response, rows, _ = await read(
                            client,
                            f"target_{view}_{actor_alias}",
                            actor_alias,
                            target.id,
                            view,
                        )
                        assert response.status_code == 200
                        assert actor_id in _ids(rows)

                response, empty_members, empty_sample = await read(
                    client, "empty_project_members", "super_admin", empty.id, "members"
                )
                assert response.status_code == 200 and empty_members == []
                assert empty_sample["observation"]["briefs_calls"] == []
                response, empty_candidates, _ = await read(
                    client,
                    "empty_project_mention_candidates",
                    "super_admin",
                    empty.id,
                    "mention-candidates",
                )
                assert response.status_code == 200
                assert _ids(empty_candidates) == [
                    "$actor_owner",
                    "$actor_external_super_admin",
                    "$actor_super_admin",
                ]

                response, inactive_candidates, _ = await read(
                    client,
                    "inactive_owner_member_fallback",
                    "super_admin",
                    inactive_owner_project.id,
                    "mention-candidates",
                )
                assert response.status_code == 200
                inactive_owner_candidate = next(
                    item
                    for item in inactive_candidates
                    if item["user_id"] == "$actor_inactive_owner"
                )
                assert inactive_owner_candidate["kind"] == "member"

                for view in ("members", "mention-candidates"):
                    response, _, denied = await read(
                        client, f"out_of_scope_{view}", "outsider", target.id, view
                    )
                    assert response.status_code == 404
                    forbidden_phases = (
                        _CANDIDATE_PHASES
                        if view == "mention-candidates"
                        else {"project_member_list"}
                    )
                    assert not any(
                        item["phase"] in forbidden_phases
                        for item in denied["observation"]["sql"]
                    )

                missing_id = uuid.uuid4()
                aliases[str(missing_id)] = "$missing_project"
                response, _, missing = await read(
                    client,
                    "missing_project_mention_candidates",
                    "super_admin",
                    missing_id,
                    "mention-candidates",
                )
                assert response.status_code == 404
                assert not any(
                    item["phase"] in _CANDIDATE_PHASES
                    for item in missing["observation"]["sql"]
                )

                if historical_role_seed["seeded"]:
                    response, _, historical_sample = await read(
                        client,
                        "historical_platform_role_member_response",
                        "super_admin",
                        projects["historical_role"].id,
                        "members",
                    )
                    assert response.status_code == 500
                    capture["observations"]["historical_role_response"] = {
                        "status": response.status_code,
                        "briefs_calls": historical_sample["observation"][
                            "briefs_calls"
                        ],
                    }

        direct_observation = {
            "sql": [],
            "briefs_calls": [],
            "transaction_events": [],
        }
        global _active_observation
        _active_observation = direct_observation
        with observe_runtime():
            autoflush_probe = await _direct_member_list_probe(
                runtime_maker=runtime_maker,
                owner_maker=owner_maker,
                project=target,
                pending_user=pending_user,
                assigner=root,
                no_autoflush=False,
                aliases=aliases,
            )
            no_autoflush_probe = await _direct_member_list_probe(
                runtime_maker=runtime_maker,
                owner_maker=owner_maker,
                project=target,
                pending_user=pending_user,
                assigner=root,
                no_autoflush=True,
                aliases=aliases,
            )
        _active_observation = None
        assert autoflush_probe["pending_member_in_route_result"] is True
        assert autoflush_probe["pending_member_in_session_new_after_read"] is False
        assert autoflush_probe["caller_transaction_open_after_read"] is True
        assert autoflush_probe["visible_from_owner_before_caller_rollback"] is False
        assert no_autoflush_probe["pending_member_in_route_result"] is False
        assert no_autoflush_probe["pending_member_in_session_new_after_read"] is True
        assert no_autoflush_probe["caller_transaction_open_after_read"] is True
        assert no_autoflush_probe["visible_from_owner_before_caller_rollback"] is False
        capture["observations"]["caller_autoflush"] = {
            "default": autoflush_probe,
            "no_autoflush": no_autoflush_probe,
            "ordered_sql_and_transaction_events": direct_observation,
        }
        capture_ready = True
    finally:
        _active_observation = None
        if previous_db_override is _MISSING:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous_db_override
        await _cleanup_owned(owner_maker, actor_ids=actor_ids, project_ids=project_ids)
        await runtime_engine.dispose()

    if capture_ready:
        _write_capture(capture)
