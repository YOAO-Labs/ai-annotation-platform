from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from aap_labeling.prediction_record_contract import (
    PREDICTION_RECORDS_FUNCTION,
    PREDICTION_RECORDS_PROTOCOL,
    InsertPredictionResult,
    PredictionRecordUnavailable,
    PurgeImportedPredictions,
    RejectPredictionCandidate,
    decode_prediction_record_command,
    decode_prediction_record_reply,
    prediction_record_command_json,
    prediction_record_schema,
)


def test_prediction_record_command_is_closed_and_keeps_null_shape_scope():
    request = RejectPredictionCandidate(
        schema=PREDICTION_RECORDS_PROTOCOL,
        operation="reject_candidate",
        prediction_id=uuid.uuid4(),
        prediction_created_at=datetime(2026, 10, 25, tzinfo=timezone.utc),
        task_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        shape_index=None,
    )

    encoded = prediction_record_command_json(request)
    assert json.loads(encoded)["shape_index"] is None
    assert decode_prediction_record_command(encoded) == request
    assert prediction_record_schema()["x-postgresql-functions"] == [
        PREDICTION_RECORDS_FUNCTION
    ]

    payload = json.loads(encoded)
    payload["privileged"] = True
    with pytest.raises(PredictionRecordUnavailable):
        decode_prediction_record_command(payload)

    project_purge = PurgeImportedPredictions(
        schema=PREDICTION_RECORDS_PROTOCOL,
        operation="purge_import_predictions",
        project_id=uuid.uuid4(),
        task_ids=None,
        source_scope="all",
    )
    purge_json = prediction_record_command_json(project_purge)
    assert json.loads(purge_json)["task_ids"] is None
    assert decode_prediction_record_command(purge_json) == project_purge


def test_insert_prediction_reply_returns_task_update_timestamp():
    prediction_id = uuid.uuid4()
    task_id = uuid.uuid4()
    project_id = uuid.uuid4()
    created_at = datetime(2026, 10, 25, 1, tzinfo=timezone.utc)
    task_updated_at = datetime(2026, 10, 25, 1, 2, tzinfo=timezone.utc)
    request = InsertPredictionResult(
        schema=PREDICTION_RECORDS_PROTOCOL,
        operation="insert_result",
        prediction_id=prediction_id,
        prediction_created_at=None,
        prediction_meta_id=uuid.uuid4(),
        task_id=task_id,
        project_id=project_id,
        ml_backend_id=None,
        ml_backend_pool_id=None,
        model_version=None,
        score=None,
        tool_unit_id="bbox",
        result=[],
        source="ml_backend",
        inference_time_ms=None,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        prompt_cost=None,
        completion_cost=None,
        total_cost=None,
        extra={},
    )
    reply = {
        "schema": PREDICTION_RECORDS_PROTOCOL,
        "ok": True,
        "request": json.loads(prediction_record_command_json(request)),
        "data": {
            "operation": "insert_result",
            "prediction_id": str(prediction_id),
            "created_at": created_at.isoformat(),
            "tool_unit_id": "bbox",
            "total_predictions": 1,
            "task_updated_at": task_updated_at.isoformat(),
        },
        "error": None,
    }

    data = decode_prediction_record_reply(request, reply)
    assert data.task_updated_at == task_updated_at
