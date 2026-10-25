"""Installed Identity directory owner and independent Node consumer tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import math
import os
import secrets
import subprocess
import time
import uuid

import pytest
from sqlalchemy import delete, select, text, update

from aap_identity.security import create_access_token
from app.db.models.group import Group
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
from tests.test_identity_admission_live import (
    ROOT,
    _start_identity,
    _token,
    _world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_DIRECTORY_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity directory verifier",
    ),
]


def _directory_command(
    operation, token, payload=None, *, operation_id=None, request_id=None
):
    if payload is None:
        payload = (
            {
                "role": None,
                "status": "active",
                "project_id": None,
            }
            if operation == "list_users"
            else {
                "status": "active",
                "project_id": None,
                "group_id": None,
                "role": None,
                "search": None,
            }
        )
        if operation == "query_users":
            payload = {"page": 1, "page_size": 50, **payload}
        elif operation == "export_users":
            payload["format"] = "csv"
    return {
        "schema": "aap-identity-directory/v1",
        "operation": operation,
        "operation_id": str(operation_id or uuid.uuid4()),
        "token": token,
        "audit_context": {
            "method": "GET",
            "path": f"/acceptance/identity-directory/{operation}",
            "ip": "127.0.0.1",
            "request_id": str(request_id or uuid.uuid4()),
        },
        "payload": payload,
    }


def _node_response(identity, control, request, *, timeout=120):
    result = subprocess.run(
        ["node", str(ROOT / "apps/api/tests/fixtures/identity_directory_peer.mjs")],
        input=json.dumps(
            {"url": identity.origin, "control_token": control, "request": request},
            ensure_ascii=False,
            allow_nan=False,
        ),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    assert result.returncode == 0, "Independent Node directory peer failed"
    for secret in (control, request["token"]):
        assert secret not in result.stdout
        assert secret not in result.stderr
    return json.loads(result.stdout), result.stdout


def _node(identity, control, request):
    return _node_response(identity, control, request)[0]


async def _seed_directory_world(maker, actor, suffix):
    actor_ids = [actor]
    project_ids = []
    group_ids = []
    async with maker.begin() as db:
        pa = await create_user(
            db,
            "project_admin",
            f"directory-pa-{suffix}@example.test",
            "Directory project admin",
        )
        foreign_pa = await create_user(
            db,
            "project_admin",
            f"directory-foreign-pa-{suffix}@example.test",
            "Foreign project admin",
        )
        own_project = await create_project(
            db, owner_id=pa.id, name=f"Directory own project {suffix}"
        )
        foreign_project = await create_project(
            db, owner_id=foreign_pa.id, name=f"Directory foreign project {suffix}"
        )

        names = {
            "own_member": ("employee", "Own member"),
            "both_member": ("employee", "Cross-project member"),
            "foreign_member": ("employee", "Foreign-only member"),
            "external": ("employee", "Alice external"),
            "inactive_outside": ("employee", "Inactive outside"),
            "inactive_own": ("employee", "Inactive own member"),
            "owned_project_owner": ("employee", "Owns a foreign project"),
            "batch_annotator": ("employee", "Batch annotator"),
            "batch_reviewer": ("employee", "Batch reviewer"),
            "task_assignee": ("employee", "Task assignee"),
            "task_reviewer": ("employee", "Task reviewer"),
            "task_lock_user": ("employee", "Task lock holder"),
            "legacy_member": ("annotator", "Legacy global role"),
        }
        users = {
            key: await create_user(
                db,
                role,
                f"directory-{key}-{suffix}@example.test",
                name,
            )
            for key, (role, name) in names.items()
        }
        group = Group(
            name=f"Directory Group {suffix[:12]}", description="directory fixture"
        )
        db.add(group)
        await db.flush()
        users["external"].group_id = group.id
        users["external"].group_name = group.name
        await db.execute(
            text("UPDATE users SET preferences=CAST(:value AS jsonb) WHERE id=:id"),
            {
                "id": users["external"].id,
                "value": (
                    '{"future":{"large_integer":9007199254740993,'
                    '"decimal":0.1234567890123456789,'
                    '"opaque":{"unknown":[true,null,"preserve"]}}}'
                ),
            },
        )
        users["external"].status = "legacy-paused"
        users["inactive_outside"].is_active = False
        users["inactive_own"].is_active = False
        users["task_lock_user"].is_active = False
        users["legacy_member"].status = "legacy-paused"

        await create_membership(
            db,
            project_id=own_project.id,
            user_id=users["own_member"].id,
            role="annotator",
            assigned_by=pa.id,
        )
        await create_membership(
            db,
            project_id=own_project.id,
            user_id=users["both_member"].id,
            role="reviewer",
            assigned_by=pa.id,
        )
        await create_membership(
            db,
            project_id=foreign_project.id,
            user_id=users["both_member"].id,
            role="annotator",
            assigned_by=foreign_pa.id,
        )
        await create_membership(
            db,
            project_id=foreign_project.id,
            user_id=users["foreign_member"].id,
            role="annotator",
            assigned_by=foreign_pa.id,
        )
        await create_membership(
            db,
            project_id=own_project.id,
            user_id=users["inactive_own"].id,
            role="annotator",
            assigned_by=pa.id,
        )
        await create_membership(
            db,
            project_id=own_project.id,
            user_id=users["legacy_member"].id,
            role="annotator",
            assigned_by=pa.id,
        )
        await create_membership(
            db,
            project_id=own_project.id,
            user_id=users["task_lock_user"].id,
            role="reviewer",
            assigned_by=pa.id,
        )
        owned_project = await create_project(
            db,
            owner_id=users["owned_project_owner"].id,
            name=f"Owned by directory target {suffix}",
        )
        batch = await create_batch(
            db, project_id=foreign_project.id, name=f"Directory batch {suffix}"
        )
        batch.annotator_id = users["batch_annotator"].id
        batch.reviewer_id = users["batch_reviewer"].id
        task = await create_task(db, project_id=foreign_project.id)
        task.assignee_id = users["task_assignee"].id
        task.reviewer_id = users["task_reviewer"].id
        db.add(
            TaskLock(
                task_id=task.id,
                user_id=users["task_lock_user"].id,
                expire_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    actor_ids.extend([pa.id, foreign_pa.id, *(user.id for user in users.values())])
    project_ids.extend([own_project.id, foreign_project.id, owned_project.id])
    group_ids.append(group.id)
    return {
        "actor_ids": actor_ids,
        "project_ids": project_ids,
        "group_ids": group_ids,
        "pa": pa,
        "foreign_pa": foreign_pa,
        "own_project": own_project,
        "foreign_project": foreign_project,
        "users": users,
    }


async def _add_large_directory_catalog(maker, actor_ids, suffix, count=540):
    now = datetime.now(UTC)
    users = [
        User(
            email=f"directory-catalog-{suffix}-{index}@example.test",
            name=f"Directory catalog {index:04d}",
            password_hash="fixture-only",
            role="employee",
            is_active=True,
            status="offline",
            preferences={},
            created_at=now - timedelta(seconds=index),
        )
        for index in range(count)
    ]
    async with maker.begin() as db:
        db.add_all(users)
        await db.flush()
    actor_ids.extend(user.id for user in users)
    return users


async def _cleanup_directory(
    maker,
    *,
    actor_ids,
    project_ids=(),
    group_ids=(),
    operation_ids=(),
):
    actors = [uuid.UUID(str(value)) for value in actor_ids]
    projects = [uuid.UUID(str(value)) for value in project_ids]
    groups = [uuid.UUID(str(value)) for value in group_ids]
    operations = [uuid.UUID(str(value)) for value in operation_ids]
    async with maker.begin() as db:
        assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
        await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
        await db.execute(
            text(
                "DELETE FROM public.identity_directory_export_receipts "
                "WHERE actor_id=ANY(CAST(:actors AS uuid[])) "
                "OR operation_id=ANY(CAST(:operations AS uuid[]))"
            ),
            {"actors": actors, "operations": operations},
        )
        targets = [str(value) for value in [*actors, *projects, *groups]]
        await db.execute(
            text(
                "DELETE FROM public.audit_logs "
                "WHERE actor_id=ANY(CAST(:actors AS uuid[])) "
                "OR target_id=ANY(CAST(:targets AS text[]))"
            ),
            {"actors": actors, "targets": targets},
        )
        if projects:
            await db.execute(
                delete(TaskLock).where(
                    TaskLock.user_id.in_(actors)
                    | TaskLock.task_id.in_(
                        select(Task.id).where(Task.project_id.in_(projects))
                    )
                )
            )
            await db.execute(delete(Task).where(Task.project_id.in_(projects)))
            await db.execute(
                delete(TaskBatch).where(TaskBatch.project_id.in_(projects))
            )
        if actors or projects:
            await db.execute(
                delete(ProjectMember).where(
                    ProjectMember.user_id.in_(actors)
                    | ProjectMember.project_id.in_(projects)
                )
            )
        if projects:
            await db.execute(delete(Project).where(Project.id.in_(projects)))
        await db.execute(delete(User).where(User.id.in_(actors)))
        if groups:
            await db.execute(
                update(User)
                .where(User.group_id.in_(groups))
                .values(group_id=None, group_name=None)
            )
            await db.execute(delete(Group).where(Group.id.in_(groups)))


def _auth_token(user, secret):
    return create_access_token(
        str(user.id), user.role, secret=secret, expire_minutes=15
    )


async def test_installed_directory_node_visibility_export_replay_restart_and_load(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    world = await _seed_directory_world(maker, actor, suffix)
    actor_ids = world["actor_ids"]
    project_ids = world["project_ids"]
    group_ids = world["group_ids"]
    operation_ids = []
    identity = None
    super_token = _token(actor, secret)
    pa_token = _auth_token(world["pa"], secret)
    async with maker() as db:
        original_actor_email = (await db.get(User, actor)).email
    try:
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
            enable_directory=True,
        )

        legacy_list = _directory_command(
            "list_users",
            pa_token,
            {
                "role": None,
                "status": "active",
                "project_id": str(world["foreign_project"].id),
            },
        )
        operation_ids.append(uuid.UUID(legacy_list["operation_id"]))
        pa_legacy = await asyncio.to_thread(_node, identity, control, legacy_list)
        pa_legacy_ids = {row["id"] for row in pa_legacy["data"]["users"]}
        assert str(world["pa"].id) in pa_legacy_ids
        assert str(world["users"]["own_member"].id) in pa_legacy_ids
        assert str(world["users"]["both_member"].id) in pa_legacy_ids
        assert str(world["users"]["foreign_member"].id) not in pa_legacy_ids
        assert str(world["users"]["external"].id) not in pa_legacy_ids
        assert str(actor) not in pa_legacy_ids

        legacy_candidates = _directory_command(
            "list_users",
            pa_token,
            {
                "role": "employee",
                "status": "active",
                "project_id": str(world["foreign_project"].id),
            },
        )
        operation_ids.append(uuid.UUID(legacy_candidates["operation_id"]))
        pa_candidates = await asyncio.to_thread(
            _node, identity, control, legacy_candidates
        )
        pa_candidate_ids = {row["id"] for row in pa_candidates["data"]["users"]}
        assert str(world["users"]["foreign_member"].id) in pa_candidate_ids
        assert str(world["users"]["external"].id) in pa_candidate_ids
        assert str(world["users"]["inactive_outside"].id) not in pa_candidate_ids

        query = _directory_command(
            "query_users",
            pa_token,
            {
                "page": 1,
                "page_size": 500,
                "status": "all",
                "project_id": None,
                "group_id": None,
                "role": None,
                "search": None,
            },
        )
        operation_ids.append(uuid.UUID(query["operation_id"]))
        pa_query = await asyncio.to_thread(_node, identity, control, query)
        by_id = {item["id"]: item for item in pa_query["data"]["items"]}
        assert str(world["users"]["own_member"].id) in by_id
        assert by_id[str(world["users"]["own_member"].id)]["is_managed"] is True
        assert (
            by_id[str(world["users"]["own_member"].id)]["is_lifecycle_managed"] is True
        )
        assert (
            by_id[str(world["users"]["both_member"].id)]["is_lifecycle_managed"]
            is False
        )
        assert by_id[str(world["users"]["external"].id)]["is_managed"] is True
        assert by_id[str(world["users"]["external"].id)]["is_lifecycle_managed"] is True
        for key in (
            "owned_project_owner",
            "batch_annotator",
            "batch_reviewer",
            "task_assignee",
            "task_reviewer",
            "task_lock_user",
        ):
            assert by_id[str(world["users"][key].id)]["is_lifecycle_managed"] is False
        assert str(actor) in by_id and by_id[str(actor)]["is_managed"] is False
        assert str(world["users"]["inactive_outside"].id) not in by_id
        assert by_id[str(world["users"]["task_lock_user"].id)]["is_managed"] is True
        assert (
            by_id[str(world["users"]["task_lock_user"].id)]["is_lifecycle_managed"]
            is False
        )

        sa_flags = _directory_command(
            "query_users",
            super_token,
            {
                "page": 1,
                "page_size": 50,
                "status": "all",
                "project_id": None,
                "group_id": None,
                "role": "super_admin",
                "search": None,
            },
        )
        operation_ids.append(uuid.UUID(sa_flags["operation_id"]))
        sa_flags_result = await asyncio.to_thread(_node, identity, control, sa_flags)
        sa_item = next(
            item
            for item in sa_flags_result["data"]["items"]
            if item["id"] == str(actor)
        )
        assert sa_item["is_managed"] is True and sa_item["is_lifecycle_managed"] is True

        foreign_intersection = _directory_command(
            "query_users",
            pa_token,
            {
                "page": 1,
                "page_size": 50,
                "status": "all",
                "project_id": str(world["foreign_project"].id),
                "group_id": None,
                "role": None,
                "search": None,
            },
        )
        operation_ids.append(uuid.UUID(foreign_intersection["operation_id"]))
        intersected = await asyncio.to_thread(
            _node, identity, control, foreign_intersection
        )
        intersection_ids = {item["id"] for item in intersected["data"]["items"]}
        assert intersection_ids == {str(world["users"]["both_member"].id)}

        group_query = _directory_command(
            "query_users",
            pa_token,
            {
                "page": 1,
                "page_size": 50,
                "status": "all",
                "project_id": None,
                "group_id": str(world["group_ids"][0]),
                "role": None,
                "search": None,
            },
        )
        operation_ids.append(uuid.UUID(group_query["operation_id"]))
        group_page = await asyncio.to_thread(_node, identity, control, group_query)
        assert {item["id"] for item in group_page["data"]["items"]} == {
            str(world["users"]["external"].id)
        }

        unicode_search = _directory_command(
            "query_users",
            pa_token,
            {
                "page": 1,
                "page_size": 50,
                "status": "all",
                "project_id": None,
                "group_id": None,
                "role": None,
                "search": "\u3000ali_e%\u00a0",
            },
        )
        operation_ids.append(uuid.UUID(unicode_search["operation_id"]))
        unicode_result = await asyncio.to_thread(
            _node, identity, control, unicode_search
        )
        assert str(world["users"]["external"].id) in {
            item["id"] for item in unicode_result["data"]["items"]
        }

        catalog = await _add_large_directory_catalog(maker, actor_ids, suffix)
        assert len(catalog) == 540

        invalid_role = _directory_command(
            "list_users",
            pa_token,
            {"role": "annotator", "status": "all", "project_id": None},
        )
        operation_ids.append(uuid.UUID(invalid_role["operation_id"]))
        invalid = await asyncio.to_thread(_node, identity, control, invalid_role)
        assert invalid["ok"] is False
        assert invalid["error"] == {
            "code": "invalid_platform_role",
            "status_code": 400,
            "detail": "非法平台角色: annotator",
        }

        super_list = _directory_command(
            "list_users",
            super_token,
            {"role": None, "status": "all", "project_id": None},
        )
        operation_ids.append(uuid.UUID(super_list["operation_id"]))
        listed, list_wire = await asyncio.to_thread(
            _node_response, identity, control, super_list
        )
        list_ids = {row["id"] for row in listed["data"]["users"]}
        assert len(list_ids) > 500
        assert len(list_wire.encode("utf-8")) > 64 * 1024
        legacy = next(
            row
            for row in listed["data"]["users"]
            if row["id"] == str(world["users"]["legacy_member"].id)
        )
        assert (legacy["role"], legacy["status"]) == ("annotator", "legacy-paused")
        assert "password_hash" not in legacy and "credential_generation" not in legacy
        assert "9007199254740993" in list_wire
        assert "0.12345678901234568" in list_wire

        super_project_list = _directory_command(
            "list_users",
            super_token,
            {
                "role": None,
                "status": "all",
                "project_id": str(world["own_project"].id),
            },
        )
        operation_ids.append(uuid.UUID(super_project_list["operation_id"]))
        scoped_sa = await asyncio.to_thread(
            _node, identity, control, super_project_list
        )
        assert {row["id"] for row in scoped_sa["data"]["users"]} == {
            str(world["users"][key].id)
            for key in (
                "own_member",
                "both_member",
                "inactive_own",
                "legacy_member",
                "task_lock_user",
            )
        }

        page_one = _directory_command(
            "query_users",
            super_token,
            {
                "page": 1,
                "page_size": 500,
                "status": "all",
                "project_id": None,
                "group_id": None,
                "role": None,
                "search": None,
            },
        )
        operation_ids.append(uuid.UUID(page_one["operation_id"]))
        first_page = await asyncio.to_thread(_node, identity, control, page_one)
        assert len(first_page["data"]["items"]) == 500
        assert first_page["data"]["pages"] >= 2
        page_two = _directory_command(
            "query_users",
            super_token,
            {
                "page": 2,
                "page_size": 500,
                "status": "all",
                "project_id": None,
                "group_id": None,
                "role": None,
                "search": None,
            },
        )
        operation_ids.append(uuid.UUID(page_two["operation_id"]))
        second_page = await asyncio.to_thread(_node, identity, control, page_two)
        assert second_page["data"]["total"] == first_page["data"]["total"]
        assert len(second_page["data"]["items"]) > 0

        stats = _directory_command(
            "user_stats",
            super_token,
            {
                "status": "all",
                "project_id": None,
                "group_id": None,
                "role": None,
                "search": None,
            },
        )
        operation_ids.append(uuid.UUID(stats["operation_id"]))
        stats_result = await asyncio.to_thread(_node, identity, control, stats)
        assert stats_result["data"]["total"] == first_page["data"]["total"]
        assert stats_result["data"]["online"] <= stats_result["data"]["total"]
        assert stats_result["data"]["weekly_active"] <= stats_result["data"]["total"]

        export = _directory_command(
            "export_users",
            super_token,
            {
                "status": "all",
                "project_id": None,
                "group_id": None,
                "role": None,
                "search": None,
                "format": "json",
            },
        )
        export_id = uuid.UUID(export["operation_id"])
        operation_ids.append(export_id)
        exported, export_wire = await asyncio.to_thread(
            _node_response, identity, control, export
        )
        assert len(exported["data"]["users"]) > 500
        assert len(export_wire.encode("utf-8")) > 64 * 1024
        assert exported["data"]["exported_by"] == original_actor_email
        original_request_id = export["audit_context"]["request_id"]
        assert exported["data"]["request_id"] == original_request_id
        original_record = next(
            row for row in exported["data"]["users"] if row["id"] == str(actor)
        )
        async with maker.begin() as db:
            await db.execute(
                update(User)
                .where(User.id == actor)
                .values(
                    name="Renamed directory owner",
                    email=f"directory-owner-{suffix}@example.test",
                )
            )
            await db.execute(
                update(User)
                .where(User.id == world["users"]["external"].id)
                .values(name="Changed after export")
            )
        replay = dict(export)
        replay["audit_context"] = {
            **export["audit_context"],
            "request_id": str(uuid.uuid4()),
        }
        replayed, _ = await asyncio.to_thread(_node_response, identity, control, replay)
        assert replayed["data"] == exported["data"]
        assert (
            replayed["principal"]["email"] == f"directory-owner-{suffix}@example.test"
        )
        assert replayed["data"]["users"]
        assert (
            next(row for row in replayed["data"]["users"] if row["id"] == str(actor))
            == original_record
        )

        export_pa = _directory_command(
            "export_users",
            pa_token,
            {
                "status": "all",
                "project_id": None,
                "group_id": None,
                "role": None,
                "search": None,
                "format": "json",
            },
        )
        pa_export_id = uuid.UUID(export_pa["operation_id"])
        operation_ids.append(pa_export_id)
        pa_export = await asyncio.to_thread(_node, identity, control, export_pa)
        assert str(world["users"]["external"].id) in {
            row["id"] for row in pa_export["data"]["users"]
        }
        async with maker.begin() as db:
            await db.execute(
                update(User)
                .where(User.id == world["users"]["external"].id)
                .values(is_active=False)
            )
        pa_replay = await asyncio.to_thread(_node, identity, control, export_pa)
        assert pa_replay["ok"] is False
        assert pa_replay["error"] == {
            "code": "operation_conflict",
            "status_code": 409,
            "detail": None,
        }

        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_directory_export_receipts "
                        "WHERE operation_id=:operation_id AND actor_id=:actor"
                    ),
                    {"operation_id": export_id, "actor": actor},
                )
                == 1
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor AND action='user.export'"
                    ),
                    {"actor": actor},
                )
                == 1
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_directory_export_receipts "
                        "WHERE operation_id=:operation_id AND actor_id=:actor"
                    ),
                    {"operation_id": pa_export_id, "actor": world["pa"].id},
                )
                == 1
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor AND action='user.export'"
                    ),
                    {"actor": world["pa"].id},
                )
                == 1
            )

        port = int(identity.origin.rsplit(":", 1)[1])
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
            enable_groups=False,
            enable_presence=False,
            enable_directory=True,
        )
        restarted = await asyncio.to_thread(_node, identity, control, replay)
        assert restarted["data"] == exported["data"]

        timings = []
        semaphore = asyncio.Semaphore(4)

        async def query_once():
            async with semaphore:
                command = _directory_command(
                    "query_users",
                    _token(actor, secret),
                    {
                        "page": 1,
                        "page_size": 25,
                        "status": "active",
                        "project_id": None,
                        "group_id": None,
                        "role": None,
                        "search": None,
                    },
                )
                started = time.monotonic()
                result = await asyncio.to_thread(_node, identity, control, command)
                timings.append(time.monotonic() - started)
                assert result["ok"] and len(result["data"]["items"]) <= 25

        load_started = time.monotonic()
        await asyncio.gather(*(query_once() for _ in range(12)))
        load_duration = time.monotonic() - load_started
        ordered = sorted(timings)
        record_property(
            "directory_bounded_load",
            f"12 reads, concurrency=4, elapsed={load_duration:.3f}s, "
            f"observed p50={ordered[math.ceil(0.50 * len(ordered)) - 1]:.3f}s, "
            f"p95={ordered[math.ceil(0.95 * len(ordered)) - 1]:.3f}s; "
            "finite local process/restart evidence, not production capacity",
        )
        async with maker.begin() as db:
            await db.execute(
                update(User).where(User.id == actor).values(credential_generation=1)
            )
        stale = _directory_command(
            "query_users",
            super_token,
            {
                "page": 1,
                "page_size": 10,
                "status": "active",
                "project_id": None,
                "group_id": None,
                "role": None,
                "search": None,
            },
        )
        operation_ids.append(uuid.UUID(stale["operation_id"]))
        stale_result = await asyncio.to_thread(_node, identity, control, stale)
        assert stale_result["ok"] is False
        assert stale_result["error"] == {
            "code": "generation_outdated",
            "status_code": None,
            "detail": None,
        }
        current = _directory_command(
            "query_users",
            _token(actor, secret, gen=1),
            stale["payload"],
        )
        operation_ids.append(uuid.UUID(current["operation_id"]))
        assert (await asyncio.to_thread(_node, identity, control, current))["ok"]
        record_property(
            "directory_owner",
            "installed-owner,node-four-reads,legacy-and-PA-policy,export-replay-visibility-fence,large-catalog,restart",
        )
    finally:
        if identity is not None:
            identity.stop()
        await _cleanup_directory(
            maker,
            actor_ids=actor_ids,
            project_ids=project_ids,
            group_ids=group_ids,
            operation_ids=operation_ids,
        )
