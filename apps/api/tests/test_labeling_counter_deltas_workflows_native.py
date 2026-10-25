"""Exercise lifecycle counter deltas through the restricted API LOGIN."""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
import re
import uuid

import httpx
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.core.security import create_access_token
from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.task import Task
from tests.factory import create_membership, create_task, create_user
from tests.test_workspace_project_authority_locking import (
    _assert_private_denied,
    _world,
)
from tests.test_workspace_project_exclusive_native import _configure_api_auth
from tests.workspace_project_query_helpers import project_query_briefs_dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier and actual API LOGIN",
    ),
]

_ROLE = "identity_api_consumer"
_DIRECT_PROJECT_SQL = re.compile(
    r"\b(?:from|join|update|insert\s+into|delete\s+from)\s+"
    r"(?:public\.)?projects\b",
    re.IGNORECASE,
)
_COUNTER_FUNCTIONS = {
    "decrement_project_review_count",
    "decrement_project_completed_count",
    "record_project_review_approval",
}


def _configure_actual_api(test_engine, monkeypatch) -> None:
    _configure_api_auth(test_engine, monkeypatch)
    monkeypatch.setattr(settings, "identity_briefs_database_url_file", "")
    monkeypatch.setattr(
        settings,
        "identity_briefs_database_url",
        project_query_briefs_dsn(test_engine),
    )


async def _project_update_acl(db):
    rows = (
        await db.execute(
            text(
                "SELECT privilege.privilege_type,privilege.is_grantable "
                "FROM pg_catalog.pg_class AS relation "
                "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE("
                "relation.relacl,pg_catalog.acldefault('r',relation.relowner)"
                ")) AS privilege "
                "JOIN pg_catalog.pg_roles AS grantee "
                "ON grantee.oid=privilege.grantee "
                "WHERE relation.oid='public.projects'::regclass "
                "AND grantee.rolname=:role "
                "AND privilege.privilege_type='UPDATE'"
            ),
            {"role": _ROLE},
        )
    ).all()
    return [(row.privilege_type, row.is_grantable) for row in rows]


async def _revoke_project_update(owner):
    async with owner.begin() as db:
        acl = (
            await db.execute(
                text(
                    "SELECT relacl::text FROM pg_catalog.pg_class "
                    "WHERE oid='public.projects'::regclass"
                )
            )
        ).scalar_one()
        update_acl = await _project_update_acl(db)
        await db.execute(
            text("REVOKE UPDATE ON public.projects FROM identity_api_consumer")
        )
        assert not await db.scalar(
            text("SELECT has_table_privilege(:role,'public.projects','UPDATE')"),
            {"role": _ROLE},
        )
    return acl, update_acl


async def _restore_project_update(owner, before):
    acl, update_acl = before
    async with owner.begin() as db:
        await db.execute(
            text("REVOKE UPDATE ON public.projects FROM identity_api_consumer")
        )
        if update_acl:
            grant_option = " WITH GRANT OPTION" if update_acl[0][1] else ""
            await db.execute(
                text(
                    "GRANT UPDATE ON public.projects TO identity_api_consumer"
                    f"{grant_option}"
                )
            )
        assert await _project_update_acl(db) == update_acl
        assert (
            await db.scalar(
                text(
                    "SELECT relacl::text FROM pg_catalog.pg_class "
                    "WHERE oid='public.projects'::regclass"
                )
            )
            == acl
        )


async def _assert_api_project_access_is_revoked(db) -> None:
    assert await db.scalar(text("SELECT current_user")) == _ROLE
    for privilege in ("SELECT", "UPDATE"):
        assert not await db.scalar(
            text(
                "SELECT has_any_column_privilege(current_user,'public.projects',:privilege)"
            ),
            {"privilege": privilege},
        )
    with pytest.raises(DBAPIError) as denied:
        async with db.begin_nested():
            await db.execute(
                text("UPDATE public.projects SET review_tasks=review_tasks WHERE false")
            )
    assert denied.value.orig.sqlstate == "42501"


@asynccontextmanager
async def _restricted_api_client(app_module, api, statements):
    from app.deps import get_db

    missing = object()
    previous = app_module.dependency_overrides.get(get_db, missing)
    capture = {"enabled": False}

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if capture["enabled"]:
            statements.append(statement)

    engine = api.kw["bind"]
    event.listen(engine.sync_engine, "before_cursor_execute", record)

    async def request_db():
        async with api() as db:
            await _assert_private_denied(db)
            await _assert_api_project_access_is_revoked(db)
            try:
                capture["enabled"] = True
                yield db
            finally:
                capture["enabled"] = False
                if db.in_transaction():
                    await db.rollback()

    app_module.dependency_overrides[get_db] = request_db
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
        if previous is missing:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous


async def _seed_workflow(owner, world, *, action):
    async with owner.begin() as db:
        task = await create_task(
            db,
            project_id=world["project_id"],
            status="in_progress",
        )
        task.assignee_id = world["member_id"]
        task.annotation_contributor_ids = [str(world["member_id"])]

        reviewer = None
        if action in {"approve", "reject", "reopen"}:
            reviewer = await create_user(
                db,
                "employee",
                f"counter-reviewer-{uuid.uuid4().hex}@example.test",
                "Counter reviewer",
            )
            await create_membership(
                db,
                project_id=world["project_id"],
                user_id=reviewer.id,
                role="reviewer",
                assigned_by=world["owner_id"],
            )

        project = await db.get(Project, world["project_id"])
        project.review_tasks = 1
        project.completed_tasks = 0

    return {
        "task_id": task.id,
        "reviewer_id": reviewer.id if reviewer else None,
        "annotator_token": create_access_token(
            subject=str(world["member_id"]), role="employee"
        ),
        "reviewer_token": (
            create_access_token(subject=str(reviewer.id), role="employee")
            if reviewer
            else None
        ),
    }


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _post(client, task_id, action, token, *, body=None):
    request = {"headers": _headers(token)}
    if body is not None:
        request["json"] = body
    return await client.post(
        f"/api/v1/tasks/{task_id}/{action}",
        **request,
    )


@pytest.mark.parametrize(
    (
        "action",
        "expected_task_status",
        "expected_review",
        "expected_completed",
        "audit_action",
        "audit_actor",
    ),
    [
        ("withdraw", "in_progress", 0, 0, "task.withdraw", "annotator"),
        ("approve", "completed", 0, 1, "task.approve", "reviewer"),
        ("reject", "rejected", 0, 0, "task.reject", "reviewer"),
        ("reopen", "in_progress", 0, 0, "task.reopen", "annotator"),
    ],
)
async def test_real_lifecycle_http_writes_counter_deltas_without_project_privileges(
    test_engine,
    app_module,
    monkeypatch,
    action,
    expected_task_status,
    expected_review,
    expected_completed,
    audit_action,
    audit_actor,
):
    _configure_actual_api(test_engine, monkeypatch)
    async with _world(test_engine, retain_audited=True) as (
        owner,
        api,
        world,
        _api_url,
    ):
        fixture = await _seed_workflow(owner, world, action=action)
        from app.middleware import audit as audit_middleware

        monkeypatch.setattr(audit_middleware, "async_session", api)
        previous_acl = await _revoke_project_update(owner)
        statements = []
        try:
            async with _restricted_api_client(app_module, api, statements) as client:
                submitted = await _post(
                    client,
                    fixture["task_id"],
                    "submit",
                    fixture["annotator_token"],
                )
                assert submitted.status_code == 200, submitted.text
                assert submitted.json()["status"] == "submitted"

                if action in {"approve", "reject", "reopen"}:
                    async with owner.begin() as assign:
                        task = await assign.get(Task, fixture["task_id"])
                        assert task.status == "review" and task.reviewer_id is None
                        task.reviewer_id = fixture["reviewer_id"]
                    claimed = await _post(
                        client,
                        fixture["task_id"],
                        "review/claim",
                        fixture["reviewer_token"],
                    )
                    assert claimed.status_code == 200, claimed.text
                    assert claimed.json()["reviewer_id"] == str(fixture["reviewer_id"])

                if action == "reopen":
                    approved = await _post(
                        client,
                        fixture["task_id"],
                        "review/approve",
                        fixture["reviewer_token"],
                    )
                    assert approved.status_code == 200, approved.text
                    assert approved.json()["status"] == "approved"

                body = (
                    {"reason_type": "wrong_label", "reason": "wrong class"}
                    if action == "reject"
                    else None
                )
                response = await _post(
                    client,
                    fixture["task_id"],
                    f"review/{action}" if action in {"approve", "reject"} else action,
                    fixture["annotator_token"]
                    if action in {"withdraw", "reopen"}
                    else fixture["reviewer_token"],
                    body=body,
                )
                assert response.status_code == 200, response.text
                assert (
                    response.json()["status"]
                    == {
                        "withdraw": "withdrawn",
                        "approve": "approved",
                        "reject": "rejected",
                        "reopen": "reopened",
                    }[action]
                )

            direct_project_sql = [
                statement
                for statement in statements
                if _DIRECT_PROJECT_SQL.search(statement)
            ]
            assert direct_project_sql == []
            native_calls = [
                name
                for name in _COUNTER_FUNCTIONS
                for statement in statements
                if f"aap_labeling_v1.{name}(" in statement
            ]
            assert sorted(native_calls) == sorted(
                {
                    "withdraw": ["decrement_project_review_count"],
                    "approve": ["record_project_review_approval"],
                    "reject": ["decrement_project_review_count"],
                    "reopen": [
                        "record_project_review_approval",
                        "decrement_project_completed_count",
                    ],
                }[action]
            )

            async with owner() as observer:
                task = await observer.get(Task, fixture["task_id"])
                project = await observer.get(Project, world["project_id"])
                audit = await observer.scalar(
                    select(AuditLog)
                    .where(
                        AuditLog.action == audit_action,
                        AuditLog.target_type == "task",
                        AuditLog.target_id == str(fixture["task_id"]),
                    )
                    .order_by(AuditLog.id.desc())
                    .limit(1)
                )
                assert task.status == expected_task_status
                assert (project.review_tasks, project.completed_tasks) == (
                    expected_review,
                    expected_completed,
                )
                assert audit is not None and audit.status_code == 200
                assert audit.actor_id == (
                    world["member_id"]
                    if audit_actor == "annotator"
                    else fixture["reviewer_id"]
                )
                assert audit.detail_json["project_id"] == str(world["project_id"])
        finally:
            await _restore_project_update(owner, previous_acl)
