"""Replay the shipped ACL installers against deliberately stale PostgreSQL grants."""

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from yoao_owner_data.postgres import verify_function_only_role

from app.services.async_job import create_job
from tests.test_asset_storage_cleanup_runtime import (
    _verified_resource_manifest,
    _verify_postgres,
)
from tests.test_grant_namespace_security import _login
from tests.test_m2b_private_access_live import _EFFECTIVE_ACCESS
from tests.test_workspace_consumer_profile_native import _grants
from tests.test_worktree_identity_runtime_role_native import _backend, _role_row

GRANTS = Path(__file__).resolve().parents[3] / "infra/docker"
_INFERENCE_FENCES = {
    "yoao_inference_backend_probe_v1.open_backend_probe_subject(text,uuid)": (
        "NULL::text, NULL::uuid"
    ),
    "yoao_inference_backend_endpoint_v1.open_execution_endpoint(text,uuid,uuid,text)": (
        "NULL::text, NULL::uuid, NULL::uuid, NULL::text"
    ),
    "yoao_inference_health_observation_v1.open_health_observation_subject(text,uuid,uuid)": (
        "NULL::text, NULL::uuid, NULL::uuid"
    ),
    "yoao_inference_health_observation_v1.seal_health_observation(text,uuid,uuid,text)": (
        "NULL::text, NULL::uuid, NULL::uuid, NULL::text"
    ),
    "yoao_inference_registry_ingress_v1.registry_by_url(text)": "NULL::text",
    "yoao_inference_registry_ingress_v1.registry_candidates(text)": "NULL::text",
}
_HEALTH_OWNER = "yoao_inference_health_observation_client"
_HEALTH_SCHEMA = "yoao_inference_health_observation_v1"
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned disposable PostgreSQL verifier",
    ),
]


@asynccontextmanager
async def _profile(test_engine, tmp_path, profile):
    if profile == "worktree":
        manifest = _verified_resource_manifest(test_engine, test_engine.url)
        backend = _backend(tmp_path, test_engine, root=Path(manifest["root"]))
        role = backend.resources["database"] + "_app"
        assert await _role_row(test_engine, role) is None
        await backend.owned_runtime_role("create")
        try:
            yield backend.runtime_url(), backend.grant_runtime
        finally:
            quote = test_engine.dialect.identifier_preparer.quote_identifier
            async with test_engine.begin() as db:
                await db.exec_driver_sql(f"DROP OWNED BY {quote(role)}")
            await backend.owned_runtime_role("drop")
        return

    remote = profile == "lan-isolated-issuer"
    selector, expected = (
        ("AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL", "identity_api_consumer")
        if remote
        else (
            "AAP_MODULE_DATA_IDENTITY_EMBEDDED_API_CONSUMER_URL",
            "identity_embedded_api_consumer",
        )
    )
    url = make_url(os.environ[selector])
    assert url.username == expected

    async def install():
        script, variables = (
            ("identity-consumer-grants.sql", {"identity_avatar_runtime": "1"})
            if remote
            else ("lan-production-grants.sql", {})
        )
        result = await _grants(test_engine, GRANTS / script, url.username, **variables)
        assert result.returncode == 0, result.stderr

    yield url, install


async def _observe_health_owner(test_engine, caller, registry_id, token):
    url = make_url(os.environ["AAP_MODULE_DATA_INFERENCE_HEALTH_OBSERVATION_URL"])
    assert (url.host, url.port, url.database, url.username) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
        _HEALTH_OWNER,
    )
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    owner = await asyncpg.connect(dsn)
    try:
        await verify_function_only_role(
            owner,
            schema=_HEALTH_SCHEMA,
            functions={
                ("health_observation_policy", ""),
                ("open_health_observation_subject", "text, uuid, uuid"),
                ("seal_health_observation", "text, uuid, uuid, text"),
            },
            role=_HEALTH_OWNER,
        )
        request_id = uuid.uuid4()
        async with caller.transaction():
            endpoint = await caller.fetchval(
                "SELECT yoao_inference_backend_endpoint_v1.capture_backend_endpoint_subject("
                "$1::uuid, $2::uuid, 'health_observation', NULL::text)",
                registry_id,
                request_id,
            )
            endpoint = json.loads(endpoint)
            assert endpoint["status"] == "ok"
            subject = await caller.fetchval(
                f"SELECT {_HEALTH_SCHEMA}.capture_health_observation_subject("
                "$1::uuid, $2::uuid, $3::text, NULL::text, 2::double precision)",
                registry_id,
                request_id,
                json.dumps(
                    {
                        "id": str(registry_id),
                        "endpoint_subject": {
                            "schema": "aap-inference-backend-endpoint/v1",
                            "registry_id": str(registry_id),
                            "request_id": str(request_id),
                            "purpose": "health_observation",
                            "authority": None,
                            "subject": endpoint["subject"],
                        },
                    }
                ),
            )
            open_sql = (
                f"SELECT {_HEALTH_SCHEMA}.open_health_observation_subject("
                "$1::text, $2::uuid, $3::uuid)"
            )
            args = (subject, registry_id, request_id)
            with pytest.raises(asyncpg.InsufficientPrivilegeError) as denied:
                async with caller.transaction():
                    await caller.fetchval(open_sql, *args)
            assert denied.value.sqlstate == "42501"
            transport = json.loads(await owner.fetchval(open_sql, *args))
            observed = json.loads(transport["backend_json"]).get("auth_token")
            if not isinstance(observed, str) or not secrets.compare_digest(
                observed, token
            ):
                raise AssertionError(
                    "Health owner did not recover the exact private token"
                )
    finally:
        await owner.close()


async def _publish_job(url):
    engine = create_async_engine(url, hide_parameters=True)
    jobs = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with jobs.begin() as db:
            job = await create_job(db, kind="export", payload={"acl_replay": True})
        return job.id
    finally:
        await engine.dispose()


@pytest.mark.parametrize("profile", ["lan-embedded", "lan-isolated-issuer", "worktree"])
async def test_ordinary_acl_removes_stale_grants_on_replay(
    test_engine, tmp_path, record_property, profile
):
    _verify_postgres(test_engine.url)
    suffix = uuid.uuid4().hex[:12]
    yoao, aap = f"yoao_acl_{suffix}", f"aap_acl_{suffix}"
    registry_id = uuid.uuid4()
    token = secrets.token_urlsafe(32)
    partition_schema = f"acl_partition_{suffix}"
    foreign = f"acl_foreign_{suffix}"
    peer = f"acl_peer_{suffix}"
    quote = test_engine.dialect.identifier_preparer.quote_identifier
    job_id = None

    async with _profile(test_engine, tmp_path, profile) as (url, install):
        assert (url.host, url.port, url.database) == (
            test_engine.url.host,
            test_engine.url.port,
            test_engine.url.database,
        )
        record_property("ordinary_login", url.username)
        await install()
        dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
        conn = await asyncpg.connect(dsn)
        try:
            assert tuple(await conn.fetchrow("SELECT current_user, session_user")) == (
                url.username,
                url.username,
            )
            assert not await conn.fetch(_EFFECTIVE_ACCESS)
            async with test_engine.begin() as db:
                await db.execute(
                    text(
                        "INSERT INTO public.ml_backend_registry "
                        "(id, name, url, state, auth_method, auth_token) "
                        "VALUES (:id, :name, :url, 'disconnected', 'token', :token)"
                    ),
                    {
                        "id": registry_id,
                        "name": f"M2b private credential {suffix}",
                        "url": f"http://{suffix}.example.test",
                        "token": token,
                    },
                )
                for schema in (yoao, aap, partition_schema):
                    await db.exec_driver_sql(f"CREATE SCHEMA {schema}")
                await db.exec_driver_sql(f"CREATE ROLE {peer} NOLOGIN")
                await db.exec_driver_sql(
                    f"CREATE FOREIGN DATA WRAPPER {foreign} NO HANDLER"
                )
                await db.exec_driver_sql(
                    f"CREATE SERVER {foreign} FOREIGN DATA WRAPPER {foreign}"
                )
                for statement in (
                    f"CREATE TABLE {yoao}.private_table (id integer)",
                    f"CREATE TABLE {aap}.private_partition (id integer) PARTITION BY RANGE (id)",
                    f"CREATE TABLE {partition_schema}.private_child PARTITION OF "
                    f"{aap}.private_partition FOR VALUES FROM (0) TO (10)",
                    f"CREATE MATERIALIZED VIEW {yoao}.private_mv AS SELECT 1 AS id",
                    f"CREATE FOREIGN TABLE {aap}.private_foreign (id integer) SERVER {foreign}",
                    f"CREATE SEQUENCE {aap}.private_sequence",
                    f"CREATE VIEW {aap}.private_view AS SELECT 1 AS id",
                    f"CREATE FUNCTION {aap}.private_fn() RETURNS integer LANGUAGE sql AS 'SELECT 1'",
                    f"GRANT SELECT ON {yoao}.private_table TO {peer}",
                ):
                    await db.exec_driver_sql(statement)
                for grantee in (quote(url.username), "PUBLIC"):
                    for statement in (
                        "GRANT SELECT, DELETE, TRUNCATE, TRIGGER ON public.async_jobs",
                        "GRANT UPDATE (status), REFERENCES (id) ON public.async_jobs",
                        "GRANT UPDATE (source_table) ON public.v_annotation_feedback_unified",
                        f"GRANT ALL ON SCHEMA {yoao}, {aap}",
                        f"GRANT USAGE ON SCHEMA {partition_schema}",
                        "GRANT CREATE ON SCHEMA public",
                        f"GRANT SELECT (id) ON {yoao}.private_table, {partition_schema}.private_child",
                        f"GRANT ALL ON TABLE {aap}.private_partition, {yoao}.private_mv, "
                        f"{aap}.private_foreign, {aap}.private_view",
                        f"GRANT ALL ON SEQUENCE {aap}.private_sequence",
                        f"GRANT EXECUTE ON FUNCTION {aap}.private_fn(), "
                        "yoao_assets_v1.capture_storage_connection(uuid,uuid,text)",
                        "GRANT EXECUTE ON FUNCTION " + ", ".join(_INFERENCE_FENCES),
                        "ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES",
                        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {aap} GRANT ALL ON TABLES",
                        "ALTER DEFAULT PRIVILEGES GRANT ALL ON SEQUENCES",
                        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {yoao} GRANT ALL ON SEQUENCES",
                        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {aap} GRANT EXECUTE ON FUNCTIONS",
                    ):
                        await db.exec_driver_sql(f"{statement} TO {grantee}")
                credential_oids = list(
                    await db.scalars(
                        text(
                            "SELECT CAST(name AS regprocedure)::oid FROM "
                            "unnest(CAST(:names AS text[])) AS t(name)"
                        ),
                        {"names": list(_INFERENCE_FENCES)},
                    )
                )

            before = {
                row["relation"]: row for row in await conn.fetch(_EFFECTIVE_ACCESS)
            }
            assert before["public.async_jobs"]["relation_rights"] == [
                "SELECT",
                "DELETE",
                "TRUNCATE",
                "TRIGGER",
            ]
            assert set(before["public.async_jobs"]["column_only_rights"]) == {
                "id:REFERENCES",
                "status:UPDATE",
            }
            assert {before[name]["kind"] for name in before if suffix in name} == {
                "r",
                "p",
                "m",
                "f",
                "S",
            }
            assert await conn.fetchval(f"SELECT {aap}.private_fn()") == 1
            assert await conn.fetchval(
                "SELECT bool_and(has_function_privilege(oid, 'EXECUTE')) "
                "FROM unnest($1::oid[]) AS t(oid)",
                credential_oids,
            )

            await install()
            await install()
            async with test_engine.begin() as db:
                for schema in ("public", aap, yoao):
                    await db.exec_driver_sql(
                        f"CREATE TABLE {schema}.future_{suffix} (id bigserial PRIMARY KEY)"
                    )
                await db.exec_driver_sql(
                    f"CREATE FUNCTION {aap}.future_fn() RETURNS integer LANGUAGE sql AS 'SELECT 2'"
                )
                assert await db.scalar(
                    text("SELECT has_table_privilege(:role, :relation, 'SELECT')"),
                    {"role": peer, "relation": f"{yoao}.private_table"},
                )
                private_view = await db.scalar(
                    text("SELECT CAST(:name AS regclass)::oid"),
                    {"name": f"{aap}.private_view"},
                )
                denied_routines = list(
                    await db.scalars(
                        text(
                            "SELECT CAST(name AS regprocedure)::oid FROM "
                            "unnest(CAST(:names AS text[])) AS t(name)"
                        ),
                        {
                            "names": [
                                f"{aap}.private_fn()",
                                f"{aap}.future_fn()",
                                "yoao_assets_v1.capture_storage_connection(uuid,uuid,text)",
                                "yoao_jobs_v1.claim_dispatches(jsonb)",
                                "yoao_insights_v1.refresh_user_perf_daily()",
                            ]
                        },
                    )
                )
            assert not await conn.fetch(_EFFECTIVE_ACCESS)
            assert await conn.fetchval(
                "SELECT has_table_privilege('public.v_annotation_feedback_unified', 'SELECT')"
            )
            assert not await conn.fetchval(
                "SELECT has_any_column_privilege('public.v_annotation_feedback_unified', 'UPDATE')"
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM public.v_annotation_feedback_unified"
                )
                >= 0
            )
            assert not await conn.fetchval(
                "SELECT has_schema_privilege('public', 'CREATE')"
            )
            assert (
                await conn.fetchval(
                    "SELECT has_table_privilege($1::oid, 'SELECT')", private_view
                )
                is False
            )
            for oid in (*denied_routines, *credential_oids):
                assert (
                    await conn.fetchval(
                        "SELECT has_function_privilege($1::oid, 'EXECUTE')", oid
                    )
                    is False
                )
            for signature, arguments in _INFERENCE_FENCES.items():
                function = signature.partition("(")[0]
                with pytest.raises(asyncpg.InsufficientPrivilegeError) as denied:
                    await conn.fetchval(f"SELECT {function}({arguments})")
                assert denied.value.sqlstate == "42501"
            await _observe_health_owner(test_engine, conn, registry_id, token)
            record_property("private_inference_rpc_sqlstate", "42501")
            record_property("inference_health_owner", _HEALTH_OWNER)
            assert await conn.fetchval(
                "SELECT has_function_privilege('yoao_identity_v1.authentication_policy()', 'EXECUTE')"
            ) == (profile != "lan-isolated-issuer")
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM yoao_identity_v1.read_ready_avatar_upload($1)",
                    suffix,
                )
                == 0
            )
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("TRUNCATE public.async_jobs")
            job_id = await _publish_job(url)
            assert (
                await conn.fetchval("SELECT id FROM yoao_jobs_v1.read_job($1)", job_id)
                == job_id
            )
            assert (
                await conn.fetchval(
                    "SELECT payload->>'acl_replay' FROM yoao_jobs_v1.async_job_creation WHERE id=$1",
                    job_id,
                )
                == "true"
            )
        finally:
            await conn.close()
            async with test_engine.begin() as db:
                await db.execute(
                    text("DELETE FROM public.ml_backend_registry WHERE id=:id"),
                    {"id": registry_id},
                )
                if job_id:
                    await db.execute(
                        text("DELETE FROM public.async_jobs WHERE id=:id"),
                        {"id": job_id},
                    )
                for grantee in (quote(url.username), "PUBLIC"):
                    for statement in (
                        "REVOKE SELECT,DELETE,TRUNCATE,TRIGGER,UPDATE(status),REFERENCES(id) ON public.async_jobs FROM",
                        "REVOKE UPDATE (source_table) ON public.v_annotation_feedback_unified FROM",
                        "REVOKE EXECUTE ON FUNCTION yoao_assets_v1.capture_storage_connection(uuid,uuid,text) FROM",
                        "REVOKE EXECUTE ON FUNCTION "
                        + ", ".join(_INFERENCE_FENCES)
                        + " FROM",
                        "REVOKE CREATE ON SCHEMA public FROM",
                        "ALTER DEFAULT PRIVILEGES REVOKE ALL ON TABLES FROM",
                        "ALTER DEFAULT PRIVILEGES REVOKE ALL ON SEQUENCES FROM",
                    ):
                        await db.exec_driver_sql(f"{statement} {grantee}")
                await db.exec_driver_sql(f"DROP TABLE IF EXISTS public.future_{suffix}")
                for schema in (partition_schema, aap, yoao):
                    await db.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                await db.exec_driver_sql(f"DROP SERVER IF EXISTS {foreign}")
                await db.exec_driver_sql(
                    f"DROP FOREIGN DATA WRAPPER IF EXISTS {foreign}"
                )
                await db.exec_driver_sql(f"DROP ROLE IF EXISTS {peer}")


async def test_ordinary_reset_requires_the_database_owner_but_not_superuser(
    test_engine,
):
    _verify_postgres(test_engine.url)
    database = f"acl_nonsuper_{uuid.uuid4().hex[:12]}_test"
    admin_url = test_engine.url.set(drivername="postgresql")
    reset = (GRANTS / "ordinary-runtime-revoke.sql").read_text()
    async with (
        _login(test_engine, "acl_migration") as (migration, migration_password),
        _login(test_engine, "acl_runtime") as (runtime, runtime_password),
    ):
        admin = await asyncpg.connect(admin_url.render_as_string(hide_password=False))
        try:
            await admin.execute(
                f"CREATE DATABASE {database} OWNER {migration} TEMPLATE template0"
            )
            migration_url = admin_url.set(
                database=database, username=migration, password=migration_password
            )
            runtime_url = admin_url.set(
                database=database, username=runtime, password=runtime_password
            )
            try:
                owner = await asyncpg.connect(
                    migration_url.render_as_string(hide_password=False)
                )
                try:
                    assert not await owner.fetchval(
                        "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
                    )
                    await owner.execute(
                        f"""
                        CREATE SCHEMA aap_acl_test;
                        CREATE TABLE public.old_ledger (id bigint);
                        CREATE SEQUENCE aap_acl_test.old_seq;
                        GRANT CREATE ON SCHEMA public TO {runtime};
                        GRANT DELETE,TRUNCATE ON TABLE public.old_ledger TO {runtime};
                        GRANT SELECT (id) ON TABLE public.old_ledger TO PUBLIC;
                        GRANT ALL ON SEQUENCE aap_acl_test.old_seq TO PUBLIC, {runtime};
                        ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO PUBLIC;
                        ALTER DEFAULT PRIVILEGES IN SCHEMA aap_acl_test
                            GRANT ALL ON SEQUENCES TO {runtime};
                        """
                    )
                    for expected_db, expected_actor, denied in (
                        (test_engine.url.database, migration, True),
                        (database, runtime, True),
                        (database, migration, False),
                    ):
                        async with owner.transaction():
                            await owner.execute(
                                "SET LOCAL search_path = pg_catalog, pg_temp"
                            )
                            await owner.execute(
                                """SELECT set_config('yoao.application_role', $1, true),
                                          set_config('yoao.migration_role', $2, true),
                                          set_config('yoao.expected_database', $3, true)""",
                                runtime,
                                expected_actor,
                                expected_db,
                            )
                            if denied:
                                with pytest.raises(
                                    asyncpg.RaiseError,
                                    match="Unexpected database or migration owner",
                                ):
                                    async with owner.transaction():
                                        await owner.execute(reset)
                                assert await owner.fetchval(
                                    "SELECT has_table_privilege($1, 'public.old_ledger', 'TRUNCATE')",
                                    runtime,
                                )
                            else:
                                await owner.execute(reset)
                    await owner.execute(
                        "CREATE TABLE aap_acl_test.fresh_ledger (id bigserial PRIMARY KEY)"
                    )
                finally:
                    await owner.close()
                caller = await asyncpg.connect(
                    runtime_url.render_as_string(hide_password=False)
                )
                try:
                    assert await caller.fetchval("SELECT session_user") == runtime
                    assert not await caller.fetch(_EFFECTIVE_ACCESS)
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        await caller.execute("CREATE TABLE public.forbidden (id int)")
                finally:
                    await caller.close()
            finally:
                await admin.execute(f"DROP DATABASE {database}")
        finally:
            await admin.close()
