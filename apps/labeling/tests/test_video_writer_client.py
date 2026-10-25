from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid

import pytest

from aap_labeling.video_writer_client import VideoWriterHttpClient
from aap_labeling.video_writer_contract import (
    LABELING_VIDEO_WRITER_PROTOCOL,
    VideoWriterExecuteReply,
    VideoWriterReceipt,
    VideoWriterRejected,
    VideoWriterStatusReply,
    make_video_writer_command,
    make_video_writer_status_request,
    video_writer_command_bytes,
    video_writer_command_digest,
    video_writer_operation_id,
    video_writer_status_request_bytes,
)
from aap_preannotation_results.video_execution import ExecutionProof


def command():
    proof = ExecutionProof(
        job_id=uuid.UUID(int=31),
        run_id=uuid.UUID(int=32),
        worker_id=uuid.UUID(int=33),
        generation=4,
        lease_token=uuid.UUID(int=34),
    )
    return make_video_writer_command(
        proof,
        outcome="failed",
        expected_input=None,
        frame_results=[],
        error_message="inference failed",
    )


def receipt_for(value):
    return VideoWriterReceipt(
        operation_id=video_writer_operation_id(value.execution.run_id),
        job_id=value.execution.job_id,
        run_id=value.execution.run_id,
        request_digest=video_writer_command_digest(value),
        outcome="failed",
        status="failed",
        completed_at=datetime.now(timezone.utc),
        staged_sha256=None,
        staged_byte_size=0,
    )


def json_body(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode()


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body
        self.headers = {
            "content-type": "application/json",
            "content-encoding": "identity",
            "content-length": str(len(body)),
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def aiter_bytes(self):
        yield self.body


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def stream(self, method, url, *, content, headers):
        self.calls.append((method, url, content, headers))
        return FakeResponse(*self.handler(url, content))


def execute_reply(value, receipt=None):
    reply = VideoWriterExecuteReply(
        schema=LABELING_VIDEO_WRITER_PROTOCOL,
        receipt=receipt or receipt_for(value),
    )
    return json_body(reply.model_dump(mode="json", by_alias=True))


def status_reply(receipt):
    reply = VideoWriterStatusReply(
        schema=LABELING_VIDEO_WRITER_PROTOCOL,
        receipt=receipt,
    )
    return json_body(reply.model_dump(mode="json", by_alias=True))


@pytest.mark.asyncio
async def test_ambiguous_execute_recovers_receipt_using_same_proof_and_digest():
    value = command()
    receipt = receipt_for(value)
    client = FakeClient(
        lambda url, body: (
            (503, b"unavailable")
            if url.endswith("/execute")
            else (200, status_reply(receipt))
        )
    )
    writer = VideoWriterHttpClient("http://labeling-writer", "v" * 40, client=client)

    result = await writer.execute(value)

    assert result == receipt
    assert len(client.calls) == 2
    execute_call, status_call = client.calls
    assert execute_call[2] == video_writer_command_bytes(value)
    expected_status = make_video_writer_status_request(
        value.operation_id,
        value.execution,
        video_writer_command_digest(value),
    )
    assert status_call[2] == video_writer_status_request_bytes(expected_status)


@pytest.mark.asyncio
async def test_empty_status_retries_only_the_exact_same_serialized_command():
    value = command()
    expected_receipt = receipt_for(value)
    calls = []

    def handler(url, body):
        calls.append((url, body))
        if (
            url.endswith("/execute")
            and sum(path.endswith("/execute") for path, _ in calls) == 1
        ):
            return 503, b"uncertain"
        if url.endswith("/status"):
            return 200, status_reply(None)
        return 200, execute_reply(value, expected_receipt)

    client = FakeClient(handler)
    writer = VideoWriterHttpClient("http://labeling-writer", "v" * 40, client=client)

    result = await writer.execute(value)

    assert result == expected_receipt
    execute_bodies = [body for path, body in calls if path.endswith("/execute")]
    assert len(execute_bodies) == 2
    assert execute_bodies == [video_writer_command_bytes(value)] * 2
    assert [path.rsplit("/", 1)[-1] for path, _ in calls] == [
        "execute",
        "status",
        "execute",
    ]


@pytest.mark.asyncio
async def test_deterministic_owner_rejection_does_not_retry_or_query_status():
    value = command()
    client = FakeClient(
        lambda _url, _body: (
            409,
            json_body(
                {
                    "schema": LABELING_VIDEO_WRITER_PROTOCOL,
                    "error": {"code": "operation_conflict"},
                }
            ),
        )
    )
    writer = VideoWriterHttpClient("http://labeling-writer", "v" * 40, client=client)

    with pytest.raises(VideoWriterRejected, match="operation_conflict"):
        await writer.execute(value)
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_publication_lookup_has_no_replay_digest_and_binds_current_run():
    value = command()
    receipt = receipt_for(value)

    def respond(_url, raw):
        request = json.loads(raw)
        assert set(request) == {"schema", "execution"}
        assert request["execution"] == value.execution.model_dump(mode="json")
        return 200, json_body(
            {
                "schema": LABELING_VIDEO_WRITER_PROTOCOL,
                "receipt": receipt.model_dump(mode="json"),
            }
        )

    fake = FakeClient(respond)
    async with VideoWriterHttpClient("http://writer", "t" * 40, client=fake) as peer:
        assert await peer.publication(value.execution) == receipt
