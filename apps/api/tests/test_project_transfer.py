"""Ownership-transfer endpoint regressions.

The transfer endpoint previously read the project and the target role without
any lock; it now serializes with platform-role changes through the shared
account -> project locking order, so a concurrent demotion and ownership
transfer cannot interleave into an unmanageable owner state (review comment:
"serialize role demotion with ownership transfer").
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import uuid

import httpx
import pytest

from app.core.security import create_access_token
from app.db.models.project import Project
from app.services.identity_contracts import IdentityActorFacts
from tests.factory import create_project, create_user

pytestmark = pytest.mark.asyncio


def _headers(user) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_access_token(subject=str(user.id), role=user.role)}"
    }


async def _transfer(
    client: httpx.AsyncClient, project_id: uuid.UUID, new_owner_id: uuid.UUID, actor
) -> httpx.Response:
    return await client.post(
        f"/api/v1/projects/{project_id}/transfer",
        json={"new_owner_id": str(new_owner_id)},
        headers=_headers(actor),
    )


async def test_transfer_moves_ownership_to_project_admin(
    db_session, httpx_client, super_admin
):
    actor, _ = super_admin
    project = await create_project(
        db_session, owner_id=actor.id, name="Transfer Source"
    )
    target = await create_user(
        db_session,
        "project_admin",
        f"transfer-target-{uuid.uuid4().hex[:6]}@test.local",
        "PA",
    )

    response = await _transfer(httpx_client, project.id, target.id, actor)
    assert response.status_code == 200, response.text
    assert response.json()["owner_id"] == str(target.id)

    refreshed = await db_session.get(Project, project.id)
    await db_session.refresh(refreshed)
    assert refreshed.owner_id == target.id


async def test_transfer_rejects_non_project_admin_target(
    db_session, httpx_client, super_admin
):
    actor, _ = super_admin
    project = await create_project(
        db_session, owner_id=actor.id, name="Transfer Reject"
    )
    employee = await create_user(
        db_session,
        "employee",
        f"transfer-emp-{uuid.uuid4().hex[:6]}@test.local",
        "E",
    )

    response = await _transfer(httpx_client, project.id, employee.id, actor)
    assert response.status_code == 400


async def test_transfer_requires_active_target(db_session, httpx_client, super_admin):
    actor, _ = super_admin
    project = await create_project(db_session, owner_id=actor.id, name="Transfer IA")
    target = await create_user(
        db_session,
        "project_admin",
        f"transfer-inactive-{uuid.uuid4().hex[:6]}@test.local",
        "PA",
    )
    target.is_active = False
    await db_session.flush()

    response = await _transfer(httpx_client, project.id, target.id, actor)
    assert response.status_code == 404


@pytest.mark.skipif(
    os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
    or os.getenv("AAP_IDENTITY_PLATFORM_ROLE_LIVE_ACCEPTANCE") != "1",
    reason="requires the owned platform-role Identity verifier",
)
async def test_transfer_and_native_role_demotion_preserve_owner_lock_orders(
    test_engine, tmp_path
):
    from app.application import project_workspace
    from tests.test_identity_admission_live import _start_identity, _world
    from tests.test_identity_platform_role_live import (
        _cleanup_platform_roles,
        _node,
        _platform_role_request,
        _role_token,
        _user_snapshot,
    )

    maker, actor_id = await _world(test_engine)
    suffix = uuid.uuid4().hex
    user_ids = [actor_id]
    project_ids = []
    operation_ids = []
    identity = None
    transfer_task = None
    project_lock_acquired = asyncio.Event()
    release_project_lock = asyncio.Event()
    transfer_actor = None
    held_project_id = None

    async def committed_transfer(project_id, new_owner_id):
        # A remote owner must observe a real commit, not the API fixture's savepoint.
        async with maker.begin() as db:
            project = await project_workspace.transfer_project_owner(
                db,
                actor=IdentityActorFacts(
                    id=transfer_actor.id,
                    name=transfer_actor.name,
                    email=transfer_actor.email,
                    role=transfer_actor.role,
                    is_active=transfer_actor.is_active,
                ),
                project_id=project_id,
                new_owner_id=new_owner_id,
            )
            if project_id == held_project_id:
                project_lock_acquired.set()
                await asyncio.wait_for(release_project_lock.wait(), timeout=15)
            return project.new_owner_id

    try:
        async with maker.begin() as db:
            transfer_actor = await create_user(
                db,
                "super_admin",
                f"write-fence-transfer-actor-{suffix}@example.test",
                "Transfer operator",
            )
            demotion_first = await create_user(
                db,
                "project_admin",
                f"demotion-first-{suffix}@example.test",
                "Demotion first owner",
            )
            first_receiver = await create_user(
                db,
                "project_admin",
                f"demotion-first-receiver-{suffix}@example.test",
                "Demotion first receiver",
            )
            demotion_project = await create_project(
                db, owner_id=demotion_first.id, name=f"Demotion first {suffix}"
            )
            transfer_first = await create_user(
                db,
                "project_admin",
                f"transfer-first-{suffix}@example.test",
                "Transfer first owner",
            )
            second_receiver = await create_user(
                db,
                "project_admin",
                f"transfer-first-receiver-{suffix}@example.test",
                "Transfer first receiver",
            )
            transfer_project = await create_project(
                db, owner_id=transfer_first.id, name=f"Transfer first {suffix}"
            )
            user_ids.extend(
                [
                    transfer_actor.id,
                    demotion_first.id,
                    first_receiver.id,
                    transfer_first.id,
                    second_receiver.id,
                ]
            )
            project_ids.extend([demotion_project.id, transfer_project.id])

        secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        actor_token = _role_token(actor_id, "super_admin", secret)
        first_request = _platform_role_request(
            "change_platform_role",
            actor_token,
            demotion_first.id,
            "employee",
        )
        first_operation = uuid.UUID(first_request["operation_id"])
        operation_ids.append(first_operation)
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_sessions=False,
            enable_accounts=False,
            enable_groups=False,
            enable_invitations=False,
            enable_directory=False,
            enable_admin_password=False,
            enable_platform_role=True,
            enable_deactivation=False,
            enable_presence=False,
        )

        # Demotion-first keeps the captured owned-project blocker and does not
        # create a receipt; transfer then lets the same operation succeed.
        blocked = await asyncio.to_thread(_node, identity, control, first_request)
        assert blocked["ok"] is False
        assert blocked["error"]["code"] == "platform_role_change_blocked"
        assert blocked["error"]["status_code"] == 409
        assert blocked["error"]["detail"] == {
            "reason": "platform_role_change_blocked",
            "blockers": ["账号仍是项目负责人，请先转移项目后变更平台角色"],
        }
        assert (await _user_snapshot(maker, demotion_first.id))[
            "role"
        ] == "project_admin"
        assert (
            await committed_transfer(demotion_project.id, first_receiver.id)
            == first_receiver.id
        )
        demotion = await asyncio.to_thread(_node, identity, control, first_request)
        assert demotion["ok"] is True
        assert demotion["data"]["user"]["role"] == "employee"
        assert (await _user_snapshot(maker, demotion_first.id))["role"] == "employee"

        second_request = _platform_role_request(
            "change_platform_role",
            actor_token,
            transfer_first.id,
            "employee",
        )
        second_operation = uuid.UUID(second_request["operation_id"])
        operation_ids.append(second_operation)
        held_project_id = transfer_project.id
        transfer_task = asyncio.create_task(
            committed_transfer(transfer_project.id, second_receiver.id)
        )
        await asyncio.wait_for(project_lock_acquired.wait(), timeout=10)
        busy = await asyncio.to_thread(_node, identity, control, second_request)
        assert busy["ok"] is False
        assert busy["error"]["code"] == "project_busy"
        assert busy["error"]["status_code"] == 409
        assert busy["error"]["detail"] == {"reason": "project_busy"}
        assert (await _user_snapshot(maker, transfer_first.id))[
            "role"
        ] == "project_admin"
        release_project_lock.set()
        transferred_owner = await asyncio.wait_for(transfer_task, timeout=20)
        transfer_task = None
        assert transferred_owner == second_receiver.id
        raced_demotion = await asyncio.to_thread(
            _node, identity, control, second_request
        )
        assert raced_demotion["ok"] is True
        assert raced_demotion["data"]["user"]["role"] == "employee"
        assert (await _user_snapshot(maker, transfer_first.id))["role"] == "employee"
    finally:
        release_project_lock.set()
        if transfer_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(transfer_task, timeout=20)
        if identity is not None:
            identity.stop()
        await _cleanup_platform_roles(
            maker,
            user_ids=user_ids,
            project_ids=project_ids,
            operation_ids=operation_ids,
        )
