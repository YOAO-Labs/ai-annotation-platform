from __future__ import annotations

import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx

from aap_jobs.queue_contract import load_queue_configuration
from app.schemas.video_tracker_job import VideoTrackerPropagateRequest
from tests.jobs_video_tracker_module_helpers import (
    _VIDEO_TRACKER_TASK_NAME,
    _OwnedPeerLoopbackTransport,
    _assert_tracker_envelope,
    _tracker_request,
    _unpack_tracker_envelope,
    _wait_task_delivery,
)
from app.services.video_tracking.execution import TrackerExecutionCancelled


def test_video_tracker_queue_contract_fixture():
    task_name = _VIDEO_TRACKER_TASK_NAME
    job_id = "4de18a66-70b8-46bc-a0f0-7e95f8c6d9e0"
    execution_id = "00000000-0000-4000-8000-000000000001"
    envelope = {
        "headers": {"task": task_name, "id": execution_id},
        "properties": {"delivery_info": {"exchange": "", "routing_key": "gpu"}},
        "body": base64.b64encode(
            json.dumps([[job_id], {}, None], separators=(",", ":")).encode()
        ).decode(),
    }

    message = _unpack_tracker_envelope(envelope)
    assert _assert_tracker_envelope(message, job_id) == execution_id
    assert load_queue_configuration()["task_routes"][task_name] == {"queue": "gpu"}
    request = VideoTrackerPropagateRequest.model_validate(_tracker_request())
    assert request.from_frame == 0
    assert request.to_frame == 5
    assert request.model_key == "sam2_video"
    assert request.direction == "forward"
    assert request.target_class_name == "car"
    assert request.target_tool_unit_id == "bbox"
    assert request.text == "car"
    assert request.output_geometry == "bbox"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,result,cancelled,accepted",
    [
        ("SUCCESS", None, False, True),
        ("FAILURE", TrackerExecutionCancelled(), True, True),
        ("FAILURE", RuntimeError("unexpected worker failure"), True, False),
        ("SUCCESS", None, True, False),
        ("FAILURE", TrackerExecutionCancelled(), False, False),
    ],
)
async def test_tracker_delivery_fixture_checks_cancellation_type(
    monkeypatch, state, result, cancelled, accepted
):
    delivery = SimpleNamespace(state=state, get=lambda **_kwargs: result)
    sender = SimpleNamespace(AsyncResult=lambda _execution_id: delivery)
    ack = AsyncMock(return_value={"acknowledged": True})
    failure_ack = AsyncMock(return_value={"acknowledged": True, "state": "FAILURE"})
    monkeypatch.setattr(
        "tests.test_workspace_worker_assembly_live._wait_broker_ack", ack
    )
    monkeypatch.setattr(
        "tests.test_workspace_export_worker_live._wait_failure_delivery_ack",
        failure_ack,
    )
    worker = object()
    if accepted:
        observed = await _wait_task_delivery(
            sender, worker, "execution-id", cancelled=cancelled
        )
        assert observed["state"] == state
        assert observed["result_type"] == type(result).__name__
        selected, other = (failure_ack, ack) if cancelled else (ack, failure_ack)
        selected.assert_awaited_once_with(sender, worker, "execution-id")
        other.assert_not_awaited()
    else:
        with pytest.raises(AssertionError):
            await _wait_task_delivery(
                sender, worker, "execution-id", cancelled=cancelled
            )
        ack.assert_not_awaited()
        failure_ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_operator_transport_reaches_only_its_owned_peer():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"owned tracker control peer")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_port}"
        async with httpx.AsyncClient(
            transport=_OwnedPeerLoopbackTransport(origin), trust_env=False, timeout=2
        ) as client:
            reply = await client.get(
                f"http://host.docker.internal:{server.server_port}/"
            )
            assert reply.status_code == 200
            assert reply.content == b"owned tracker control peer"
            for unsafe in (
                f"http://unowned.invalid:{server.server_port}/",
                "http://host.docker.internal:1/",
            ):
                with pytest.raises(AssertionError):
                    await client.get(unsafe)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
