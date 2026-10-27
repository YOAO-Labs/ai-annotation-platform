"""Real credentials and native Project facts without owner-private reads."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased

from aap_workspace_contract.read_relations import (
    PROJECT_COUNTER_COLUMNS,
    PROJECT_COUNTER_FACT_COLUMNS,
    PROJECT_FACT_COLUMNS,
    PROJECT_COUNTER_FACTS_RELATION,
    PROJECT_FACTS_RELATION,
    read_relations_schema,
)
from aap_workspace_contract.project_query_contract import PROJECT_ROW_COLUMNS
from app.db.models.project import Project
from app.services.annotation_comment import AnnotationCommentService
from app.services.notification import NotificationService
from app.services.workspace_read_relations import (
    project_counter_facts,
    project_facts,
    project_rows,
)
from types import SimpleNamespace
from tests.test_workspace_project_authority_locking import _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned module-data resources",
    ),
]
_ROOT = Path(__file__).resolve().parents[3]


async def test_native_column_contract_owner_and_grants_match_database(test_engine):
    schema = read_relations_schema()
    native = json.loads(
        (
            _ROOT / "apps/api/alembic/sql/0377_workspace_project_snapshots.schema.json"
        ).read_text()
    )
    observations = {}
    async with _world(test_engine) as (owner, _api, _world_data, _url):
        async with owner() as db:
            base = {
                name: {"type": kind, "nullable": not required}
                for name, kind, required in (
                    await db.execute(
                        text(
                            "SELECT attname,format_type(atttypid,atttypmod),attnotnull "
                            "FROM pg_attribute WHERE attrelid='public.projects'::regclass "
                            "AND attnum>0 AND NOT attisdropped ORDER BY attnum"
                        )
                    )
                ).all()
            }
            for relation, expected_owner in [
                (PROJECT_FACTS_RELATION, "workspace_code"),
                (PROJECT_COUNTER_FACTS_RELATION, "labeling_code"),
            ]:
                contract = schema["x-postgresql-relations"][relation]
                assert native["x-postgresql-relations"][relation] == contract
                assert (
                    await db.execute(
                        text(
                            "SELECT pg_get_userbyid(relowner),reloptions,relkind::text FROM pg_class "
                            "WHERE oid=CAST(:view AS regclass)"
                        ),
                        {"view": relation},
                    )
                ).one() == (expected_owner, ["security_barrier=true"], "v")
                columns = (
                    await db.execute(
                        text(
                            "SELECT attname,format_type(atttypid,atttypmod) FROM pg_attribute "
                            "WHERE attrelid=CAST(:view AS regclass) AND attnum>0 ORDER BY attnum"
                        ),
                        {"view": relation},
                    )
                ).all()
                assert [row[0] for row in columns] == contract["columns"]
                observations[relation] = {
                    name: {
                        "type": kind,
                        "nullable": base["id" if name == "project_id" else name][
                            "nullable"
                        ],
                    }
                    for name, kind in columns
                }
                for role, expected in [
                    ("identity_api_consumer", True),
                    ("legacy_runtime", True),
                    ("append_client", False),
                    ("inference_code", False),
                    ("workspace_membership_client", False),
                ]:
                    assert (
                        await db.scalar(
                            text("SELECT has_table_privilege(:role,:view,'SELECT')"),
                            {"role": role, "view": relation},
                        )
                        is expected
                    )
                for privilege in [
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                    "TRUNCATE",
                    "REFERENCES",
                    "TRIGGER",
                ]:
                    assert not await db.scalar(
                        text(
                            "SELECT has_table_privilege('identity_api_consumer',:view,:privilege)"
                        ),
                        {"view": relation, "privilege": privilege},
                    )
                assert not await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_class c CROSS JOIN LATERAL "
                        "aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a "
                        "WHERE c.oid=CAST(:view AS regclass) AND a.grantee=0)"
                    ),
                    {"view": relation},
                )
            assert not await db.scalar(
                text(
                    "SELECT has_column_privilege('workspace_code','public.projects','total_tasks','SELECT')"
                )
            )
    (_ROOT / ".worktree/workspace-project-facts-column-observation.json").write_text(
        json.dumps(observations, indent=2) + "\n"
    )
    assert observations == {
        name: schema["x-postgresql-relations"][name]["column_types"]
        for name in observations
    }


async def test_factories_keep_owner_columns_and_full_native_row_order():
    assert tuple(project_facts().c.keys()) == PROJECT_FACT_COLUMNS
    assert tuple(project_counter_facts().c.keys()) == PROJECT_COUNTER_FACT_COLUMNS
    assert tuple(project_rows().c.keys()) == PROJECT_ROW_COLUMNS
    assert not set(PROJECT_COUNTER_COLUMNS).intersection(project_facts().c.keys())
    assert project_facts() is not project_facts()
    assert project_rows() is not project_rows()
    sql = str(select(project_rows()))
    assert "aap_workspace_v1.project_facts" in sql
    assert "aap_labeling_v1.project_catalog_counters" in sql
    assert "public.projects" not in sql


async def test_restricted_native_rows_preserve_raw_values_transaction_and_no_locks(
    test_engine,
):
    async with _world(test_engine) as (owner, api, world, _url):
        async with owner.begin() as setup:
            await setup.execute(
                text(
                    "UPDATE projects SET label_config=CAST(:config AS jsonb),"
                    "batch_summary='null'::jsonb,iou_dedup_threshold='NaN'::double precision,"
                    "box_threshold=0.123456789::real,total_tasks=17 WHERE id=:id"
                ),
                {
                    "id": world["project_id"],
                    "config": json.dumps([1, None, {"legacy": True}]),
                },
            )
        async with owner() as reference:
            expected = (
                (
                    await reference.execute(
                        select(
                            *(Project.__table__.c[name] for name in PROJECT_ROW_COLUMNS)
                        ).where(Project.id == world["project_id"])
                    )
                )
                .one()
                ._mapping
            )
        async with api() as db:
            for table in ("users", "projects", "project_members"):
                with pytest.raises(DBAPIError) as denied:
                    async with db.begin_nested():
                        await db.execute(text(f"SELECT id FROM public.{table} LIMIT 1"))
                assert denied.value.orig.sqlstate == "42501"
            for relation, key in [
                (PROJECT_FACTS_RELATION, "id"),
                (PROJECT_COUNTER_FACTS_RELATION, "project_id"),
            ]:
                for statement in [
                    f"UPDATE {relation} SET {key}={key} WHERE false",
                    f"DELETE FROM {relation} WHERE false",
                    f"INSERT INTO {relation} DEFAULT VALUES",
                    f"SELECT {key} FROM {relation} FOR UPDATE",
                ]:
                    with pytest.raises(DBAPIError) as denied:
                        async with db.begin_nested():
                            await db.execute(text(statement))
                    assert denied.value.orig.sqlstate in {"42501", "55000", "0A000"}
            await db.rollback()
            await db.execute(text("SET TRANSACTION READ ONLY"))
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            rows = project_rows()
            actual = (
                (await db.execute(select(rows).where(rows.c.id == world["project_id"])))
                .one()
                ._mapping
            )
            assert tuple(actual) == PROJECT_ROW_COLUMNS
            for key, value in expected.items():
                if isinstance(value, float) and math.isnan(value):
                    assert math.isnan(actual[key])
                else:
                    assert actual[key] == value, key
            assert actual["label_config"] == [1, None, {"legacy": True}]
            assert actual["batch_summary"] is None
            async with owner.begin() as writer:
                await writer.execute(text("SET LOCAL lock_timeout='500ms'"))
                await writer.execute(
                    text("SELECT id FROM projects WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": world["project_id"]},
                )
                await writer.execute(
                    text(
                        "UPDATE projects SET name='committed',total_tasks=31 WHERE id=:id"
                    ),
                    {"id": world["project_id"]},
                )
            assert (
                await db.execute(
                    select(rows.c.name, rows.c.total_tasks).where(
                        rows.c.id == world["project_id"]
                    )
                )
            ).one() == ("committed", 31)
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == before
            await db.rollback()


async def test_existing_counter_view_is_equivalent_under_enforced_nonnull_columns(
    test_engine,
):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner() as db:
            for column in PROJECT_COUNTER_COLUMNS:
                with pytest.raises(DBAPIError) as denied:
                    async with db.begin_nested():
                        await db.execute(
                            text(f"UPDATE projects SET {column}=NULL WHERE id=:id"),
                            {"id": world["project_id"]},
                        )
                assert denied.value.orig.sqlstate == "23502", column
            await db.rollback()


@pytest.mark.parametrize("cache", ["miss", "clean", "expired", "partial"])
async def test_restricted_full_entity_keeps_identity_and_expired_fields(
    test_engine, cache
):
    async with _world(test_engine) as (owner, api, world, _url):
        async with api() as db:
            entity = aliased(Project, project_rows(), adapt_on_names=True)
            cached = (
                None
                if cache == "miss"
                else await db.scalar(
                    select(entity).where(entity.id == world["project_id"])
                )
            )
            async with owner.begin() as change:
                await change.execute(
                    text(
                        "UPDATE projects SET name='committed',total_tasks=31 WHERE id=:id"
                    ),
                    {"id": world["project_id"]},
                )
            if cache == "expired":
                db.expire(cached)
            elif cache == "partial":
                db.expire(cached, ["total_tasks"])
            statements = []

            def capture(_conn, _cursor, statement, _parameters, _context, _many):
                statements.append(statement)

            event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
            try:
                actual = await db.scalar(
                    select(entity).where(entity.id == world["project_id"])
                )
            finally:
                event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
            assert len(statements) == 1
            assert "FROM projects" not in statements[0]
            assert actual.name == (
                "E2E Project" if cache in {"clean", "partial"} else "committed"
            )
            assert actual.total_tasks == (0 if cache == "clean" else 31)
            if cached is not None:
                assert actual is cached
            await db.rollback()


async def test_restricted_delivery_and_mentions_keep_distinct_policies(test_engine):
    async with _world(test_engine) as (owner, api, world, _url):
        rows = [
            SimpleNamespace(user_id=world["owner_id"]),
            SimpleNamespace(user_id=world["member_id"]),
            SimpleNamespace(user_id=world["member_id"]),
        ]
        scopes = [
            (True, world["project_id"], False),
            (True, world["project_id"], False),
            (True, world["project_id"], True),
        ]
        async with api() as db:
            assert await NotificationService(db)._allowed_delivery_indices(
                rows, scopes
            ) == {0, 1}
            await AnnotationCommentService(db).validate_mentions(
                world["project_id"], [world["owner_id"], world["member_id"]]
            )
            async with owner.begin() as change:
                await change.execute(
                    text(
                        "UPDATE users SET is_active=false WHERE id IN (:owner,:member)"
                    ),
                    {"owner": world["owner_id"], "member": world["member_id"]},
                )
                await change.execute(
                    text("UPDATE project_members SET role='unknown' WHERE id=:id"),
                    {"id": world["membership_id"]},
                )
            assert (
                await NotificationService(db)._allowed_delivery_indices(rows, scopes)
                == set()
            )
            await AnnotationCommentService(db).validate_mentions(
                world["project_id"], [world["owner_id"], world["member_id"]]
            )
            await db.rollback()
