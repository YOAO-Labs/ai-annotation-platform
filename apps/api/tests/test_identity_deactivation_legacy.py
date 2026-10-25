"""Native deactivation reservations coexist safely with legacy administration."""

from __future__ import annotations

import uuid

import pytest

from app.db.models.user import User
from tests.factory import create_user


@pytest.mark.parametrize("operation", ["delete", "deactivate", "demote", "offboard"])
async def test_legacy_reductions_preserve_the_last_unreserved_super_admin(
    operation, httpx_client, db_session, super_admin
):
    actor, token = super_admin
    target = await create_user(
        db_session, "super_admin", f"successor-{uuid.uuid4()}@test.local", "Successor"
    )
    headers = {"Authorization": f"Bearer {token}"}
    accepted = await httpx_client.post(
        "/api/v1/auth/me/deactivation-request", headers=headers, json={}
    )
    assert accepted.status_code == 200, accepted.text
    path = f"/api/v1/users/{target.id}"
    if operation == "delete":
        result = await httpx_client.delete(path, headers=headers)
    elif operation == "deactivate":
        result = await httpx_client.post(f"{path}/deactivate", headers=headers)
    elif operation == "demote":
        result = await httpx_client.patch(
            f"{path}/role", headers=headers, json={"role": "employee"}
        )
    else:
        result = await httpx_client.get(f"{path}/offboarding-preview", headers=headers)
    assert result.status_code == 400, result.text
    await db_session.refresh(target)
    await db_session.refresh(actor)
    assert target.is_active and target.role == "super_admin"
    assert actor.deactivation_scheduled_at is not None


async def test_an_inactive_super_admin_can_finish_handoff_with_one_active_successor(
    httpx_client, db_session, super_admin
):
    _, token = super_admin
    target = await create_user(
        db_session, "super_admin", f"inactive-{uuid.uuid4()}@test.local", "Inactive"
    )
    target.is_active = False
    target.disabled_kind = "emergency_suspended"
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/api/v1/users/{target.id}"
    preview = await httpx_client.get(f"{path}/offboarding-preview", headers=headers)
    assert preview.status_code == 200, preview.text
    assert preview.json()["can_commit"] is True
    result = await httpx_client.post(
        f"{path}/offboarding",
        headers=headers,
        json={
            "preview_version": preview.json()["preview_version"],
            "mode": "handoff",
            "projects": [],
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["user"]["is_active"] is False


async def test_accepted_request_survives_administrative_suspend_and_reactivate(
    httpx_client, db_session, super_admin, annotator
):
    _, admin_token = super_admin
    target, token = annotator
    accepted = await httpx_client.post(
        "/api/v1/auth/me/deactivation-request",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "Accepted intent"},
    )
    assert accepted.status_code == 200, accepted.text
    await db_session.refresh(target)
    original = (
        target.deactivation_requested_at,
        target.deactivation_scheduled_at,
        target.deactivation_reason,
    )
    assert all(value is not None for value in original)
    headers = {"Authorization": f"Bearer {admin_token}"}
    for action in ("deactivate", "reactivate"):
        result = await httpx_client.post(
            f"/api/v1/users/{target.id}/{action}", headers=headers
        )
        assert result.status_code == 200, result.text
        await db_session.refresh(target)
        assert (
            target.deactivation_requested_at,
            target.deactivation_scheduled_at,
            target.deactivation_reason,
        ) == original
    assert target.is_active
    assert await db_session.get(User, target.id) is target
