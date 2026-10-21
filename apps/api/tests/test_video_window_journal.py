from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aap_preannotation_results import contract as c
from aap_preannotation_results.video_execution import ExecutionProof
from aap_preannotation_results.video_execution_journal import ReadRequest, SCHEMA
from aap_preannotation_results.video_execution_plan import VideoContinuationPostState
from aap_preannotation_results.video_execution_planner import (
    VideoExecutionInputFacts,
    build_video_execution_plan,
    freeze_video_window_settings,
)
from aap_preannotation_results.video_window_checkpoint import (
    make_video_window_checkpoint_payload,
    snapshot_video_window_checkpoint,
)
from app.services.video_tracking.backend_identity import (
    build_video_backend_compatibility,
)
from app.services.video_tracking import journal as bridge

pytestmark = pytest.mark.asyncio


def _artifact(monkeypatch):
    proof = ExecutionProof(
        job_id=uuid4(),
        run_id=uuid4(),
        worker_id=uuid4(),
        generation=1,
        lease_token=uuid4(),
    )
    monkeypatch.setattr(bridge, "execution_proof", lambda job_id: proof)
    facts = VideoExecutionInputFacts(
        **{key: uuid4() for key in ("input_id", "task_id", "project_id")},
        job_id=proof.job_id,
        source_item_id=None,
        request_digest="a" * 64,
        prompt_digest="b" * 64,
        source_plan_digest="c" * 64,
        requested_model_key="mock_bbox",
        job_kind="tracking",
        direction="forward",
        from_frame=0,
        to_frame=0,
        grid_step=1,
        correction_frame=None,
        segment_id=None,
        source=dict(
            source_version_id="version",
            source_sha256="d" * 64,
            byte_size=10,
            frame_count=1,
            width=2,
            height=2,
            duration_ms=1,
            video_stream=0,
        ),
        timebase=dict(
            timebase_digest="e" * 64,
            selected_frame_count=1,
            sampling_step=1,
            grid_step=1,
        ),
    )
    backend = build_video_backend_compatibility(
        model_key="mock_bbox", selected_backend=None
    )
    settings = freeze_video_window_settings(
        requested_model_key="mock_bbox",
        platform_default_frames=1,
        platform_sam3_frames=1,
        backend_compatibility=backend,
    )
    plan = build_video_execution_plan(
        job_id=proof.job_id,
        facts=facts,
        window_settings=settings,
        backend_compatibility=backend,
        output_geometry="bbox",
        initial_seed_source="none",
        initial_seed_digest=None,
    )
    geometry = dict(type="bbox", x=0.1, y=0.2, w=0.3, h=0.4)

    def payload(raw="1" * 64, geom=None):
        selected = geom or geometry
        return make_video_window_checkpoint_payload(
            plan=plan,
            window_ordinal=0,
            raw_response_digest=raw,
            frame_results=[
                dict(
                    frame_index=0,
                    geometry=selected,
                    confidence=1.0,
                    outside=False,
                    instance_id=None,
                    primary=False,
                )
            ],
            continuation=VideoContinuationPostState(
                last_geometry=selected,
                last_geom_by_instance={},
                mp_prev_boundary={},
                mp_next_global=1,
            ),
        )

    return proof, plan, payload


async def test_held_window_survives_contention_and_ambiguous_seal_commit(monkeypatch):
    proof, plan, payload = _artifact(monkeypatch)
    db = SimpleNamespace(
        no_autoflush=nullcontext(), commit=AsyncMock(), rollback=AsyncMock()
    )
    journal = bridge.TrackerWindowJournal(db, proof.job_id)
    journal.plan = plan
    capture_id, actions, captures = uuid4(), [], []
    part_busy, seal_commit_lost = True, True
    current_action = None

    async def scalar(statement, parameters):
        nonlocal part_busy, current_action
        action = current_action = parameters["action"]
        args = c.parse_json(parameters["arguments"].encode())
        actions.append((action, args))
        result = dict(schema=SCHEMA, status="ok")
        if action == "capture_window":
            captures.append(args["manifest"])
            result["capture"] = dict(
                capture_id=str(capture_id),
                window_ordinal=0,
                state="receiving",
                manifest=captures[-1],
            )
        elif action == "put_part":
            if part_busy:
                part_busy = False
                return dict(schema=SCHEMA, status="rejected", reason="execution_busy")
            manifest = captures[-1]
            result["part"] = dict(
                capture_id=str(capture_id),
                checkpoint_id=manifest["checkpoint_id"],
                manifest_digest=manifest["manifest_digest"],
                part_index=args["index"],
                part_count=manifest["payload_part_count"],
                payload_digest=manifest["payload_digest"],
                payload_byte_size=manifest["payload_byte_size"],
                part_digest=args["sha256"],
            )
        elif action == "seal_window":
            result["window"] = dict(
                capture_id=str(capture_id),
                ordinal=0,
                checkpoint_id=captures[-1]["checkpoint_id"],
                manifest=captures[-1],
            )
        else:
            raise AssertionError(action)
        return result

    async def commit():
        nonlocal seal_commit_lost
        if current_action == "seal_window" and seal_commit_lost:
            seal_commit_lost = False
            raise OSError("ambiguous commit")

    db.scalar = scalar
    db.commit.side_effect = commit
    monkeypatch.setattr(bridge.asyncio, "sleep", AsyncMock())
    held = payload()
    saved = await journal.save(
        plan.windows[0], SimpleNamespace(mode="execute", capture_id=capture_id), held
    )
    assert saved is held
    assert [action for action, _ in actions] == [
        "capture_window",
        "put_part",
        "put_part",
        "seal_window",
        "seal_window",
    ]
    assert actions[1][1] == actions[2][1]
    assert actions[3][1] == actions[4][1]
    assert len(captures) == 1
    assert journal.manifests[0].raw_response_digest == held.raw_response_digest
    assert db.rollback.await_count == 2


async def test_replay_retains_original_bytes_and_only_records_observation(monkeypatch):
    proof, plan, payload = _artifact(monkeypatch)
    journal = bridge.TrackerWindowJournal(object(), proof.job_id)
    journal.plan = plan
    original = payload()
    with snapshot_video_window_checkpoint(plan, original) as snapshot:
        manifest = snapshot.manifest
    journal.manifests[0] = manifest
    journal.load = AsyncMock(return_value=original)
    journal.committed = AsyncMock(return_value={"verified": True})
    attempt = SimpleNamespace(mode="replay", capture_id=uuid4(), checkpoint=manifest)
    assert (
        await journal.save(plan.windows[0], attempt, payload(raw="2" * 64)) is original
    )
    assert journal.manifests[0] == manifest
    action, request = journal.committed.await_args.args
    assert action == "verify_replay"
    assert request.raw_response_digest == "2" * 64
    journal.committed.reset_mock()
    with pytest.raises(
        bridge.TrackerExecutionDeferred, match="window_checkpoint_replay_conflict"
    ):
        await journal.save(
            plan.windows[0],
            attempt,
            payload(geom=dict(type="bbox", x=0.6, y=0.2, w=0.3, h=0.4)),
        )
    journal.committed.assert_not_awaited()
    assert journal.manifests[0] == manifest


async def test_finish_flushes_business_state_without_committing_a_separate_transaction(
    monkeypatch,
):
    proof, _, _ = _artifact(monkeypatch)
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock())
    journal = bridge.TrackerWindowJournal(db, proof.job_id)
    seen = []

    async def step(action, request):
        assert db.flush.await_count == 1
        seen.append((action, request.outcome))
        return {"finished": True}

    journal.step = step
    await journal.finish("completed")
    assert seen == [("finish", "completed")]
    db.commit.assert_not_awaited()


async def test_lost_claim_stops_persistence_retries(monkeypatch):
    proof, _, _ = _artifact(monkeypatch)
    db = SimpleNamespace(
        no_autoflush=nullcontext(),
        scalar=AsyncMock(
            return_value=dict(schema=SCHEMA, status="rejected", reason="lease_lost")
        ),
        rollback=AsyncMock(),
        commit=AsyncMock(),
    )
    journal = bridge.TrackerWindowJournal(db, proof.job_id)
    with pytest.raises(bridge.TrackerExecutionLost):
        await journal.committed("read_plan", journal.request(ReadRequest))
    db.scalar.assert_awaited_once()
    db.commit.assert_not_awaited()
