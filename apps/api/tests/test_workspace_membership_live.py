from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token
from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from tests.factory import (
    create_batch,
    create_membership,
    create_project,
    create_task,
    create_user,
)

ROOT = Path(__file__).resolve().parents[3]
PEER = ROOT / "apps/api/tests/fixtures/workspace_membership_peer.mjs"
LEGACY = ROOT / "apps/api/tests/fixtures/workspace_membership_legacy.json"
WORKSPACE_ROLE_URL = "AAP_MODULE_DATA_WORKSPACE_MEMBERSHIP_URL"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_WORKSPACE_MEMBERSHIP_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Workspace membership verifier",
    ),
]


def _legacy() -> dict:
    if not LEGACY.is_file():
        pytest.fail("Root did not integrate the captured Workspace membership baseline")
    value = json.loads(LEGACY.read_text(encoding="utf-8"))
    assert value.get("kind") == "actual-legacy-workspace-membership"
    assert len(value["responses"]) == 20
    assert len(value["digest_vectors"]) == 8
    assert len(value["audits"]) == 4
    return value


def _membership_dsn(test_engine) -> str:
    raw = os.environ[WORKSPACE_ROLE_URL]
    value = make_url(raw)
    assert (value.host, value.port, value.database) == (
        "127.0.0.1",
        test_engine.url.port,
        test_engine.url.database,
    )
    assert value.database.endswith("_test")
    container_id = os.environ["AAP_MODULE_DATA_PG_CONTAINER_ID"]
    assert len(container_id) == 64 and all(
        c in "0123456789abcdef" for c in container_id
    )
    return value.set(drivername="postgresql").render_as_string(hide_password=False)


def _node(*args: str, payload: dict | None = None) -> dict:
    result = subprocess.run(
        ["node", str(PEER), *args],
        input=(
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
            if payload is not None
            else None
        ),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=60,
    )
    if payload is not None:
        for secret_field in ("control_token", "bearer_token"):
            secret = payload.get(secret_field)
            if isinstance(secret, str):
                assert secret not in result.stdout + result.stderr
    assert result.returncode == 0, (
        f"Independent Workspace membership peer failed: {result.stderr.strip()}"
    )
    return json.loads(result.stdout)


async def _node_public(
    origin: str,
    bearer_token: str,
    *,
    method: str,
    path: str,
    body: dict | None,
    operation: str,
    expected: dict,
    aliases: dict,
) -> dict:
    return await asyncio.to_thread(
        _node,
        "--call",
        payload={
            "mode": "public_http",
            "origin": origin,
            "bearer_token": bearer_token,
            "method": method,
            "path": path,
            "body": body,
            "operation": operation,
            "expected": expected,
            "aliases": aliases,
        },
    )


async def _seed_membership_world(test_engine, legacy):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    # Preserve frozen UUID sort order without sharing rows between bindings.
    prefix = secrets.randbits(96) << 32
    identifiers = {
        alias: uuid.UUID(int=prefix + index, version=4)
        for index, (_, alias) in enumerate(sorted(legacy["aliases"].items()))
    }
    aliases: dict[str, str] = {}
    users: dict[str, User] = {}
    project = None
    memberships = {}
    tasks = {}
    batch = None
    async with maker.begin() as db:
        for name, platform_role in (
            ("owner", "project_admin"),
            ("member", "employee"),
            ("annotator", "employee"),
            ("reviewer", "employee"),
            ("other_reviewer", "employee"),
            ("new_employee", "employee"),
            ("viewer", "viewer"),
        ):
            users[name] = await create_user(
                db,
                platform_role,
                f"member-baseline-{name}-{uuid.uuid4().hex}@example.test",
                name,
            )
            users[name].id = identifiers[name]
            await db.flush()
            aliases[str(users[name].id)] = name

        project = await create_project(
            db, owner_id=users["owner"].id, name="Membership baseline"
        )
        project.id = identifiers["project"]
        await db.flush()
        aliases[str(project.id)] = "project"
        for position, (name, role) in enumerate(
            (
                ("member", "annotator"),
                ("annotator", "annotator"),
                ("reviewer", "reviewer"),
                ("other_reviewer", "reviewer"),
            )
        ):
            memberships[name] = await create_membership(
                db,
                project_id=project.id,
                user_id=users[name].id,
                role=role,
                assigned_by=users["owner"].id,
            )
            memberships[name].id = identifiers[f"membership_{name}"]
            memberships[name].assigned_at = datetime(2020, 1, 4 - position, tzinfo=UTC)
            await db.flush()
            aliases[str(memberships[name].id)] = f"membership_{name}"

        batch = await create_batch(db, project_id=project.id, status="annotating")
        batch.id = identifiers["batch"]
        await db.flush()
        aliases[str(batch.id)] = "batch"
        batch.annotator_id = users["member"].id
        batch.reviewer_id = users["member"].id
        batch.assigned_user_ids = [str(users["member"].id)]
        for name, status in (
            ("annotation", "in_progress"),
            ("inherited", "pending"),
            ("history", "completed"),
            ("review", "review"),
            ("pool", "review"),
        ):
            task = await create_task(db, project_id=project.id, status=status)
            task.id = identifiers[f"task_{name}"]
            await db.flush()
            tasks[name] = task
            aliases[str(task.id)] = f"task_{name}"
            if name != "pool":
                task.batch_id = batch.id
            task.annotation_contributor_ids = []
            task.review_contributor_ids = []
        tasks["annotation"].assignee_id = users["member"].id
        tasks["annotation"].assignee_is_override = False
        tasks["annotation"].assigned_at = datetime(2020, 1, 1, tzinfo=UTC)
        tasks["review"].reviewer_id = users["member"].id
        tasks["review"].reviewer_claimed_at = datetime(2020, 1, 1, tzinfo=UTC)
        tasks["review"].annotation_contributor_ids = None
        tasks["review"].review_contributor_ids = None
        lock = TaskLock(
            task_id=tasks["annotation"].id,
            user_id=users["member"].id,
            expire_at=datetime(2999, 1, 1, tzinfo=UTC),
        )
        db.add(lock)
        await db.flush()
    return maker, aliases, users, project, memberships, tasks, batch


def _add_email_aliases(
    legacy: dict, users: dict[str, User], aliases: dict[str, str]
) -> None:
    for response_name in ("list_before", "add"):
        body = legacy["responses"][response_name]["body"]
        rows = body if response_name == "list_before" else [body]
        for row in rows:
            alias = str(row["user_id"]).removeprefix("$")
            expected_email = row["user_email"]
            actual_email = users[alias].email
            for email in (expected_email, actual_email):
                if not re.fullmatch(
                    rf"member-baseline-{re.escape(alias)}-[0-9a-f]{{32}}@example\.test",
                    email,
                ):
                    raise AssertionError("Captured membership email shape changed")
                aliases[email] = f"{alias}_email"


async def _cleanup_project(maker, project_id: uuid.UUID) -> None:
    async with maker.begin() as db:
        task_ids = select(Task.id).where(Task.project_id == project_id)
        await db.execute(delete(TaskLock).where(TaskLock.task_id.in_(task_ids)))
        await db.execute(delete(Task).where(Task.project_id == project_id))
        await db.execute(delete(TaskBatch).where(TaskBatch.project_id == project_id))
        await db.execute(
            delete(ProjectMember).where(ProjectMember.project_id == project_id)
        )
        await db.execute(delete(Project).where(Project.id == project_id))


def _normalize(value, aliases: dict[str, str]):
    if isinstance(value, dict):
        return {
            aliases.get(key, key): _normalize(item, aliases)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize(item, aliases) for item in value]
    if isinstance(value, str) and value in aliases:
        return f"${aliases[value]}"
    return value


@pytest.mark.parametrize("binding", ["remote_container", "dedicated_dsn"])
async def test_project_member_http_preserves_the_actual_legacy_contract(
    test_engine, tmp_path, binding
):
    runtime_helpers = pytest.importorskip("tests.test_workspace_membership_runtime")
    legacy = _legacy()
    (
        maker,
        aliases,
        users,
        project,
        memberships,
        tasks,
        batch,
    ) = await _seed_membership_world(test_engine, legacy)
    _add_email_aliases(legacy, users, aliases)
    task_ids = {name: task.id for name, task in tasks.items()}
    control = secrets.token_urlsafe(32)
    workspace = api = None
    try:
        if binding == "remote_container":
            workspace = await runtime_helpers._start_container_workspace(
                test_engine, tmp_path, control
            )
            api = await runtime_helpers._start_workspace_api(
                test_engine,
                tmp_path,
                workspace_url=workspace.origin,
                control=control,
            )
        else:
            api = await runtime_helpers._start_workspace_api(
                test_engine,
                tmp_path,
                workspace_dsn=_membership_dsn(test_engine),
                control=control,
            )

        token = create_access_token(
            subject=str(users["owner"].id), role=users["owner"].role
        )
        path = f"/api/v1/projects/{project.id}/members"
        aliases[str(users["owner"].id)] = "owner"
        aliases[str(project.id)] = "project"
        aliases[str(memberships["member"].id)] = "membership_member"

        async def call(
            name: str, method: str, suffix: str = "", body: dict | None = None
        ):
            result = await _node_public(
                api.origin,
                token,
                method=method,
                path=path + suffix,
                body=body,
                operation=(
                    "list_members"
                    if name.startswith("list_")
                    else name_to_operation(name)
                ),
                expected=legacy["responses"][name],
                aliases=aliases,
            )
            if name == "add" and result["status"] == 201:
                aliases[result["body"]["id"]] = "membership_added"
            return result, result["body"]

        def name_to_operation(name: str) -> str:
            if name in {
                "same_role",
                "blocked_viewer",
                "self_receiver",
                "invalid_receiver",
                "identical_receiver",
                "unknown_review_evidence",
                "annotation_handoff_ready",
                "review_handoff_ready",
            }:
                return "preview_role_change"
            if name in {
                "invalid_version",
                "stale_version",
                "stale_snapshot",
                "annotation_handoff",
                "review_handoff",
            }:
                return "change_role"
            if name in {"blocked_removal", "idle_removal", "missing_removal"}:
                return "remove_member"
            return "add_member"

        await call("list_before", "GET")

        async def preview(name: str, role: str, **extra) -> dict:
            response, payload = await call(
                name,
                "POST",
                f"/{memberships['member'].id}/role/preview",
                {"project_role": role, **extra},
            )
            assert response["status"] == 200, response
            return payload

        await preview("same_role", "annotator")
        await preview("blocked_viewer", "viewer")
        await preview(
            "self_receiver", "viewer", replacement_annotator_id=str(users["member"].id)
        )
        await preview(
            "invalid_receiver",
            "viewer",
            replacement_annotator_id=str(users["viewer"].id),
        )
        await preview(
            "identical_receiver",
            "viewer",
            replacement_annotator_id=str(users["annotator"].id),
            replacement_reviewer_id=str(users["annotator"].id),
        )
        await preview(
            "unknown_review_evidence",
            "viewer",
            replacement_annotator_id=str(users["annotator"].id),
            replacement_reviewer_id=str(users["reviewer"].id),
        )
        await call("blocked_removal", "DELETE", f"/{memberships['member'].id}")

        ready = await preview(
            "annotation_handoff_ready",
            "reviewer",
            replacement_annotator_id=str(users["annotator"].id),
        )
        common = {
            "project_role": "reviewer",
            "reason": "baseline rotation",
            "replacement_annotator_id": str(users["annotator"].id),
            "preview_token": ready["preview_token"],
        }
        for expected_version in (1, 2):
            for field in ("preview_token", "reason"):
                body = {
                    **common,
                    "expected_version": expected_version,
                    "preview_token": "0" * 64,
                    field: "\u0000",
                }
                await _node_public(
                    api.origin,
                    token,
                    method="PATCH",
                    path=path + f"/{memberships['member'].id}/role",
                    body=body,
                    operation="change_role",
                    aliases=aliases,
                    expected=legacy["responses"][
                        "stale_version" if expected_version == 2 else "stale_snapshot"
                    ],
                )
        await call(
            "invalid_version",
            "PATCH",
            f"/{memberships['member'].id}/role",
            {**common, "expected_version": 0},
        )
        await call(
            "stale_version",
            "PATCH",
            f"/{memberships['member'].id}/role",
            {**common, "expected_version": 2},
        )
        await call(
            "stale_snapshot",
            "PATCH",
            f"/{memberships['member'].id}/role",
            {**common, "expected_version": 1, "preview_token": "0" * 64},
        )
        await call(
            "annotation_handoff",
            "PATCH",
            f"/{memberships['member'].id}/role",
            {**common, "expected_version": 1},
        )

        async with maker.begin() as db:
            for name, task_id in task_ids.items():
                tasks[name] = await db.get(Task, task_id, populate_existing=True)
            assert tasks["annotation"].assignee_id == users["annotator"].id
            assert tasks["annotation"].assigned_at == datetime(2020, 1, 1, tzinfo=UTC)
            assert tasks["history"].assignee_id == users["member"].id
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(TaskLock)
                    .where(TaskLock.user_id == users["member"].id)
                )
                == 0
            )
            tasks["review"].annotation_contributor_ids = [str(users["annotator"].id)]
            tasks["review"].review_contributor_ids = [str(users["annotator"].id)]
            tasks["review"].review_submitter_id = users["annotator"].id
            tasks["review"].review_round_id = uuid.uuid4()
            aliases[str(tasks["review"].review_round_id)] = "review_round"

        ready = await preview(
            "review_handoff_ready",
            "viewer",
            replacement_reviewer_id=str(users["reviewer"].id),
        )
        await call(
            "review_handoff",
            "PATCH",
            f"/{memberships['member'].id}/role",
            {
                "project_role": "viewer",
                "expected_version": 2,
                "preview_token": ready["preview_token"],
                "reason": "baseline review rotation",
                "replacement_reviewer_id": str(users["reviewer"].id),
            },
        )
        await call("idle_removal", "DELETE", f"/{memberships['member'].id}")
        await call("missing_removal", "DELETE", f"/{memberships['member'].id}")
        await call(
            "add",
            "POST",
            body={"user_id": str(users["new_employee"].id), "role": "annotator"},
        )
        await call(
            "duplicate_add",
            "POST",
            body={"user_id": str(users["new_employee"].id), "role": "annotator"},
        )
        await call("list_after", "GET")

        async with maker() as db:
            audit_rows = list(
                (
                    await db.scalars(
                        select(AuditLog)
                        .where(AuditLog.actor_id == users["owner"].id)
                        .order_by(AuditLog.id)
                    )
                ).all()
            )
            business_rows = [
                row for row in audit_rows if not row.action.startswith("http.")
            ]
            assert len(audit_rows) - len(business_rows) == 22
            audit_values = [
                {
                    "action": row.action,
                    "target_type": row.target_type,
                    "target_id": row.target_id,
                    "detail": row.detail_json,
                    "actor_id": str(row.actor_id),
                    "actor_role": row.actor_role,
                }
                for row in business_rows
            ]
        assert len(audit_values) == 4
        assert _normalize(audit_values, aliases) == legacy["audits"]
    finally:
        if api is not None:
            api.stop()
        if workspace is not None:
            workspace.stop()
        await _cleanup_project(maker, project.id)


async def test_node_membership_owner_and_native_same_transaction_consumers(
    test_engine, tmp_path
):
    runtime_helpers = pytest.importorskip("tests.test_workspace_membership_runtime")
    _membership_dsn(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    control = secrets.token_urlsafe(32)
    workspace = None
    project = None
    native_project = None
    async with maker.begin() as db:
        owner = await create_user(
            db,
            "project_admin",
            f"workspace-peer-owner-{uuid.uuid4().hex}@example.test",
            "Peer owner",
        )
        add_target = await create_user(
            db,
            "employee",
            f"workspace-peer-add-{uuid.uuid4().hex}@example.test",
            "Add target",
        )
        native_target = await create_user(
            db,
            "employee",
            f"workspace-peer-native-{uuid.uuid4().hex}@example.test",
            "Native target",
        )
        rollback_target = await create_user(
            db,
            "employee",
            f"workspace-peer-rollback-{uuid.uuid4().hex}@example.test",
            "Rollback target",
        )
        project = await create_project(db, owner_id=owner.id, name="Node owner peer")
        native_project = await create_project(
            db, owner_id=owner.id, name="Node native peer"
        )
    try:
        workspace = await runtime_helpers._start_workspace(
            test_engine, tmp_path, control
        )

        def request(
            operation: str,
            payload: dict,
            project_id=project.id,
            *,
            method="POST",
            path=None,
            operation_id=None,
        ):
            return {
                "schema": "aap-workspace-membership/v1",
                "operation_id": str(operation_id or uuid.uuid4()),
                "operation": operation,
                "actor_id": str(owner.id),
                "project_id": str(project_id),
                "payload": payload,
                "audit_context": {
                    "method": method,
                    "path": path or f"/api/v1/projects/{project_id}/members",
                    "ip": None,
                    "request_id": None,
                },
            }

        add_request = request(
            "add_member",
            {"target_user_id": str(add_target.id), "project_role": "annotator"},
        )
        add_reply = _node(
            "--call",
            payload={
                "mode": "owner_http",
                "origin": workspace.origin,
                "control_token": control,
                "request": add_request,
            },
        )
        assert add_reply["ok"] is True and add_reply["reply"]["ok"] is True
        assert add_reply["reply"]["data"]["member"]["user_id"] == str(add_target.id)

        async with maker() as db:
            audit_count_before_restart = await db.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.actor_id == owner.id,
                    AuditLog.target_type == "project_member",
                )
            )
        workspace.stop()
        workspace = await runtime_helpers._start_workspace(
            test_engine, tmp_path / "owner-restarted", control
        )

        replay = _node(
            "--call",
            payload={
                "mode": "owner_http",
                "origin": workspace.origin,
                "control_token": control,
                "request": add_request,
            },
        )
        assert replay["reply"] == add_reply["reply"]
        async with maker() as db:
            audit_count_after_restart = await db.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.actor_id == owner.id,
                    AuditLog.target_type == "project_member",
                )
            )
        assert audit_count_after_restart == audit_count_before_restart

        member_id = add_reply["reply"]["data"]["member"]["id"]
        preview_request = request(
            "preview_role_change",
            {
                "member_id": member_id,
                "target_role": "reviewer",
                "replacement_annotator_id": None,
                "replacement_reviewer_id": None,
            },
            method="POST",
            path=f"/api/v1/projects/{project.id}/members/{member_id}/role/preview",
        )
        preview_reply = _node(
            "--call",
            payload={
                "mode": "owner_http",
                "origin": workspace.origin,
                "control_token": control,
                "request": preview_request,
            },
        )["reply"]
        assert preview_reply["ok"] is True
        version = preview_reply["data"]["current_version"]
        change_request = request(
            "change_role",
            {
                "member_id": member_id,
                "target_role": "reviewer",
                "expected_version": version,
                "preview_token": preview_reply["data"]["preview_token"],
                "reason": "Node contract consumer",
                "replacement_annotator_id": None,
                "replacement_reviewer_id": None,
            },
            method="PATCH",
            path=f"/api/v1/projects/{project.id}/members/{member_id}/role",
        )
        changed = _node(
            "--call",
            payload={
                "mode": "owner_http",
                "origin": workspace.origin,
                "control_token": control,
                "request": change_request,
            },
        )["reply"]
        assert changed["ok"] is True and changed["data"]["member"]["role"] == "reviewer"

        remove_request = request(
            "remove_member",
            {"member_id": member_id},
            method="DELETE",
            path=f"/api/v1/projects/{project.id}/members/{member_id}",
        )
        removed = _node(
            "--call",
            payload={
                "mode": "owner_http",
                "origin": workspace.origin,
                "control_token": control,
                "request": remove_request,
            },
        )["reply"]
        assert removed["ok"] is True and removed["data"]["removed"] is True

        # Exercise the restricted SQL client through prepare and apply in one psql transaction.
        native_request = request(
            "add_member",
            {"target_user_id": str(native_target.id), "project_role": "annotator"},
            project_id=native_project.id,
        )
        native_input = {
            "mode": "native",
            "container_id": os.environ["AAP_MODULE_DATA_PG_CONTAINER_ID"],
            "database": make_url(_membership_dsn(test_engine)).database,
            "transaction": "commit",
            "request": native_request,
        }
        native_reply = _node("--call", payload=native_input)
        assert native_reply["ok"] is True
        assert native_reply["current_user"] == "workspace_membership_client"
        assert native_reply["executable_membership_functions"] == 3
        assert native_reply["reply"]["data"]["result"]["member"]["user_id"] == str(
            native_target.id
        )

        async with maker() as db:
            audit_count_before_replay = await db.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.actor_id == owner.id,
                    AuditLog.target_type == "project_member",
                )
            )
        native_replay = _node("--call", payload=native_input)
        assert native_replay["ok"] is True and native_replay["replayed"] is True
        assert native_replay["reply"]["data"]["kind"] == "replay"
        async with maker() as db:
            audit_count_after_replay = await db.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.actor_id == owner.id,
                    AuditLog.target_type == "project_member",
                )
            )
        assert audit_count_after_replay == audit_count_before_replay

        rollback_request = request(
            "add_member",
            {"target_user_id": str(rollback_target.id), "project_role": "annotator"},
            project_id=native_project.id,
        )
        async with maker() as db:
            audit_count_before_rollback = await db.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.actor_id == owner.id,
                    AuditLog.target_type == "project_member",
                )
            )
        rolled_back = _node(
            "--call",
            payload={
                **native_input,
                "transaction": "rollback",
                "request": rollback_request,
            },
        )
        assert rolled_back["ok"] is True
        async with maker() as db:
            assert (
                await db.scalar(
                    select(ProjectMember.id).where(
                        ProjectMember.project_id == native_project.id,
                        ProjectMember.user_id == rollback_target.id,
                    )
                )
                is None
            )
            audit_count_after_rollback = await db.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.actor_id == owner.id,
                    AuditLog.target_type == "project_member",
                )
            )
            assert audit_count_after_rollback == audit_count_before_rollback

        retry_after_rollback = _node(
            "--call",
            payload={
                **native_input,
                "transaction": "commit",
                "request": rollback_request,
            },
        )
        assert retry_after_rollback["ok"] is True
        assert retry_after_rollback["reply"]["data"]["kind"] == "applied"

        async with maker.begin() as db:
            actor = await db.get(User, owner.id, populate_existing=True)
            actor.role = "employee"
        revoked_replay = _node(
            "--call",
            payload={
                "mode": "owner_http",
                "origin": workspace.origin,
                "control_token": control,
                "request": add_request,
            },
        )
        assert revoked_replay["reply"]["ok"] is False
        assert revoked_replay["reply"]["error"]["status_code"] == 403
    finally:
        if workspace is not None:
            workspace.stop()
        await _cleanup_project(maker, project.id)
        await _cleanup_project(maker, native_project.id)
