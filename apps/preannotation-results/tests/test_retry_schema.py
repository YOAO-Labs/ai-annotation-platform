from __future__ import annotations

import json
from importlib.resources import files

from aap_preannotation_results.retry_contract import (
    OPERATION,
    SCHEMA,
    contract_document,
)


SCHEMA_PATH = files("aap_preannotation_results").joinpath(
    "prediction-retries.schema.json"
)


def test_retry_schema_snapshot_matches_public_contract_and_native_allowlist():
    document = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert document == contract_document()
    assert document["x-aap-schema"] == SCHEMA
    assert set(document["x-native-operations"]) == set(OPERATION)
    assert set(document["x-native-operations"]) == {
        "admit_prediction_retry",
        "read_prediction_retry",
        "finish_prediction_retry_write",
        "finish_prediction_retry_failure",
        "claim_prediction_retry_deliveries",
        "finish_prediction_retry_delivery",
    }
    assert document["x-native-operations"]["admit_prediction_retry"]["role"] == "writer"
    assert (
        document["x-native-operations"]["claim_prediction_retry_deliveries"]["role"]
        == "executor"
    )
    assert not any(key in document for key in ("openapi", "paths"))
    assert (
        document["$defs"]["RetryAttempt"]["properties"]["write_scope"]["properties"][
            "operation"
        ]["const"]
        == "prediction_retry"
    )
