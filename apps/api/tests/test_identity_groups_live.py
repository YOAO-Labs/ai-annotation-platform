"""Independent Node consumer checks for the Identity group capability."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete, select, text, update

from aap_identity.security import create_access_token, decode_access_token
from app.db.models.group import Group
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.factory import create_membership, create_project, create_user
from tests.test_identity_admission_live import (
    ROOT,
    _start_container_identity,
    _start_identity,
    _token,
    _world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_GROUPS_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity group verifier",
    ),
]


def _command(operation, token, payload=None, *, operation_id=None):
    return {
        "schema": "aap-identity-groups/v1",
        "operation": operation,
        "operation_id": str(operation_id or uuid.uuid4()),
        "token": token,
        "audit_context": {
            "method": "POST",
            "path": "/acceptance/identity-groups",
            "ip": "127.0.0.1",
            "request_id": str(uuid.uuid4()),
        },
        "payload": payload or {},
    }


def _node(identity, control, request):
    result = subprocess.run(
        ["node", str(ROOT / "apps/api/tests/fixtures/identity_groups_peer.mjs")],
        input=json.dumps(
            {"url": identity.origin, "control_token": control, "request": request}
        ),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert result.returncode == 0, "Independent Node group peer failed"
    for secret in (control, request["token"]):
        assert secret not in result.stdout
        assert secret not in result.stderr
    return json.loads(result.stdout)


async def _call(client, identity, control, request):
    response = await client.post(
        identity.origin + "/v1/identity/groups/execute",
        headers={"Authorization": f"Bearer {control}"},
        json=request,
    )
    assert response.status_code == 200, "Group owner returned a transport failure"
    return response.json()


async def _cleanup_groups(
    maker,
    *,
    actor_ids,
    group_ids,
    project_ids=(),
    operation_ids=(),
    jtis=(),
):
    async with maker.begin() as db:
        assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
        await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
        if actor_ids or operation_ids:
            await db.execute(
                text(
                    "DELETE FROM public.identity_group_operation_receipts "
                    "WHERE actor_id=ANY(CAST(:actors AS uuid[])) "
                    "OR operation_id=ANY(CAST(:operations AS uuid[]))"
                ),
                {"actors": list(actor_ids), "operations": list(operation_ids)},
            )
        target_ids = [str(value) for value in [*actor_ids, *group_ids, *project_ids]]
        if actor_ids or target_ids:
            await db.execute(
                text(
                    "DELETE FROM public.audit_logs WHERE actor_id=ANY(CAST(:actors AS uuid[])) "
                    "OR target_id=ANY(CAST(:targets AS text[]))"
                ),
                {"actors": list(actor_ids), "targets": target_ids},
            )
        if group_ids:
            await db.execute(
                update(User)
                .where(User.group_id.in_(list(group_ids)))
                .values(group_id=None, group_name=None)
            )
            await db.execute(delete(Group).where(Group.id.in_(list(group_ids))))
        if project_ids:
            await db.execute(
                delete(ProjectMember).where(
                    ProjectMember.project_id.in_(list(project_ids))
                )
            )
            await db.execute(delete(Project).where(Project.id.in_(list(project_ids))))
        if actor_ids:
            await db.execute(
                delete(ProjectMember).where(ProjectMember.user_id.in_(list(actor_ids)))
            )
            await db.execute(delete(User).where(User.id.in_(list(actor_ids))))
        if jtis:
            await db.execute(
                text(
                    "DELETE FROM public.identity_revoked_jtis "
                    "WHERE jti=ANY(CAST(:jtis AS uuid[]))"
                ),
                {"jtis": list(jtis)},
            )


async def _group_state(maker, group_id):
    async with maker() as db:
        group = await db.get(Group, group_id)
        users = list(
            (
                await db.scalars(
                    select(User).where(User.group_id == group_id).order_by(User.id)
                )
            ).all()
        )
        count = await db.scalar(
            select(text("count(*)"))
            .select_from(User)
            .where(User.group_id == group_id, User.is_active.is_(True))
        )
        return group, users, int(count or 0)


async def _receipt_count(maker, operation_id):
    async with maker() as db:
        return await db.scalar(
            text(
                "SELECT count(*) FROM public.identity_group_operation_receipts "
                "WHERE operation_id=:operation_id"
            ),
            {"operation_id": operation_id},
        )


def _assert_group_sync(group, users):
    if group is None:
        assert users == []
        return
    assert all(user.group_name == group.name for user in users)


async def _assert_tracked_group_sync(maker, group_id, user_ids):
    group, members, count = await _group_state(maker, group_id)
    async with maker() as db:
        users = list(
            (await db.scalars(select(User).where(User.id.in_(list(user_ids))))).all()
        )
    if group is None:
        assert all(user.group_id is None and user.group_name is None for user in users)
        assert members == [] and count == 0
        return None
    assert all(
        (user.group_id == group_id and user.group_name == group.name)
        or (user.group_id is None and user.group_name is None)
        for user in users
    )
    _assert_group_sync(group, members)
    return group


async def test_installed_group_owner_node_all_operations_restart_and_revocation(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    actor_ids = [actor]
    group_ids = []
    operation_ids = []
    jtis = []
    identity = None
    suffix = uuid.uuid4().hex
    try:
        async with maker.begin() as db:
            targets = [
                await create_user(
                    db,
                    "employee",
                    f"groups-{suffix}-{index}@example.test",
                    f"Group target {index}",
                )
                for index in range(4)
            ]
        target_ids = [user.id for user in targets]
        actor_ids.extend(target_ids)
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_keys=False,
            enable_sessions=False,
            enable_accounts=False,
            enable_groups=True,
            enable_presence=False,
        )
        token = _token(actor, secret)
        jtis.append(uuid.UUID(decode_access_token(token, secret=secret)["jti"]))

        list_request = _command("list_groups", token)
        operation_ids.append(uuid.UUID(list_request["operation_id"]))
        listed = await asyncio.to_thread(_node, identity, control, list_request)
        assert listed["ok"] and listed["operation"] == "list_groups"
        assert await _receipt_count(maker, list_request["operation_id"]) == 0

        name = f"NodeGroups-{suffix[:12]}"
        create_request = _command(
            "create_group",
            token,
            {"name": f"\u3000{name}\u3000", "description": "initial group"},
        )
        create_id = uuid.UUID(create_request["operation_id"])
        operation_ids.append(create_id)
        group_ids.append(create_id)
        created = await asyncio.to_thread(_node, identity, control, create_request)
        assert created["ok"] and created["data"]["group"]["id"] == str(create_id)
        assert created["data"]["group"]["name"] == name
        group_id = uuid.UUID(created["data"]["group"]["id"])

        replay = await asyncio.to_thread(_node, identity, control, create_request)
        assert replay["ok"] and replay["data"] == created["data"]
        async with maker.begin() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_group_operation_receipts "
                        "WHERE operation_id=:operation_id AND actor_id=:actor_id"
                    ),
                    {"operation_id": create_id, "actor_id": actor},
                )
                == 1
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor_id AND action='group.create' "
                        "AND target_id=:target_id"
                    ),
                    {"actor_id": actor, "target_id": str(group_id)},
                )
                == 1
            )

        query_request = _command(
            "query_groups",
            token,
            {"page": 1, "page_size": 1, "search": suffix[:12]},
        )
        query_id = uuid.UUID(query_request["operation_id"])
        operation_ids.append(query_id)
        queried = await asyncio.to_thread(_node, identity, control, query_request)
        assert queried["ok"]
        assert queried["data"]["total"] == 1
        assert queried["data"]["items"][0]["id"] == str(group_id)
        assert queried["data"]["pages"] == 1
        assert await _receipt_count(maker, query_id) == 0

        async with maker() as db:
            update_audit_before = await db.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE actor_id=:actor AND action='group.update' AND target_id=:target"
                ),
                {"actor": actor, "target": str(group_id)},
            )
        update_request = _command(
            "update_group",
            token,
            {
                "group_id": str(group_id),
                "name": f" Renamed-{suffix[:12]}\u3000",
                "description": "",
            },
        )
        update_id = uuid.UUID(update_request["operation_id"])
        operation_ids.append(update_id)
        updated = await asyncio.to_thread(_node, identity, control, update_request)
        assert updated["ok"]
        assert updated["data"]["group"]["name"] == f"Renamed-{suffix[:12]}"
        assert updated["data"]["group"]["description"] is None
        group, members, count = await _group_state(maker, group_id)
        assert group is not None and group.name == updated["data"]["group"]["name"]
        assert members == [] and count == 0

        noop_request = _command(
            "update_group",
            token,
            {"group_id": str(group_id), "name": group.name, "description": None},
        )
        noop_id = uuid.UUID(noop_request["operation_id"])
        operation_ids.append(noop_id)
        noop = await asyncio.to_thread(_node, identity, control, noop_request)
        assert noop["ok"] and noop["data"]["group"] == updated["data"]["group"]
        async with maker() as db:
            update_audit_after = await db.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE actor_id=:actor AND action='group.update' AND target_id=:target"
                ),
                {"actor": actor, "target": str(group_id)},
            )
        assert update_audit_after == update_audit_before + 1

        assign_request = _command(
            "assign_user_group",
            token,
            {"user_id": str(target_ids[0]), "group_id": str(group_id)},
        )
        assign_id = uuid.UUID(assign_request["operation_id"])
        operation_ids.append(assign_id)
        assigned = await asyncio.to_thread(_node, identity, control, assign_request)
        assert assigned["ok"]
        assert assigned["data"]["account"]["id"] == str(target_ids[0])
        assert assigned["data"]["account"]["group_id"] == str(group_id)

        bulk_ids = [target_ids[0], target_ids[1], uuid.uuid4(), target_ids[0]]
        preview_request = _command(
            "preview_group_assignments",
            token,
            {"user_ids": [str(value) for value in bulk_ids], "group_id": str(group_id)},
        )
        preview_id = uuid.UUID(preview_request["operation_id"])
        operation_ids.append(preview_id)
        preview = await asyncio.to_thread(_node, identity, control, preview_request)
        assert preview["ok"]
        assert [item["user_id"] for item in preview["data"]["items"]] == [
            str(value) for value in bulk_ids
        ]
        assert preview["data"]["applicable"] == 3
        assert preview["data"]["blocked"] == 1
        assert preview["data"]["items"][2]["email"] is None
        assert preview["data"]["items"][2]["name"] is None

        apply_request = _command(
            "apply_group_assignments",
            token,
            {"user_ids": [str(value) for value in bulk_ids], "group_id": str(group_id)},
        )
        apply_id = uuid.UUID(apply_request["operation_id"])
        operation_ids.append(apply_id)
        applied = await asyncio.to_thread(_node, identity, control, apply_request)
        assert applied["ok"]
        assert [item["user_id"] for item in applied["data"]["items"]] == [
            str(value) for value in bulk_ids
        ]
        assert applied["data"]["succeeded"] == 3
        assert applied["data"]["failed"] == 1
        assert applied["data"]["items"][2]["ok"] is False
        applied_replay = await asyncio.to_thread(
            _node, identity, control, apply_request
        )
        assert applied_replay["ok"] and applied_replay["data"] == applied["data"]
        assert await _receipt_count(maker, apply_id) == 1
        group, members, count = await _group_state(maker, group_id)
        assert count == 2
        _assert_group_sync(group, members)

        # Rename and assignment contend through the same Group -> User lock order.
        rename_race = _command(
            "update_group",
            token,
            {
                "group_id": str(group_id),
                "name": f"RaceRename-{suffix[:12]}",
                "description": None,
            },
        )
        rename_race_id = uuid.UUID(rename_race["operation_id"])
        operation_ids.append(rename_race_id)
        assign_race = _command(
            "assign_user_group",
            token,
            {"user_id": str(target_ids[2]), "group_id": str(group_id)},
        )
        assign_race_id = uuid.UUID(assign_race["operation_id"])
        operation_ids.append(assign_race_id)
        rename_result, assign_race_result = await asyncio.gather(
            asyncio.to_thread(_node, identity, control, rename_race),
            asyncio.to_thread(_node, identity, control, assign_race),
        )
        for result in (rename_result, assign_race_result):
            assert result["ok"] or result["error"]["code"] in {
                "group_busy",
                "account_busy",
            }
        group = await _assert_tracked_group_sync(maker, group_id, target_ids)
        assert group is not None

        # Delete and assignment also serialize under the real owner row locks.
        delete_race = _command("delete_group", token, {"group_id": str(group_id)})
        delete_race_id = uuid.UUID(delete_race["operation_id"])
        operation_ids.append(delete_race_id)
        assign_delete_race = _command(
            "assign_user_group",
            token,
            {"user_id": str(target_ids[3]), "group_id": str(group_id)},
        )
        assign_delete_id = uuid.UUID(assign_delete_race["operation_id"])
        operation_ids.append(assign_delete_id)
        delete_result, assign_delete_result = await asyncio.gather(
            asyncio.to_thread(_node, identity, control, delete_race),
            asyncio.to_thread(_node, identity, control, assign_delete_race),
        )
        assert delete_result["ok"] or delete_result["error"]["code"] in {
            "group_busy",
            "account_busy",
        }
        assert assign_delete_result["ok"] or assign_delete_result["error"]["code"] in {
            "group_not_found",
            "group_busy",
            "account_busy",
        }
        group = await _assert_tracked_group_sync(maker, group_id, target_ids)
        if group is not None:
            final_delete = _command("delete_group", token, {"group_id": str(group_id)})
            operation_ids.append(uuid.UUID(final_delete["operation_id"]))
            deleted = await asyncio.to_thread(_node, identity, control, final_delete)
            assert deleted["ok"] and deleted["data"]["deleted"] is True
            replay_delete = await asyncio.to_thread(
                _node, identity, control, final_delete
            )
            assert replay_delete["ok"] and replay_delete["data"] == deleted["data"]
        else:
            replay_delete = await asyncio.to_thread(
                _node, identity, control, delete_race
            )
            assert replay_delete["ok"] and replay_delete["data"]["deleted"] is True
        group = await _assert_tracked_group_sync(maker, group_id, target_ids)
        assert group is None
        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor_id AND action='group.delete' "
                        "AND target_id=:target_id"
                    ),
                    {"actor_id": actor, "target_id": str(group_id)},
                )
                == 1
            )

        # Drop the response body after commit, restart the owner, and replay.
        lost_name = f"LostReply-{suffix[:12]}"
        lost_request = _command(
            "create_group", token, {"name": lost_name, "description": None}
        )
        lost_id = uuid.UUID(lost_request["operation_id"])
        operation_ids.append(lost_id)
        group_ids.append(lost_id)
        port = int(identity.origin.rsplit(":", 1)[1])
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            async with client.stream(
                "POST",
                identity.origin + "/v1/identity/groups/execute",
                headers={"Authorization": f"Bearer {control}"},
                json=lost_request,
            ) as response:
                assert response.status_code == 200
        identity.stop()
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            port=port,
            enable_keys=False,
            enable_sessions=False,
            enable_accounts=False,
            enable_groups=True,
            enable_presence=False,
        )
        lost_replay = await asyncio.to_thread(_node, identity, control, lost_request)
        assert lost_replay["ok"] and lost_replay["data"]["group"]["id"] == str(lost_id)
        async with maker.begin() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_group_operation_receipts "
                        "WHERE operation_id=:operation_id AND actor_id=:actor_id"
                    ),
                    {"operation_id": lost_id, "actor_id": actor},
                )
                == 1
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor_id AND action='group.create' AND target_id=:target_id"
                    ),
                    {"actor_id": actor, "target_id": str(lost_id)},
                )
                == 1
            )
        lost_delete = _command("delete_group", token, {"group_id": str(lost_id)})
        operation_ids.append(uuid.UUID(lost_delete["operation_id"]))
        assert (await asyncio.to_thread(_node, identity, control, lost_delete))["ok"]

        async with maker.begin() as db:
            await db.execute(
                text(
                    "SELECT aap_identity_v1.revoke_jti(CAST(:jti AS uuid), :retain_until)"
                ),
                {"jti": jtis[0], "retain_until": datetime.now(UTC) + timedelta(days=8)},
            )
        stale_request = _command("list_groups", token)
        operation_ids.append(uuid.UUID(stale_request["operation_id"]))
        stale = await asyncio.to_thread(_node, identity, control, stale_request)
        assert stale["ok"] is False
        assert stale["error"]["code"] == "credential_revoked"
        record_property(
            "group_node_process",
            "eight-operations,viewer-target,partial-bulk,ordered-duplicates,group-user-rename-delete-races,lost-response-restart,replay,current-jti-revocation",
        )
    finally:
        if identity is not None:
            identity.stop()
        await _cleanup_groups(
            maker,
            actor_ids=actor_ids,
            group_ids=group_ids,
            operation_ids=operation_ids,
            jtis=jtis,
        )


async def test_project_admin_group_scope_rechecks_workspace_and_hides_targets(
    test_engine, tmp_path, record_property
):
    maker, super_admin = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    actor_ids = [super_admin]
    group_ids = []
    project_ids = []
    operation_ids = []
    identity = None
    try:
        async with maker.begin() as db:
            manager = await create_user(
                db,
                "project_admin",
                f"groups-pa-{suffix}@example.test",
                "Workspace group manager",
            )
            new_owner = await create_user(
                db,
                "super_admin",
                f"groups-owner-{suffix}@example.test",
                "Replacement project owner",
            )
            owned_a = await create_user(
                db,
                "project_admin",
                f"groups-owned-a-{suffix}@example.test",
                "Owned member A",
            )
            owned_b = await create_user(
                db,
                "project_admin",
                f"groups-owned-b-{suffix}@example.test",
                "Owned member B",
            )
            foreign = await create_user(
                db,
                "project_admin",
                f"groups-foreign-{suffix}@example.test",
                "Foreign project admin",
            )
            global_employee = await create_user(
                db,
                "employee",
                f"groups-global-{suffix}@example.test",
                "Globally assignable employee",
            )
            race_member = await create_user(
                db,
                "project_admin",
                f"groups-race-{suffix}@example.test",
                "Owner race member",
            )
            project_a = await create_project(
                db, owner_id=manager.id, name=f"Owned group project A {suffix}"
            )
            project_b = await create_project(
                db, owner_id=manager.id, name=f"Owned group project B {suffix}"
            )
            foreign_project = await create_project(
                db, owner_id=super_admin, name=f"Foreign group project {suffix}"
            )
            await create_membership(
                db, project_id=project_a.id, user_id=owned_a.id, role="annotator"
            )
            await create_membership(
                db, project_id=project_a.id, user_id=race_member.id, role="annotator"
            )
            await create_membership(
                db, project_id=project_b.id, user_id=owned_b.id, role="annotator"
            )
            await create_membership(
                db, project_id=foreign_project.id, user_id=foreign.id, role="annotator"
            )
        actor_ids.extend(
            [
                manager.id,
                new_owner.id,
                owned_a.id,
                owned_b.id,
                foreign.id,
                global_employee.id,
                race_member.id,
            ]
        )
        project_ids.extend([project_a.id, project_b.id, foreign_project.id])
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_keys=False,
            enable_sessions=False,
            enable_accounts=False,
            enable_groups=True,
            enable_presence=False,
        )
        pa_token = create_access_token(
            str(manager.id), "project_admin", secret=secret, expire_minutes=15
        )

        create_request = _command(
            "create_group",
            pa_token,
            {"name": f"PA-group-{suffix[:12]}", "description": None},
        )
        create_id = uuid.UUID(create_request["operation_id"])
        operation_ids.append(create_id)
        group_ids.append(create_id)
        created = await asyncio.to_thread(_node, identity, control, create_request)
        assert created["ok"] and created["data"]["group"]["id"] == str(create_id)

        successful_requests = []
        for target in (owned_a, owned_b, global_employee):
            request = _command(
                "assign_user_group",
                pa_token,
                {"user_id": str(target.id), "group_id": str(create_id)},
            )
            operation_ids.append(uuid.UUID(request["operation_id"]))
            result = await asyncio.to_thread(_node, identity, control, request)
            assert result["ok"]
            successful_requests.append((target.id, request))

        foreign_request = _command(
            "assign_user_group",
            pa_token,
            {"user_id": str(foreign.id), "group_id": str(create_id)},
        )
        operation_ids.append(uuid.UUID(foreign_request["operation_id"]))
        forbidden = await asyncio.to_thread(_node, identity, control, foreign_request)
        assert forbidden["ok"] is False
        assert forbidden["error"]["code"] == "target_not_found"

        preview_request = _command(
            "preview_group_assignments",
            pa_token,
            {
                "user_ids": [str(owned_a.id), str(foreign.id), str(global_employee.id)],
                "group_id": str(create_id),
            },
        )
        operation_ids.append(uuid.UUID(preview_request["operation_id"]))
        preview = await asyncio.to_thread(_node, identity, control, preview_request)
        assert preview["ok"]
        assert await _receipt_count(maker, preview_request["operation_id"]) == 0
        assert preview["data"]["items"][0]["ok"] is True
        hidden_item = preview["data"]["items"][1]
        assert hidden_item["ok"] is False
        assert hidden_item["email"] is None and hidden_item["name"] is None
        assert preview["data"]["items"][2]["ok"] is True

        # An owner transfer races an assignment through Workspace's project lock.
        owner_race = _command(
            "assign_user_group",
            pa_token,
            {"user_id": str(race_member.id), "group_id": str(create_id)},
        )
        owner_race_id = uuid.UUID(owner_race["operation_id"])
        operation_ids.append(owner_race_id)

        async def transfer_owner():
            async with maker.begin() as db:
                await db.execute(
                    update(Project)
                    .where(Project.id == project_a.id)
                    .values(owner_id=new_owner.id)
                )

        race_result, _ = await asyncio.gather(
            asyncio.to_thread(_node, identity, control, owner_race), transfer_owner()
        )
        assert race_result["ok"] or race_result["error"]["code"] in {
            "group_busy",
            "account_busy",
            "target_not_found",
        }
        owner_replay = await asyncio.to_thread(_node, identity, control, owner_race)
        assert owner_replay["ok"] is False
        assert owner_replay["error"]["code"] == "target_not_found"

        # A successful receipt cannot reveal the target after membership revocation.
        owned_b_request = next(
            request for user_id, request in successful_requests if user_id == owned_b.id
        )
        async with maker.begin() as db:
            await db.execute(
                delete(ProjectMember).where(
                    ProjectMember.project_id == project_b.id,
                    ProjectMember.user_id == owned_b.id,
                )
            )
        membership_replay = await asyncio.to_thread(
            _node, identity, control, owned_b_request
        )
        assert membership_replay["ok"] is False
        assert membership_replay["error"]["code"] == "target_not_found"

        still_allowed = _command(
            "assign_user_group",
            pa_token,
            {"user_id": str(global_employee.id), "group_id": str(create_id)},
        )
        operation_ids.append(uuid.UUID(still_allowed["operation_id"]))
        assert (await asyncio.to_thread(_node, identity, control, still_allowed))["ok"]

        async with maker.begin() as db:
            await db.execute(
                update(User).where(User.id == manager.id).values(role="employee")
            )
        role_revoked = _command("list_groups", pa_token)
        operation_ids.append(uuid.UUID(role_revoked["operation_id"]))
        fenced = await asyncio.to_thread(_node, identity, control, role_revoked)
        assert fenced["ok"] is False
        assert fenced["error"] == {
            "code": "policy_rejected",
            "status_code": 403,
            "detail": "当前账号已无管理权限",
        }
        record_property(
            "group_workspace_scope",
            "project-admin,cross-project-employee,hidden-project-admin,owner-transfer-race,membership-revocation-replay,current-role-fence",
        )
    finally:
        if identity is not None:
            identity.stop()
        await _cleanup_groups(
            maker,
            actor_ids=actor_ids,
            group_ids=group_ids,
            project_ids=project_ids,
            operation_ids=operation_ids,
        )


async def test_group_container_preserves_unpaginated_catalog_and_narrow_grants(
    test_engine, tmp_path, record_property
):
    from tests.test_asset_storage_cleanup_runtime import _inspect_docker

    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    group_ids = []
    identity = None
    suffix = uuid.uuid4().hex[:12]
    try:
        async with maker.begin() as db:
            rows = [
                Group(
                    name=f"catalog-{suffix}-{index:04d}-{'x' * 70}",
                    description="large unpaginated Node fixture",
                )
                for index in range(600)
            ]
            db.add_all(rows)
            await db.flush()
            group_ids.extend(row.id for row in rows)
        identity = await _start_container_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_accounts=False,
            enable_groups=True,
            enable_presence=False,
        )
        token = _token(actor, secret)
        request = _command("list_groups", token)
        result = await asyncio.to_thread(_node, identity, control, request)
        assert result["ok"]
        assert len(result["data"]["items"]) >= 600
        encoded_size = len(json.dumps(result).encode())
        assert encoded_size > 64 * 1024
        details = _inspect_docker(identity.container_id)
        assert details["HostConfig"]["ReadonlyRootfs"] is True
        assert details["Config"]["User"] == "10003:10003"
        async with maker.begin() as db:
            execute_names = set(
                (
                    await db.execute(
                        text(
                            "SELECT proc.proname "
                            "FROM pg_catalog.pg_proc AS proc "
                            "JOIN pg_catalog.pg_namespace AS ns ON ns.oid=proc.pronamespace "
                            "WHERE ns.nspname='aap_identity_groups_v1' "
                            "AND pg_catalog.has_function_privilege("
                            "'identity_group_client',proc.oid,'EXECUTE')"
                        )
                    )
                ).scalars()
            )
            assert execute_names == {"group_policy", "execute_group_command"}
            for table in ("groups", "users", "projects", "project_members"):
                assert not await db.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege("
                        "'identity_group_client',:table_name,'SELECT')"
                    ),
                    {"table_name": f"public.{table}"},
                )
        record_property(
            "group_container_catalog",
            json.dumps(
                {
                    "groups": len(result["data"]["items"]),
                    "response_bytes": encoded_size,
                    "container_memory_limit": details["HostConfig"]["Memory"],
                    "functions": sorted(execute_names),
                }
            ),
        )
    finally:
        if identity is not None:
            identity.stop()
        await _cleanup_groups(
            maker,
            actor_ids=[actor],
            group_ids=group_ids,
        )
