from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.labeling_prediction_contract import (
    ApplyPredictionResultRequest,
    BatchResultPredictionIdentity,
)
from app.services.labeling_write_scope import LabelingWriteScope


def _scope() -> LabelingWriteScope:
    return LabelingWriteScope(
        task_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        actor_known=True,
        operation="preannotation",
        execution_scope="bulk",
        execution_scope_known=True,
        task_version=1,
        task_status="pending",
        assignee_id=None,
        review_round_id=None,
        source_annotation_id=None,
        source_annotation_version=None,
        expected_batch_id=None,
        expected_batch_id_known=True,
        job_id=uuid.uuid4(),
        job_id_known=True,
    )


def _request(
    identity: BatchResultPredictionIdentity | None = None,
    scope: LabelingWriteScope | None = None,
):
    scope = scope or _scope()
    return ApplyPredictionResultRequest(
        task_id=scope.task_id,
        project_id=scope.project_id,
        ml_backend_id=None,
        result=[
            {
                "type": "video_track_bbox",
                "track_id": 17,
                "class_name": "car",
                "keyframes": [],
            }
        ],
        write_scope=scope,
        batch_result_identity=identity,
    )


def test_batch_result_identity_requires_aware_utc_timestamp():
    with pytest.raises(ValueError, match="aware UTC"):
        BatchResultPredictionIdentity(uuid.uuid4(), datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="aware UTC"):
        BatchResultPredictionIdentity(
            uuid.uuid4(), datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8)))
        )

    identity = BatchResultPredictionIdentity(uuid.uuid4(), datetime.now(timezone.utc))
    assert identity.prediction_id


@pytest.mark.asyncio
@pytest.mark.parametrize("as_bundle", [False, True])
async def test_batch_writer_persists_stable_identity_without_remapping_tracks(
    monkeypatch, as_bundle
):
    from app.services import prediction as prediction_module
    from app.services.prediction import PredictionService

    task_id, project_id = uuid.uuid4(), uuid.uuid4()
    task = SimpleNamespace(id=task_id, project_id=project_id, total_predictions=0)
    created_at = datetime(2026, 5, 4, 3, 2, tzinfo=timezone.utc)
    scope = _scope_for(task_id, project_id)
    identities = [
        BatchResultPredictionIdentity(
            uuid.uuid4(), created_at + timedelta(seconds=index)
        )
        for index in range(2 if as_bundle else 1)
    ]
    requests = tuple(_request(identity, scope) for identity in identities)
    rows = []

    class Session:
        async def get(self, *_args, **_kwargs):
            return task

        def add(self, row):
            rows.append(row)

        async def flush(self):
            for row in rows:
                if getattr(row, "created_at", None) is None:
                    row.created_at = created_at

    async def preflight(*_args, **_kwargs):
        return None

    async def lock(_db, _scope, **_kwargs):
        return task, object(), object()

    async def prepare(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        prediction_module, "preflight_mask_payload_for_write", preflight
    )
    monkeypatch.setattr(prediction_module, "lock_labeling_write", lock)
    monkeypatch.setattr(prediction_module, "prepare_mask_payload_for_write", prepare)

    writer = PredictionService(Session())
    receipts = (
        await writer.apply_prediction_result_bundle(requests)
        if as_bundle
        else (await writer.apply_prediction_result(requests[0]),)
    )
    assert [receipt.id for receipt in receipts] == [
        identity.prediction_id for identity in identities
    ]
    predictions = [row for row in rows if row.__class__.__name__ == "Prediction"]
    assert [prediction.id for prediction in predictions] == [
        identity.prediction_id for identity in identities
    ]
    assert [prediction.created_at for prediction in predictions] == [
        identity.prediction_created_at for identity in identities
    ]
    assert all(prediction.result[0]["track_id"] == 17 for prediction in predictions)


def _scope_for(task_id: uuid.UUID, project_id: uuid.UUID) -> LabelingWriteScope:
    return LabelingWriteScope(
        task_id=task_id,
        project_id=project_id,
        actor_id=uuid.uuid4(),
        actor_known=True,
        operation="preannotation",
        execution_scope="bulk",
        execution_scope_known=True,
        task_version=1,
        task_status="pending",
        assignee_id=None,
        review_round_id=None,
        source_annotation_id=None,
        source_annotation_version=None,
        expected_batch_id=None,
        expected_batch_id_known=True,
        job_id=uuid.uuid4(),
        job_id_known=True,
    )


def test_legacy_prediction_write_keeps_track_id_remapping():
    from app.services.prediction import _prediction_result_for_write

    result = _prediction_result_for_write(_request())
    assert result[0]["track_id"].startswith("trk_")
    assert result[0]["track_id"] != 17


@pytest.mark.asyncio
async def test_create_failed_accepts_a_stable_internal_failure_id():
    from app.services.prediction import PredictionService

    failure_id = uuid.uuid4()
    rows = []

    class Session:
        def add(self, row):
            rows.append(row)

        async def flush(self):
            pass

    failed = await PredictionService(Session()).create_failed(
        task_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        ml_backend_id=None,
        error_type="batch_result_write",
        message="failed",
        failure_id=failure_id,
    )
    assert failed.id == failure_id
    assert rows == [failed]
