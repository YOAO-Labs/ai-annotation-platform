from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from aap_labeling.prediction_partition_contract import (
    EnsurePredictionPartition,
    PREDICTION_PARTITIONS_PROTOCOL,
    PredictionPartitionUnavailable,
    decode_prediction_partition_reply,
    prediction_partition_command_json,
    prediction_partition_schema,
)


def test_prediction_partition_contract_matches_frozen_artifacts():
    api_root = Path(__file__).parents[1]
    apps_root = api_root.parent
    api_sql = api_root / "alembic/sql/0357_labeling_prediction_partitions.sql"
    package_sql = (
        apps_root
        / "labeling/src/aap_labeling/sql/0357_labeling_prediction_partitions.sql"
    )
    api_schema = (
        api_root / "alembic/sql/0357_labeling_prediction_partitions.schema.json"
    )
    package_schema = (
        apps_root / "labeling/src/aap_labeling/prediction-partitions.schema.json"
    )

    assert api_sql.read_bytes() == package_sql.read_bytes()
    assert api_schema.read_bytes() == package_schema.read_bytes()
    assert json.loads(api_schema.read_text(encoding="utf-8")) == (
        prediction_partition_schema()
    )

    request = EnsurePredictionPartition(
        schema=PREDICTION_PARTITIONS_PROTOCOL,
        operation="ensure",
        month=date(2526, 3, 1),
    )
    encoded = prediction_partition_command_json(request)
    payload = json.loads(encoded)
    assert payload == {
        "schema": PREDICTION_PARTITIONS_PROTOCOL,
        "operation": "ensure",
        "month": "2526-03-01",
    }
    reply = {
        "schema": PREDICTION_PARTITIONS_PROTOCOL,
        "ok": True,
        "request": payload,
        "data": {
            "operation": "ensure",
            "month": "2526-03-01",
            "created": "predictions_y2526m03",
        },
    }
    assert decode_prediction_partition_reply(request, reply).created == (
        "predictions_y2526m03"
    )
    reply["unexpected"] = True
    with pytest.raises(PredictionPartitionUnavailable):
        decode_prediction_partition_reply(request, reply)

    with pytest.raises(ValidationError):
        EnsurePredictionPartition(
            schema=PREDICTION_PARTITIONS_PROTOCOL,
            operation="ensure",
            month=date(2526, 3, 2),
        )
