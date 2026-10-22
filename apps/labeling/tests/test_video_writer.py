"""Transaction ordering and replay behavior for the video writer manager."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

import pytest

import aap_labeling.video_writer as writer

from aap_labeling.video_writer_contract import (
    LABELING_VIDEO_WRITER_DATA_SCHEMA,
    VideoWriterCommand,
    VideoWriterFacts,
    VideoWriterLockMasksData,
    VideoWriterPrepareData,
    VideoWriterReceipt,
    VideoWriterReceiptData,
    VideoWriterRejected,
    VideoWriterUnavailable,
    make_video_writer_command,
    video_writer_command_digest,
    video_writer_operation_id,
)
from aap_preannotation_results.video_execution import ExecutionProof


_JOB_ID = UUID("00000000-0000-0000-0000-0000000003e9")
_RUN_ID = UUID("00000000-0000-0000-0000-0000000003f1")
_INPUT = {
    "input_id": "00000000-0000-0000-0000-0000000003ee",
    "request_digest": "a" * 64,
    "plan_digest": "c" * 64,
    "source_version_id": "fixture-version-1",
    "source_sha256": "e" * 64,
}
_MASK_REFERENCE = {
    "encoding": "coco_rle_ref",
    "size": [2, 2],
    "object_key": (
        "raster-masks/sha256/65/33/"
        "6533cda72e36d657c93de74e24c1e7e8e3c0498910feef206fe96ccea37613f5.json"
    ),
    "sha256": "6533cda72e36d657c93de74e24c1e7e8e3c0498910feef206fe96ccea37613f5",
    "runs": 2,
    "bytes": 51,
}


def _proof(job_id: UUID) -> ExecutionProof:
    return ExecutionProof(
        job_id=job_id,
        run_id=_RUN_ID,
        worker_id=UUID("00000000-0000-0000-0000-0000000003f2"),
        generation=1,
        lease_token=UUID("00000000-0000-0000-0000-0000000003f3"),
    )


def _command() -> VideoWriterCommand:
    return make_video_writer_command(
        _proof(_JOB_ID),
        outcome="completed",
        expected_input=_INPUT,
        frame_results=[],
    )


def _receipt(command: VideoWriterCommand, digest: str) -> VideoWriterReceipt:
    return VideoWriterReceipt(
        operation_id=command.operation_id,
        job_id=command.execution.job_id,
        run_id=command.execution.run_id,
        request_digest=digest,
        outcome="completed",
        status="pending_review",
        completed_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
        staged_sha256=None,
        staged_byte_size=0,
    )


class _FakeTransaction:
    def __init__(self, respond):
        self.respond = respond
        self.actions = []

    async def call(self, action, arguments):
        self.actions.append((action, arguments))
        return self.respond(action, arguments)


class _FakeData:
    def __init__(self, respond):
        self.respond = respond
        self.transaction_port = None
        self.committed = False
        self.rolled_back = False

    @asynccontextmanager
    async def transaction(self):
        self.transaction_port = _FakeTransaction(self.respond)
        try:
            yield self.transaction_port
        except BaseException:
            self.rolled_back = True
            raise
        else:
            self.committed = True


@pytest.mark.asyncio
async def test_matching_receipt_short_circuits_without_reapplying():
    command = _command()
    digest = video_writer_command_digest(command)
    receipt = _receipt(command, digest)

    def respond(action, _arguments):
        assert action == "prepare"
        return {
            "schema": LABELING_VIDEO_WRITER_DATA_SCHEMA,
            "ok": True,
            "data": {
                "receipt": receipt.model_dump(mode="json", by_alias=True),
                "facts": None,
            },
        }

    data = _FakeData(respond)
    result = await writer.VideoWriterManager(data).execute(command)
    assert result == receipt
    assert [action for action, _ in data.transaction_port.actions] == ["prepare"]
    assert data.committed
    assert not data.rolled_back


@pytest.mark.asyncio
async def test_invalid_native_reply_rolls_back_before_any_later_action():
    command = _command()
    data = _FakeData(
        lambda _action, _arguments: {
            "schema": "wrong-data-protocol",
            "ok": True,
            "data": {},
        }
    )
    with pytest.raises(VideoWriterUnavailable):
        await writer.VideoWriterManager(data).execute(command)
    assert [action for action, _ in data.transaction_port.actions] == ["prepare"]
    assert data.rolled_back
    assert not data.committed


@pytest.mark.asyncio
async def test_mask_verification_runs_between_lock_and_apply(monkeypatch):
    command = _command()
    digest = video_writer_command_digest(command)
    receipt = _receipt(command, digest)
    facts = VideoWriterFacts.model_construct(
        job=type("Job", (), {"job_id": _JOB_ID, "job_kind": "tracking"})(),
        effective_outcome="completed",
        acceptance=None,
        execution_plan=None,
    )
    staged = {
        "results": [
            {
                "geometry": {
                    "type": "mask",
                    "mask": _MASK_REFERENCE,
                }
            }
        ],
        "grid_step": 1,
        "output_geometry": "mask",
        "source_instance_map": {},
        "tracker_input_evidence": {},
    }
    prepare = VideoWriterPrepareData.model_construct(receipt=None, facts=facts)
    lock = VideoWriterLockMasksData(
        epochs={
            "raster-masks/sha256/65/33/6533cda72e36d657c93de74e24c1e7e8e3c0498910feef206fe96ccea37613f5.json": 0
        }
    )
    apply = VideoWriterReceiptData(receipt=receipt)
    decoded = {"prepare": prepare, "lock_masks": lock, "apply": apply}
    events = []

    def fake_decode(action, _arguments, _raw):
        events.append(action)
        return decoded[action]

    monkeypatch.setattr(writer, "decode_video_writer_data_reply", fake_decode)
    monkeypatch.setattr(
        writer, "_validate_facts_for_command", lambda _command, value: value
    )
    monkeypatch.setattr(writer, "_staged_result", lambda *_args: staged)

    async def verify(value):
        assert value is staged
        events.append("verify_masks")

    data = _FakeData(lambda action, _arguments: {"ignored": action})
    result = await writer.VideoWriterManager(data, verify_masks=verify).execute(command)
    assert result == receipt
    assert events == ["prepare", "lock_masks", "verify_masks", "apply"]
    assert [action for action, _ in data.transaction_port.actions] == [
        "prepare",
        "lock_masks",
        "apply",
    ]


@pytest.mark.asyncio
async def test_permanent_mask_verification_error_prevents_apply(monkeypatch):
    command = _command()
    facts = VideoWriterFacts.model_construct(
        job=type("Job", (), {"job_id": _JOB_ID, "job_kind": "tracking"})(),
        effective_outcome="completed",
        acceptance=None,
        execution_plan=None,
    )
    staged = {
        "results": [{"geometry": {"type": "mask", "mask": _MASK_REFERENCE}}],
    }
    decoded = {
        "prepare": VideoWriterPrepareData.model_construct(receipt=None, facts=facts),
        "lock_masks": VideoWriterLockMasksData(epochs={}),
    }
    monkeypatch.setattr(
        writer,
        "decode_video_writer_data_reply",
        lambda action, _arguments, _raw: decoded[action],
    )
    monkeypatch.setattr(
        writer, "_validate_facts_for_command", lambda _command, value: value
    )
    monkeypatch.setattr(writer, "_staged_result", lambda *_args: staged)

    async def reject(_value):
        raise VideoWriterRejected("mask_content_invalid")

    data = _FakeData(lambda action, _arguments: {"ignored": action})
    with pytest.raises(VideoWriterRejected) as raised:
        await writer.VideoWriterManager(data, verify_masks=reject).execute(command)
    assert raised.value.code == "mask_content_invalid"
    assert [action for action, _ in data.transaction_port.actions] == [
        "prepare",
        "lock_masks",
    ]
    assert data.rolled_back
    assert not data.committed


def test_stable_operation_id_is_bound_to_the_claim_run():
    command = _command()
    assert command.operation_id == video_writer_operation_id(_RUN_ID)
    assert command.execution == _proof(_JOB_ID)


@pytest.mark.asyncio
async def test_status_reads_committed_receipt_through_closed_native_arguments():
    command = _command()
    digest = video_writer_command_digest(command)
    receipt = _receipt(command, digest)

    def respond(action, arguments):
        assert action == "status"
        assert set(arguments.model_dump()) == {
            "operation_id",
            "execution",
            "request_digest",
        }
        return {
            "schema": LABELING_VIDEO_WRITER_DATA_SCHEMA,
            "ok": True,
            "data": {"receipt": receipt.model_dump(mode="json")},
        }

    data = _FakeData(respond)
    assert (
        await writer.VideoWriterManager(data).status(
            command.operation_id, command.execution, digest
        )
        == receipt
    )
    assert data.committed
