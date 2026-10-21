from __future__ import annotations

import json
from importlib.resources import files

from aap_preannotation_results.contract import OPERATION, SCHEMA, contract_document


SCHEMA_PATH = files("aap_preannotation_results").joinpath("protocol.schema.json")


def test_schema_snapshot_matches_public_contract_and_native_allowlist():
    document = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert document == contract_document()
    assert document["x-aap-schema"] == SCHEMA
    assert set(document["x-native-operations"]) == set(OPERATION)
    assert (
        document["x-native-operations"]["finish_batch_result_write"]["role"] == "writer"
    )
    assert "record_batch_result_member" not in document["x-native-operations"]
    assert "batch_result_referenced" not in document["x-native-operations"]
    assert not any(key in document for key in ("openapi", "paths"))
    operation_constraint = document["$defs"]["StageBatchResultMembersRequest"]["allOf"][
        0
    ]["properties"]["members"]["items"]["properties"]["write_scope"]["properties"][
        "operation"
    ]["const"]
    assert operation_constraint == "preannotation"
