"""Configured finalization never falls back or reposts inference on uncertainty."""

from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aap_labeling.video_writer_contract import (
    VideoWriterReceipt,
    VideoWriterRejected,
    VideoWriterUnavailable,
    video_writer_command_digest,
)
from aap_preannotation_results.video_execution import ExecutionProof
from app.services.video_tracking import writer
from app.services.video_tracking.execution import (
    TrackerExecutionDeferred,
    TrackerExecutionLost,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def writer_scope(monkeypatch):
    for field in (
        "url",
        "database_url",
        "database_url_file",
        "control_token",
        "control_token_file",
    ):
        monkeypatch.setattr(writer.settings, "labeling_video_writer_" + field, "")
    proof = ExecutionProof(
        job_id=uuid4(),
        run_id=uuid4(),
        worker_id=uuid4(),
        generation=3,
        lease_token=uuid4(),
    )
    monkeypatch.setattr(writer, "execution_proof", lambda _job: proof)
    monkeypatch.setattr(writer.asyncio, "sleep", AsyncMock())
    db = SimpleNamespace(
        commit=AsyncMock(),
        rollback=AsyncMock(),
        no_autoflush=nullcontext(),
        scalar=AsyncMock(),
        connection=AsyncMock(),
    )
    return db, proof


def receipt(command):
    return VideoWriterReceipt(
        operation_id=command.operation_id,
        job_id=command.execution.job_id,
        run_id=command.execution.run_id,
        request_digest=video_writer_command_digest(command),
        outcome="failed",
        status="failed",
        completed_at=datetime.now(timezone.utc),
        staged_sha256=None,
        staged_byte_size=0,
    )


async def test_conflicting_explicit_writer_selectors_do_not_touch_caller_transaction(
    monkeypatch, writer_scope
):
    db, proof = writer_scope
    monkeypatch.setattr(
        writer.settings, "labeling_video_writer_url", "http://owner.test"
    )
    monkeypatch.setattr(
        writer.settings, "labeling_video_writer_database_url", "postgresql://private"
    )
    with pytest.raises(TrackerExecutionDeferred, match="video_writer_unavailable"):
        await writer.commit_tracker_outcome(
            db, proof.job_id, "failed", error_message="fixture"
        )
    db.commit.assert_not_awaited()
    db.connection.assert_not_awaited()


async def test_explicit_http_writer_outage_never_uses_borrowed_native_fallback(
    monkeypatch, writer_scope
):
    db, proof = writer_scope
    monkeypatch.setattr(
        writer.settings, "labeling_video_writer_url", "http://owner.test"
    )
    monkeypatch.setattr(
        writer.settings, "labeling_video_writer_control_token", "t" * 32
    )
    calls = []

    class UnavailableClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def execute(self, command):
            calls.append(video_writer_command_digest(command))
            raise VideoWriterUnavailable()

    monkeypatch.setattr(writer, "VideoWriterHttpClient", UnavailableClient)
    with pytest.raises(
        TrackerExecutionDeferred, match="video_writer_unavailable"
    ) as failure:
        await writer.commit_tracker_outcome(
            db, proof.job_id, "failed", error_message="fixture"
        )
    assert len(calls) == 3 and len(set(calls)) == 1
    assert str(proof.lease_token) not in str(failure.value)
    db.connection.assert_not_awaited()
    db.scalar.assert_not_awaited()


async def test_repeated_writer_contention_keeps_exact_held_command(
    monkeypatch, writer_scope
):
    db, proof = writer_scope
    calls = []

    class BusyManager:
        def __init__(self, *_args, **_kwargs):
            pass

        async def execute(self, command):
            calls.append(command)
            if len(calls) <= 12:
                raise VideoWriterRejected("execution_busy")
            return receipt(command)

    monkeypatch.setattr(writer, "VideoWriterManager", BusyManager)
    result = await writer.commit_tracker_outcome(
        db, proof.job_id, "failed", error_message="fixture"
    )
    assert result.status == "failed" and len(calls) == 13
    assert all(command is calls[0] for command in calls)
    assert len({video_writer_command_digest(command) for command in calls}) == 1


async def test_ambiguous_caller_commit_reads_receipt_without_reapplying(
    monkeypatch, writer_scope
):
    db, proof = writer_scope
    commands, status_reads = [], []
    saved = None
    db.commit.side_effect = [None, OSError("test: commit reply lost"), None]

    class ReceiptManager:
        def __init__(self, *_args, **_kwargs):
            pass

        async def execute(self, command):
            nonlocal saved
            commands.append(command)
            saved = receipt(command)
            return saved

        async def status(self, operation_id, execution, digest):
            status_reads.append((operation_id, execution, digest))
            return saved

    monkeypatch.setattr(writer, "VideoWriterManager", ReceiptManager)
    result = await writer.commit_tracker_outcome(
        db, proof.job_id, "failed", error_message="fixture"
    )
    assert result == saved and len(commands) == len(status_reads) == 1
    assert status_reads[0] == (commands[0].operation_id, proof, saved.request_digest)
    db.rollback.assert_awaited_once()


async def test_held_writer_stops_retrying_when_its_claim_is_lost(
    monkeypatch, writer_scope
):
    db, proof = writer_scope
    calls = []
    current = True

    def current_proof(_job):
        if not current:
            raise TrackerExecutionLost()
        return proof

    async def lose_claim(_delay):
        nonlocal current
        current = False

    class BusyManager:
        def __init__(self, *_args, **_kwargs):
            pass

        async def execute(self, command):
            calls.append(command)
            raise VideoWriterRejected("execution_busy")

    monkeypatch.setattr(writer, "VideoWriterManager", BusyManager)
    monkeypatch.setattr(writer, "execution_proof", current_proof)
    monkeypatch.setattr(writer.asyncio, "sleep", lose_claim)
    with pytest.raises(TrackerExecutionLost):
        await writer.commit_tracker_outcome(
            db, proof.job_id, "failed", error_message="fixture"
        )
    assert len(calls) == 1


async def test_each_distinct_mask_reference_is_verified_even_when_object_keys_match(
    monkeypatch,
):
    key = "raster-masks/sha256/" + "a" * 2 + "/" + "a" * 2 + "/" + "a" * 64 + ".json"
    first = {
        "encoding": "coco_rle_ref",
        "size": [2, 2],
        "object_key": key,
        "sha256": "a" * 64,
        "runs": 2,
        "bytes": 51,
    }
    second = {**first, "bytes": 52}
    load = AsyncMock()
    monkeypatch.setattr(writer, "load_coco_rle", load)
    await writer._verify_candidate_masks(
        {"results": [{"mask": first}, {"mask": second}]}
    )
    assert load.await_count == 2
    assert {call.args[0]["bytes"] for call in load.await_args_list} == {51, 52}
