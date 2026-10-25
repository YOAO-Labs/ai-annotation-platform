"""Deployment ACL closure for ordinary Workspace API/worker consumers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import uuid

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.security import create_access_token
from app.services.project_access import get_workspace_project
from app.services.project_workspace import lock_project_row
from tests.factory import create_project, create_task, create_user
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_labeling_counter_deltas_workflows_native import _configure_actual_api
from tests.test_workspace_configuration_live import _public_configuration_commands

ROOT = Path(__file__).resolve().parents[3]
_PROFILE = ROOT / "infra/docker/workspace-consumer-grants.sql"
_PRIVATE = (
    "projects",
    "project_members",
    "project_templates",
    "project_deletion_operations",
    "dataset_unlink_operations",
    "workspace_configuration_operation_states",
    "workspace_configuration_operation_attempts",
    "workspace_configuration_operation_receipts",
    "workspace_membership_operation_receipts",
    "workspace_owner_transfer_operation_receipts",
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data PostgreSQL verifier",
    ),
]


async def _grants(test_engine, filename, role, **overrides):
    _verify_postgres(test_engine.url)
    variables = {
        "expected_database": test_engine.url.database,
        "migration_role": test_engine.url.username,
        "application_role": role,
        "consumer_role": role,
        "workspace_owner": "workspace_code",
        "identity_owner": "identity_code",
        "asset_cleanup_role": "asset_cleanup_client",
        **overrides,
    }
    command = [
        "docker",
        "exec",
        "-i",
        os.environ["AAP_MODULE_DATA_PG_CONTAINER_ID"],
        "psql",
        "-X",
        "-q",
        "-U",
        test_engine.url.username,
        "-d",
        test_engine.url.database,
    ]
    for key, value in variables.items():
        command.extend(["-v", f"{key}={value}"])
    return await asyncio.to_thread(
        subprocess.run,
        command,
        input=filename.read_text(),
        text=True,
        capture_output=True,
        timeout=30,
    )


_MAINTENANCE_OWNERS = text("""
SELECT relation.oid,relation.relkind,format('%I.%I',namespace.nspname,relation.relname) AS qualified,
       format('%I',owner.rolname) AS owner,tree.family
FROM (
    SELECT relid,'audit_logs' AS family FROM pg_partition_tree('public.audit_logs')
    UNION ALL
    SELECT relid,'predictions' FROM pg_partition_tree('public.predictions')
    UNION ALL
    SELECT 'public.mv_user_perf_daily'::regclass,'mv_user_perf_daily'
    UNION ALL
    SELECT 'public.mv_audit_bi_daily'::regclass,'mv_audit_bi_daily'
) tree
JOIN pg_class relation ON relation.oid=tree.relid
JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace
JOIN pg_roles owner ON owner.oid=relation.relowner
ORDER BY relation.oid
""")


@asynccontextmanager
async def _world(test_engine, *, apply=True):
    _verify_postgres(test_engine.url)
    role = "workspace_consumer_" + uuid.uuid4().hex[:12]
    cleanup_role = role + "_gc"
    password = uuid.uuid4().hex
    async with test_engine.begin() as db:
        before = (await db.execute(_MAINTENANCE_OWNERS)).mappings().all()
        await db.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
        await db.execute(text(f"CREATE ROLE {cleanup_role} LOGIN"))
    engine = None
    try:
        for filename in ("lan-production-grants.sql", "identity-consumer-grants.sql"):
            result = await _grants(
                test_engine,
                ROOT / "infra/docker" / filename,
                role,
                asset_cleanup_role=cleanup_role,
            )
            assert result.returncode == 0, result.stderr
        async with test_engine.begin() as db:
            await db.execute(
                text(f"GRANT SELECT(id),UPDATE(name) ON public.projects TO {role}")
            )
            await db.execute(
                text(
                    f"GRANT SELECT ON aap_workspace_v1.project_catalog_configs TO {role}"
                )
            )
            await db.execute(
                text(
                    f"GRANT EXECUTE ON FUNCTION aap_workspace_v1._project_delete_failure(text) TO {role}"
                )
            )
            await db.execute(text(f"GRANT CREATE ON SCHEMA aap_workspace_v1 TO {role}"))
        if apply:
            result = await _grants(test_engine, _PROFILE, role)
            assert result.returncode == 0, result.stderr
        url = test_engine.url.set(username=role, password=password)
        engine = create_async_engine(url, hide_parameters=True)
        api = async_sessionmaker(engine, expire_on_commit=False)
        owner = async_sessionmaker(test_engine, expire_on_commit=False)
        yield owner, api, role, url
    finally:
        if engine is not None:
            await engine.dispose()
        async with test_engine.begin() as db:
            original = {row["oid"]: row["owner"] for row in before}
            family_owner = {
                row["family"]: row["owner"]
                for row in before
                if row["qualified"] == "public." + row["family"]
            }
            current = (await db.execute(_MAINTENANCE_OWNERS)).mappings().all()
            for row in current:
                owner = original.get(row["oid"], family_owner[row["family"]])
                kind = "MATERIALIZED VIEW" if row["relkind"] == "m" else "TABLE"
                await db.execute(
                    text(f"ALTER {kind} {row['qualified']} OWNER TO {owner}")
                )
            after = (await db.execute(_MAINTENANCE_OWNERS)).mappings().all()
            assert {
                row["oid"]: row["owner"] for row in after if row["oid"] in original
            } == original
            # Preserve audited data; only this fixture LOGIN's ACLs are removed.
            assert not await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_class WHERE relowner=(SELECT oid FROM pg_roles WHERE rolname=:role))"
                ),
                {"role": role},
            )
            await db.execute(text(f"DROP OWNED BY {role}"))
            await db.execute(text(f"DROP ROLE {role}"))
            await db.execute(text(f"DROP OWNED BY {cleanup_role}"))
            await db.execute(text(f"DROP ROLE {cleanup_role}"))


async def _assert_closed(db):
    for table in _PRIVATE:
        assert not await db.scalar(
            text(
                "SELECT has_table_privilege(current_user,:table,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
                "OR has_any_column_privilege(current_user,:table,'SELECT,INSERT,UPDATE,REFERENCES')"
            ),
            {"table": "public." + table},
        ), table
    assert not await db.scalar(
        text(
            "SELECT has_function_privilege(current_user,'aap_workspace_v1._project_delete_failure(text)','EXECUTE')"
        )
    )
    assert not await db.scalar(
        text("SELECT has_schema_privilege(current_user,'aap_workspace_v1','CREATE')")
    )
    assert not await db.scalar(
        text(
            "SELECT has_sequence_privilege(current_user,'public.display_seq_projects','USAGE,SELECT,UPDATE')"
        )
    )


async def test_profile_closes_private_columns_receipts_helpers_and_sequences(
    test_engine,
):
    async with _world(test_engine) as (owner, api, _role, _url):
        async with api() as db:
            await _assert_closed(db)
            assert not await db.scalar(
                text(
                    "SELECT has_table_privilege(current_user,'public.task_events','SELECT') "
                    "OR has_any_column_privilege(current_user,'public.task_events','SELECT')"
                )
            )
            assert await db.scalar(
                text(
                    "SELECT has_table_privilege(current_user,"
                    "'aap_labeling_v1.task_event_facts','SELECT')"
                )
            )
            mv_source = await db.scalar(
                text(
                    "SELECT pg_catalog.lower(pg_catalog.pg_get_viewdef("
                    "'public.mv_user_perf_daily'::regclass,true))"
                )
            )
            assert "aap_labeling_v1.task_event_facts" in mv_source
            assert "public.task_events" not in mv_source
            assert not await db.scalar(
                text(
                    "SELECT EXISTS ("
                    "SELECT FROM pg_catalog.pg_class AS relation "
                    "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE("
                    "relation.relacl,pg_catalog.acldefault('r',relation.relowner))) AS acl "
                    "WHERE relation.oid='public.mv_user_perf_daily'::regclass "
                    "AND acl.grantee=0 AND acl.privilege_type='SELECT')"
                )
            )
            await db.execute(
                text("REFRESH MATERIALIZED VIEW public.mv_user_perf_daily")
            )
            await db.commit()
            functions = (
                await db.execute(
                    text(
                        "SELECT p.oid::regprocedure::text, pg_get_userbyid(p.proowner),p.prosecdef "
                        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname ~ '^aap_workspace(_[a-z0-9]+)*_v[0-9]+$' "
                        "AND has_function_privilege(current_user,p.oid,'EXECUTE') ORDER BY 1"
                    )
                )
            ).all()
            assert len(functions) == 32
            assert all(row[1:] == ("workspace_code", True) for row in functions)
            relations = (
                (
                    await db.execute(
                        text(
                            "SELECT n.nspname||'.'||c.relname FROM pg_class c "
                            "JOIN pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE n.nspname ~ '^aap_workspace(_[a-z0-9]+)*_v[0-9]+$' "
                            "AND c.relkind IN ('r','p','v','m','f') "
                            "AND has_any_column_privilege(current_user,c.oid,'SELECT') ORDER BY 1"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert relations == [
                "aap_workspace_v1.project_facts",
                "aap_workspace_v1.project_member_facts",
            ]
            # Unrelated owner capabilities and Labeling counters stay usable.
            assert await db.scalar(
                text("SELECT has_table_privilege(current_user,'public.tasks','UPDATE')")
            )
            assert await db.scalar(
                text(
                    "SELECT has_table_privilege(current_user,'aap_labeling_v1.project_catalog_counters','SELECT')"
                )
            )
            assert await db.scalar(
                text(
                    "SELECT has_function_privilege(current_user,'aap_identity_v1.read_actor_facts(uuid)','EXECUTE')"
                )
            )
        # Reapplying the deployment profile keeps the same closed surface.
        result = await _grants(test_engine, _PROFILE, _role)
        assert result.returncode == 0, result.stderr
        async with api() as db:
            await _assert_closed(db)


@pytest.mark.parametrize("invalid", ["database", "owner", "consumer", "membership"])
async def test_profile_rejects_unsafe_identity_before_acl_changes(test_engine, invalid):
    async with _world(test_engine, apply=False) as (_owner, api, role, _url):
        options = {}
        if invalid == "database":
            options["expected_database"] = "other_test"
        elif invalid == "owner":
            options["workspace_owner"] = "labeling_code"
        elif invalid == "consumer":
            options["consumer_role"] = "workspace_code"
        else:
            async with test_engine.begin() as db:
                await db.execute(text(f"GRANT workspace_code TO {role}"))
        result = await _grants(test_engine, _PROFILE, role, **options)
        assert result.returncode != 0
        async with api() as db:
            assert await db.scalar(
                text(
                    "SELECT has_table_privilege(current_user,'public.projects','UPDATE')"
                )
            )
            assert await db.scalar(
                text(
                    "SELECT has_schema_privilege(current_user,'aap_workspace_v1','CREATE')"
                )
            )


async def test_public_column_grant_causes_atomic_profile_rejection(test_engine):
    async with _world(test_engine, apply=False) as (_owner, api, role, _url):
        async with test_engine.begin() as db:
            await db.execute(text("GRANT SELECT(id) ON public.projects TO PUBLIC"))
        try:
            result = await _grants(test_engine, _PROFILE, role)
            assert result.returncode != 0
            assert "retains private data" in result.stderr
            async with api() as db:
                assert await db.scalar(
                    text(
                        "SELECT has_table_privilege(current_user,'public.projects','UPDATE')"
                    )
                )
                assert await db.scalar(
                    text(
                        "SELECT has_schema_privilege(current_user,'aap_workspace_v1','CREATE')"
                    )
                )
        finally:
            async with test_engine.begin() as db:
                await db.execute(
                    text("REVOKE SELECT(id) ON public.projects FROM PUBLIC")
                )


async def test_borrowed_workspace_reads_and_locks_keep_outer_rollback(test_engine):
    async with _world(test_engine) as (owner, api, _role, _url):
        async with owner.begin() as db:
            actor = await create_user(
                db, "project_admin", f"profile-{uuid.uuid4()}@example.test", "Owner"
            )
            project = await create_project(db, owner_id=actor.id)
            project_id = project.id
        async with api() as db:
            await _assert_closed(db)
            await db.rollback()
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            found = await get_workspace_project(db, project_id=project_id)
            locked = await lock_project_row(
                db, project_id, nowait=True, populate_existing=True
            )
            assert found is locked and locked.id == project_id
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == before
            await db.execute(
                text(
                    "SELECT aap_workspace_v1.write_point_cloud_quality_config(:id,CAST(:config AS jsonb))"
                ),
                {"id": project_id, "config": json.dumps({"profile": True})},
            )
            await db.rollback()
        async with owner() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT point_cloud_quality_config FROM public.projects WHERE id=:id"
                    ),
                    {"id": project_id},
                )
                == {}
            )


async def test_actual_http_workspace_lifecycle_uses_closed_borrowed_profile(
    test_engine, app_module, monkeypatch
):
    _configure_actual_api(test_engine, monkeypatch)
    for capability in (
        "membership",
        "owner_transfer",
        "configuration",
        "template_query",
        "project_query",
        "directory_query",
    ):
        for suffix in ("url", "database_url", "database_url_file"):
            monkeypatch.setattr(settings, f"workspace_{capability}_{suffix}", "")
    async with _world(test_engine) as (owner, api, _role, _url):
        async with owner.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"profile-http-{uuid.uuid4()}@example.test",
                "HTTP owner",
            )
            target = await create_user(
                db, "employee", f"profile-member-{uuid.uuid4()}@example.test", "Member"
            )
        from app.deps import get_db
        from app.middleware import audit as audit_middleware

        monkeypatch.setattr(audit_middleware, "async_session", api)
        statements = []

        def record(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        engine = api.kw["bind"]
        event.listen(engine.sync_engine, "before_cursor_execute", record)

        async def request_db():
            async with api() as db:
                await _assert_closed(db)
                try:
                    yield db
                finally:
                    await db.rollback()

        monkeypatch.setitem(app_module.dependency_overrides, get_db, request_db)
        try:
            headers = {
                "Authorization": "Bearer "
                + create_access_token(subject=str(actor.id), role="project_admin")
            }
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_module), base_url="http://test"
            ) as client:
                (
                    template_id,
                    project_id,
                    template_name,
                    project_name,
                ) = await _public_configuration_commands(
                    client, actor, uuid.uuid4().hex
                )
                for path in (
                    "/api/v1/projects",
                    f"/api/v1/projects/{project_id}",
                    "/api/v1/project-templates",
                    f"/api/v1/project-templates/{template_id}",
                    f"/api/v1/projects/{project_id}/members",
                ):
                    response = await client.get(path, headers=headers)
                    assert response.status_code == 200, response.text
                added = await client.post(
                    f"/api/v1/projects/{project_id}/members",
                    headers=headers,
                    json={"user_id": str(target.id), "role": "annotator"},
                )
                assert added.status_code == 201, added.text
                member = added.json()
                searched = await client.get("/api/v1/search?q=Member", headers=headers)
                assert searched.status_code == 200, searched.text
                assert str(target.id) in {
                    row["id"] for row in searched.json()["members"]
                }
                removed = await client.delete(
                    f"/api/v1/projects/{project_id}/members/{member['id']}",
                    headers=headers,
                )
                assert removed.status_code == 204, removed.text
                deleted_template = await client.delete(
                    f"/api/v1/project-templates/{template_id}", headers=headers
                )
                assert deleted_template.status_code == 204, deleted_template.text
                deleted_project = await client.delete(
                    f"/api/v1/projects/{project_id}", headers=headers
                )
                assert deleted_project.status_code == 204, deleted_project.text
            assert any(
                "aap_workspace_configuration_v1.configuration_step" in s
                for s in statements
            )
            assert any(
                "aap_workspace_membership_v1.apply_membership_command" in s
                for s in statements
            )
            assert any("aap_workspace_v1.delete_project" in s for s in statements)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)


async def test_node_consumes_closed_workspace_profile(test_engine):
    async with _world(test_engine) as (owner, _api, _role, url):
        async with owner.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"profile-node-{uuid.uuid4()}@example.test",
                "Node owner",
            )
            project = await create_project(db, owner_id=actor.id)
            project_id = project.id
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "node",
                str(
                    ROOT / "apps/api/tests/fixtures/workspace_consumer_profile_peer.mjs"
                ),
            ],
            input=json.dumps(
                {
                    "database_url": url.set(drivername="postgresql").render_as_string(
                        hide_password=False
                    ),
                    "project_id": str(project_id),
                }
            ),
            capture_output=True,
            text=True,
            timeout=20,
            cwd=ROOT,
        )
        assert result.returncode == 0, "Workspace consumer Node peer failed"
        assert json.loads(result.stdout) == {
            "ok": True,
            "functions": 32,
            "private_denied": True,
            "same_transaction": True,
        }


async def test_profile_removes_future_private_relation_defaults(test_engine):
    async with _world(test_engine) as (_owner, api, _role, _url):
        table = "workspace_profile_future_" + uuid.uuid4().hex[:12]
        sequence = "display_seq_projects_profile_" + uuid.uuid4().hex[:12]
        try:
            async with test_engine.begin() as db:
                await db.execute(text(f"CREATE TABLE public.{table}(id uuid)"))
                await db.execute(text(f"CREATE SEQUENCE public.{sequence}"))
            async with api() as db:
                assert not await db.scalar(
                    text(
                        "SELECT has_table_privilege(current_user,:table,'SELECT,INSERT,UPDATE,DELETE')"
                    ),
                    {"table": "public." + table},
                )
                assert not await db.scalar(
                    text(
                        "SELECT has_sequence_privilege(current_user,:sequence,'USAGE,SELECT,UPDATE')"
                    ),
                    {"sequence": "public." + sequence},
                )
        finally:
            async with test_engine.begin() as db:
                await db.execute(text(f"DROP TABLE IF EXISTS public.{table}"))
                await db.execute(text(f"DROP SEQUENCE IF EXISTS public.{sequence}"))


async def test_migration_owner_inheritance_keeps_the_existing_maintenance_direction(
    test_engine,
):
    async with _world(test_engine, apply=False) as (_owner, api, role, _url):
        migrator = test_engine.url.username
        async with test_engine.begin() as db:
            await db.execute(text(f'GRANT {role} TO "{migrator}"'))
        try:
            result = await _grants(test_engine, _PROFILE, role)
            assert result.returncode == 0, result.stderr
            async with api() as db:
                await _assert_closed(db)
                assert not await db.scalar(
                    text("SELECT pg_has_role(current_user,:migrator,'MEMBER')"),
                    {"migrator": migrator},
                )
        finally:
            async with test_engine.begin() as db:
                await db.execute(text(f'REVOKE {role} FROM "{migrator}"'))


async def test_worker_counter_service_uses_labeling_owner_and_keeps_rollback(
    test_engine,
):
    from app.db.models.task import Task
    from app.services.batch import BatchService

    async with _world(test_engine) as (owner, api, _role, _url):
        async with owner.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"profile-worker-{uuid.uuid4()}@example.test",
                "Worker owner",
            )
            project = await create_project(db, owner_id=actor.id)
            task = await create_task(db, project_id=project.id, status="pending")
            project_id, task_id = project.id, task.id
        async with api() as db:
            await _assert_closed(db)
            task = await db.get(Task, task_id)
            task.status = "completed"
            await BatchService(db)._sync_project_counters(project_id)
            await db.flush()
            project = await get_workspace_project(db, project_id=project_id)
            assert project.total_tasks == project.completed_tasks == 1
            await db.rollback()
        async with owner() as db:
            assert (
                await db.scalar(
                    text("SELECT status FROM tasks WHERE id=:id"), {"id": task_id}
                )
                == "pending"
            )
            assert (
                await db.execute(
                    text(
                        "SELECT total_tasks,completed_tasks FROM projects WHERE id=:id"
                    ),
                    {"id": project_id},
                )
            ).one() == (0, 0)


async def test_legacy_grants_keep_embedded_workspace_capabilities_available(
    test_engine,
):
    async with _world(test_engine, apply=False) as (_owner, api, _role, _url):
        async with api() as db:
            for function in (
                "aap_workspace_configuration_v1.configuration_policy()",
                "aap_workspace_configuration_v1.configuration_step(jsonb,text,jsonb,text[],text[])",
                "aap_workspace_template_query_v1.template_query_policy()",
                "aap_workspace_template_query_v1.template_query_step(jsonb,text,jsonb,text,text[],text)",
                "aap_workspace_directory_query_v1.directory_query_policy()",
                "aap_workspace_directory_query_v1.directory_query_step(jsonb,text,jsonb)",
                "aap_workspace_project_query_v1.project_search_member_ids(uuid,text)",
            ):
                assert await db.scalar(
                    text(
                        "SELECT has_function_privilege(current_user,:function,'EXECUTE')"
                    ),
                    {"function": function},
                ), function
            for schema in (
                "aap_workspace_configuration_v1",
                "aap_workspace_template_query_v1",
                "aap_workspace_directory_query_v1",
            ):
                assert await db.scalar(
                    text("SELECT has_schema_privilege(current_user,:schema,'USAGE')"),
                    {"schema": schema},
                )
            await db.execute(
                text("SELECT aap_workspace_configuration_v1.configuration_policy()")
            )
            await db.execute(
                text("SELECT aap_workspace_template_query_v1.template_query_policy()")
            )
            await db.execute(
                text("SELECT aap_workspace_directory_query_v1.directory_query_policy()")
            )
            await db.execute(
                text(
                    "SELECT * FROM aap_workspace_project_query_v1.project_search_member_ids(:actor,'employee')"
                ),
                {"actor": uuid.uuid4()},
            )
