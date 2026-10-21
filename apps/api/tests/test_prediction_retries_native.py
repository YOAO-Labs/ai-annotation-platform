"""Retry admission, publication uncertainty and saved-output recovery as real roles."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from decimal import Decimal
import json
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import func, select, text

from app.application.durable_prediction_retry import call_retry, retry_scope
from app.application.preannotation_results import (
    BatchResultExecution,
    PreannotationResultDeferred,
)
from app.db.models.async_job import AsyncJob
from app.db.models.notification import Notification
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.services.prediction_retry_data import (
    SqlAlchemyPredictionRetryData,
)
from tests.test_batch_result_consumers_native import _world as _batch_world
from tests.test_batch_result_consumers_native import pytestmark as pytestmark


@asynccontextmanager
async def _world(
    test_engine, *, extra=None, task=True, backend=True, origin_member=False
):
    async with _batch_world(test_engine, staged=origin_member) as world:
        failed_id, job_id = uuid.uuid4(), uuid.uuid4()
        async with world.owner_factory() as db:
            if origin_member:
                failed_id = await db.scalar(
                    text(
                        "SELECT failure_id FROM batch_result_members WHERE job_id=:job AND ordinal=1"
                    ),
                    {"job": world.execution.scope["job_id"]},
                )
            db.add(
                FailedPrediction(
                    id=failed_id,
                    project_id=world.project_id,
                    task_id=world.task_id if task else None,
                    ml_backend_id=world.backend_id if backend else None,
                    ml_backend_pool_id=world.pool_id,
                    error_type="TIMEOUT",
                    message="original model failure",
                    retry_count=0,
                    extra=extra or {},
                )
            )
            await db.commit()
        async with world.writers() as db:
            admitted = await call_retry(
                SqlAlchemyPredictionRetryData(db, writer=True),
                "admit_prediction_retry",
                job_id=job_id,
                failed_prediction_id=failed_id,
                actor_id=world.actor_id,
                execution_queue="ml.cpu",
                legacy_delivery=False,
            )
            await db.commit()
        yield SimpleNamespace(
            **vars(world), attempt=admitted.attempt, failed_id=failed_id, job_id=job_id
        )


def _predict(monkeypatch, world, *, fail=False, returned_task=None, count=1):
    calls = []

    async def predict(client, tasks, **kwargs):
        from aap_inference.backend_transport import PredictionResult

        calls.append((tasks, kwargs))
        if fail:
            raise ValueError("model failed once")
        client.last_instance_id = world.backend_id
        client.pool_id = world.pool_id
        return [
            PredictionResult(
                task_id=str(returned_task or world.task_id),
                result=[],
                score=0.9,
                model_version="retry-ledger",
                inference_time_ms=7,
                meta={"total_cost": 0.25},
            )
            for _ in range(count)
        ]

    monkeypatch.setattr(
        "app.services.ml_routing.client.RoutedMLBackendClient.predict", predict
    )
    monkeypatch.setattr(
        "app.services.storage.resolve_task_url",
        lambda _task: "https://example.test/original.txt",
    )
    return calls


async def _run(world):
    from app.workers.predictions_retry import _do_retry_with_factory

    return await _do_retry_with_factory(
        world.writers,
        str(world.failed_id),
        str(world.actor_id),
        celery_task_id=str(world.job_id),
        job_id=str(world.job_id),
        executor_session_factory=world.executors,
    )


async def test_admission_and_ambiguous_publication_keep_one_job_and_original_claim(
    test_engine,
):
    from app.application.prediction_retry_delivery import (
        publish_prediction_retry_deliveries,
    )

    async with _world(test_engine) as world:
        published = []

        class Dispatcher:
            def dispatch_prediction_retry(
                self, failed_id, actor_id, *, execution_queue, job_id
            ):
                published.append((failed_id, actor_id, execution_queue, job_id))
                if len(published) == 1:
                    raise RuntimeError("broker accepted before connection loss")

        assert await publish_prediction_retry_deliveries(
            world.executors, Dispatcher(), job_id=world.job_id
        ) == {"submitted": 0, "pending": 1}
        async with world.owner_factory() as db:
            failed = await db.get(FailedPrediction, world.failed_id)
            job = await db.get(AsyncJob, world.job_id)
            assert failed.retry_count == 0 and failed.extra["retry_pending"] is True
            assert failed.extra["retry_job_id"] == str(world.job_id)
            assert job.status == "pending" and job.celery_task_id == str(world.job_id)
            await db.execute(
                text(
                    "UPDATE prediction_retry_deliveries SET next_attempt_at=clock_timestamp()-interval '1 second' WHERE job_id=:job"
                ),
                {"job": world.job_id},
            )
            await db.commit()
        assert await publish_prediction_retry_deliveries(
            world.executors, Dispatcher(), job_id=world.job_id
        ) == {"submitted": 1, "pending": 0}
        assert len(published) == 2 and published[0] == published[1]
        async with world.writers() as db:
            from aap_preannotation_results.retry_contract import PredictionRetryFailure

            with pytest.raises(PredictionRetryFailure, match="already_pending"):
                await call_retry(
                    SqlAlchemyPredictionRetryData(db, writer=True),
                    "admit_prediction_retry",
                    job_id=uuid.uuid4(),
                    failed_prediction_id=world.failed_id,
                    actor_id=world.actor_id,
                    execution_queue="ml.cpu",
                    legacy_delivery=False,
                )
            await db.rollback()


async def test_immediate_publication_uses_the_configured_function_only_connection(
    test_engine, monkeypatch
):
    import os
    from sqlalchemy.engine import make_url
    from app.config import settings
    from app.application.prediction_retry_delivery import publish_admitted_retry

    async with _world(test_engine) as world:
        executor = make_url(os.environ["AAP_MODULE_DATA_PREANNOTATION_RESULT_URL"])
        monkeypatch.setattr(
            settings,
            "preannotation_results_database_url",
            executor.set(drivername="postgresql").render_as_string(hide_password=False),
        )
        monkeypatch.setattr(settings, "preannotation_results_database_url_file", "")
        calls = []

        def unavailable_business_session():
            pytest.fail("publication must use the configured executor connection")

        class Dispatcher:
            def dispatch_prediction_retry(
                self, failed, actor, *, execution_queue, job_id
            ):
                calls.append((failed, actor, execution_queue, job_id))

        await publish_admitted_retry(
            unavailable_business_session, Dispatcher(), world.job_id
        )
        assert calls == [(world.failed_id, world.actor_id, "ml.cpu", world.job_id)]


async def test_real_retry_terminal_receipt_survives_redelivery_without_another_model_call(
    test_engine, monkeypatch
):
    async with _world(
        test_engine, extra={"request_context": {"output": "mask"}}
    ) as world:
        calls = _predict(monkeypatch, world)
        result = await _run(world)
        assert result["status"] == "succeeded", result
        assert await _run(world) == result
        assert len(calls) == 1
        async with world.owner_factory() as db:
            assert await db.get(FailedPrediction, world.failed_id) is None
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(Prediction)
                    .where(Prediction.task_id == world.task_id)
                )
                == 1
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(PredictionMeta)
                    .where(
                        PredictionMeta.prediction_id
                        == uuid.UUID(result["prediction_id"])
                    )
                )
                == 1
            )
            job = await db.get(AsyncJob, world.job_id)
            assert job.status == "completed" and job.result["success_count"] == 1
            assert Decimal(job.result["total_cost"]) == Decimal("0.25")
            notifications = (
                await db.scalars(
                    select(Notification.type).where(
                        Notification.user_id == world.actor_id
                    )
                )
            ).all()
            assert notifications.count("failed_prediction.retry.started") == 1
            assert notifications.count("job.completed") == 1
            await db.delete(job)
            await db.commit()
        assert await _run(world) == result


@pytest.mark.parametrize(
    "failure",
    [
        "model",
        "wrong_task",
        "extra_result",
        "missing_backend",
        "missing_task",
        "invalid_result",
        "model_http",
        "invalid_metadata",
    ],
)
async def test_permanent_failure_is_charged_once_and_replayed_exactly(
    test_engine, monkeypatch, failure
):
    async with _world(
        test_engine,
        task=failure != "missing_task",
        backend=failure != "missing_backend",
    ) as world:
        calls = _predict(
            monkeypatch,
            world,
            fail=failure == "model",
            returned_task=uuid.uuid4() if failure == "wrong_task" else None,
            count=2 if failure == "extra_result" else 1,
        )
        if failure == "invalid_metadata":
            from app.services.ml_routing.client import RoutedMLBackendClient

            predict = RoutedMLBackendClient.predict

            async def invalid_metadata(client, tasks, **kwargs):
                results = await predict(client, tasks, **kwargs)
                results[0].score = "not a numeric score"
                return results

            monkeypatch.setattr(RoutedMLBackendClient, "predict", invalid_metadata)
        if failure == "model_http":
            from fastapi import HTTPException

            async def unavailable_model(_client, _tasks, **_kwargs):
                calls.append("attempted")
                raise HTTPException(status_code=503, detail="model unavailable")

            monkeypatch.setattr(
                "app.services.ml_routing.client.RoutedMLBackendClient.predict",
                unavailable_model,
            )
        if failure == "invalid_result":
            from app.services.labeling_prediction_contract import (
                ApplyPredictionResultError,
            )

            async def invalid_result(_self, _requests):
                raise ApplyPredictionResultError(
                    code="invalid_coco_rle",
                    status_code=422,
                    detail={"reason": "invalid_coco_rle"},
                )

            monkeypatch.setattr(
                "app.services.prediction.PredictionService.apply_prediction_result_bundle",
                invalid_result,
            )
        outcome = await _run(world)
        assert outcome["status"] == "failed" and outcome["retry_count"] == 1
        assert await _run(world) == outcome
        assert len(calls) == (
            0 if failure in {"missing_task", "missing_backend"} else 1
        )
        async with world.owner_factory() as db:
            failed = await db.get(FailedPrediction, world.failed_id)
            assert failed.retry_count == 1 and failed.last_retry_at is not None
            assert (
                not failed.extra.get("retry_pending")
                and "retry_job_id" not in failed.extra
            )
            assert (await db.get(AsyncJob, world.job_id)).status == "failed"
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(Prediction)
                    .where(Prediction.task_id == world.task_id)
                )
                == 0
            )


async def test_exhausted_write_contention_resumes_saved_output_without_charging_failure(
    test_engine, monkeypatch
):
    from app.services.labeling_prediction_contract import ApplyPredictionResultError
    from app.services.prediction import PredictionService

    async with _world(test_engine) as world:
        calls = _predict(monkeypatch, world)
        apply = PredictionService.apply_prediction_result_bundle
        writes = []

        async def busy(self, requests):
            writes.append(requests)
            raise ApplyPredictionResultError(
                status_code=409, code="task_busy", detail={"reason": "task_busy"}
            )

        monkeypatch.setattr(PredictionService, "apply_prediction_result_bundle", busy)
        with pytest.raises(PreannotationResultDeferred, match="task_busy"):
            await _run(world)
        assert len(writes) == 3 and len(calls) == 1
        async with world.owner_factory() as db:
            failed = await db.get(FailedPrediction, world.failed_id)
            assert failed.retry_count == 0 and failed.extra["retry_pending"] is True
            assert (await db.get(AsyncJob, world.job_id)).status == "running"
        from aap_preannotation_results.contract import ClaimBatchResultExecutionRequest
        from tests.test_batch_result_native_peer import _psql_peer

        scope = retry_scope(world.attempt)
        claim = ClaimBatchResultExecutionRequest(
            **scope, worker_id=uuid.uuid4(), execution_id="native-retry-peer"
        ).model_dump(mode="json")
        lines, _database = await asyncio.to_thread(
            _psql_peer,
            scope,
            claim,
            {
                **scope,
                "ordinal": 1,
                "stage": "prepared",
                "request_index": 0,
                "part_index": None,
            },
        )
        assert json.loads(lines[2])["data"]["members"][0]["state"] == "ready"
        assert json.loads(lines[3])["data"]["result"]["raw_complete"] is True
        monkeypatch.setattr(PredictionService, "apply_prediction_result_bundle", apply)
        resumed = await _run(world)
        assert resumed["status"] == "succeeded", resumed
        assert len(calls) == 1


async def test_permanent_normalization_failure_can_settle_after_complete_raw(
    test_engine, monkeypatch
):
    async with _world(test_engine) as world:
        calls = _predict(monkeypatch, world)

        async def invalid_mask(_db, _requests):
            raise ValueError("invalid model mask")

        monkeypatch.setattr(
            "app.services.batch_result_masks.normalize_batch_prediction_masks",
            invalid_mask,
        )
        outcome = await _run(world)
        assert outcome["status"] == "failed" and outcome["retry_count"] == 1
        assert await _run(world) == outcome and len(calls) == 1
        async with world.owner_factory() as db:
            failed = await db.get(FailedPrediction, world.failed_id)
            assert failed.retry_count == 1 and not failed.extra.get("retry_pending")
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM batch_result_payloads WHERE job_id=:job AND (body IS NOT NULL OR roots_active)"
                    ),
                    {"job": world.job_id},
                )
                == 0
            )


async def test_lost_business_commit_ack_reads_the_terminal_attempt_receipt(
    test_engine, monkeypatch
):
    from app.application import durable_prediction_retry as workflow
    from sqlalchemy.exc import SQLAlchemyError

    async with _world(test_engine) as world:
        calls = _predict(monkeypatch, world)
        actual_apply = workflow._apply_snapshot
        commits = []

        async def apply_with_lost_ack(
            execution, attempt, data, factory, result, requests
        ):
            @asynccontextmanager
            async def lost_commit_factory():
                async with factory() as db:
                    actual_commit = db.commit

                    async def commit():
                        await actual_commit()
                        commits.append("committed")
                        raise SQLAlchemyError("commit response lost")

                    db.commit = commit
                    yield db

            return await actual_apply(
                execution, attempt, data, lost_commit_factory, result, requests
            )

        monkeypatch.setattr(workflow, "_apply_snapshot", apply_with_lost_ack)
        result = await _run(world)
        assert result["status"] == "succeeded", result
        assert commits == ["committed"]
        assert await _run(world) == result and len(calls) == 1
        async with world.owner_factory() as db:
            assert await db.get(FailedPrediction, world.failed_id) is None
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(Prediction)
                    .where(Prediction.task_id == world.task_id)
                )
                == 1
            )


async def test_stale_failure_token_cannot_charge_or_clear_an_active_claim(test_engine):
    async with _world(test_engine) as world:
        first = BatchResultExecution(
            None,
            world.writers,
            execution_id="first",
            executor_session_factory=world.executors,
            scope=retry_scope(world.attempt),
        )
        await first.claim()
        stale = first._values()
        await first.close()
        second = BatchResultExecution(
            None,
            world.writers,
            execution_id="second",
            executor_session_factory=world.executors,
            scope=retry_scope(world.attempt),
        )
        await second.claim()
        try:
            async with world.writers() as db:
                with pytest.raises(PreannotationResultDeferred, match="lease_lost"):
                    await call_retry(
                        SqlAlchemyPredictionRetryData(db, writer=True),
                        "finish_prediction_retry_failure",
                        **stale,
                        error_code="model_failure",
                        reason="stale model failure",
                        gpu_arbiter_error=None,
                        context={},
                    )
                await db.rollback()
            async with world.owner_factory() as db:
                failed = await db.get(FailedPrediction, world.failed_id)
                assert failed.retry_count == 0 and failed.extra["retry_job_id"] == str(
                    world.job_id
                )
        finally:
            await second.close()


@pytest.mark.parametrize("change", ["source", "actor", "dismissed"])
async def test_invalidated_saved_output_abandons_only_its_claim_without_a_failure_charge(
    test_engine, monkeypatch, change
):
    from app.db.models.task import Task
    from app.db.models.user import User
    from app.services.labeling_prediction_contract import ApplyPredictionResultError
    from app.services.prediction import PredictionService

    async with _world(test_engine) as world:
        calls = _predict(monkeypatch, world)
        apply = PredictionService.apply_prediction_result_bundle

        async def busy(_self, _requests):
            raise ApplyPredictionResultError(
                status_code=409, code="task_busy", detail={"reason": "task_busy"}
            )

        monkeypatch.setattr(PredictionService, "apply_prediction_result_bundle", busy)
        with pytest.raises(PreannotationResultDeferred):
            await _run(world)
        monkeypatch.setattr(PredictionService, "apply_prediction_result_bundle", apply)
        async with world.owner_factory() as db:
            if change == "source":
                (await db.get(Task, world.task_id)).file_path = "text://another-source"
            elif change == "actor":
                (await db.get(User, world.actor_id)).is_active = False
            else:
                from datetime import datetime, timezone

                (
                    await db.get(FailedPrediction, world.failed_id)
                ).dismissed_at = datetime.now(timezone.utc)
            await db.commit()
        outcome = await _run(world)
        assert outcome["status"] in {"failed", "cancelled"}
        assert await _run(world) == outcome
        assert len(calls) == 1
        async with world.owner_factory() as db:
            failed = await db.get(FailedPrediction, world.failed_id)
            assert failed.retry_count == 0 and not failed.extra.get("retry_pending")
            assert "retry_job_id" not in failed.extra
            assert (await db.get(AsyncJob, world.job_id)).status == "cancelled"
            assert (
                await db.scalar(
                    text(
                        "SELECT state FROM prediction_retry_attempts WHERE job_id=:job"
                    ),
                    {"job": world.job_id},
                )
                == "abandoned"
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(Prediction)
                    .where(Prediction.task_id == world.task_id)
                )
                == 0
            )


async def test_worker_loss_signal_cannot_terminalize_an_admitted_retry(test_engine):
    from app.application.job_terminal import apply_signal_terminal

    async with _world(test_engine) as world:
        async with world.writers() as db:
            job = await db.get(AsyncJob, world.job_id)
            assert not await apply_signal_terminal(
                db, job, cancelled=False, error="WorkerLostError"
            )
            await db.commit()
        async with world.owner_factory() as db:
            assert (await db.get(AsyncJob, world.job_id)).status == "pending"
            assert (await db.get(FailedPrediction, world.failed_id)).extra[
                "retry_pending"
            ] is True


async def test_retry_member_identity_does_not_collide_with_its_origin_batch_member(
    test_engine, monkeypatch
):
    async with _world(test_engine, origin_member=True) as world:
        calls = _predict(monkeypatch, world)
        async with world.owner_factory() as db:
            member_failure = await db.scalar(
                text(
                    "SELECT failure_id FROM batch_result_members WHERE job_id=:job AND ordinal=1"
                ),
                {"job": world.job_id},
            )
            assert member_failure != world.failed_id
        assert (await _run(world))["status"] == "succeeded"
        assert len(calls) == 1


async def test_pending_input_has_a_visible_job_without_model_count_or_started_notification(
    test_engine, monkeypatch
):
    from app.workers import predictions_retry as worker
    from app.application.prediction_retry_inputs import FrozenInputRetryDeferred

    async with _world(test_engine) as world:
        calls = _predict(monkeypatch, world)

        @asynccontextmanager
        async def pending(*_args, **_kwargs):
            raise FrozenInputRetryDeferred("input_not_ready")
            yield

        monkeypatch.setattr(worker, "_prepare_retry_input", pending)
        with pytest.raises(PreannotationResultDeferred, match="input_not_ready"):
            await _run(world)
        assert calls == []
        async with world.owner_factory() as db:
            assert (await db.get(AsyncJob, world.job_id)).status == "pending"
            failed = await db.get(FailedPrediction, world.failed_id)
            assert failed.retry_count == 0 and failed.extra["retry_pending"] is True
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(Notification)
                    .where(Notification.user_id == world.actor_id)
                )
                == 0
            )
