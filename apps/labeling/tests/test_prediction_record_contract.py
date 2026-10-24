from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from aap_labeling.prediction_record_contract import (
    PREDICTION_RECORDS_FUNCTION,
    PREDICTION_RECORDS_PROTOCOL,
    PredictionRecordUnavailable,
    PurgeImportedPredictions,
    RejectPredictionCandidate,
    decode_prediction_record_command,
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
