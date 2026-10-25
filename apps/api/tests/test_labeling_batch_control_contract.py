"""Pure checks for the closed Labeling batch-control protocol."""

import json
import uuid

import pytest

from aap_labeling.batch_control_contract import (
    BATCH_CONTROL_DATA_SCHEMA,
    BATCH_CONTROL_FUNCTION,
    BATCH_CONTROL_PROTOCOL,
    BatchControlFailure,
    BatchControlUnavailable,
    PrepareBatchCleanup,
    UpdateBatch,
    batch_control_command_json,
    batch_control_schema,
    decode_batch_control_command,
    decode_batch_control_reply,
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


@pytest.mark.parametrize("ok", [True, False])
def test_batch_reply_uses_the_native_data_schema(ok):
    request = PrepareBatchCleanup(
        schema=BATCH_CONTROL_PROTOCOL,
        operation="prepare_batch_cleanup",
        batch_id=uuid.uuid4(),
        tracked_task_ids=[],
        protect_task_work=False,
        expected_non_pending=None,
        expected_predicted=None,
    )
    reply = {
        "schema": BATCH_CONTROL_DATA_SCHEMA,
        "ok": ok,
        "request": json.loads(batch_control_command_json(request)),
    }
    if ok:
        reply["data"] = {"batch_id": str(request.batch_id), "total_tasks": 0}
        result = decode_batch_control_reply(request, reply)
        assert result.batch_id == request.batch_id and result.total_tasks == 0
    else:
        reply["error"] = {"code": "batch_not_found"}
        with pytest.raises(BatchControlFailure) as failure:
            decode_batch_control_reply(request, reply)
        assert failure.value.code == "batch_not_found"
    reply["schema"] = BATCH_CONTROL_PROTOCOL
    with pytest.raises(BatchControlUnavailable):
        decode_batch_control_reply(request, reply)
