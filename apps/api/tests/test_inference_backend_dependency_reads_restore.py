"""Restore nonempty Learning and Jobs dependency reads from an owned dump."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import uuid
import warnings

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.async_job import AsyncJob
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.trained_model import ModelDeployment
from tests.factory import create_user
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_recovery import _alembic, _postgres_tool
from tests.test_inference_backend_dependency_reads_native import (
    _EVALUATION_FUNCTION,
    _JOBS_FUNCTION,
    _connect_inference,
    _job,
    _sample_evaluation_run,
    _owner_dsn,
)
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.sample_evaluation_fixtures import Objects, seed as seed_evaluation_source

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_FUNCTIONS = (_EVALUATION_FUNCTION, _JOBS_FUNCTION)


async def _database_head(engine) -> str:
    async with engine.connect() as connection:
        return await connection.scalar(
            text("SELECT version_num FROM public.alembic_version")
        )


async def _function_metadata(engine) -> list[dict]:
    connection = await asyncpg.connect(_owner_dsn(engine), timeout=5)
    try:
        rows = await connection.fetch(
            """SELECT namespace.nspname || '.' || function.proname || '(' ||
                         pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature,
                      pg_catalog.pg_get_userbyid(function.proowner) AS owner,
                      ARRAY(SELECT entry::text FROM unnest(function.proacl) AS entry
                            ORDER BY entry::text) AS acl,
                      function.prosecdef AS security_definer,
                      function.proconfig AS configuration,
                      function.provolatile::text AS volatility,
                      function.proparallel::text AS parallel_mode,
                      pg_catalog.pg_get_function_result(function.oid) AS result,
                      pg_catalog.pg_get_functiondef(function.oid) AS definition,
                      pg_catalog.has_function_privilege(
                          'inference_code', function.oid, 'EXECUTE'
                      ) AS inference_can_execute,
                      pg_catalog.has_function_privilege(
                          'legacy_runtime', function.oid, 'EXECUTE'
                      ) AS legacy_api_can_execute,
                      EXISTS (
                          SELECT 1
                          FROM pg_catalog.aclexplode(COALESCE(
                              function.proacl,
                              pg_catalog.acldefault('f', function.proowner)
                          )) AS privilege
                          WHERE privilege.grantee = 0
                            AND privilege.privilege_type = 'EXECUTE'
                      ) AS public_can_execute
               FROM pg_catalog.pg_proc AS function
               JOIN pg_catalog.pg_namespace AS namespace
                 ON namespace.oid = function.pronamespace
               WHERE function.oid = ANY(ARRAY[
                   pg_catalog.to_regprocedure($1)::oid,
                   pg_catalog.to_regprocedure($2)::oid
               ])
               ORDER BY signature""",
            *_FUNCTIONS,
        )
        return [dict(row) for row in rows]
    finally:
        await connection.close()


async def _assert_function_metadata(metadata: list[dict]) -> None:
    expected = {
        _EVALUATION_FUNCTION: ("learning_code", "boolean"),
        _JOBS_FUNCTION: ("jobs_code", "bigint"),
    }
    assert [row["signature"] for row in metadata] == sorted(expected)
    for row in metadata:
        owner, result = expected[row["signature"]]
        assert row["owner"] == owner
        assert row["security_definer"] is True
        assert row["volatility"] == "s"
        assert row["parallel_mode"] == "u"
        assert row["result"] == result
        assert {
            "search_path=pg_catalog, pg_temp",
            "TimeZone=UTC",
            "DateStyle=ISO, YMD",
            "extra_float_digits=3",
        }.issubset(set(row["configuration"]))
        assert row["inference_can_execute"] is True
        assert row["legacy_api_can_execute"] is True
        assert row["public_can_execute"] is False


async def _row_state(engine, world) -> dict:
    connection = await asyncpg.connect(_owner_dsn(engine), timeout=5)
    try:
        evaluations = await connection.fetch(
            """SELECT id, project_id, dataset_id, model_version_id, deployment_id,
                      request_id, request_digest, name, status, spec, spec_digest,
                      "authorization", generation, created_at, completed_at
               FROM public.sample_evaluation_runs
               WHERE id = ANY($1::uuid[])
               ORDER BY id""",
            world["run_ids"],
        )
        deployments = await connection.fetch(
            """SELECT id, model_version_id, registry_id, status, receipt, trial, created_at
               FROM public.trained_model_deployments
               WHERE id = ANY($1::uuid[])
               ORDER BY id""",
            world["deployment_ids"],
        )
        jobs = await connection.fetch(
            """SELECT id, kind, project_id, user_id, status, payload, result,
                      error_message, created_at
               FROM public.async_jobs
               WHERE id = ANY($1::uuid[])
               ORDER BY id""",
            world["job_ids"],
        )
        return {
            "evaluations": [dict(row) for row in evaluations],
            "deployments": [dict(row) for row in deployments],
            "jobs": [dict(row) for row in jobs],
        }
    finally:
        await connection.close()


async def _answers(engine, world) -> dict:
    connection = await _connect_inference(engine)
    try:
        async with connection.transaction():
            return {
                "evaluation_target": await connection.fetchval(
                    "SELECT yoao_learning_v1.registry_evaluation_reference_exists($1::uuid)",
                    world["target_registry_id"],
                ),
                "evaluation_other": await connection.fetchval(
                    "SELECT yoao_learning_v1.registry_evaluation_reference_exists($1::uuid)",
                    world["other_registry_id"],
                ),
                "evaluation_missing": await connection.fetchval(
                    "SELECT yoao_learning_v1.registry_evaluation_reference_exists($1::uuid)",
                    world["missing_registry_id"],
                ),
                "running_target": await connection.fetchval(
                    "SELECT yoao_jobs_v1.count_running_registry_predictions($1::uuid)",
                    world["target_registry_id"],
                ),
                "running_other": await connection.fetchval(
                    "SELECT yoao_jobs_v1.count_running_registry_predictions($1::uuid)",
                    world["other_registry_id"],
                ),
                "running_missing": await connection.fetchval(
                    "SELECT yoao_jobs_v1.count_running_registry_predictions($1::uuid)",
                    world["missing_registry_id"],
                ),
            }
    finally:
        await connection.close()


async def _assert_inference_has_no_raw_reads(engine) -> None:
    connection = await _connect_inference(engine)
    try:
        async with connection.transaction():
            for statement in (
                "SELECT id FROM public.sample_evaluation_runs LIMIT 1",
                "SELECT id FROM public.trained_model_deployments LIMIT 1",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with connection.transaction():
                        await connection.fetch(statement)
            assert await connection.fetchval(
                "SELECT pg_catalog.has_table_privilege("
                "current_user, 'public.async_jobs', 'SELECT')"
            )
    finally:
        await connection.close()


async def _assert_ordinary_dependency_reads(engine, world) -> None:
    url = make_url(os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"])
    assert (url.host, url.port, url.username) == (
        engine.url.host,
        engine.url.port,
        "legacy_runtime",
    )
    connection = await asyncpg.connect(
        url.set(drivername="postgresql", database=engine.url.database).render_as_string(
            hide_password=False
        ),
        timeout=5,
    )
    try:
        observed = await connection.fetchrow(
            "SELECT session_user, current_user, current_database()"
        )
        assert tuple(observed) == (url.username, url.username, engine.url.database)
        async with connection.transaction():
            for relation in (
                "public.async_jobs",
                "public.sample_evaluation_runs",
                "public.trained_model_deployments",
            ):
                assert not await connection.fetchval(
                    "SELECT pg_catalog.has_table_privilege($1::text,"
                    "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
                    "pg_catalog.has_any_column_privilege($1::text,'SELECT,INSERT,UPDATE,REFERENCES')",
                    relation,
                )
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with connection.transaction():
                        await connection.fetch(f"SELECT id FROM {relation} LIMIT 0")
            assert await connection.fetchval(
                "SELECT yoao_learning_v1.registry_evaluation_reference_exists($1::uuid)",
                world["target_registry_id"],
            )
            assert (
                await connection.fetchval(
                    "SELECT yoao_jobs_v1.count_running_registry_predictions($1::uuid)",
                    world["target_registry_id"],
                )
                == 2
            )
    finally:
        await connection.close()


async def _assert_clone_identity(engine, database: str, ownership: str) -> None:
    _verify_postgres(engine.url)
    async with engine.connect() as connection:
        current_user, actual_database, can_create = (
            await connection.execute(
                text(
                    "SELECT current_user, current_database(), "
                    "pg_catalog.has_schema_privilege(current_user, "
                    "'yoao_learning_v1', 'CREATE')"
                )
            )
        ).one()
    assert actual_database == database and actual_database.endswith("_test")
    assert current_user not in {"legacy_runtime", "inference_code"}
    assert can_create
    async with engine.connect() as connection:
        marker = await connection.scalar(
            text(
                "SELECT pg_catalog.shobj_description(oid,'pg_database') "
                "FROM pg_catalog.pg_database WHERE datname=:name"
            ),
            {"name": database},
        )
    assert marker == ownership


async def _database_acl(test_engine) -> list[tuple[str, str, bool]]:
    async with test_engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT COALESCE(role.rolname,'PUBLIC'), "
                    "entry.privilege_type, entry.is_grantable "
                    "FROM pg_catalog.pg_database AS database "
                    "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE("
                    "database.datacl, pg_catalog.acldefault('d',database.datdba)"
                    ")) AS entry "
                    "LEFT JOIN pg_catalog.pg_roles AS role "
                    "ON role.oid=entry.grantee "
                    "WHERE database.datname=current_database()"
                )
            )
        ).all()
    result = [(row[0], row[1], row[2]) for row in rows]
    assert all(
        privilege in {"CREATE", "CONNECT", "TEMPORARY"} for _, privilege, _ in result
    )
    return result


async def _assert_copied_authority_quarantined(engine) -> None:
    """Copied routing state stays bound to its source database, without rebinding."""
    async with engine.connect() as connection:
        assert await connection.scalar(
            text("SELECT yoao_inference_routing_v1._routing_secret() IS NULL")
        )


def _replay_installers(test_engine, database: str) -> None:
    from tests.test_labeling_video_writer_restore import (
        ROOT,
        _grant_variables,
        _run_psql_file,
    )

    variables = _grant_variables(database)
    _run_psql_file(
        test_engine, database, ROOT / "infra/docker/module-data-grants.sql", variables
    )
    _run_psql_file(
        test_engine,
        database,
        ROOT / "infra/docker/lan-production-grants.sql",
        {
            "expected_database": database,
            "migration_role": variables["migration_role"],
            "application_role": "legacy_runtime",
            "asset_cleanup_role": variables["asset_cleanup_role"],
        },
    )


async def _initialize_fresh_source(test_engine, admin, engine) -> None:
    """Install a new source through the forward chain, never from copied authority."""
    from tests.test_labeling_video_writer_restore import _revoke_public_temp

    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT pg_catalog.to_regclass('public.alembic_version')")
            )
            is None
        )
    database = engine.url.database
    await _revoke_public_temp(admin, test_engine, database)
    await engine.dispose()
    migration = _alembic(engine.url, "upgrade", "head")
    assert migration.returncode == 0, "fresh restore source migration failed"
    _replay_installers(test_engine, database)
    assert await _database_head(engine) == await _database_head(test_engine)
    async with engine.connect() as connection:
        assert await connection.scalar(
            text("SELECT yoao_inference_routing_v1._routing_secret() IS NOT NULL")
        )


async def _create_owned_clone(
    admin,
    test_engine,
    database: str,
    ownership: str,
    database_acl: list[tuple[str, str, bool]],
    created: set[str],
) -> None:
    quoted = test_engine.dialect.identifier_preparer.quote_identifier(database)
    _verify_postgres(test_engine.url)
    async with admin.connect() as connection:
        await connection.execute(text(f"CREATE DATABASE {quoted} TEMPLATE template0"))
        created.add(database)
        await connection.execute(text(f"COMMENT ON DATABASE {quoted} IS '{ownership}'"))
        await connection.execute(text(f"REVOKE ALL ON DATABASE {quoted} FROM PUBLIC"))
        quote = connection.dialect.identifier_preparer.quote
        for grantee, privilege, grantable in database_acl:
            role = "PUBLIC" if grantee == "PUBLIC" else quote(grantee)
            grant_option = " WITH GRANT OPTION" if grantable else ""
            await connection.execute(
                text(f"GRANT {privilege} ON DATABASE {quoted} TO {role}{grant_option}")
            )


async def _drop_owned_clone(admin, test_engine, database: str, ownership: str) -> None:
    quoted = test_engine.dialect.identifier_preparer.quote_identifier(database)
    _verify_postgres(test_engine.url)
    async with admin.connect() as connection:
        marker = await connection.scalar(
            text(
                "SELECT pg_catalog.shobj_description(oid,'pg_database') "
                "FROM pg_catalog.pg_database WHERE datname=:name"
            ),
            {"name": database},
        )
        assert marker == ownership
        active_sessions = text(
            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity "
            "WHERE datname=:name AND pid <> pg_catalog.pg_backend_pid() "
            "AND backend_type IS DISTINCT FROM 'autovacuum worker')"
        )
        async with asyncio.timeout(10):
            while await connection.scalar(active_sessions, {"name": database}):
                await asyncio.sleep(0.2)
        await connection.execute(text(f"DROP DATABASE {quoted}"))


async def _seed_database(engine) -> dict:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker.begin() as db:
        actor = await create_user(
            db,
            "super_admin",
            f"inference-dependency-restore-{uuid.uuid4().hex}@example.test",
            "Inference Dependency Restore Owner",
        )
        world = await seed_evaluation_source(db, actor, Objects(), count=1)
        world.deployment.status = "failed"

        other_backend = MLBackendRegistry(
            name=f"Restored unrelated backend {uuid.uuid4().hex[:10]}",
            url=f"http://127.0.0.1/{uuid.uuid4().hex}",
            source="manual",
        )
        db.add(other_backend)
        await db.flush()
        other_deployment = ModelDeployment(
            model_version_id=world.model.id,
            registry_id=other_backend.id,
            status="ready",
        )
        db.add(other_deployment)
        await db.flush()

        target_run = _sample_evaluation_run(
            actor=actor,
            world=world,
            deployment=world.deployment,
            status="cancelled",
        )
        other_run = _sample_evaluation_run(
            actor=actor,
            world=world,
            deployment=other_deployment,
            status="running",
        )
        db.add_all((target_run, other_run))

        target_jobs = [
            _job(
                actor=actor,
                world=world,
                registry_id=world.backend.id,
                kind="batch_predict",
                status="running",
                suffix=f"target-running-{index}",
            )
            for index in range(2)
        ]
        other_job = _job(
            actor=actor,
            world=world,
            registry_id=other_backend.id,
            kind="batch_predict",
            status="running",
            suffix="other-running",
        )
        nonmatching_jobs = [
            _job(
                actor=actor,
                world=world,
                registry_id=world.backend.id,
                kind="batch_predict",
                status=status,
                suffix=f"target-{status}",
            )
            for status in ("pending", "completed", "failed", "cancelled")
        ]
        wrong_kind_job = _job(
            actor=actor,
            world=world,
            registry_id=world.backend.id,
            kind="prediction_retry",
            status="running",
            suffix="wrong-kind",
        )
        missing_payload_job = AsyncJob(
            kind="batch_predict",
            project_id=world.project.id,
            user_id=actor.id,
            status="running",
            payload={},
            result={},
            error_message="missing registry payload",
        )
        db.add_all(
            [
                *target_jobs,
                other_job,
                *nonmatching_jobs,
                wrong_kind_job,
                missing_payload_job,
            ]
        )
        await db.flush()

        return {
            "target_registry_id": world.backend.id,
            "other_registry_id": other_backend.id,
            "missing_registry_id": uuid.uuid4(),
            "target_deployment_id": world.deployment.id,
            "other_deployment_id": other_deployment.id,
            "target_run_id": target_run.id,
            "other_run_id": other_run.id,
            "run_ids": [target_run.id, other_run.id],
            "deployment_ids": [world.deployment.id, other_deployment.id],
            "target_job_ids": [job.id for job in target_jobs],
            "other_job_id": other_job.id,
            "job_ids": [
                job.id
                for job in [
                    *target_jobs,
                    other_job,
                    *nonmatching_jobs,
                    wrong_kind_job,
                    missing_payload_job,
                ]
            ],
        }


async def test_dependency_functions_survive_nonempty_owned_dump_restore(
    test_engine, tmp_path, record_property
):
    await assert_owned_test_database(test_engine)
    assert test_engine.url.database and test_engine.url.database.endswith("_test")
    identity = uuid.uuid4().hex[:10]
    source_database = f"aap_inference_backend_dependency_source_{identity}_test"
    restored_database = f"aap_inference_backend_dependency_restore_{identity}_test"
    assert len(source_database.encode("utf-8")) <= 63
    assert len(restored_database.encode("utf-8")) <= 63
    source_owner = f"inference-backend-dependency-source:{uuid.uuid4()}"
    restored_owner = f"inference-backend-dependency-restore:{uuid.uuid4()}"
    source_dump = tmp_path / "inference-backend-dependency-source.dump"
    database_acl = await _database_acl(test_engine)
    database_created: set[str] = set()
    source_engine = None
    restored_engine = None
    admin = None

    try:
        admin = create_async_engine(
            test_engine.url.set(database="postgres"),
            isolation_level="AUTOCOMMIT",
            hide_parameters=True,
        )

        await _create_owned_clone(
            admin,
            test_engine,
            source_database,
            source_owner,
            database_acl,
            database_created,
        )
        source_engine = create_async_engine(
            test_engine.url.set(database=source_database), hide_parameters=True
        )
        await _initialize_fresh_source(test_engine, admin, source_engine)
        await _assert_clone_identity(source_engine, source_database, source_owner)

        world = await _seed_database(source_engine)
        source_function_metadata = await _function_metadata(source_engine)
        source_rows = await _row_state(source_engine, world)
        assert len(source_rows["evaluations"]) == 2
        assert len(source_rows["deployments"]) == 2
        assert len(source_rows["jobs"]) == 9
        source_answers = await _answers(source_engine, world)
        assert source_answers == {
            "evaluation_target": True,
            "evaluation_other": True,
            "evaluation_missing": False,
            "running_target": 2,
            "running_other": 1,
            "running_missing": 0,
        }
        await _assert_inference_has_no_raw_reads(source_engine)
        await _assert_ordinary_dependency_reads(source_engine, world)

        source_dump.touch(mode=0o600)
        source_dump.write_bytes(
            _postgres_tool(
                test_engine,
                ["pg_dump", "-U", "owner", "-Fc", source_database],
            )
        )
        assert source_dump.read_bytes().startswith(b"PGDMP")
        assert stat.S_IMODE(source_dump.stat().st_mode) == 0o600

        await _create_owned_clone(
            admin,
            test_engine,
            restored_database,
            restored_owner,
            [],
            database_created,
        )
        _verify_postgres(test_engine.url)
        _postgres_tool(
            test_engine,
            [
                "pg_restore",
                "-U",
                "owner",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--dbname",
                restored_database,
            ],
            body=source_dump.read_bytes(),
        )
        restored_engine = create_async_engine(
            test_engine.url.set(database=restored_database), hide_parameters=True
        )
        await _assert_clone_identity(restored_engine, restored_database, restored_owner)

        await _assert_copied_authority_quarantined(restored_engine)
        assert {row["owner"] for row in await _function_metadata(restored_engine)} == {
            "owner"
        }
        async with restored_engine.connect() as connection:
            assert not await connection.scalar(
                text(
                    "SELECT pg_catalog.has_database_privilege("
                    "'legacy_runtime', current_database(), 'CONNECT')"
                )
            )
        _replay_installers(test_engine, restored_database)
        await _assert_copied_authority_quarantined(restored_engine)
        assert await _function_metadata(restored_engine) == source_function_metadata
        assert await _row_state(restored_engine, world) == source_rows
        assert await _answers(restored_engine, world) == source_answers
        await _assert_inference_has_no_raw_reads(restored_engine)
        await _assert_ordinary_dependency_reads(restored_engine, world)
        record_property("restore_mode", "no-owner,no-privileges; replay owner and LAN")
        record_property("restored_consumer_login", "legacy_runtime")

        mutation = await asyncpg.connect(_owner_dsn(restored_engine), timeout=5)
        try:
            transaction = mutation.transaction()
            await transaction.start()
            try:
                await mutation.execute(
                    "UPDATE public.sample_evaluation_runs SET status='failed' "
                    "WHERE id=$1::uuid",
                    world["target_run_id"],
                )
                await mutation.execute(
                    "UPDATE public.trained_model_deployments SET status='checking' "
                    "WHERE id=$1::uuid",
                    world["target_deployment_id"],
                )
                await mutation.execute(
                    "UPDATE public.async_jobs SET status='cancelled' WHERE id=$1::uuid",
                    world["target_job_ids"][0],
                )
                await mutation.execute(
                    "UPDATE public.async_jobs "
                    "SET payload=pg_catalog.jsonb_set(payload, '{ml_backend_id}', "
                    "pg_catalog.to_jsonb($2::text)) WHERE id=$1::uuid",
                    world["other_job_id"],
                    str(world["target_registry_id"]),
                )
                await mutation.execute("SET LOCAL ROLE inference_code")
                assert (
                    await mutation.fetchval(
                        "SELECT yoao_learning_v1.registry_evaluation_reference_exists($1::uuid)",
                        world["target_registry_id"],
                    )
                    is True
                )
                assert (
                    await mutation.fetchval(
                        "SELECT yoao_jobs_v1.count_running_registry_predictions($1::uuid)",
                        world["target_registry_id"],
                    )
                    == 2
                )
                await mutation.execute("RESET ROLE")
                await mutation.execute(
                    "DELETE FROM public.sample_evaluation_runs WHERE id=$1::uuid",
                    world["target_run_id"],
                )
                await mutation.execute("SET LOCAL ROLE inference_code")
                assert (
                    await mutation.fetchval(
                        "SELECT yoao_learning_v1.registry_evaluation_reference_exists($1::uuid)",
                        world["target_registry_id"],
                    )
                    is False
                )
                assert (
                    await mutation.fetchval(
                        "SELECT yoao_learning_v1.registry_evaluation_reference_exists($1::uuid)",
                        world["other_registry_id"],
                    )
                    is True
                )
                assert (
                    await mutation.fetchval(
                        "SELECT yoao_jobs_v1.count_running_registry_predictions($1::uuid)",
                        world["target_registry_id"],
                    )
                    == 2
                )
            finally:
                await transaction.rollback()
        finally:
            await mutation.close()

        assert await _row_state(restored_engine, world) == source_rows
        assert await _answers(restored_engine, world) == source_answers
        assert await _row_state(source_engine, world) == source_rows
        assert await _answers(source_engine, world) == source_answers
    finally:
        primary_error = sys.exc_info()[0] is not None
        cleanup_errors = []
        for engine in (restored_engine, source_engine):
            if engine is not None:
                try:
                    await engine.dispose()
                except Exception as exc:  # pragma: no cover - acceptance cleanup path
                    cleanup_errors.append(exc)
        try:
            if admin is not None:
                for database, ownership in (
                    (restored_database, restored_owner),
                    (source_database, source_owner),
                ):
                    if database in database_created:
                        try:
                            await _drop_owned_clone(
                                admin, test_engine, database, ownership
                            )
                        except (
                            Exception
                        ) as exc:  # pragma: no cover - acceptance cleanup path
                            cleanup_errors.append(exc)
        finally:
            if admin is not None:
                try:
                    await admin.dispose()
                except Exception as exc:  # pragma: no cover - acceptance cleanup path
                    cleanup_errors.append(exc)
            try:
                source_dump.unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - acceptance cleanup path
                cleanup_errors.append(exc)
        if cleanup_errors:
            if primary_error:
                warnings.warn(
                    f"owned backend dependency restore cleanup had {len(cleanup_errors)} error(s)",
                    RuntimeWarning,
                    stacklevel=2,
                )
            else:
                raise cleanup_errors[0]
