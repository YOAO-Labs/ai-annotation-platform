"""Standalone owner-transfer receipts and public caller-session behavior."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess
import uuid

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token
from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.services.workspace_membership import (
    add_member,
    change_role,
    preview_role_change,
)
from app.services.workspace_owner_transfer import transfer_project_owner
from tests.factory import create_project, create_user
from tests.labeling_class_query_helpers import make_request_runtime
from tests.workspace_configuration_helpers import public_workspace_client


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_WORKSPACE_OWNER_TRANSFER_LIVE") != "1",
        reason="requires the disposable Workspace owner-transfer verifier",
    ),
]

ROOT = Path(__file__).resolve().parents[3]
NODE_PEER = ROOT / "apps/api/tests/fixtures/workspace_owner_transfer_peer.mjs"
SCHEMA = "aap-workspace-owner-transfer/v1"


def _owner_transfer_dsn(test_engine) -> str:
    from tests.workspace_owner_transfer_helpers import transfer_dsn

    return transfer_dsn(test_engine)


def _request(actor_id, project_id, new_owner_id, operation_id=None):
    return {
        "schema": SCHEMA,
        "operation_id": str(operation_id or uuid.uuid4()),
        "actor_id": str(actor_id),
        "project_id": str(project_id),
        "new_owner_id": str(new_owner_id),
    }


def _peer(origin, token, request, *, expect_transport_failure=False):
    call = {
        "mode": "owner_http",
        "origin": origin,
        "control_token": token,
        "request": request,
        "expect_transport_failure": expect_transport_failure,
    }
    result = subprocess.run(
        ["node", str(NODE_PEER)],
        input=json.dumps(call, ensure_ascii=True),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, "independent Node owner-transfer consumer failed"
    assert token not in result.stdout
    assert token not in result.stderr
    return json.loads(result.stdout)


def _auth(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role)}"}


async def _seed_world(test_engine, label):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    async with maker.begin() as db:
        actor = await create_user(
            db,
            "super_admin",
            f"owner-transfer-{label}-actor-{suffix}@example.test",
            "Transfer actor",
        )
        first = await create_user(
            db,
            "project_admin",
            f"owner-transfer-{label}-first-{suffix}@example.test",
            "First owner",
        )
        second = await create_user(
            db,
            "project_admin",
            f"owner-transfer-{label}-second-{suffix}@example.test",
            "Second owner",
        )
        employee = await create_user(
            db,
            "employee",
            f"owner-transfer-{label}-employee-{suffix}@example.test",
            "Not an owner",
        )
        project = await create_project(
            db, owner_id=actor.id, name=f"Owner transfer {label} {suffix}"
        )
    return maker, actor, first, second, employee, project


async def _snapshot(maker, project_id, actor_id):
    async with maker() as db:
        project = await db.get(Project, project_id)
        assert project is not None
        audits = await db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.actor_id == actor_id,
                AuditLog.target_type == "project",
                AuditLog.target_id == str(project_id),
            )
        )
        members = await db.scalar(
            select(func.count(ProjectMember.id)).where(
                ProjectMember.project_id == project_id
            )
        )
        return project.owner_id, project.updated_at, audits, members


async def _cleanup_world(maker, actor_id, project_id, user_ids):
    async with maker.begin() as db:
        database = await db.scalar(text("SELECT current_database()"))
        assert database.endswith("_test")
        await db.execute(
            text(
                "DELETE FROM public.workspace_owner_transfer_operation_receipts "
                "WHERE actor_id=:actor_id AND project_id=:project_id"
            ),
            {"actor_id": actor_id, "project_id": project_id},
        )
        await db.execute(delete(Project).where(Project.id == project_id))
        await db.execute(delete(User).where(User.id.in_(list(user_ids))))


async def test_node_owner_transfer_survives_response_loss_restart_and_supersession(
    test_engine, app_module, monkeypatch
):
    from tests.workspace_owner_transfer_helpers import node_provider_url

    dsn = _owner_transfer_dsn(test_engine)
    control = secrets.token_urlsafe(32)
    maker, actor, first, second, employee, project = await _seed_world(
        test_engine, "replay"
    )
    operation_id = uuid.uuid4()
    command = _request(actor.id, project.id, first.id, operation_id)
    users = [actor.id, first.id, second.id, employee.id]
    try:
        async with node_provider_url(
            dsn, control, drop_operation_id=str(operation_id)
        ) as origin:
            lost = _peer(origin, control, command, expect_transport_failure=True)
            assert lost == {"ok": True, "transport_failure": "connection_lost"}

        async with node_provider_url(dsn, control) as origin:
            recovered = _peer(origin, control, command)
            assert recovered["ok"] is True
            frozen = recovered["reply"]
            assert frozen["ok"] is True
            assert frozen["data"]["previous_owner_id"] == str(actor.id)
            assert frozen["data"]["changed"] is True
            owner, first_updated_at, audit_count, member_count = await _snapshot(
                maker, project.id, actor.id
            )
            assert owner == first.id
            assert member_count == 0

            async with public_workspace_client(
                test_engine, app_module, monkeypatch
            ) as client:
                response = await _public_transfer(client, project.id, second.id, actor)
            assert response.status_code == 200, response.text
            assert response.json()["owner_id"] == str(second.id)
            superseded = await _snapshot(maker, project.id, actor.id)
            assert superseded[0] == second.id
            assert superseded[1] >= first_updated_at

            replayed = _peer(origin, control, command)
            assert replayed == recovered
            after_replay = await _snapshot(maker, project.id, actor.id)
            assert after_replay == superseded
            assert after_replay[2] == audit_count
    finally:
        await _cleanup_world(maker, actor.id, project.id, users)


async def _public_transfer(client, project_id, new_owner_id, actor):
    return await client.post(
        f"/api/v1/projects/{project_id}/transfer",
        json={"new_owner_id": str(new_owner_id)},
        headers=_auth(actor),
    )


async def test_public_owner_transfer_preserves_response_and_noop_behavior(
    test_engine, app_module, monkeypatch
):
    maker, actor, first, second, employee, project = await _seed_world(
        test_engine, "caller"
    )
    users = [actor.id, first.id, second.id, employee.id]
    try:
        async with public_workspace_client(
            test_engine, app_module, monkeypatch
        ) as client:
            changed = await _public_transfer(client, project.id, first.id, actor)
            assert changed.status_code == 200, changed.text
            assert changed.json()["owner_id"] == str(first.id)
            after_change = await _snapshot(maker, project.id, actor.id)
            assert after_change[0] == first.id

            noop = await _public_transfer(client, project.id, first.id, actor)
            assert noop.status_code == 200, noop.text
            assert noop.json()["owner_id"] == str(first.id)
            after_noop = await _snapshot(maker, project.id, actor.id)
            assert after_noop[1] == after_change[1]

            rejected = await _public_transfer(client, project.id, employee.id, actor)
            assert rejected.status_code == 400
            assert rejected.json()["detail"] == "仅可转移给 project_admin"
            assert await _snapshot(maker, project.id, actor.id) == after_noop

            project_admin_actor = await _create_second_actor(maker, "caller", users)
            forbidden = await _public_transfer(
                client, project.id, second.id, project_admin_actor
            )
            assert forbidden.status_code == 403
            assert await _snapshot(maker, project.id, actor.id) == after_noop
        assert after_noop[2] == after_change[2]
        assert after_noop[3] == 0
    finally:
        await _cleanup_world(maker, actor.id, project.id, users)


async def test_membership_and_owner_transfer_share_the_ordinary_caller_transaction(
    test_engine,
):
    maker, actor, first, second, employee, project = await _seed_world(
        test_engine, "caller-rollback"
    )
    users = [actor.id, first.id, second.id, employee.id]
    engine = None
    initial = await _snapshot(maker, project.id, actor.id)
    try:
        engine, runtime = await make_request_runtime(
            test_engine, application_name="workspace-membership-transfer-caller"
        )
        async with runtime() as caller:
            member = await add_member(
                caller,
                project_id=project.id,
                actor_id=actor.id,
                target_user_id=employee.id,
                project_role="annotator",
            )
            preview = await preview_role_change(
                caller,
                project_id=project.id,
                actor_id=actor.id,
                member_id=member.id,
                target_role="viewer",
            )
            assert preview["blockers"] == []
            updated = await change_role(
                caller,
                project_id=project.id,
                actor_id=actor.id,
                member_id=member.id,
                target_role="viewer",
                expected_version=member.version,
                preview_token=preview["preview_token"],
                reason="Caller-owned role change",
            )
            assert updated.role == "viewer" and updated.version == member.version + 1
            transfer = await transfer_project_owner(
                caller, actor_id=actor.id, project_id=project.id, new_owner_id=first.id
            )
            assert transfer.data.changed and transfer.data.previous_owner_id == actor.id
            assert caller.in_transaction()
            assert await _snapshot(maker, project.id, actor.id) == initial
            await caller.rollback()
        assert await _snapshot(maker, project.id, actor.id) == initial
        async with maker() as observer:
            for owner in ("membership", "owner_transfer"):
                count = await observer.scalar(
                    text(
                        f"SELECT count(*) FROM public.workspace_{owner}_operation_receipts "
                        "WHERE project_id=:id"
                    ),
                    {"id": project.id},
                )
                assert count == 0
    finally:
        if engine is not None:
            await engine.dispose()
        async with maker.begin() as cleanup:
            await cleanup.execute(
                text(
                    "DELETE FROM public.workspace_membership_operation_receipts "
                    "WHERE project_id=:id"
                ),
                {"id": project.id},
            )
        await _cleanup_world(maker, actor.id, project.id, users)


async def _create_second_actor(maker, label, users):
    async with maker.begin() as db:
        actor = await create_user(
            db,
            "project_admin",
            f"owner-transfer-{label}-non-super-{uuid.uuid4()}@example.test",
            "Project administrator",
        )
        users.append(actor.id)
        return actor
