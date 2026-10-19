"""Recoverable persistence must not turn accepted inference into a failed member."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.application.durable_preannotation import (
    _apply_ready_result,
    freeze_batch_members,
)
from app.application.preannotation_contract import BatchPredictionRequest
from app.application.preannotation_results import (
    BatchResultExecution,
    PreannotationResultDeferred,
    _request_json,
    _restore_request,
    preannotation_request_digest,
    restore_write_scope,
    scope_json,
)
from app.services.labeling_prediction_contract import (
    ApplyPredictionResultError,
    ApplyPredictionResultRequest,
    BatchResultPredictionIdentity,
)
from app.services.labeling_write_scope import capture_labeling_write_scope


def facts():
    task = SimpleNamespace(
        id=uuid4(),
        project_id=uuid4(),
        version=7,
        status="pending",
        assignee_id=None,
        review_round_id=None,
        batch_id=None,
        media_source_generation=3,
        dataset_item_id=uuid4(),
        file_path="s3://source/original",
        file_type="text",
    )
    actor_id, job_id = uuid4(), uuid4()
    scope = capture_labeling_write_scope(
        task,
        actor_id=actor_id,
        actor_known=True,
        operation="preannotation",
        execution_scope="bulk",
        execution_scope_known=True,
        job_id=job_id,
        job_id_known=True,
    )
    request = ApplyPredictionResultRequest(
        task_id=task.id,
        project_id=task.project_id,
        ml_backend_id=uuid4(),
        result=[{"type": "labels", "value": ["原始结果"]}],
        write_scope=scope,
        token_meta={"total_cost": "1.2500"},
    )
    member = SimpleNamespace(ordinal=2, task_id=task.id, write_scope=scope_json(scope))
    result = SimpleNamespace(result_id=uuid4(), prepared_digest="a" * 64)
    identity = BatchResultPredictionIdentity(uuid4(), datetime.now(timezone.utc))
    return task, scope, replace(request, batch_result_identity=identity), member, result


def test_snapshot_retains_original_scope_but_excludes_ephemeral_write_identity():
    task, scope, request, _member, _result = facts()
    value = _request_json(request)
    assert "batch_result_identity" not in value
    assert value["result"] is request.result
    import json
    from app.application.preannotation_results import _json_value

    restored = _restore_request(json.loads(json.dumps(value, default=_json_value)))
    task.version += 1
    task.file_path = "s3://source/replacement"
    assert restored.write_scope == scope
    assert restore_write_scope(value["write_scope"]) == scope
    assert restored.batch_result_identity is None
    assert restored.result == request.result


def test_queued_digest_binds_parameters_but_not_job_or_delivery_identity():
    request = BatchPredictionRequest(
        project_id=str(uuid4()),
        ml_backend_id=str(uuid4()),
        user_id=str(uuid4()),
        params={"threshold": 0.4},
        task_ids=[],
    )
    digest = preannotation_request_digest(request)
    assert digest == preannotation_request_digest(
        replace(request, async_job_id=str(uuid4()))
    )
    assert digest != preannotation_request_digest(replace(request, task_ids=None))
    assert digest != preannotation_request_digest(
        replace(request, params={"threshold": 0.5})
    )
    assert digest != preannotation_request_digest(
        replace(request, pipeline_stages=[{"stage": 0, "params": {"threshold": 0.4}}])
    )
    with pytest.raises(ValueError):
        preannotation_request_digest(
            replace(request, params={"threshold": float("nan")})
        )


@pytest.mark.asyncio
async def test_exhausted_task_contention_defers_same_prepared_bundle():
    _task, _scope, request, member, result = facts()
    seen = []

    async def blocked(requests):
        seen.append(requests)
        raise ApplyPredictionResultError(
            code="task_busy", status_code=409, detail={"reason": "task_busy"}
        )

    writer = SimpleNamespace(
        apply_prediction_result_bundle=blocked, create_failed=AsyncMock()
    )
    execution = SimpleNamespace(call=AsyncMock(), read_result=AsyncMock())
    db = SimpleNamespace(rollback=AsyncMock(), commit=AsyncMock())
    with pytest.raises(PreannotationResultDeferred, match="task_busy"):
        await _apply_ready_result(
            execution, db, writer, member, result, (request,), manifest_backed=False
        )
    assert len(seen) == 3
    assert all(bundle == (request,) for bundle in seen)
    writer.create_failed.assert_not_awaited()
    db.commit.assert_not_awaited()
    assert db.rollback.await_count == 3
    execution.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_commit_ack_resolves_applied_receipt_without_second_write():
    _task, _scope, request, member, result = facts()
    events = []

    async def write(_requests):
        events.append("predictions")

    async def call(operation, **values):
        events.append(operation)
        if operation in {"lock_batch_result_write", "finish_batch_result_write"}:
            assert values["db"] is db

    async def commit():
        events.append("commit_ack_lost")
        raise SQLAlchemyError("lost acknowledgement")

    execution = SimpleNamespace(
        call=call,
        read_result=AsyncMock(
            return_value=SimpleNamespace(result=SimpleNamespace(state="applied"))
        ),
    )
    writer = SimpleNamespace(
        apply_prediction_result_bundle=write, create_failed=AsyncMock()
    )
    db = SimpleNamespace(commit=commit, rollback=AsyncMock())
    await _apply_ready_result(
        execution, db, writer, member, result, (request,), manifest_backed=False
    )
    assert events == [
        "predictions",
        "lock_batch_result_write",
        "finish_batch_result_write",
        "commit_ack_lost",
        "read_batch_result_execution",
    ]
    execution.read_result.assert_awaited_once_with(member.ordinal)
    writer.create_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_commit_outcome_defers_instead_of_recording_failure():
    _task, _scope, request, member, result = facts()
    execution = SimpleNamespace(
        call=AsyncMock(),
        read_result=AsyncMock(
            return_value=SimpleNamespace(result=SimpleNamespace(state="ready"))
        ),
    )
    writer = SimpleNamespace(
        apply_prediction_result_bundle=AsyncMock(), create_failed=AsyncMock()
    )
    db = SimpleNamespace(
        commit=AsyncMock(side_effect=SQLAlchemyError("lost ack")), rollback=AsyncMock()
    )
    with pytest.raises(PreannotationResultDeferred, match="storage_unavailable"):
        await _apply_ready_result(
            execution, db, writer, member, result, (request,), manifest_backed=False
        )
    writer.apply_prediction_result_bundle.assert_awaited_once()
    writer.create_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_pagination_uses_exclusive_read_cursor_without_repeating_members():
    request = BatchPredictionRequest(
        project_id=str(uuid4()),
        ml_backend_id=str(uuid4()),
        user_id=str(uuid4()),
        async_job_id=str(uuid4()),
    )
    execution = BatchResultExecution(request, None, execution_id="delivery")
    execution.call = AsyncMock(
        side_effect=[
            SimpleNamespace(members=("one", "two"), next_ordinal=2),
            SimpleNamespace(members=("three",), next_ordinal=None),
        ]
    )
    assert [item async for item in execution.members()] == ["one", "two", "three"]
    assert execution.call.await_args_list[1].kwargs["after_ordinal"] == 2


@pytest.mark.asyncio
async def test_non_manifest_member_selection_is_staged_in_one_transaction():
    task, scope, _request, _member, _result = facts()
    execution = SimpleNamespace(
        progress=SimpleNamespace(selection_complete=False, selection_cursor=0),
        scope={"actor_id": scope.actor_id, "job_id": scope.job_id},
        call=AsyncMock(),
    )
    request = BatchPredictionRequest(
        project_id=str(task.project_id),
        ml_backend_id=str(uuid4()),
        user_id=str(scope.actor_id),
    )

    async def entries():
        for index in range(101):
            yield index, task, None, None

    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    await freeze_batch_members(
        execution, db, entries(), request=request, accepted_input=None
    )
    assert execution.call.await_count == 2
    assert all(call.kwargs["db"] is db for call in execution.call.await_args_list)
    assert len(execution.call.await_args_list[0].kwargs["members"]) == 100
    assert execution.call.await_args_list[1].kwargs["after_ordinal"] == 100
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_complete_selection_never_reselects_current_tasks():
    execution = SimpleNamespace(progress=SimpleNamespace(selection_complete=True))

    async def entries():
        raise AssertionError("must resume the frozen member sequence")
        yield

    await freeze_batch_members(
        execution, None, entries(), request=None, accepted_input=None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_code", ["storage_unavailable", "invalid_response"])
async def test_captured_mask_result_survives_byte_provider_outage(
    monkeypatch, failure_code
):
    from aap_assets_mask_content.contract import MaskContentFailure
    from app.application import durable_preannotation as workflow
    from app.services import batch_result_masks

    _task, scope, request, member, _result = facts()
    member.state = "captured"
    member.manifest_member = None
    result = SimpleNamespace(
        raw_complete=True,
        prepared_complete=False,
        state="captured",
        context={"task_id": str(member.task_id), "total_cost": "1.25"},
    )

    async def members(**_values):
        yield member

    execution = SimpleNamespace(
        scope={
            "job_id": scope.job_id,
            "actor_id": scope.actor_id,
            "project_id": scope.project_id,
        },
        progress=SimpleNamespace(selection_complete=True, next_ordinal=member.ordinal),
        ensure_owned=lambda: None,
        call=AsyncMock(),
        members=members,
        read_result=AsyncMock(return_value=SimpleNamespace(result=result)),
        load=AsyncMock(return_value=(result, (request,))),
        capture=AsyncMock(),
    )
    monkeypatch.setattr(workflow, "_actor_available", AsyncMock(return_value=True))
    monkeypatch.setattr(workflow, "_cancel_requested", AsyncMock(return_value=False))
    monkeypatch.setattr(
        batch_result_masks,
        "normalize_batch_prediction_masks",
        AsyncMock(side_effect=MaskContentFailure(failure_code)),
    )
    infer = AsyncMock()
    writer = SimpleNamespace(create_failed=AsyncMock())
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    async def entries():
        raise AssertionError("saved members must not be reselected")
        yield

    with pytest.raises(PreannotationResultDeferred, match=failure_code):
        await workflow.run_durable_batch(
            execution,
            db,
            BatchPredictionRequest(
                project_id=str(scope.project_id),
                ml_backend_id=str(uuid4()),
                predict_mode="append",
            ),
            entries=entries(),
            accepted_input=None,
            infer=infer,
            writer=writer,
            job_meta={},
            result_extra={},
        )
    infer.assert_not_awaited()
    writer.create_failed.assert_not_awaited()
    execution.capture.assert_not_awaited()
    assert result.state == "captured"
    assert result.context["total_cost"] == "1.25"


def test_mask_verification_outage_is_retryable_for_the_actual_writer():
    error = ApplyPredictionResultError(
        code="mask_storage_unavailable",
        status_code=503,
        detail={"reason": "mask_storage_unavailable"},
    )
    assert error.retryable


@pytest.mark.parametrize("confirmed_failed", [False, True])
def test_worker_loss_does_not_publish_a_false_terminal_error(
    monkeypatch, confirmed_failed
):
    from unittest.mock import Mock
    from app.workers import tasks

    record = AsyncMock(return_value=confirmed_failed)
    publish = Mock()
    monkeypatch.setattr(tasks, "_mark_job_failed", record)
    monkeypatch.setattr(tasks.prediction_progress, "publish_progress", publish)
    tasks._BatchPredictTask().on_failure(
        RuntimeError("worker lost"), "delivery", (), {"project_id": "project"}, None
    )
    record.assert_awaited_once()
    assert publish.called is confirmed_failed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", ["execution_busy", "storage_unavailable", "invalid_response"]
)
async def test_transient_heartbeat_failure_can_renew_the_same_lease(monkeypatch, code):
    import app.application.preannotation_results as application

    request = BatchPredictionRequest(
        project_id=str(uuid4()),
        ml_backend_id=str(uuid4()),
        user_id=str(uuid4()),
        async_job_id=str(uuid4()),
    )
    execution = BatchResultExecution(request, None, execution_id="delivery")
    execution.lease = SimpleNamespace(generation=1, lease_token=uuid4())
    renewed = SimpleNamespace(generation=1, lease_token=execution.lease.lease_token)
    execution.call = AsyncMock(
        side_effect=[PreannotationResultDeferred(code), SimpleNamespace(lease=renewed)]
    )
    delays = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(application.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await execution._renew()
    execution.ensure_owned()
    assert execution.lease is renewed and execution._lost is None
    assert delays == [30, 5, 30]


@pytest.mark.asyncio
async def test_definitive_heartbeat_lease_loss_stops_further_writes(monkeypatch):
    import app.application.preannotation_results as application

    request = BatchPredictionRequest(
        project_id=str(uuid4()),
        ml_backend_id=str(uuid4()),
        user_id=str(uuid4()),
        async_job_id=str(uuid4()),
    )
    execution = BatchResultExecution(request, None, execution_id="delivery")
    execution.lease = SimpleNamespace(generation=1, lease_token=uuid4())
    execution.call = AsyncMock(side_effect=PreannotationResultDeferred("lease_lost"))
    monkeypatch.setattr(application.asyncio, "sleep", AsyncMock())
    await execution._renew()
    with pytest.raises(PreannotationResultDeferred, match="lease_lost"):
        execution.ensure_owned()
    execution.call.assert_awaited_once()


@pytest.mark.asyncio
async def test_six_hundred_results_fit_a_small_open_file_budget(monkeypatch):
    import json
    import app.application.preannotation_results as application
    import aap_preannotation_results.codec as codec

    _task, scope, request, _member, _result = facts()
    execution = BatchResultExecution(
        BatchPredictionRequest(
            project_id=str(scope.project_id),
            ml_backend_id=str(request.ml_backend_id),
            user_id=str(scope.actor_id),
            async_job_id=str(scope.job_id),
        ),
        None,
        execution_id="delivery",
    )
    original_file = application.tempfile.TemporaryFile
    opened, peak = 0, 0

    class TrackedFile:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, key):
            return getattr(self.handle, key)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def close(self):
            nonlocal opened
            if not self.handle.closed:
                self.handle.close()
                opened -= 1

    def bounded_file(*args, **kwargs):
        nonlocal opened, peak
        opened += 1
        peak = max(peak, opened)
        assert opened <= 8, "multi-result capture exhausted its open-file budget"
        return TrackedFile(original_file(*args, **kwargs))

    monkeypatch.setattr(application.tempfile, "TemporaryFile", bounded_file)
    monkeypatch.setattr(codec, "TemporaryFile", bounded_file)
    seen = []

    async def call(operation, **values):
        if operation == "begin_batch_result":
            assert values["result_count"] == 600
        elif operation == "put_batch_result_part":
            body = json.loads(codec.decode_part(values["content"]))
            assert body["result"] == [{"value": values["request_index"]}]
            seen.append(values["request_index"])

    execution.call = call
    execution.seal = AsyncMock()
    await execution.capture(
        1,
        tuple(replace(request, result=[{"value": index}]) for index in range(600)),
        stage="raw",
        context={"task_id": str(request.task_id)},
    )
    assert seen == list(range(600))
    assert peak <= 8 and opened == 0
    execution.seal.assert_awaited_once()


@pytest.mark.asyncio
async def test_job_retry_reads_all_failed_members_instead_of_the_bounded_summary(
    monkeypatch,
):
    from app.application.prediction_retry import _recorded_failed_ids
    from app.services.batch_result_data import SqlAlchemyBatchResultData

    job_id, actor_id, project_id = uuid4(), uuid4(), uuid4()
    ids = [uuid4() for _ in range(101)]
    job = SimpleNamespace(
        id=job_id,
        project_id=project_id,
        retained_project_id=None,
        result={
            "failed_prediction_ids": [str(ids[0])],
            "failed_prediction_ids_truncated": True,
            "batch_result_scope": {
                "job_id": str(job_id),
                "actor_id": str(actor_id),
                "project_id": str(project_id),
                "request_digest": "a" * 64,
            },
        },
    )
    pages = AsyncMock(
        side_effect=[
            SimpleNamespace(
                members=[
                    SimpleNamespace(failed_prediction_id=value) for value in ids[:100]
                ],
                next_ordinal=100,
            ),
            SimpleNamespace(
                members=[SimpleNamespace(failed_prediction_id=ids[100])],
                next_ordinal=None,
            ),
        ]
    )
    monkeypatch.setattr(SqlAlchemyBatchResultData, "call", pages)
    assert await _recorded_failed_ids(None, job) == ids
    assert [call.args[1].after_ordinal for call in pages.await_args_list] == [0, 100]
    assert all(call.args[1].job_id == job_id for call in pages.await_args_list)
