from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.group import Group
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.db.models.user_invitation import UserInvitation
from tests.test_identity_auth_native import _cleanup, _seed


_ROOT = Path(__file__).parents[3]
_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_ARTIFACT = _SQL_DIR / "0240_identity_groups.sql"
_SCHEMA = _SQL_DIR / "0240_identity_groups.schema.json"
_MIGRATION = Path(__file__).parents[1] / "alembic/versions/0240_identity_groups.py"
_NATIVE_SCHEMA = Path(__file__).parents[1] / "app/db/native_schema.py"
_MODULE_GRANTS = _ROOT / "infra/docker/module-data-grants.sql"
_GROUP_GRANTS = _ROOT / "infra/docker/identity-groups-grants.sql"
_LEGACY_GRANTS = _ROOT / "infra/docker/lan-production-grants.sql"


def _context() -> dict[str, str]:
    return {
        "method": "POST",
        "path": "/api/v1/identity/groups/execute",
        "ip": "127.0.0.1",
        "request_id": str(uuid.uuid4()),
    }


def _actor(actor_id: uuid.UUID, *, role: str = "super_admin") -> dict:
    return {
        "actor_id": str(actor_id),
        "name": "stale caller name",
        "email": "stale-caller@example.test",
        "role": role,
        "is_active": True,
        "credential_type": "jwt",
        "credential_id": str(uuid.uuid4()),
        "generation": 0,
        "scopes": None,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }


def _command(
    operation: str,
    operation_id: uuid.UUID,
    actor: dict,
    payload: dict,
    *,
    fingerprint: str | None = None,
) -> dict:
    return {
        "schema": "aap-identity-groups-native/v1",
        "operation": operation,
        "operation_id": str(operation_id),
        "actor": actor,
        "expected_key_hash": None,
        "request_fingerprint": fingerprint or uuid.uuid4().hex * 2,
        "derivation_id": "d" * 64,
        "audit_context": _context(),
        "payload": payload,
    }


async def _execute(session: AsyncSession, command: dict) -> dict:
    result = await session.execute(
        text(
            "SELECT aap_identity_groups_v1.execute_group_command(CAST(:request AS jsonb))"
        ),
        {"request": json.dumps(command)},
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_invitation_group_helper_rechecks_opaque_challenge_and_rolls_back(
    test_engine,
):
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    invitation_id = uuid.uuid4()
    token = "旧链接" * 21 + "x"
    name = f"Invite owner {invitation_id.hex}"

    async def resolve(db, value):
        return await db.scalar(
            text("SELECT aap_identity_groups_v1.resolve_invitation_group(:id, :token)"),
            {"id": invitation_id, "token": value},
        )

    try:
        async with maker.begin() as db:
            db.add(
                UserInvitation(
                    id=invitation_id,
                    token=token,
                    email=f"{invitation_id.hex}@groups.test",
                    role="employee",
                    group_name=f"\u3000{name}\u3000",
                    invited_by=fixture["actor_id"],
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
        async with maker() as db:
            assert await resolve(db, "wrong challenge") == {
                "error": {"code": "invitation_invalid"}
            }
            assert (
                await db.scalar(
                    sa.select(sa.func.count())
                    .select_from(Group)
                    .where(Group.name == name)
                )
                == 0
            )
            resolved = await resolve(db, token)
            assert resolved["name"] == name
            uuid.UUID(resolved["id"])
            assert await resolve(db, token) == resolved
            await db.rollback()
        async with maker.begin() as db:
            assert (
                await db.scalar(
                    sa.select(sa.func.count())
                    .select_from(Group)
                    .where(Group.name == name)
                )
                == 0
            )
            inviter = await db.get(User, fixture["actor_id"], with_for_update=True)
            inviter.role = "employee"
        async with maker() as db:
            assert await resolve(db, token) == {"error": {"code": "invitation_invalid"}}
            assert (
                await db.scalar(
                    sa.select(sa.func.count())
                    .select_from(Group)
                    .where(Group.name == name)
                )
                == 0
            )
    finally:
        async with maker.begin() as db:
            await db.execute(
                sa.delete(UserInvitation).where(UserInvitation.id == invitation_id)
            )
            await db.execute(sa.delete(Group).where(Group.name == name))
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_group_policy_rejections_are_atomic_and_keep_public_details(test_engine):
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor = _actor(fixture["actor_id"])
    first_id, second_id = uuid.uuid4(), uuid.uuid4()
    name = f"Existing {first_id.hex}"
    try:
        async with maker.begin() as db:
            db.add_all(
                [
                    Group(id=first_id, name=name),
                    Group(id=second_id, name=f"Other {second_id.hex}"),
                ]
            )
        cases = [
            (
                "create_group",
                {"name": "\u3000 ", "description": None},
                400,
                "名称不能为空",
            ),
            (
                "create_group",
                {"name": f"  {name} ", "description": None},
                409,
                f"已存在同名数据组: {name}",
            ),
            (
                "update_group",
                {"group_id": str(second_id), "name": f"  {name} ", "description": None},
                409,
                f"已存在同名数据组:   {name} ",
            ),
        ]
        for operation, payload, status, detail in cases:
            async with maker.begin() as db:
                result = await _execute(
                    db, _command(operation, uuid.uuid4(), actor, payload)
                )
                assert result["ok"] is False
                assert result["error"] == {
                    "code": "policy_rejected",
                    "status_code": status,
                    "detail": detail,
                }
        async with maker.begin() as db:
            user = await db.get(User, fixture["actor_id"], with_for_update=True)
            user.role = "employee"
        async with maker.begin() as db:
            result = await _execute(
                db, _command("list_groups", uuid.uuid4(), actor, {})
            )
            assert result["error"] == {
                "code": "policy_rejected",
                "status_code": 403,
                "detail": "当前账号已无管理权限",
            }
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM identity_group_operation_receipts WHERE actor_id=:id"
                    ),
                    {"id": fixture["actor_id"]},
                )
                == 0
            )
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM audit_logs WHERE actor_id=:id"),
                    {"id": fixture["actor_id"]},
                )
                == 0
            )
            assert (
                await db.scalar(sa.select(Group.name).where(Group.id == second_id))
                == f"Other {second_id.hex}"
            )
    finally:
        async with maker.begin() as db:
            await db.execute(
                sa.delete(Group).where(Group.id.in_([first_id, second_id]))
            )
        await _cleanup(test_engine, fixture)


def test_frozen_group_artifacts_and_owner_capabilities():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    artifact = _ARTIFACT.read_text(encoding="utf-8")
    migration = _MIGRATION.read_text(encoding="utf-8")
    module_grants = _MODULE_GRANTS.read_text(encoding="utf-8")
    group_grants = _GROUP_GRANTS.read_text(encoding="utf-8")
    legacy_grants = _LEGACY_GRANTS.read_text(encoding="utf-8")
    native_schema = _NATIVE_SCHEMA.read_text(encoding="utf-8")
    statements = [
        statement.strip()
        for statement in artifact.split("-- aap-statement-break")
        if statement.strip()
    ]

    assert set(schema["x-postgresql-functions"]) == {
        "aap_identity_groups_v1.group_policy()",
        "aap_identity_groups_v1.execute_group_command(jsonb)",
    }
    assert (
        "aap_workspace_v1.lock_group_assignment_scope(uuid, uuid[], boolean)"
        in schema["x-workspace-function"]
    )
    assert artifact.count("CREATE FUNCTION") == 11
    assert artifact.count("REVOKE ALL ON FUNCTION") == 11
    assert artifact.count("SECURITY DEFINER") == 11
    assert artifact.count("SET search_path = pg_catalog, pg_temp") == 11
    assert "FOR KEY SHARE NOWAIT" in artifact
    assert "FOR NO KEY UPDATE NOWAIT" in artifact
    assert "pg_try_advisory_xact_lock(" in artifact
    assert "identity_group_operation_receipts" in native_schema
    assert "request_fingerprint" in artifact and "derivation_id" in artifact
    assert (
        "token"
        not in artifact.split(
            "CREATE TABLE public.identity_group_operation_receipts", 1
        )[1].split("-- aap-statement-break", 1)[0]
    )

    assert "Cannot remove Identity group operation receipts" in migration
    assert 'identity_group_operation_receipts TO :"identity_owner"' in module_grants
    assert "lock_group_assignment_scope" in module_grants
    assert "record_identity_group_event" in module_grants
    assert 'user_invitations TO :"identity_owner"' in module_grants
    assert "identity_group_role=identity_group_client" in group_grants
    assert group_grants.count("aap_identity_groups_v1.group_policy()") == 2
    assert (
        group_grants.count("aap_identity_groups_v1.execute_group_command(jsonb)") == 2
    )
    assert "resolve_invitation_group(uuid, text)" not in group_grants
    assert "aap_identity_groups_v1.group_policy()" in legacy_grants
    assert "aap_identity_groups_v1.execute_group_command(jsonb)" in legacy_grants
    assert (
        "aap_identity_groups_v1.resolve_invitation_group(uuid, text)"
        not in legacy_grants
    )
    assert all(
        not sa.text(statement.replace(":", r"\:")).compile().params
        for statement in statements
    )


@pytest.mark.asyncio
async def test_group_receipts_replay_frozen_results_and_assignment_is_atomic(
    test_engine,
):
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    target_id = uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    create_id, assign_id, clear_id, rename_id, noop_id, delete_id = (
        uuid.uuid4() for _ in range(6)
    )
    group_id = create_id
    group_label = f"Research team {create_id.hex[:8]}"
    renamed_label = f"New {group_label}"
    actor = _actor(fixture["actor_id"])
    try:
        async with maker.begin() as session:
            session.add(
                User(
                    id=target_id,
                    email=f"identity-group-target-{target_id.hex}@example.test",
                    name="Group target",
                    password_hash="test-only",
                    role="employee",
                    is_active=True,
                )
            )

        create = _command(
            "create_group",
            create_id,
            actor,
            {"name": f"\u2003{group_label}\u2003", "description": "video"},
            fingerprint="a" * 64,
        )
        async with maker.begin() as session:
            policy = await session.scalar(
                text("SELECT aap_identity_groups_v1.group_policy()")
            )
            assert policy == {
                "schema": "aap-identity-groups-policy/v1",
                "policy": "aap-identity-groups/v1",
            }
            created = await _execute(session, create)
            assert created["ok"] is True
            assert created["data"]["group"]["id"] == str(group_id)
            assert created["data"]["group"]["name"] == group_label

        async with maker.begin() as session:
            listed = await _execute(
                session,
                _command("list_groups", uuid.uuid4(), actor, {}, fingerprint="1" * 64),
            )
            queried = await _execute(
                session,
                _command(
                    "query_groups",
                    uuid.uuid4(),
                    actor,
                    {"page": 1, "page_size": 10, "search": group_label},
                    fingerprint="2" * 64,
                ),
            )
            assert any(item["id"] == str(group_id) for item in listed["data"]["items"])
            assert queried["data"]["items"][0]["id"] == str(group_id)
            assert queried["data"]["total"] == 1
            assert queried["data"]["pages"] == 1

        rename = _command(
            "update_group",
            rename_id,
            actor,
            {"group_id": str(group_id), "name": renamed_label, "description": ""},
            fingerprint="b" * 64,
        )
        async with maker.begin() as session:
            updated = await _execute(session, rename)
            assert updated["ok"] is True
            assert updated["data"]["group"]["description"] is None

        assign = _command(
            "assign_user_group",
            assign_id,
            actor,
            {"user_id": str(target_id), "group_id": str(group_id)},
            fingerprint="c" * 64,
        )
        async with maker.begin() as session:
            assigned = await _execute(session, assign)
            assert assigned["ok"] is True
            assert assigned["data"]["account"]["group_id"] == str(group_id)

        clear = _command(
            "assign_user_group",
            clear_id,
            actor,
            {"user_id": str(target_id), "group_id": None},
            fingerprint="e" * 64,
        )
        async with maker.begin() as session:
            cleared = await _execute(session, clear)
            assert cleared["ok"] is True
            assert cleared["data"]["account"]["group_id"] is None

        async with maker.begin() as session:
            no_op = await _execute(
                session,
                _command(
                    "update_group",
                    noop_id,
                    actor,
                    {"group_id": str(group_id), "name": renamed_label},
                    fingerprint="9" * 64,
                ),
            )
            assert no_op["ok"] is True

        async with maker.begin() as session:
            deleted = await _execute(
                session,
                _command(
                    "delete_group",
                    delete_id,
                    actor,
                    {"group_id": str(group_id)},
                    fingerprint="8" * 64,
                ),
            )
            assert deleted["ok"] is True
            assert deleted["data"] == {"group_id": str(group_id), "deleted": True}

        async with maker.begin() as session:
            replay = await _execute(session, create)
            assert replay["ok"] is True
            assert replay["data"]["group"]["name"] == group_label
            current = await session.scalar(
                text("SELECT name FROM public.groups WHERE id=:id"), {"id": group_id}
            )
            current_target_group = await session.scalar(
                text("SELECT group_id FROM public.users WHERE id=:id"),
                {"id": target_id},
            )
            receipt_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.identity_group_operation_receipts "
                    "WHERE operation_id = ANY(CAST(:ids AS uuid[]))"
                ),
                {
                    "ids": [
                        create_id,
                        rename_id,
                        assign_id,
                        clear_id,
                        noop_id,
                        delete_id,
                    ]
                },
            )
            audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor "
                    "AND action IN ('group.create','group.update','group.delete','user.group_change')"
                ),
                {"actor": fixture["actor_id"]},
            )
            assert current is None
            assert current_target_group is None
            assert receipt_count == 6
            assert audit_count == 5
            conflict = await _execute(
                session,
                {
                    **create,
                    "request_fingerprint": "f" * 64,
                },
            )
            assert conflict["error"]["code"] == "operation_conflict"

    finally:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM public.identity_group_operation_receipts WHERE actor_id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
            await session.execute(
                text("DELETE FROM public.groups WHERE id=:id"), {"id": group_id}
            )
            await session.execute(
                text("DELETE FROM public.users WHERE id=:id"), {"id": target_id}
            )
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_preview_hides_out_of_scope_accounts_and_empty_scope_group_name(
    test_engine,
):
    fixture = await _seed(test_engine, role="project_admin", create_key=False)
    hidden = await _seed(test_engine, role="viewer", create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    group_id = uuid.uuid4()
    actor = _actor(fixture["actor_id"], role="project_admin")
    command = _command(
        "preview_group_assignments",
        uuid.uuid4(),
        actor,
        {"user_ids": [str(hidden["actor_id"])], "group_id": str(group_id)},
    )
    try:
        async with maker.begin() as session:
            session.add(
                # This group exists, but the viewer is outside the PA's scope.
                Group(id=group_id, name=f"preview-{group_id.hex}")
            )
        async with maker.begin() as session:
            response = await _execute(session, command)
            assert response["ok"] is True
            item = response["data"]["items"][0]
            assert response["data"]["group_name"] is None
            assert response["data"]["applicable"] == 0
            assert item["ok"] is False
            assert item["email"] is None and item["name"] is None
            assert item["current_group_id"] is None
            assert item["next_group_id"] is None
            assert item["error"] == "用户不存在或不在管理范围内"
    finally:
        async with maker.begin() as session:
            await session.execute(
                text("DELETE FROM public.groups WHERE id=:id"), {"id": group_id}
            )
        await _cleanup(test_engine, fixture)
        await _cleanup(test_engine, hidden)


@pytest.mark.asyncio
async def test_bulk_assignment_preserves_order_duplicates_and_partial_receipts(
    test_engine,
):
    fixture = await _seed(test_engine, role="project_admin", create_key=False)
    employee_id, hidden_id, group_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:10]
    actor = _actor(fixture["actor_id"], role="project_admin")
    ids = [employee_id, hidden_id, employee_id]
    preview_id, apply_id, failed_apply_id, missing_group_apply_id = (
        uuid.uuid4() for _ in range(4)
    )
    try:
        async with maker.begin() as session:
            session.add_all(
                [
                    User(
                        id=employee_id,
                        email=f"identity-group-employee-{suffix}@example.test",
                        name="Visible employee",
                        password_hash="test-only",
                        role="employee",
                        is_active=True,
                    ),
                    User(
                        id=hidden_id,
                        email=f"identity-group-hidden-{suffix}@example.test",
                        name="Hidden viewer",
                        password_hash="test-only",
                        role="viewer",
                        is_active=True,
                    ),
                    Group(id=group_id, name=f"bulk-{suffix}"),
                ]
            )

        async with maker.begin() as session:
            preview = await _execute(
                session,
                _command(
                    "preview_group_assignments",
                    preview_id,
                    actor,
                    {
                        "user_ids": [str(item) for item in ids],
                        "group_id": str(group_id),
                    },
                    fingerprint="a" * 64,
                ),
            )
            assert preview["ok"] is True
            assert [item["user_id"] for item in preview["data"]["items"]] == [
                str(item) for item in ids
            ]
            assert preview["data"]["applicable"] == 2
            assert preview["data"]["blocked"] == 1
            assert preview["data"]["group_name"] == f"bulk-{suffix}"
            assert preview["data"]["items"][1]["email"] is None

        async with maker.begin() as session:
            applied = await _execute(
                session,
                _command(
                    "apply_group_assignments",
                    apply_id,
                    actor,
                    {
                        "user_ids": [str(item) for item in ids],
                        "group_id": str(group_id),
                    },
                    fingerprint="b" * 64,
                ),
            )
            assert applied["ok"] is True
            data = applied["data"]
            assert [item["user_id"] for item in data["items"]] == [
                str(item) for item in ids
            ]
            assert [item["ok"] for item in data["items"]] == [True, False, True]
            assert data["items"][1]["error"] == "用户不存在或不在管理范围内"
            assert data["succeeded"] == 2 and data["failed"] == 1

        async with maker.begin() as session:
            all_failed = await _execute(
                session,
                _command(
                    "apply_group_assignments",
                    failed_apply_id,
                    actor,
                    {"user_ids": [str(hidden_id)], "group_id": None},
                    fingerprint="c" * 64,
                ),
            )
            assert all_failed["ok"] is True
            assert all_failed["data"]["failed"] == 1
            assert all_failed["data"]["items"][0]["retryable"] is True

        missing_group_id = uuid.uuid4()
        async with maker.begin() as session:
            missing_group = await _execute(
                session,
                _command(
                    "apply_group_assignments",
                    missing_group_apply_id,
                    actor,
                    {"user_ids": [str(employee_id)], "group_id": str(missing_group_id)},
                    fingerprint="d" * 64,
                ),
            )
            assert missing_group["error"]["code"] == "group_not_found"
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_group_operation_receipts "
                        "WHERE operation_id=:id"
                    ),
                    {"id": missing_group_apply_id},
                )
                == 0
            )

        async with maker.begin() as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_group_operation_receipts "
                        "WHERE operation_id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": [preview_id, apply_id, failed_apply_id]},
                )
                == 2
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE actor_id=:id AND action='user.bulk_group_assign'"
                    ),
                    {"id": fixture["actor_id"]},
                )
                == 2
            )
    finally:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM public.identity_group_operation_receipts WHERE actor_id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
            await session.execute(
                text("DELETE FROM public.users WHERE id=ANY(CAST(:ids AS uuid[]))"),
                {"ids": [employee_id, hidden_id]},
            )
            await session.execute(
                text("DELETE FROM public.groups WHERE id=:id"), {"id": group_id}
            )
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_workspace_scope_holds_project_owner_fence_through_assignment_transaction(
    test_engine,
):
    fixture = await _seed(test_engine, role="project_admin", create_key=False)
    target_id = uuid.uuid4()
    new_owner_id = uuid.uuid4()
    project_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10].upper()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            session.add_all(
                [
                    User(
                        id=target_id,
                        email=f"identity-group-member-{suffix}@example.test",
                        name="Managed viewer",
                        password_hash="test-only",
                        role="viewer",
                        is_active=False,
                    ),
                    User(
                        id=new_owner_id,
                        email=f"identity-group-owner-{suffix}@example.test",
                        name="Replacement owner",
                        password_hash="test-only",
                        role="project_admin",
                        is_active=True,
                    ),
                    Project(
                        id=project_id,
                        display_id=f"IG-{suffix}",
                        name=f"Identity group scope {suffix}",
                        type_label="Image",
                        type_key="image",
                        owner_id=fixture["actor_id"],
                    ),
                ]
            )
            await session.flush()
            session.add(
                ProjectMember(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    user_id=target_id,
                    role="viewer",
                    assigned_by=fixture["actor_id"],
                )
            )

        async with maker() as scope_session:
            scope_tx = await scope_session.begin()
            managed = await scope_session.scalar(
                text(
                    "SELECT aap_workspace_v1.lock_group_assignment_scope("
                    "CAST(:actor AS uuid), CAST(:targets AS uuid[]), true)"
                ),
                {"actor": fixture["actor_id"], "targets": [target_id]},
            )
            assert managed == [target_id]
            async with maker() as transfer_session:
                transfer_tx = await transfer_session.begin()
                await transfer_session.execute(text("SET LOCAL lock_timeout='100ms'"))
                with pytest.raises(sa.exc.DBAPIError):
                    await transfer_session.execute(
                        text(
                            "UPDATE public.projects SET owner_id=:new WHERE id=:project"
                        ),
                        {"new": new_owner_id, "project": project_id},
                    )
                await transfer_tx.rollback()
            await scope_tx.commit()

        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.projects SET owner_id=:new WHERE id=:project"),
                {"new": new_owner_id, "project": project_id},
            )
    finally:
        async with maker.begin() as session:
            await session.execute(
                text("DELETE FROM public.project_members WHERE project_id=:id"),
                {"id": project_id},
            )
            await session.execute(
                text("DELETE FROM public.projects WHERE id=:id"), {"id": project_id}
            )
            await session.execute(
                text("DELETE FROM public.users WHERE id=:id"), {"id": target_id}
            )
            await session.execute(
                text("DELETE FROM public.users WHERE id=:id"), {"id": new_owner_id}
            )
        await _cleanup(test_engine, fixture)
