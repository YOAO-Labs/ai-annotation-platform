"""Workspace member facts under real caller credentials and native consumers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
from pydantic import TypeAdapter
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased

from aap_workspace_contract.member_values import (
    PROJECT_MEMBER_COLUMNS,
    ProjectMemberRow,
)
from aap_workspace_contract.read_relations import read_relations_schema
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.config import settings
from app.core.security import create_access_token
from app.services.annotation_comment import AnnotationCommentService
from app.services.batch import BatchService
from app.services.notification import NotificationService
from app.services.project_aggregates import member_user_ids
from app.services.workspace_read_relations import project_member_facts
from app.services.project_access import get_workspace_project
from tests.factory import create_batch, create_task
from tests.test_workspace_actor_scope_native import _consumer_world
from tests.test_workspace_member_facts_legacy import _roster
from tests.test_workspace_project_authority_locking import _world
from tests.test_workspace_project_exclusive_native import _configure_api_auth
from tests.workspace_project_query_helpers import project_query_briefs_dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data verifier",
    ),
]
_ROOT = Path(__file__).resolve().parents[3]
_VIEW = "aap_workspace_v1.project_member_facts"


async def test_fact_descriptor_reuses_fixed_member_row_and_native_mirror():
    schema = read_relations_schema()
    expected = TypeAdapter(ProjectMemberRow).json_schema()
    expected["additionalProperties"] = False
    assert schema["$defs"]["ProjectMemberRow"] == expected
    assert (
        json.loads(
            (
                _ROOT
                / "apps/_shared/workspace_contract/src/aap_workspace_contract/read_relations.schema.json"
            ).read_text()
        )
        == schema
    )
    native = json.loads(
        (
            _ROOT / "apps/api/alembic/sql/0297_workspace_member_facts.schema.json"
        ).read_text()
    )
    assert native["$defs"] == schema["$defs"]
    relation = dict(schema["x-postgresql-relations"][_VIEW])
    relation.pop("semantics")
    assert native["x-postgresql-relations"] == {_VIEW: relation}
    assert list(project_member_facts().c.keys()) == list(PROJECT_MEMBER_COLUMNS)
    assert project_member_facts() is not project_member_facts()


async def test_view_owner_columns_and_acl_are_narrow(test_engine):
    async with _world(test_engine) as (owner, _api, _world_data, _url):
        async with owner() as db:
            assert (
                await db.execute(
                    text(
                        "SELECT pg_get_userbyid(relowner),reloptions,relkind::text "
                        "FROM pg_class WHERE oid=CAST(:view AS regclass)"
                    ),
                    {"view": _VIEW},
                )
            ).one() == ("workspace_code", ["security_barrier=true"], "v")
            columns = (
                await db.execute(
                    text(
                        "SELECT attname,format_type(atttypid,atttypmod) FROM pg_attribute "
                        "WHERE attrelid=CAST(:view AS regclass) AND attnum>0 ORDER BY attnum"
                    ),
                    {"view": _VIEW},
                )
            ).all()
            assert [row[0] for row in columns] == list(PROJECT_MEMBER_COLUMNS)
            assert [row[1] for row in columns] == [
                "uuid",
                "uuid",
                "uuid",
                "character varying(30)",
                "uuid",
                "timestamp with time zone",
                "integer",
                "timestamp with time zone",
                "integer",
            ]
            for role, expected in [
                ("identity_api_consumer", True),
                ("legacy_runtime", True),
                ("append_client", False),
                ("assets_code", False),
                ("inference_code", False),
                ("workspace_membership_client", False),
            ]:
                assert (
                    await db.scalar(
                        text("SELECT has_table_privilege(:role,:view,'SELECT')"),
                        {"role": role, "view": _VIEW},
                    )
                    is expected
                )
                if role in {"identity_api_consumer", "legacy_runtime"}:
                    for privilege in [
                        "INSERT",
                        "UPDATE",
                        "DELETE",
                        "TRUNCATE",
                        "REFERENCES",
                        "TRIGGER",
                    ]:
                        assert not await db.scalar(
                            text("SELECT has_table_privilege(:role,:view,:privilege)"),
                            {"role": role, "view": _VIEW, "privilege": privilege},
                        )
            assert not await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_class c CROSS JOIN LATERAL "
                    "aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a "
                    "WHERE c.oid=CAST(:view AS regclass) AND a.grantee=0)"
                ),
                {"view": _VIEW},
            )


async def test_restricted_reads_are_typed_read_only_unlocked_and_current(test_engine):
    async with _world(test_engine) as (owner, api, world, _url):
        async with api() as db:
            for statement in [
                "SELECT id FROM public.project_members",
                f"INSERT INTO {_VIEW} DEFAULT VALUES",
                f"UPDATE {_VIEW} SET role='viewer' WHERE false",
                f"DELETE FROM {_VIEW} WHERE false",
                f"SELECT id FROM {_VIEW} FOR UPDATE",
            ]:
                with pytest.raises(DBAPIError) as denied:
                    async with db.begin_nested():
                        await db.execute(text(statement))
                assert denied.value.orig.sqlstate == "42501"
            await db.rollback()
            await db.execute(text("SET TRANSACTION READ ONLY"))
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            facts = project_member_facts()
            row = (
                await db.execute(
                    select(facts).where(facts.c.id == world["membership_id"])
                )
            ).one()
            assert tuple(row._mapping) == PROJECT_MEMBER_COLUMNS
            assert (
                row.id,
                row.project_id,
                row.user_id,
                row.role,
                row.assigned_by,
                row.version,
                row.weekly_target,
            ) == (
                world["membership_id"],
                world["project_id"],
                world["member_id"],
                "annotator",
                world["owner_id"],
                1,
                None,
            )
            assert (
                isinstance(row.assigned_at, datetime)
                and row.assigned_at.tzinfo is not None
            )
            async with owner.begin() as writer:
                await writer.execute(text("SET LOCAL lock_timeout='500ms'"))
                await writer.execute(
                    text(
                        "SELECT id FROM public.project_members WHERE id=:id FOR UPDATE NOWAIT"
                    ),
                    {"id": row.id},
                )
                await writer.execute(
                    text(
                        "UPDATE public.project_members SET role='unknown',weekly_target=17,version=2 WHERE id=:id"
                    ),
                    {"id": row.id},
                )
            changed = (
                await db.execute(
                    select(facts.c.role, facts.c.weekly_target, facts.c.version).where(
                        facts.c.id == row.id
                    )
                )
            ).one()
            assert changed == ("unknown", 17, 2)
            assert list(await db.scalars(member_user_ids(world["project_id"]))) == []
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == before
            await db.rollback()


@pytest.mark.parametrize("cache", ["miss", "clean", "expired", "stale_partial"])
async def test_restricted_roster_keeps_mapper_identity_and_stale_fields(
    test_engine, cache
):
    async with _world(test_engine) as (owner, api, world, _url):
        async with api() as db:
            member = aliased(ProjectMember, project_member_facts(), adapt_on_names=True)
            cached = (
                None
                if cache == "miss"
                else await db.scalar(
                    select(member).where(member.id == world["membership_id"])
                )
            )
            project = await get_workspace_project(db, project_id=world["project_id"])
            async with owner.begin() as writer:
                await writer.execute(
                    text(
                        "UPDATE public.project_members SET role='reviewer',weekly_target=31,version=2 WHERE id=:id"
                    ),
                    {"id": world["membership_id"]},
                )
            if cache == "expired":
                db.expire(cached)
            elif cache == "stale_partial":
                db.expire(cached, ["role", "weekly_target"])
            statements = []

            def capture(_conn, _cursor, statement, _parameters, _context, _many):
                statements.append(statement)

            event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
            try:
                rows = await _roster(db, project)
            finally:
                event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
            entry = next(row for row in rows if row.user.id == world["member_id"])
            assert entry.project_role == (
                "annotator" if cache == "clean" else "reviewer"
            )
            assert len(statements) == 2
            assert "aap_workspace_v1.project_member_facts" in statements[0]
            assert not any("FROM project_members" in sql for sql in statements)
            if cached is not None:
                assert (
                    db.sync_session.identity_map[
                        ProjectMember.__mapper__.identity_key_from_primary_key(
                            (world["membership_id"],)
                        )
                    ]
                    is cached
                )
                assert cached.version == (2 if cache == "expired" else 1)
            await db.rollback()


async def test_delivery_and_mentions_need_no_private_member_select(test_engine):
    async with _world(test_engine) as (owner, api, world, _url):
        # These operations still contain independent Project queries, deliberately
        # outside this membership-only qualification.
        async with owner.begin() as setup:
            await setup.execute(
                text("GRANT SELECT ON public.projects TO identity_api_consumer")
            )
            await setup.execute(
                text("UPDATE project_members SET role='unknown' WHERE id=:id"),
                {"id": world["membership_id"]},
            )
            await setup.execute(
                text("UPDATE users SET is_active=false WHERE id=:id"),
                {"id": world["member_id"]},
            )
        async with api() as db:
            assert not await db.scalar(
                text(
                    "SELECT has_any_column_privilege(current_user,'public.project_members','SELECT')"
                )
            )
            await AnnotationCommentService(db).validate_mentions(
                world["project_id"], [world["member_id"], world["owner_id"]]
            )
            rows = [
                SimpleNamespace(user_id=world["member_id"]),
                SimpleNamespace(user_id=world["owner_id"]),
            ]
            assert await NotificationService(db)._allowed_delivery_indices(
                rows, [(True, world["project_id"], False)] * 2
            ) == {1}
            await db.rollback()


async def test_batch_role_only_correlated_updates_keep_overrides_and_rollback(
    test_engine,
):
    async with _consumer_world(test_engine, role="reviewer") as (
        owner,
        api,
        world,
        _url,
    ):
        async with owner.begin() as setup:
            batch = await create_batch(
                setup, project_id=world["project_id"], status="active"
            )
            batch_id = batch.id
            ids = []
            for override in [False, True]:
                task = await create_task(
                    setup, project_id=world["project_id"], status="completed"
                )
                task.batch_id = batch_id
                task.assignee_id = world["member_id"]
                task.reviewer_id = world["member_id"]
                task.assignee_is_override = override
                task.reviewer_is_override = override
                task.reviewer_claimed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
                ids.append(task.id)
            await setup.execute(
                text("UPDATE users SET is_active=false WHERE id=:id"),
                {"id": world["member_id"]},
            )
        async with api() as db:
            trace = []

            def capture(_conn, _cursor, statement, _parameters, _context, _many):
                if "aap_workspace_v1.project_member_facts" in statement:
                    trace.append(statement)

            event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
            try:
                await BatchService(db)._reset_and_clean_batch_tasks(batch_id)
            finally:
                event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
            assert len(trace) == 2
            assert all(
                sql.startswith("UPDATE tasks ") and "NOT (EXISTS" in sql
                for sql in trace
            )
            assert all("account_profiles" not in sql for sql in trace)
            rows = {
                task.id: task
                for task in await db.scalars(select(Task).where(Task.id.in_(ids)))
            }
            assert rows[ids[0]].assignee_id is None
            assert rows[ids[0]].reviewer_id == world["member_id"]
            assert rows[ids[0]].reviewer_claimed_at is not None
            assert rows[ids[1]].assignee_id == world["member_id"]
            assert rows[ids[1]].reviewer_id == world["member_id"]
            await db.rollback()
        async with owner() as check:
            assert set(
                await check.scalars(select(Task.status).where(Task.id.in_(ids)))
            ) == {"completed"}


async def test_node_reads_fact_relation_without_python_provider(test_engine):
    async with _world(test_engine) as (_owner, _api, world, url):
        process = await asyncio.create_subprocess_exec(
            "node",
            str(Path(__file__).parent / "fixtures/workspace_member_facts_peer.mjs"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(
                json.dumps(
                    {
                        "database_url": url.set(
                            drivername="postgresql"
                        ).render_as_string(hide_password=False),
                        "project_id": str(world["project_id"]),
                        "member_id": str(world["member_id"]),
                        "membership_id": str(world["membership_id"]),
                    }
                ).encode()
            ),
            timeout=20,
        )
        assert process.returncode == 0, stderr.decode()
        assert json.loads(stdout) == {
            "ok": True,
            "read_only": True,
            "same_transaction": True,
        }


@pytest.mark.parametrize("surface", ["admin", "annotator"])
async def test_actual_http_dashboards_without_private_member_reads(
    test_engine, app_module, monkeypatch, surface
):
    from app.deps import get_db

    _configure_api_auth(test_engine, monkeypatch)
    monkeypatch.setattr(settings, "identity_briefs_database_url_file", "")
    monkeypatch.setattr(
        settings, "identity_briefs_database_url", project_query_briefs_dsn(test_engine)
    )
    async with _consumer_world(test_engine, role="annotator") as (
        owner,
        api,
        world,
        _url,
    ):
        # Independent raw Project projections remain a later slice.
        async with owner.begin() as setup:
            await setup.execute(
                text("GRANT SELECT ON public.projects TO identity_api_consumer")
            )

        async def request_db():
            async with api() as db:
                assert not await db.scalar(
                    text(
                        "SELECT has_any_column_privilege(current_user,'public.project_members','SELECT')"
                    )
                )
                await db.rollback()
                await db.execute(text("SET TRANSACTION READ ONLY"))
                yield db

        monkeypatch.setitem(app_module.dependency_overrides, get_db, request_db)
        actor_id = world["owner_id"] if surface == "admin" else world["member_id"]
        headers = {
            "Authorization": "Bearer "
            + create_access_token(
                subject=str(actor_id),
                role="project_admin" if surface == "admin" else "employee",
            )
        }
        paths = (
            [
                f"/api/v1/dashboard/admin/people?project={world['project_id']}&role=annotator",
                f"/api/v1/dashboard/admin/people/{world['member_id']}?project={world['project_id']}",
            ]
            if surface == "admin"
            else ["/api/v1/dashboard/me/performance"]
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module), base_url="http://test"
        ) as client:
            responses = [await client.get(path, headers=headers) for path in paths]
        assert all(response.status_code == 200 for response in responses), [
            (response.status_code, response.text) for response in responses
        ]
        if surface == "admin":
            assert str(world["member_id"]) in [
                row["user_id"] for row in responses[0].json()["items"]
            ]
            assert responses[1].json()["user_id"] == str(world["member_id"])
        else:
            assert responses[0].json()["throughput"] == 0
