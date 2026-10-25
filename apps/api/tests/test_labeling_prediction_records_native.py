"""Exercise Prediction records through the function-only Labeling LOGIN."""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timezone

import asyncpg
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_labeling.prediction_partition_contract import (
    EnsurePredictionPartition,
    PREDICTION_PARTITIONS_PROTOCOL,
)
from aap_labeling.prediction_partition_data import PredictionPartitionDataClient
from aap_labeling.prediction_record_contract import (
    PREDICTION_RECORDS_PROTOCOL,
    CreateFailedPrediction,
    DeletePredictionsForTasks,
    DismissFailedPrediction,
    InsertPredictionResult,
    PurgeImportedPredictions,
    RejectPredictionCandidate,
    RestoreFailedPrediction,
)
from aap_labeling.prediction_record_data import PredictionRecordDataClient
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.db.models.task import Task
from tests.factory import create_project, create_task, create_user
from tests.test_labeling_class_usage_reads_legacy import _cleanup_owned
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable Prediction-record owner capability",
    ),
]


_PREDICTION_FUNCTIONS = {
    ("prediction_record_command", "jsonb"),
    ("annotation_prediction_command", "jsonb"),
    ("prediction_partition_command", "jsonb"),
}


async def test_function_only_prediction_records_preserve_transaction_and_cleanup(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    task_ids: list[uuid.UUID] = []
    connection = None
    actor_id = project_id = task_id = None
    prediction_id = uuid.uuid4()
    prediction_meta_id = uuid.uuid4()
    failure_id = uuid.uuid4()
    prediction_created_at = datetime.now(timezone.utc)
    result = [
        {
            "type": "rectanglelabels",
            "value": {"x": 10, "y": 20, "width": 5, "height": 6},
        }
    ]
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"prediction-record-{uuid.uuid4().hex}@example.test",
                "Prediction record owner",
            )
            actor_id = actor.id
            actor_ids.append(actor.id)
            project = await create_project(db, owner_id=actor.id)
            project_id = project.id
            project_ids.append(project.id)
            task = await create_task(db, project_id=project.id)
            task_id = task.id
            task_ids.append(task.id)

        # The selector is test-only; _dsn verifies that its database is disposable.
        connection = await asyncpg.connect(
            _dsn(
                test_engine,
                "AAP_MODULE_DATA_LABELING_PREDICTION_URL",
                "labeling_prediction_client",
            ),
            timeout=5,
            command_timeout=10,
        )
        assert await connection.fetchval("SELECT current_user") == (
            "labeling_prediction_client"
        )
        await verify_function_only_role(
            connection,
            schema="aap_labeling_v1",
            functions=_PREDICTION_FUNCTIONS,
            role="labeling_prediction_client",
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch("SELECT id FROM public.predictions LIMIT 1")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                "DELETE FROM public.predictions WHERE id=$1", prediction_id
            )

        async def execute(request_json):
            return await connection.fetchval(
                "SELECT aap_labeling_v1.prediction_record_command($1::jsonb)",
                request_json,
            )

        data = PredictionRecordDataClient(execute)
        request = InsertPredictionResult(
            schema=PREDICTION_RECORDS_PROTOCOL,
            operation="insert_result",
            prediction_id=prediction_id,
            prediction_created_at=prediction_created_at,
            prediction_meta_id=prediction_meta_id,
            task_id=task_id,
            project_id=project_id,
            ml_backend_id=None,
            ml_backend_pool_id=None,
            model_version="fixture-model",
            score=0.9,
            tool_unit_id="bbox",
            result=result,
            source="ml_backend",
            inference_time_ms=12,
            prompt_tokens=3,
            completion_tokens=None,
            total_tokens=3,
            prompt_cost=0.01,
            completion_cost=None,
            total_cost=0.01,
            extra={"trained_model": {"model_id": "fixture"}},
        )

        transaction = connection.transaction()
        await transaction.start()
        rolled_back = await data.command(request)
        assert rolled_back.total_predictions == 1
        await transaction.rollback()
        async with maker() as db:
            assert (
                await db.get(Prediction, (prediction_id, prediction_created_at)) is None
            )
            assert (await db.get(Task, task_id)).total_predictions == 0

        async with connection.transaction():
            inserted = await data.command(request)
            assert inserted.created_at == prediction_created_at
            imported_created_at = datetime.now(timezone.utc)
            imported_request = request.model_copy(
                update={
                    "prediction_id": uuid.uuid4(),
                    "prediction_created_at": imported_created_at,
                    "prediction_meta_id": uuid.uuid4(),
                    "source": "external_import",
                }
            )
            await data.command(imported_request)
            purged = await data.command(
                PurgeImportedPredictions(
                    schema=PREDICTION_RECORDS_PROTOCOL,
                    operation="purge_import_predictions",
                    project_id=project_id,
                    task_ids=[task_id],
                    source_scope="external_import",
                )
            )
            assert (purged.external_import, purged.total) == (1, 1)
            assert len(purged.deleted_prediction_meta_ids) == 1
            rejected = await data.command(
                RejectPredictionCandidate(
                    schema=PREDICTION_RECORDS_PROTOCOL,
                    operation="reject_candidate",
                    prediction_id=prediction_id,
                    prediction_created_at=prediction_created_at,
                    task_id=task_id,
                    project_id=project_id,
                    shape_index=0,
                )
            )
            assert rejected.rejected_shape_indexes == [0]
            failed = await data.command(
                CreateFailedPrediction(
                    schema=PREDICTION_RECORDS_PROTOCOL,
                    operation="create_failed",
                    failure_id=failure_id,
                    task_id=task_id,
                    project_id=project_id,
                    ml_backend_id=None,
                    ml_backend_pool_id=None,
                    model_version="fixture-model",
                    error_type="fixture_error",
                    message="fixture failure",
                    extra={"source": "native-test"},
                )
            )
            assert failed.failure_id == failure_id
            await data.command(
                DismissFailedPrediction(
                    schema=PREDICTION_RECORDS_PROTOCOL,
                    operation="dismiss_failed",
                    failure_id=failure_id,
                    dismissed_at=datetime.now(timezone.utc),
                )
            )
            restored = await data.command(
                RestoreFailedPrediction(
                    schema=PREDICTION_RECORDS_PROTOCOL,
                    operation="restore_failed",
                    failure_id=failure_id,
                )
            )
            assert restored.changed
            deleted = await data.command(
                DeletePredictionsForTasks(
                    schema=PREDICTION_RECORDS_PROTOCOL,
                    operation="delete_for_tasks",
                    task_ids=[task_id],
                )
            )
            assert deleted.predictions == 1
            assert deleted.failed_predictions == 1
            assert len(deleted.deleted_prediction_meta_ids) == 1

        async with maker() as db:
            assert (
                await db.get(Prediction, (prediction_id, prediction_created_at)) is None
            )
            assert await db.get(FailedPrediction, failure_id) is None
            assert (
                await db.scalar(
                    select(PredictionMeta).where(
                        PredictionMeta.prediction_id == prediction_id
                    )
                )
                is None
            )
            assert (await db.get(Task, task_id)).total_predictions == 2
    finally:
        if connection is not None:
            await connection.close()
        if actor_id is not None and project_id is not None and task_id is not None:
            await _cleanup_owned(
                maker,
                actor_ids=actor_ids,
                project_ids=project_ids,
                task_ids=task_ids,
            )


async def test_prediction_partition_command_owns_monthly_ddl_and_rolls_back(
    test_engine,
):
    async with test_engine.connect() as observer:
        owners = (
            (
                await observer.execute(
                    text(
                        "SELECT pg_catalog.pg_get_userbyid(relation.relowner) "
                        "FROM pg_catalog.pg_partition_tree('public.predictions') AS tree "
                        "JOIN pg_catalog.pg_class AS relation ON relation.oid=tree.relid"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert set(owners) == {"labeling_code"}
        owner_profile = (
            await observer.execute(
                text(
                    "SELECT rolcanlogin, pg_catalog.has_schema_privilege("
                    "rolname,'public','CREATE') FROM pg_catalog.pg_roles "
                    "WHERE rolname='labeling_code'"
                )
            )
        ).one()
        assert tuple(owner_profile) == (False, True)
    connection = await asyncpg.connect(
        _dsn(
            test_engine,
            "AAP_MODULE_DATA_LABELING_PREDICTION_URL",
            "labeling_prediction_client",
        ),
        timeout=5,
        command_timeout=10,
    )
    try:
        assert await connection.fetchval("SELECT current_user") == (
            "labeling_prediction_client"
        )
        await verify_function_only_role(
            connection,
            schema="aap_labeling_v1",
            functions=_PREDICTION_FUNCTIONS,
            role="labeling_prediction_client",
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch("SELECT id FROM public.predictions LIMIT 1")
        forbidden_table = f"prediction_partition_forbidden_{uuid.uuid4().hex}"
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                f"CREATE TABLE public.{forbidden_table} (id integer)"
            )

        current_month = datetime.now(timezone.utc).date().replace(day=1)
        month_index = current_month.year * 12 + current_month.month - 1
        month_index += 2400 + uuid.uuid4().int % 600
        year, zero_based_month = divmod(month_index, 12)
        month = date(year, zero_based_month + 1, 1)
        expected_name = f"predictions_y{month.year:04d}m{month.month:02d}"

        async def execute(request_json: str):
            return await connection.fetchval(
                "SELECT aap_labeling_v1.prediction_partition_command($1::jsonb)",
                request_json,
            )

        client = PredictionPartitionDataClient(execute)
        request = EnsurePredictionPartition(
            schema=PREDICTION_PARTITIONS_PROTOCOL,
            operation="ensure",
            month=month,
        )
        transaction = connection.transaction()
        await transaction.start()
        try:
            created = await client.ensure(request)
            assert created.created == expected_name
            repeated = await client.ensure(request)
            assert repeated.created is None
        finally:
            await transaction.rollback()

        async with test_engine.connect() as observer:
            persisted = await observer.scalar(
                text("SELECT pg_catalog.to_regclass(:name)"),
                {"name": f"public.{expected_name}"},
            )
            assert persisted is None

        async with test_engine.begin() as observer:
            await observer.execute(
                text(f"CREATE TABLE public.{expected_name} (id integer)")
            )
        try:
            with pytest.raises(
                asyncpg.InvalidParameterValueError,
                match="not a prediction partition",
            ):
                await client.ensure(request)
        finally:
            async with test_engine.begin() as observer:
                await observer.execute(text(f"DROP TABLE public.{expected_name}"))
    finally:
        await connection.close()
