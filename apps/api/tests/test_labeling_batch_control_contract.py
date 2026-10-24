"""Pure checks for the closed Labeling batch-control protocol."""

import json
import uuid

import pytest

from aap_labeling.batch_control_contract import (
    BATCH_CONTROL_FUNCTION,
    BatchControlUnavailable,
    UpdateBatch,
    batch_control_command_json,
    batch_control_schema,
    decode_batch_control_command,
)


def test_batch_control_schema_is_closed_and_rejects_generic_patch_fields():
    request = UpdateBatch(
        schema="aap-labeling-batch-control/v1",
        operation="update_batch",
        batch_id=uuid.UUID("b2c3a857-11c0-4666-9076-74fa6a44ce30"),
        project_id=None,
        name_set=True,
        name="Revised batch",
        description_set=False,
        description=None,
        priority_set=False,
        priority=None,
        deadline_set=False,
        deadline=None,
        annotator_set=False,
        annotator_id=None,
        reviewer_set=False,
        reviewer_id=None,
    )
    encoded = batch_control_command_json(request)
    assert decode_batch_control_command(encoded) == request

    schema = batch_control_schema()
    assert schema["x-postgresql-functions"] == [BATCH_CONTROL_FUNCTION]
    command = schema["$defs"]["UpdateBatch"]
    assert command["additionalProperties"] is False
    from importlib.resources import files

    assert (
        json.loads(
            files("aap_labeling").joinpath("batch-control.schema.json").read_text()
        )
        == schema
    )

    with pytest.raises(BatchControlUnavailable):
        decode_batch_control_command(encoded[:-1] + ',"patch":{"status":"approved"}}')
