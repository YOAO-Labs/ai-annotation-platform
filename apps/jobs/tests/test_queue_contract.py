from __future__ import annotations


import pytest

from aap_jobs.queue_contract import (
    JOBS_QUEUE_PROTOCOL,
    JobsQueueContractError,
    QueueCanvas,
    QueueDispatch,
    QueueRevoke,
    QueueTaskSpec,
    decode_queue_reply,
    decode_queue_request,
    encode_canvas_request,
    encode_dispatch_request,
    encode_queue_reply,
    encode_revoke_request,
)


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
        b'{"schema":"aap-jobs-queue/v2","operation":"revoke",'
        b'"execution_id":"x","extra":true}',
        b'{"schema":"aap-jobs-queue/v2","schema":"aap-jobs-queue/v2",'
        b'"operation":"revoke","execution_id":"x"}',
        b'{"schema":"aap-jobs-queue/v2","operation":"revoke","execution_id":""}',
        b'{"schema":"aap-jobs-queue/v2","operation":"revoke","execution_id":true}',
        b'{"schema":"aap-jobs-queue/v2","operation":"revoke",'
        b'"execution_id":"' + b"x" * 65 + b'"}',
        b'{"schema":"aap-jobs-queue/v2","operation":"dispatch",'
        b'"task_name":"x","execution_id":"id","args":[],"kwargs":{},'
        b'"options":{"unsupported":true}}',
        b'{"schema":"aap-jobs-queue/v2","operation":"dispatch",'
        b'"task_name":"x","execution_id":"id","args":[NaN],"kwargs":{},'
        b'"options":{}}',
    ],
)
def test_queue_contract_rejects_ambiguous_or_unsupported_requests(body: bytes):
    with pytest.raises(JobsQueueContractError):
        decode_queue_request(body)


def test_dispatch_accepts_only_the_supported_task_options():
    request = decode_queue_request(
        encode_dispatch_request(
            "some.task",
            "stable-task-id",
            [],
            options={"queue": "gpu", "soft_time_limit": 3.5},
        )
    )
    assert isinstance(request, QueueDispatch)
    assert request.options == {"queue": "gpu", "soft_time_limit": 3.5}
    for options in (
        {"unsupported": True},
        {"queue": ""},
        {"soft_time_limit": 0},
        {"soft_time_limit": -1},
        {"soft_time_limit": True},
        {"soft_time_limit": float("inf")},
        {"soft_time_limit": float("nan")},
    ):
        with pytest.raises(JobsQueueContractError):
            encode_dispatch_request("some.task", "stable-task-id", [], options=options)


def test_flat_canvas_preserves_stable_ids_name_only_specs_and_segment_limit():
    task = {
        "task_name": "app.workers.frame_preannotate.predict_video_segment",
        "execution_id": "segment-task-id",
        "args": ["job-id", 0, 10],
        "kwargs": {},
        "options": {"queue": "ml.cpu", "soft_time_limit": 900},
    }
    callback = {
        "task_name": "app.workers.frame_preannotate.launch_predict_phase",
        "execution_id": "callback-job-id",
        "args": ["job-id"],
        "kwargs": {},
        "options": {},
    }
    request = decode_queue_request(
        encode_canvas_request(
            "callback-job-id",
            "stable-group-id",
            [task],
            callback,
        )
    )
    assert isinstance(request, QueueCanvas)
    assert request.execution_id == request.callback.execution_id
    assert request.group_id == "stable-group-id"
    assert request.tasks == [
        QueueTaskSpec(
            task["task_name"],
            task["execution_id"],
            task["args"],
            task["kwargs"],
            task["options"],
        )
    ]
    assert request.callback.task_name == callback["task_name"]
    assert request.tasks[0].options["soft_time_limit"] == 900


def test_canvas_rejects_callback_id_mismatch_nested_fields_and_duplicate_ids():
    task = {
        "task_name": "some.task",
        "execution_id": "child-id",
        "args": [],
        "kwargs": {},
        "options": {},
    }
    callback = {
        "task_name": "some.callback",
        "execution_id": "callback-id",
        "args": [],
        "kwargs": {},
        "options": {},
    }
    with pytest.raises(JobsQueueContractError):
        encode_canvas_request("other-id", "group-id", [task], callback)
    with pytest.raises(JobsQueueContractError):
        encode_canvas_request("callback-id", "group-id", [task, task], callback)
    with pytest.raises(JobsQueueContractError):
        encode_canvas_request(
            "callback-id",
            "group-id",
            [{**task, "canvas": []}],
            callback,
        )


def test_queue_v1_requests_are_not_accepted():
    with pytest.raises(JobsQueueContractError):
        decode_queue_request(
            b'{"schema":"aap-jobs-queue/v1","operation":"revoke",'
            b'"execution_id":"legacy-id"}'
        )


def test_queue_reply_is_closed_and_uses_strict_boolean():
    with pytest.raises(JobsQueueContractError):
        decode_queue_reply(
            b'{"schema":"aap-jobs-queue/v2","accepted":1,"execution_id":"x"}'
        )
    with pytest.raises(JobsQueueContractError):
        decode_queue_reply(
            b'{"schema":"aap-jobs-queue/v2","accepted":true,'
            b'"execution_id":"x","extra":null}'
        )
    assert JOBS_QUEUE_PROTOCOL == "aap-jobs-queue/v2"


def test_local_dispatch_rejects_cyclic_python_values_without_looping():
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(JobsQueueContractError):
        encode_dispatch_request("some.task", "task-id", [cyclic])
