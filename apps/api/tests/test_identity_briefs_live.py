"""Installed Identity owner qualification for private UserBrief reads."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import secrets
import subprocess
import uuid

import httpx
import pytest
from sqlalchemy import delete, select, text

from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from tests.factory import create_membership, create_project, create_task
from tests.test_identity_admission_live import ROOT, _start_identity, _world

BRIEFS_ENDPOINT = "/v1/identity/briefs/execute"
LEGACY_FIXTURE = Path("apps/api/tests/fixtures/identity_briefs_legacy.json")
BRIEF_FIELDS = {
    "id",
    "name",
    "email",
    "role",
    "avatar_initial",
    "avatar_ref",
}

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_BRIEFS_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity briefs verifier",
    ),
]


def _legacy_fixture():
    return json.loads((ROOT / LEGACY_FIXTURE).read_text(encoding="utf-8"))


def _request(operation, payload):
    return {
        "schema": "aap-identity-briefs/v1",
        "operation": operation,
        "payload": payload,
    }


def _node(identity, control, request):
    result = subprocess.run(
        ["node", str(ROOT / "apps/api/tests/fixtures/identity_briefs_peer.mjs")],
        input=json.dumps(
            {"url": identity.origin, "control_token": control, "request": request},
            ensure_ascii=False,
            allow_nan=False,
        ),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert result.returncode == 0, "Independent Node briefs consumer failed"
    assert control not in result.stdout and control not in result.stderr
    return json.loads(result.stdout)


async def _seed_briefs_world(maker, fixture):
    inputs = fixture["inputs"]
    user_ids = [uuid.UUID(item["id"]) for item in inputs]
    project_id = uuid.UUID(fixture["project_id"])
    extra_id = uuid.uuid4()
    pending_id = uuid.uuid4()
    user_ids.append(extra_id)
    user_ids.append(pending_id)
    async with maker.begin() as db:
        assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
        baseline_ids = [uuid.UUID(item["id"]) for item in inputs]
        baseline_ids.extend([extra_id, pending_id])
        existing_users = await db.scalar(
            select(text("count(*)")).select_from(User).where(User.id.in_(baseline_ids))
        )
        existing_project = await db.get(Project, project_id)
        assert existing_users == 0 and existing_project is None
        users = {}
        for item in inputs:
            user = User(
                id=uuid.UUID(item["id"]),
                email=item["email"],
                name=item["name"],
                password_hash="brief-fixture-only",
                role=item["role"],
                is_active=item["is_active"],
                status="offline",
                disabled_kind="suspended" if item["key"] == "inactive" else None,
                avatar_ref=item["avatar_ref"],
            )
            db.add(user)
            users[item["key"]] = user
        fallback = User(
            id=extra_id,
            email="brief-empty-role@example.test",
            name="",
            password_hash="brief-fixture-only",
            role="legacy-global-role",
            is_active=True,
            status="offline",
            avatar_ref=None,
        )
        pending = User(
            id=pending_id,
            email="brief-pending-retirement@example.test",
            name="Pending retirement",
            password_hash="brief-fixture-only",
            role="employee",
            is_active=True,
            status="offline",
            deactivation_requested_at=datetime.now(UTC),
            deactivation_scheduled_at=datetime.now(UTC) + timedelta(days=7),
            deactivation_reason="preserved reservation",
        )
        db.add_all([fallback, pending])
        await db.flush()
        project = await create_project(
            db,
            owner_id=users["owner"].id,
            name="Captured UserBrief baseline",
        )
        project.id = project_id
        await db.flush()
        await create_membership(
            db,
            project_id=project_id,
            user_id=users["staff"].id,
            role="reviewer",
            assigned_by=users["owner"].id,
        )
        await create_membership(
            db,
            project_id=project_id,
            user_id=users["historical"].id,
            role="annotator",
            assigned_by=users["owner"].id,
        )
        await create_membership(
            db,
            project_id=project_id,
            user_id=extra_id,
            role="",
            assigned_by=users["owner"].id,
        )
        historical_task = await create_task(
            db, project_id=project_id, status="completed"
        )
        historical_task.assignee_id = users["combining"].id
    return {
        "user_ids": user_ids,
        "project_ids": [project_id],
        "extra_id": extra_id,
        "pending_id": pending_id,
        "users": users,
    }


async def _cleanup_briefs(maker, *, user_ids, project_ids):
    users = [uuid.UUID(str(value)) for value in user_ids]
    projects = [uuid.UUID(str(value)) for value in project_ids]
    async with maker.begin() as db:
        assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
        if projects:
            task_ids = select(Task.id).where(Task.project_id.in_(projects))
            await db.execute(
                delete(TaskLock).where(
                    TaskLock.user_id.in_(users) | TaskLock.task_id.in_(task_ids)
                )
            )
            await db.execute(delete(Task).where(Task.project_id.in_(projects)))
            await db.execute(
                delete(TaskBatch).where(TaskBatch.project_id.in_(projects))
            )
            await db.execute(
                delete(ProjectMember).where(
                    ProjectMember.project_id.in_(projects)
                    | ProjectMember.user_id.in_(users)
                )
            )
            await db.execute(delete(Project).where(Project.id.in_(projects)))
        await db.execute(delete(User).where(User.id.in_(users)))


@pytest.mark.asyncio
async def test_installed_owner_node_consumer_matches_legacy_fixture_and_accepts_large_inputs(
    test_engine, tmp_path
):
    fixture = _legacy_fixture()
    maker, actor_id = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    world = {"user_ids": [], "project_ids": []}
    identity = None
    try:
        world = await _seed_briefs_world(maker, fixture)
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_keys=False,
            enable_sessions=False,
            enable_accounts=False,
            enable_groups=False,
            enable_invitations=False,
            enable_directory=False,
            enable_admin_password=False,
            enable_platform_role=False,
            enable_briefs=True,
            enable_suspension=False,
            enable_deactivation=False,
            enable_deletions=False,
            enable_offboarding=False,
            enable_presence=False,
        )
        input_ids = [item["id"] for item in fixture["inputs"]]
        missing_ids = [
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"briefs-missing-{index}"))
            for index in range(501)
        ]
        global_ids = [
            *missing_ids,
            *input_ids,
            str(world["pending_id"]),
            input_ids[0],
            input_ids[2],
        ]
        assert len(global_ids) > 500

        async with httpx.AsyncClient(trust_env=False, timeout=20) as client:
            unauthorized = await client.post(
                identity.origin + BRIEFS_ENDPOINT,
                headers={"Authorization": "Bearer wrong-briefs-control"},
                json=_request("get_user_briefs", {"user_ids": []}),
            )
        assert unauthorized.status_code == 401, unauthorized.text
        assert control not in unauthorized.text
        assert secret not in unauthorized.text

        global_reply = await asyncio.to_thread(
            _node,
            identity,
            control,
            _request("get_user_briefs", {"user_ids": global_ids}),
        )
        assert global_reply["data"]["project_id"] is None
        actual_global = {user["id"]: user for user in global_reply["data"]["users"]}
        expected_global = dict(fixture["responses"]["global"])
        expected_global[str(world["pending_id"])] = {
            "id": str(world["pending_id"]),
            "name": "Pending retirement",
            "email": "brief-pending-retirement@example.test",
            "role": "employee",
            "avatar_initial": "P",
            "avatar_ref": None,
        }
        assert actual_global == expected_global
        assert all(set(user) == BRIEF_FIELDS for user in actual_global.values())

        project_ids = [*input_ids, input_ids[1], input_ids[-1]]
        project_reply = await asyncio.to_thread(
            _node,
            identity,
            control,
            _request(
                "get_project_user_briefs",
                {"project_id": fixture["project_id"], "user_ids": project_ids},
            ),
        )
        assert project_reply["data"]["project_id"] == fixture["project_id"]
        actual_project = {user["id"]: user for user in project_reply["data"]["users"]}
        assert actual_project == fixture["responses"]["project"]

        extra_role_reply = await asyncio.to_thread(
            _node,
            identity,
            control,
            _request(
                "get_project_user_briefs",
                {
                    "project_id": fixture["project_id"],
                    "user_ids": [str(world["extra_id"])],
                },
            ),
        )
        fallback = extra_role_reply["data"]["users"]
        assert fallback == [
            {
                "id": str(world["extra_id"]),
                "name": "",
                "email": "brief-empty-role@example.test",
                "role": "legacy-global-role",
                "avatar_initial": "B",
                "avatar_ref": None,
            }
        ]

        empty_reply = await asyncio.to_thread(
            _node,
            identity,
            control,
            _request("get_user_briefs", {"user_ids": []}),
        )
        assert empty_reply["data"] == {"project_id": None, "users": []}
    finally:
        if identity:
            identity.stop()
        await _cleanup_briefs(
            maker,
            user_ids=[actor_id, *world["user_ids"]],
            project_ids=world["project_ids"],
        )


@pytest.mark.asyncio
async def test_installed_owner_briefs_capability_disabled_returns_service_unavailable(
    test_engine, tmp_path
):
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = _start_identity(
        test_engine,
        tmp_path,
        secret,
        control,
        enable_keys=False,
        enable_sessions=False,
        enable_accounts=False,
        enable_groups=False,
        enable_presence=False,
    )
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=20) as client:
            response = await client.post(
                identity.origin + BRIEFS_ENDPOINT,
                headers={"Authorization": f"Bearer {control}"},
                json=_request("get_user_briefs", {"user_ids": []}),
            )
        assert response.status_code == 503
        assert response.json() == {"detail": "Identity brief reads unavailable"}
        assert control not in response.text
        assert secret not in response.text
    finally:
        identity.stop()
