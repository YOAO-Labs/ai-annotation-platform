from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

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


def _request(identity: BatchResultPredictionIdentity | None = None):
    scope = _scope()
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


def test_batch_result_write_preserves_frozen_track_identity():
    from app.services.prediction import _prediction_result_for_write

    identity = BatchResultPredictionIdentity(uuid.uuid4(), datetime.now(timezone.utc))
    result = _prediction_result_for_write(_request(identity))
    assert result[0]["track_id"] == 17


def test_legacy_prediction_write_keeps_track_id_remapping():
    from app.services.prediction import _prediction_result_for_write

    result = _prediction_result_for_write(_request())
    assert result[0]["track_id"].startswith("trk_")
    assert result[0]["track_id"] != 17
