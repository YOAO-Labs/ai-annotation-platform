from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aap_preannotation_results.video_execution_journal import (
    NATIVE_FUNCTIONS,
    SCHEMA,
    BeginWindowRequest,
    CaptureWindowRequest,
    PolicyRequest,
    PutPartRequest,
    ReadPartRequest,
    ReadRequest,
    ResumePageRequest,
    VideoJournalFailure,
    decode_native_reply,
    request_bytes,
)
from aap_preannotation_results.video_execution_plan import (
    BackendCompatibilityIdentity,
    VideoContinuationPostState,
)
from aap_preannotation_results.video_execution_planner import (
    VideoExecutionInputFacts,
    build_video_execution_plan,
    freeze_video_window_settings,
)
from aap_preannotation_results.video_window_checkpoint import (
    make_video_window_checkpoint_payload,
    snapshot_video_window_checkpoint,
)
from aap_preannotation_results_runtime.video_journal_database import (
    VideoJournalPostgresData,
)


def _proof():
    return dict(
        job_id=uuid4(),
        run_id=uuid4(),
        worker_id=uuid4(),
        generation=1,
        lease_token=uuid4(),
    )


def _facts(job_id):
    return VideoExecutionInputFacts(
        job_id=job_id,
        input_id=uuid4(),
        task_id=uuid4(),
        project_id=uuid4(),
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
            source_version_id="source-v1",
            source_sha256="d" * 64,
            byte_size=64,
            frame_count=1,
            width=3,
            height=2,
            duration_ms=1000,
            video_stream=0,
        ),
        timebase=dict(
            timebase_digest="e" * 64,
            selected_frame_count=1,
            sampling_step=1,
            grid_step=1,
        ),
    )


def _artifact(proof):
    backend = BackendCompatibilityIdentity(
        role="track",
        provider_kind="builtin",
        registry_id=None,
        backend_incarnation=None,
        builtin_implementation_id="aap-mock-bbox/v1",
        builtin_implementation_digest="f" * 64,
        model_key="mock_bbox",
        model_id="mock_bbox",
        weights_release_id=None,
        weights_sha256=None,
        tracker_context_mode="none",
        max_context_frames=0,
        max_window_frames=None,
        capability_digest="1" * 64,
    )
    settings = freeze_video_window_settings(
        requested_model_key="mock_bbox",
        platform_default_frames=2,
        platform_sam3_frames=2,
        backend_compatibility=[backend],
    )
    plan = build_video_execution_plan(
        job_id=proof["job_id"],
        facts=_facts(proof["job_id"]),
        window_settings=settings,
        backend_compatibility=[backend],
        output_geometry="bbox",
        initial_seed_source="none",
        initial_seed_digest=None,
    )
    geometry = dict(type="bbox", x=0.1, y=0.2, w=0.3, h=0.4)
    payload = make_video_window_checkpoint_payload(
        plan=plan,
        window_ordinal=0,
        raw_response_digest="2" * 64,
        frame_results=[
            dict(
                frame_index=0,
                geometry=geometry,
                confidence=0.9,
                outside=False,
                instance_id="1",
                primary=True,
            )
        ],
        continuation=VideoContinuationPostState(
            last_geometry=geometry,
            last_geom_by_instance={},
            mp_prev_boundary={},
            mp_next_global=1,
        ),
    )
    with snapshot_video_window_checkpoint(plan, payload) as snapshot:
        return plan, snapshot.manifest, tuple(snapshot.iter_parts())


def _ok(**data):
    return dict(schema=SCHEMA, status="ok", **data)


def test_input_facts_cannot_be_echoed_for_another_job():
    proof = _proof()
    request = ReadRequest(**proof)
    facts = _facts(proof["job_id"])
    assert (
        decode_native_reply(
            "input_facts", request, _ok(facts=facts.model_dump(mode="json"))
        )["facts"]
        == facts
    )
    wrong = {**facts.model_dump(mode="json"), "job_id": str(uuid4())}
    with pytest.raises(VideoJournalFailure, match="invalid_response"):
        decode_native_reply("input_facts", request, _ok(facts=wrong))


def test_begin_echo_must_match_generation_window_and_seed():
    proof = _proof()
    plan, _, _ = _artifact(proof)
    request = BeginWindowRequest(
        **proof,
        plan_digest=plan.plan_digest,
        window_ordinal=0,
        window_digest=plan.windows[0].window_digest,
        seed_input_digest=None,
        predecessor_checkpoint_digest=None,
    )
    attempt = dict(
        **{key: str(proof[key]) for key in ("job_id", "run_id")},
        generation=1,
        mode="execute",
        state="started",
        capture_id=str(uuid4()),
        window_ordinal=0,
        window_digest=plan.windows[0].window_digest,
        seed_input_digest=None,
        predecessor_checkpoint_digest=None,
        checkpoint=None,
    )
    assert (
        decode_native_reply("begin_window", request, _ok(attempt=attempt))[
            "attempt"
        ].generation
        == 1
    )
    for key, value in (
        ("generation", 2),
        ("window_ordinal", 1),
        ("seed_input_digest", "a" * 64),
        ("job_id", str(uuid4())),
    ):
        with pytest.raises(VideoJournalFailure, match="invalid_response"):
            decode_native_reply(
                "begin_window", request, _ok(attempt={**attempt, key: value})
            )


def test_capture_and_part_acknowledgements_cannot_cross_capture_identity():
    proof = _proof()
    plan, manifest, parts = _artifact(proof)
    capture_id = uuid4()
    request = CaptureWindowRequest(
        **proof,
        plan_digest=plan.plan_digest,
        window_ordinal=0,
        capture_id=capture_id,
        manifest=manifest,
    )
    capture = dict(
        capture_id=str(capture_id),
        window_ordinal=0,
        state="receiving",
        manifest=manifest.model_dump(mode="json", by_alias=True),
    )
    assert (
        decode_native_reply("capture_window", request, _ok(capture=capture))[
            "capture"
        ].manifest
        == manifest
    )
    with pytest.raises(VideoJournalFailure):
        decode_native_reply(
            "capture_window",
            request,
            _ok(capture={**capture, "capture_id": str(uuid4())}),
        )
    part = parts[0]
    put = PutPartRequest(
        **proof,
        capture_id=capture_id,
        index=0,
        sha256=part.part_digest,
        data_base64=part.data_base64,
    )
    receipt = part.model_dump(mode="json", exclude={"schema_", "data_base64"})
    receipt["capture_id"] = str(capture_id)
    assert (
        decode_native_reply("put_part", put, _ok(part=receipt))["part"].part_index == 0
    )
    with pytest.raises(VideoJournalFailure):
        decode_native_reply(
            "put_part", put, _ok(part={**receipt, "capture_id": str(uuid4())})
        )
    assert str(proof["lease_token"]) not in repr(put)
    assert part.data_base64 not in repr(put)


def test_resume_pages_reject_repeated_cursors_and_stale_generation():
    proof = _proof()
    plan, manifest, _ = _artifact(proof)
    slot = dict(
        ordinal=0,
        window_digest=plan.windows[0].window_digest,
        context_span_digest=plan.windows[0].context_span.span_digest,
        phase="track",
        branch="forward",
        seed_source="none",
        seed_digest=None,
        completed=True,
        checkpoint=manifest.model_dump(mode="json", by_alias=True),
        attempt=None,
    )
    request = ResumePageRequest(**proof)
    reply = _ok(windows=[slot], closed_spans=[], next_ordinal=None)
    assert (
        decode_native_reply("read_resume", request, reply)["page"].windows[0].completed
    )
    for changes in (
        dict(next_ordinal=1),
        dict(windows=[slot, slot]),
        dict(windows=[{**slot, "completed": False}]),
    ):
        with pytest.raises(VideoJournalFailure):
            decode_native_reply("read_resume", request, {**reply, **changes})
    stale = dict(
        generation=2,
        mode="execute",
        state="captured",
        capture_id=str(uuid4()),
        seed_input_digest=None,
        predecessor_checkpoint_digest=None,
    )
    with pytest.raises(VideoJournalFailure):
        decode_native_reply(
            "read_resume", request, {**reply, "windows": [{**slot, "attempt": stale}]}
        )


def test_part_reads_and_unknown_errors_fail_without_echoing_private_values():
    proof = _proof()
    _, manifest, parts = _artifact(proof)
    request = ReadPartRequest(**proof, checkpoint_id=manifest.checkpoint_id, index=0)
    assert (
        decode_native_reply(
            "read_part",
            request,
            _ok(part=parts[0].model_dump(mode="json", by_alias=True)),
        )["part"]
        == parts[0]
    )
    with pytest.raises(VideoJournalFailure, match="invalid_response") as error:
        decode_native_reply(
            "read_part",
            request,
            dict(schema=SCHEMA, status="rejected", reason=str(proof["lease_token"])),
        )
    assert str(proof["lease_token"]) not in str(error.value)
    with pytest.raises(VideoJournalFailure):
        request_bytes("begin_window", request)


def test_journal_data_binding_expires_with_the_actual_transaction(monkeypatch):
    async def exercise():
        from aap_owner_data import postgres

        verify = AsyncMock()
        monkeypatch.setattr(postgres, "verify_function_only_role", verify)

        class Connection:
            xid = "21"
            calls = 0

            def is_in_transaction(self):
                return True

            async def fetchval(self, statement, *arguments):
                if "pg_current_xact_id" in statement:
                    return self.xid
                if arguments == ("policy", "{}"):
                    return _ok(ready=True, functions=list(NATIVE_FUNCTIONS))
                self.calls += 1
                raise AssertionError("stale binding sent mutation")

        connection = Connection()
        data = await VideoJournalPostgresData.bind(connection)
        assert verify.await_args.kwargs["functions"] == {
            ("video_execution_step", "text, text"),
            ("video_execution_checkpoint_step", "text, text"),
        }
        connection.xid = "22"
        with pytest.raises(VideoJournalFailure, match="storage_unavailable"):
            await data.step("read_plan", ReadRequest(**_proof()))
        assert connection.calls == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("ready", [False, 1, "true"])
def test_policy_requires_explicit_readiness_and_exact_function_profile(ready):
    with pytest.raises(VideoJournalFailure):
        decode_native_reply(
            "policy",
            PolicyRequest(),
            _ok(ready=ready, functions=list(NATIVE_FUNCTIONS)),
        )
