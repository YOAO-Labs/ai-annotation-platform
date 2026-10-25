"""Frozen aggregate scope results before moving their SQL to Workspace."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import delete, event, func, select, text

from app.db.models.dataset import Dataset
from app.db.models.task import Task
from app.services.labeling_core_read_relations import task_row
from app.services.project_aggregates import (
    managed_project_scope_clause,
    membership_project_ids,
    project_scope_clause,
)
from tests.factory import create_membership, create_project
from tests.test_workspace_project_authority_locking import _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data verifier",
    ),
]
_BASE = "a6ddf032006bfea57fe9a63d22609fc33fef7d03"
_GOLDEN = Path(__file__).parent / "fixtures/workspace_actor_scope_legacy.json"
_ACTORS = {
    "employee": ("employee", True, "employee", True),
    "manager": ("project_admin", True, "project_admin", True),
    "viewer": ("viewer", True, "viewer", True),
    "super_admin": ("super_admin", True, "super_admin", True),
    "legacy_caller": ("annotator", True, "employee", True),
    "unknown_caller": ("unknown", True, "employee", True),
    "inactive_caller": ("employee", False, "employee", True),
    "inactive_database": ("employee", True, "employee", False),
    "manager_inactive_database": ("project_admin", True, "project_admin", False),
    "viewer_database_employee": ("viewer", True, "employee", True),
    "employee_database_viewer": ("employee", True, "viewer", True),
    "manager_database_employee": ("project_admin", True, "employee", True),
    "manager_database_legacy": ("project_admin", True, "reviewer", True),
    "super_admin_inactive_database": ("super_admin", True, "super_admin", False),
}
_OPERATIONS = [
    (kind, roles)
    for kind in ("membership", "project")
    for roles in ((), ("annotator",), ("reviewer",), ("invalid",))
] + [("managed", ())]


def _capture(key, observed):
    if os.getenv("AAP_ACTOR_SCOPE_CAPTURE") == "1":
        assert (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).resolve().parents[3],
                text=True,
            ).strip()
            == _BASE
        )
        document = (
            json.loads(_GOLDEN.read_text())
            if _GOLDEN.exists()
            else {"source_commit": _BASE, "cases": {}}
        )
        assert key not in document["cases"], "never overwrite frozen legacy scope"
        document["cases"][key] = observed
        _GOLDEN.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    else:
        assert observed == json.loads(_GOLDEN.read_text())["cases"][key]


async def _seed(owner, world, actual_role, actual_active):
    projects = {"original": world["project_id"]}
    roles = {
        "annotator": "annotator",
        "reviewer": "reviewer",
        "viewer": "viewer",
        "invalid": "invalid",
        "owned_review": "reviewer",
        "late_date": "reviewer",
    }
    async with owner.begin() as db:
        for label in (
            "owned",
            "owned_review",
            "annotator",
            "reviewer",
            "viewer",
            "invalid",
            "foreign",
            "late_date",
        ):
            project = await create_project(
                db,
                owner_id=world["member_id"]
                if label.startswith("owned")
                else world["owner_id"],
                name=label,
            )
            projects[label] = project.id
            if label in roles:
                member = await create_membership(
                    db,
                    project_id=project.id,
                    user_id=world["member_id"],
                    role=roles[label],
                    assigned_by=world["owner_id"],
                )
                if label == "late_date":
                    await db.execute(
                        text(
                            "UPDATE public.project_members SET assigned_at=TIMESTAMPTZ '10000-01-01' WHERE id=:id"
                        ),
                        {"id": member.id},
                    )
        world["project_ids"].extend(
            pid for label, pid in projects.items() if label != "original"
        )
        for label, project_id in projects.items():
            for index in range(2):
                task_id = uuid.uuid4()
                db.add(
                    Task(
                        id=task_id,
                        project_id=project_id,
                        display_id=f"SCOPE-{task_id.hex[:18]}",
                        file_name=f"{label}:{index}",
                        file_path=f"scope/{task_id}.png",
                        file_type="image",
                        status="pending",
                    )
                )
        await db.flush()
        await db.execute(
            text("UPDATE public.users SET role=:role,is_active=:active WHERE id=:id"),
            {"id": world["member_id"], "role": actual_role, "active": actual_active},
        )
    return projects


@pytest.mark.parametrize("actor_case", _ACTORS)
@pytest.mark.parametrize(
    "operation,roles",
    _OPERATIONS,
    ids=[
        kind + ":" + ("any" if not roles else roles[0]) for kind, roles in _OPERATIONS
    ],
)
async def test_aggregate_scopes_match_frozen_results(
    test_engine, actor_case, operation, roles
):
    await _check_scope_case(test_engine, actor_case, operation, roles)


async def _check_scope_case(
    test_engine, actor_case, operation, roles, *, binding="owner"
):
    caller_role, caller_active, actual_role, actual_active = _ACTORS[actor_case]
    async with _world(test_engine) as (owner, api, world, _url):
        projects = await _seed(owner, world, actual_role, actual_active)
        try:
            observed = {}
            maker = owner if binding == "owner" else api
            row_model = Task if binding == "owner" else task_row
            for autoflush in (False, True):
                async with maker() as db:
                    if binding == "api":
                        assert (
                            await db.scalar(text("SELECT current_user"))
                            == "identity_api_consumer"
                        )
                        for table in ("users", "projects", "project_members"):
                            assert not await db.scalar(
                                text(
                                    "SELECT has_any_column_privilege(current_user,:table,'SELECT')"
                                ),
                                {"table": "public." + table},
                            )
                    db.autoflush = autoflush
                    actor = SimpleNamespace(
                        id=world["member_id"], role=caller_role, is_active=caller_active
                    )
                    if operation == "membership":
                        clause = row_model.project_id.in_(
                            membership_project_ids(actor, *roles)
                        )
                    elif operation == "project":
                        clause = project_scope_clause(
                            actor, row_model.project_id, project_roles=roles
                        )
                    else:
                        clause = managed_project_scope_clause(
                            actor, row_model.project_id
                        )
                    task_id = uuid.uuid4()
                    if binding == "api":
                        pending = Dataset(
                            id=task_id,
                            display_id="DS-" + task_id.hex[:16],
                            name="Pending caller work",
                            created_by=world["owner_id"],
                        )
                    else:
                        pending = Task(
                            id=task_id,
                            project_id=projects["annotator"],
                            display_id=f"SCOPE-P-{task_id.hex[:18]}",
                            file_name="annotator:pending",
                            file_path=f"scope/{task_id}.png",
                            file_type="image",
                            status="pending",
                        )
                    db.add(pending)
                    flushes = []

                    def mark_flush(session, _context, _instances):
                        flushes.append(
                            sorted(type(obj).__name__ for obj in session.new)
                        )

                    event.listen(db.sync_session, "before_flush", mark_flush)
                    try:
                        before = tuple(
                            (
                                await db.execute(
                                    text("SELECT pg_backend_pid(),txid_current()")
                                )
                            ).one()
                        )
                        statement = select(row_model.file_name).where(
                            row_model.project_id.in_(list(projects.values()))
                        )
                        count = (
                            select(func.count())
                            .select_from(row_model)
                            .where(row_model.project_id.in_(list(projects.values())))
                        )
                        if clause is not None:
                            statement = statement.where(clause)
                            count = count.where(clause)
                        rows = list(
                            (
                                await db.scalars(
                                    statement.order_by(row_model.file_name)
                                )
                            ).all()
                        )
                        total = await db.scalar(count)
                        page = list(
                            (
                                await db.scalars(
                                    statement.order_by(row_model.file_name)
                                    .offset(1)
                                    .limit(3)
                                )
                            ).all()
                        )
                        assert (
                            tuple(
                                (
                                    await db.execute(
                                        text("SELECT pg_backend_pid(),txid_current()")
                                    )
                                ).one()
                            )
                            == before
                        )
                        observed[str(autoflush)] = {
                            "rows": rows,
                            "count": total,
                            "page": page,
                            "flushes": flushes,
                            "pending": pending in db.new,
                        }
                    finally:
                        event.remove(db.sync_session, "before_flush", mark_flush)
                        await db.rollback()
            key = f"{actor_case}:{operation}:{','.join(roles)}"
            if binding == "owner":
                _capture(key, observed)
            else:
                # Freeze scope results independently of retired raw Task writes.
                expected = json.loads(_GOLDEN.read_text())["cases"][key]["False"]
                fields = ("rows", "count", "page")
                for autoflush, state in observed.items():
                    assert {field: state[field] for field in fields} == {
                        field: expected[field] for field in fields
                    }
                    assert state["pending"] is (autoflush == "False")
        finally:
            async with owner.begin() as db:
                await db.execute(
                    delete(Task).where(Task.project_id.in_(world["project_ids"]))
                )
