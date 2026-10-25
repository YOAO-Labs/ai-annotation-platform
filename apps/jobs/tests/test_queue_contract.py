from __future__ import annotations

import json
from pathlib import Path

import pytest

from aap_jobs.queue_contract import (
    JOBS_QUEUE_PROTOCOL,
    JobsQueueContractError,
    QueueDispatch,
    QueueRevoke,
    decode_queue_reply,
    decode_queue_request,
    encode_dispatch_request,
    encode_queue_reply,
    encode_revoke_request,
    load_queue_configuration,
)


def test_queue_routes_are_a_closed_shared_celery_configuration():
    root = Path(__file__).resolve().parents[3]
    actual = json.loads(
        (root / "apps/jobs/src/aap_jobs/queue_routes.json").read_text(encoding="utf-8")
    )
    assert load_queue_configuration() == actual
    assert set(actual) == {
        "task_routes",
        "task_default_queue",
        "task_serializer",
        "result_serializer",
        "accept_content",
    }
    assert actual["task_default_queue"] == "default"
    assert actual["task_serializer"] == "json"
    assert actual["result_serializer"] == "json"
    assert actual["accept_content"] == ["json"]
    assert actual["task_routes"]["app.workers.cross_frame_job.run_cross_frame_job"] == {
        "queue": "default"
    }


def test_dispatch_preserves_native_json_and_legacy_execution_ids():
    deep_value = {"value": 9007199254740993123456789}
    for _ in range(200):
        deep_value = {"nested": deep_value}
    encoded = encode_dispatch_request(
        "app.workers.cross_frame_job.run_cross_frame_job",
        " legacy-task-id ",
        ["8ccbf8e4-cce2-4eec-a15d-9f58222987a1", deep_value, [None, False, "中"]],
    )
    request = decode_queue_request(encoded)
    assert isinstance(request, QueueDispatch)
    assert request.execution_id == " legacy-task-id "
    assert request.kwargs == {}
    assert request.options == {}
    assert request.args[1] == deep_value


def test_revoke_and_reply_echo_the_exact_opaque_execution_id():
    execution_id = "legacy/task-id:with:punctuation"
    request = decode_queue_request(encode_revoke_request(execution_id))
    assert request == QueueRevoke(execution_id)
    assert (
        decode_queue_reply(
            encode_queue_reply(accepted=True, execution_id=execution_id)
        ).execution_id
        == execution_id
    )


@pytest.mark.parametrize(
    "body",
    [
        b'{"schema":"aap-jobs-queue/v1","operation":"revoke",'
        b'"execution_id":"x","extra":true}',
        b'{"schema":"aap-jobs-queue/v1","schema":"aap-jobs-queue/v1",'
        b'"operation":"revoke","execution_id":"x"}',
        b'{"schema":"aap-jobs-queue/v1","operation":"revoke","execution_id":""}',
        b'{"schema":"aap-jobs-queue/v1","operation":"revoke","execution_id":true}',
        b'{"schema":"aap-jobs-queue/v1","operation":"revoke",'
        b'"execution_id":"' + b"x" * 65 + b'"}',
        b'{"schema":"aap-jobs-queue/v1","operation":"dispatch",'
        b'"task_name":"x","execution_id":"id","args":[],"kwargs":{},'
        b'"options":{"queue":"gpu"}}',
        b'{"schema":"aap-jobs-queue/v1","operation":"dispatch",'
        b'"task_name":"x","execution_id":"id","args":[NaN],"kwargs":{},'
        b'"options":{}}',
    ],
)
def test_queue_contract_rejects_ambiguous_or_unsupported_requests(body: bytes):
    with pytest.raises(JobsQueueContractError):
        decode_queue_request(body)


def test_queue_reply_is_closed_and_uses_strict_boolean():
    with pytest.raises(JobsQueueContractError):
        decode_queue_reply(
            b'{"schema":"aap-jobs-queue/v1","accepted":1,"execution_id":"x"}'
        )
    with pytest.raises(JobsQueueContractError):
        decode_queue_reply(
            b'{"schema":"aap-jobs-queue/v1","accepted":true,'
            b'"execution_id":"x","extra":null}'
        )
    assert JOBS_QUEUE_PROTOCOL == "aap-jobs-queue/v1"


def test_local_dispatch_rejects_cyclic_python_values_without_looping():
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(JobsQueueContractError):
        encode_dispatch_request("some.task", "task-id", [cyclic])
