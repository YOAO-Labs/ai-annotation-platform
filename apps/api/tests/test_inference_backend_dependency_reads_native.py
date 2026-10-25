"""Owned PostgreSQL acceptance for fixed ML backend deletion dependency reads."""

from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.async_job import AsyncJob
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.sample_evaluation import SampleEvaluationRun
from app.db.models.trained_model import ModelDeployment
from tests.factory import create_user
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.sample_evaluation_fixtures import Objects, seed as seed_evaluation_source

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_EVALUATION_FUNCTION = "aap_learning_v1.registry_evaluation_reference_exists(uuid)"
_JOBS_FUNCTION = "aap_jobs_v1.count_running_registry_predictions(uuid)"
_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "sql"
    / "0273_inference_backend_dependency_reads.schema.json"
)


def _owner_dsn(test_engine) -> str:
    value = test_engine.url.set(drivername="postgresql")
    assert value.database and value.database.endswith(("_test", "_e2e"))
    return value.render_as_string(hide_password=False)


async def _connect_inference(test_engine):
    connection = await asyncpg.connect(
        _owner_dsn(test_engine), timeout=5, command_timeout=10
    )
    await connection.execute("SET ROLE inference_code")
    assert await connection.fetchval("SELECT current_user") == "inference_code"
    return connection


def _sample_evaluation_run(*, actor, world, deployment, status: str):
    return SampleEvaluationRun(
        project_id=world.project.id,
        creator_id=actor.id,
        dataset_id=world.dataset.id,
        model_version_id=world.model.id,
        deployment_id=deployment.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        name=f"Deletion dependency {uuid.uuid4().hex[:8]}",
        status=status,
        spec={},
        spec_digest="b" * 64,
        authorization={},
    )


def _job(*, actor, world, registry_id: uuid.UUID, kind: str, status: str, suffix: str):
    return AsyncJob(
        kind=kind,
        project_id=world.project.id,
        user_id=actor.id,
        status=status,
        payload={"ml_backend_id": str(registry_id)},
        result={},
        error_message=suffix,
    )


async def _evaluation_reference(db, registry_id: uuid.UUID) -> bool:
    value = await db.scalar(
        text(
            "SELECT aap_learning_v1.registry_evaluation_reference_exists("
            "CAST(:registry_id AS uuid))"
        ),
        {"registry_id": registry_id},
    )
    assert isinstance(value, bool)
    return value


async def _running_prediction_count(db, registry_id: uuid.UUID) -> int:
    return int(
        await db.scalar(
            text(
                "SELECT aap_jobs_v1.count_running_registry_predictions("
                "CAST(:registry_id AS uuid))"
            ),
            {"registry_id": registry_id},
        )
    )


async def test_backend_dependency_reads_match_old_queries_and_rollback_with_caller(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    target_registry_id = uuid.uuid4()
    other_registry_id = uuid.uuid4()
    missing_registry_id = uuid.uuid4()

    async with maker() as db:
        transaction = await db.begin()
        try:
            actor = await create_user(
                db,
                "super_admin",
                f"inference-backend-dependency-{uuid.uuid4().hex}@example.test",
                "Inference Backend Dependency Owner",
            )
            world = await seed_evaluation_source(db, actor, Objects(), count=1)
            target_registry_id = world.backend.id
            world.deployment.status = "failed"

            other_backend = MLBackendRegistry(
                name=f"Unrelated evaluation backend {uuid.uuid4().hex[:10]}",
                url=f"http://127.0.0.1/{uuid.uuid4().hex}",
                source="manual",
            )
            db.add(other_backend)
            await db.flush()
            other_registry_id = other_backend.id
            missing_registry_id = uuid.uuid4()

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

            jobs = [
                _job(
                    actor=actor,
                    world=world,
                    registry_id=target_registry_id,
                    kind="batch_predict",
                    status="running",
                    suffix=f"target-running-{index}",
                )
                for index in range(2)
            ]
            jobs.extend(
                _job(
                    actor=actor,
                    world=world,
                    registry_id=target_registry_id,
                    kind="batch_predict",
                    status=status,
                    suffix=f"target-{status}",
                )
                for status in ("pending", "completed", "failed", "cancelled")
            )
            jobs.extend(
                (
                    _job(
                        actor=actor,
                        world=world,
                        registry_id=other_registry_id,
                        kind="batch_predict",
                        status="running",
                        suffix="other-running",
                    ),
                    _job(
                        actor=actor,
                        world=world,
                        registry_id=target_registry_id,
                        kind="prediction_retry",
                        status="running",
                        suffix="wrong-kind",
                    ),
                    AsyncJob(
                        kind="batch_predict",
                        project_id=world.project.id,
                        user_id=actor.id,
                        status="running",
                        payload={},
                        result={},
                        error_message="missing registry payload",
                    ),
                )
            )
            db.add_all(jobs)
            await db.flush()

            assert await _evaluation_reference(db, target_registry_id) is True
            assert await _evaluation_reference(db, other_registry_id) is True
            assert await _evaluation_reference(db, missing_registry_id) is False
            assert await _running_prediction_count(db, target_registry_id) == 2
            assert await _running_prediction_count(db, other_registry_id) == 1
            assert await _running_prediction_count(db, missing_registry_id) == 0
        finally:
            await transaction.rollback()

    async with maker() as observer:
        assert await _evaluation_reference(observer, target_registry_id) is False
        assert await _evaluation_reference(observer, other_registry_id) is False
        assert await _running_prediction_count(observer, target_registry_id) == 0
        assert await _running_prediction_count(observer, other_registry_id) == 0


async def test_inference_role_gets_owner_reads_without_raw_table_access(test_engine):
    await assert_owned_test_database(test_engine)
    connection = await asyncpg.connect(_owner_dsn(test_engine), timeout=5)
    try:
        signatures = (
            (_EVALUATION_FUNCTION, "learning_code", "boolean"),
            (_JOBS_FUNCTION, "jobs_code", "bigint"),
        )
        for signature, expected_owner, expected_result in signatures:
            metadata = await connection.fetchrow(
                """SELECT pg_catalog.pg_get_userbyid(function.proowner) AS owner,
                          pg_catalog.pg_get_function_result(function.oid) AS result,
                          function.prosecdef AS security_definer,
                          function.provolatile::text AS volatility,
                          function.proparallel::text AS parallel_mode,
                          function.proconfig AS settings,
                          pg_catalog.has_function_privilege(
                              'inference_code', function.oid, 'EXECUTE'
                          ) AS inference_execute,
                          pg_catalog.has_function_privilege(
                              'legacy_runtime', function.oid, 'EXECUTE'
                          ) AS legacy_api_execute,
                          EXISTS (
                              SELECT 1
                              FROM pg_catalog.aclexplode(COALESCE(
                                  function.proacl,
                                  pg_catalog.acldefault('f', function.proowner)
                              )) AS privilege
                              WHERE privilege.grantee = 0
                                AND privilege.privilege_type = 'EXECUTE'
                          ) AS public_execute
                   FROM pg_catalog.pg_proc AS function
                   WHERE function.oid = pg_catalog.to_regprocedure($1)""",
                signature,
            )
            assert metadata is not None
            assert metadata["owner"] == expected_owner
            assert metadata["result"] == expected_result
            assert metadata["security_definer"] is True
            assert metadata["volatility"] == "s"
            assert metadata["parallel_mode"] == "u"
            assert metadata["settings"] is not None
            assert {
                "search_path=pg_catalog, pg_temp",
                "TimeZone=UTC",
                "DateStyle=ISO, YMD",
                "extra_float_digits=3",
            }.issubset(set(metadata["settings"]))
            assert metadata["inference_execute"] is True
            assert metadata["legacy_api_execute"] is True
            assert metadata["public_execute"] is False

        for relation in (
            "public.sample_evaluation_runs",
            "public.trained_model_deployments",
            "public.async_jobs",
        ):
            assert (
                await connection.fetchval(
                    "SELECT pg_catalog.has_table_privilege("
                    "'inference_code', $1::text, 'SELECT')",
                    relation,
                )
                is False
            )

        inference = await _connect_inference(test_engine)
        try:
            async with inference.transaction():
                for statement in (
                    "SELECT id FROM public.sample_evaluation_runs LIMIT 1",
                    "SELECT id FROM public.trained_model_deployments LIMIT 1",
                    "SELECT id FROM public.async_jobs LIMIT 1",
                ):
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        async with inference.transaction():
                            await inference.fetch(statement)

                for signature in (_EVALUATION_FUNCTION, _JOBS_FUNCTION):
                    with pytest.raises(asyncpg.PostgresError) as caught:
                        async with inference.transaction():
                            await inference.fetchval(
                                f"SELECT {signature.rsplit('(', maxsplit=1)[0]}(NULL::uuid)"
                            )
                    assert caught.value.sqlstate == "22023"

                assert (
                    await inference.fetchval(
                        "SELECT aap_learning_v1.registry_evaluation_reference_exists("
                        "$1::uuid)",
                        uuid.uuid4(),
                    )
                    is False
                )
                assert (
                    await inference.fetchval(
                        "SELECT aap_jobs_v1.count_running_registry_predictions($1::uuid)",
                        uuid.uuid4(),
                    )
                    == 0
                )
        finally:
            await inference.close()
    finally:
        await connection.close()


async def test_backend_dependency_catalog_schema_is_closed_and_typed() -> None:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    signatures = [_EVALUATION_FUNCTION, _JOBS_FUNCTION]

    assert schema["$id"] == "aap-inference-backend-dependency-reads/v1"
    assert schema["function_defaults"] == {
        "volatility": "STABLE",
        "parallel": "UNSAFE",
        "security_definer": True,
        "search_path": ["pg_catalog", "pg_temp"],
    }
    assert schema["x-postgresql-functions"] == signatures
    assert [function["signature"] for function in schema["functions"]] == signatures
    assert [function["arguments"] for function in schema["functions"]] == [
        [{"name": "p_registry_id", "type": "uuid", "nullable": False}],
        [{"name": "p_registry_id", "type": "uuid", "nullable": False}],
    ]
    assert [function["returns"] for function in schema["functions"]] == [
        {"kind": "scalar", "type": "boolean"},
        {"kind": "scalar", "type": "bigint"},
    ]
