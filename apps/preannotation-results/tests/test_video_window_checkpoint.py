from __future__ import annotations

from importlib.resources import files
import json

import hashlib
import uuid

import pytest

from aap_preannotation_results.video_execution_plan import (
    BackendCompatibilityIdentity,
    FrozenWindowSettings,
    VideoContinuationPostState,
    VideoExecutionContractFailure,
    VideoExecutionWindowSpec,
    VideoOutputIdentity,
    VideoSourceIdentity,
    VideoTimebaseIdentity,
    canonical_video_json,
    make_video_context_span,
    make_video_execution_plan,
)
from aap_preannotation_results.video_window_checkpoint import (
    MAX_VIDEO_WINDOW_CHECKPOINT_PARTS,
    assemble_video_window_checkpoint_parts,
    decide_video_window_resume,
    decode_video_window_checkpoint_manifest,
    decode_video_window_checkpoint_payload,
    make_video_window_checkpoint_payload,
    require_video_window_replay_match,
    snapshot_video_window_checkpoint,
    validate_video_window_checkpoint_payload,
    verify_video_window_checkpoint_parts,
    video_window_checkpoint_schema,
)

_REQUEST = uuid.UUID("11111111-1111-4111-8111-111111111111")
_INPUT = uuid.UUID("22222222-2222-4222-8222-222222222222")
_TASK = uuid.UUID("33333333-3333-4333-8333-333333333333")
_PROJECT = uuid.UUID("44444444-4444-4444-8444-444444444444")
_REGISTRY = uuid.UUID("55555555-5555-4555-8555-555555555555")
_INCARNATION = uuid.UUID("66666666-6666-4666-8666-666666666666")
_PROMPT = "a" * 64


def _backend(*, context_mode="none", max_context_frames=0, known=True):
    return BackendCompatibilityIdentity(
        role="track",
        registry_id=_REGISTRY,
        backend_incarnation=_INCARNATION if known else None,
        model_key="sam2_video",
        model_id="tracker-model" if known else None,
        weights_release_id="release-2026-10" if known else None,
        weights_sha256="b" * 64 if known else None,
        tracker_context_mode=context_mode,
        max_context_frames=max_context_frames,
        max_window_frames=2,
        capability_digest="c" * 64 if known else None,
    )


def _window(
    *,
    ordinal,
    start,
    end,
    is_seed_window=False,
    seed_source="continuation",
    seed_digest=None,
    phase="track",
    backend_role="track",
    branch="forward",
    span_start=None,
    span_end=None,
    span_from=None,
    span_to=None,
    span_mode="none",
):
    span = make_video_context_span(
        start_ordinal=ordinal if span_start is None else span_start,
        end_ordinal=ordinal if span_end is None else span_end,
        from_frame=start if span_from is None else span_from,
        to_frame=end if span_to is None else span_to,
        mode=span_mode,
    )
    return VideoExecutionWindowSpec(
        phase=phase,
        backend_role=backend_role,
        branch=branch,
        ordinal=ordinal,
        from_frame=start,
        to_frame=end,
        is_seed_window=is_seed_window,
        seed_required=seed_source not in {"none", "session"},
        seed_source=seed_source,
        seed_digest=seed_digest,
        context_span=span,
    )


def _plan(
    *,
    windows=None,
    frame_count=4,
    context_mode="none",
    max_context_frames=0,
    job_kind="tracking",
    direction="forward",
    correction_frame=None,
    backend=None,
    requested_model_key="sam2_video",
    backends=None,
    settings=None,
    output_geometry="bbox",
):
    if windows is None:
        windows = [
            _window(
                ordinal=0,
                start=0,
                end=1,
                is_seed_window=True,
                seed_source="prompt",
                seed_digest=_PROMPT,
            ),
            _window(
                ordinal=1,
                start=2,
                end=3,
                seed_source="continuation",
            ),
        ]
    if backends is None:
        backends = [
            backend
            or _backend(
                context_mode=context_mode,
                max_context_frames=max_context_frames,
            )
        ]
    if settings is None:
        settings = FrozenWindowSettings(
            platform_default_frames=4,
            platform_sam3_frames=4,
            track_backend_max_frames=2,
            effective_track_max_frames=2,
            combo_discovery_window_frames=None,
            discovery_backend_max_frames=None,
            effective_discovery_max_frames=None,
        )
    return make_video_execution_plan(
        request_id=_REQUEST,
        input_id=_INPUT,
        task_id=_TASK,
        project_id=_PROJECT,
        request_digest="1" * 64,
        prompt_digest=_PROMPT,
        source_plan_digest="2" * 64,
        source=VideoSourceIdentity(
            source_version_id="video-version-1",
            source_sha256="d" * 64,
            byte_size=4096,
            frame_count=frame_count,
            width=640,
            height=480,
            duration_ms=1000,
            video_stream=0,
        ),
        timebase=VideoTimebaseIdentity(
            timebase_digest="e" * 64,
            selected_frame_count=frame_count,
            sampling_step=1,
            grid_step=1,
        ),
        requested_model_key=requested_model_key,
        job_kind=job_kind,
        direction=direction,
        from_frame=0,
        to_frame=frame_count - 1,
        correction_frame=correction_frame,
        segment_id=None,
        window_settings=settings,
        backend_compatibility=backends,
        output=VideoOutputIdentity(geometry=output_geometry, grid_step=1),
        windows=windows,
    )


def _row(frame, *, instance_id="1", geometry=None, outside=False, confidence=0.9):
    return {
        "frame_index": frame,
        "geometry": geometry
        or {"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        "confidence": confidence,
        "outside": outside,
        "instance_id": instance_id,
        "primary": True,
    }


def _state(next_global=2):
    geometry = {"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    return VideoContinuationPostState(
        last_geometry=geometry,
        last_geom_by_instance={"1": geometry},
        mp_prev_boundary={},
        mp_next_global=next_global,
    )


def _payload(plan, ordinal, *, predecessor=None, raw_digest="f" * 64, rows=None):
    window = plan.windows[ordinal]
    if rows is None:
        rows = [_row(frame) for frame in range(window.from_frame, window.to_frame + 1)]
    return make_video_window_checkpoint_payload(
        plan=plan,
        window_ordinal=ordinal,
        raw_response_digest=raw_digest,
        frame_results=rows,
        continuation=None if window.phase == "discovery" else _state(),
        predecessor_checkpoint=predecessor,
    )


def _snapshot(plan, ordinal, *, predecessor=None, payload=None):
    payload = payload or _payload(plan, ordinal, predecessor=predecessor)
    with snapshot_video_window_checkpoint(
        plan, payload, predecessor_checkpoint=predecessor
    ) as snapshot:
        return payload, snapshot.manifest, tuple(snapshot.iter_parts())


def _session_plan():
    span = {
        "span_start": 0,
        "span_end": 1,
        "span_from": 0,
        "span_to": 3,
        "span_mode": "session",
    }
    windows = [
        _window(
            ordinal=0,
            start=0,
            end=1,
            is_seed_window=True,
            seed_source="prompt",
            seed_digest=_PROMPT,
            **span,
        ),
        _window(
            ordinal=1,
            start=2,
            end=3,
            seed_source="session",
            **span,
        ),
    ]
    return _plan(
        windows=windows,
        context_mode="session",
        max_context_frames=4,
    )


def test_one_window_snapshot_is_bounded_roundtrips_and_has_stable_identity():
    plan = _plan()
    payload, manifest, parts = _snapshot(plan, 0)

    assert manifest.checkpoint_id == payload.checkpoint_id
    assert payload.schema_ == "aap-video-window-result-content/v1"
    assert manifest.schema_ == "aap-video-window-result/v1"
    assert (
        manifest.checkpoint_id
        == hashlib.sha256(
            canonical_video_json({"plan_digest": plan.plan_digest, "window_ordinal": 0})
        ).hexdigest()
    )
    manifest_identity = manifest.model_dump(mode="json", by_alias=True)
    manifest_identity.pop("checkpoint_id")
    manifest_identity.pop("manifest_digest")
    assert (
        manifest.manifest_digest
        == hashlib.sha256(canonical_video_json(manifest_identity)).hexdigest()
    )
    assert manifest.window_ordinal == 0
    assert 0 < manifest.payload_byte_size <= 64 * 1024 * 1024
    assert manifest.payload_part_count == len(parts)
    assert parts[0].schema_ == "aap-video-window-result-part/v1"
    assert b"session_token" not in str(payload.model_dump()).encode()
    assert b"Authorization" not in str(payload.model_dump()).encode()
    with verify_video_window_checkpoint_parts(plan, manifest, parts) as restored:
        assert restored == payload

    with assemble_video_window_checkpoint_parts(manifest, parts) as assembled:
        assert len(assembled.read()) == manifest.payload_byte_size


def test_typed_payload_is_revalidated_after_mutation_and_rejects_coercion_and_extras():
    plan = _plan()
    payload = _payload(plan, 0)

    malformed = payload.model_dump(mode="json", by_alias=True)
    malformed["window_ordinal"] = True
    with pytest.raises(VideoExecutionContractFailure):
        decode_video_window_checkpoint_payload(malformed)
    malformed = payload.model_dump(mode="json", by_alias=True)
    malformed["unexpected"] = "field"
    with pytest.raises(VideoExecutionContractFailure):
        decode_video_window_checkpoint_payload(malformed)

    payload.frame_results[0].geometry["x"] = 0.8
    with pytest.raises(VideoExecutionContractFailure):
        with snapshot_video_window_checkpoint(plan, payload):
            pass

    clean = _payload(plan, 0)
    clean.__dict__["unexpected"] = "field"
    with pytest.raises(VideoExecutionContractFailure):
        validate_video_window_checkpoint_payload(plan, clean)


def test_window_range_geometry_type_and_correction_human_frame_are_enforced():
    plan = _plan()
    with pytest.raises(VideoExecutionContractFailure):
        _payload(
            plan,
            0,
            rows=[_row(2)],
        )
    with pytest.raises(VideoExecutionContractFailure):
        _payload(
            plan,
            0,
            rows=[_row(0, geometry={"type": "polygon", "points": []})],
        )

    correction_windows = [
        _window(
            ordinal=0,
            start=2,
            end=3,
            branch="backward",
            is_seed_window=True,
            seed_source="correction",
            seed_digest="9" * 64,
        ),
        _window(
            ordinal=1,
            start=0,
            end=1,
            branch="backward",
            seed_source="continuation",
        ),
        _window(
            ordinal=2,
            start=3,
            end=4,
            branch="forward",
            is_seed_window=True,
            seed_source="correction",
            seed_digest="9" * 64,
        ),
        _window(
            ordinal=3,
            start=5,
            end=5,
            branch="forward",
            seed_source="continuation",
        ),
    ]
    correction_plan = _plan(
        windows=correction_windows,
        frame_count=6,
        job_kind="correction",
        direction="bidirectional",
        correction_frame=3,
    )
    with pytest.raises(VideoExecutionContractFailure):
        _payload(correction_plan, 0, rows=[_row(3)])


def test_replay_accepts_same_output_with_new_raw_digest_but_never_replaces_evidence():
    plan = _plan()
    old_payload, manifest, _ = _snapshot(plan, 0)
    observed = _payload(plan, 0, raw_digest="0" * 64)

    match = require_video_window_replay_match(
        plan=plan,
        stored_manifest=manifest,
        stored_payload=old_payload,
        replayed_payload=observed,
    )
    assert match.checkpoint_id == manifest.checkpoint_id
    assert match.stored_manifest_digest == manifest.manifest_digest
    assert match.stored_raw_response_digest == manifest.raw_response_digest
    assert match.observed_raw_response_digest == "0" * 64
    assert match.raw_response_matches is False

    changed = _payload(
        plan,
        0,
        raw_digest="0" * 64,
        rows=[
            _row(
                0,
                geometry={
                    "type": "bbox",
                    "x": 0.8,
                    "y": 0.2,
                    "w": 0.3,
                    "h": 0.4,
                },
            )
        ],
    )
    with pytest.raises(VideoExecutionContractFailure):
        require_video_window_replay_match(
            plan=plan,
            stored_manifest=manifest,
            stored_payload=old_payload,
            replayed_payload=changed,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("confidence", 0.7), ("primary", False), ("outside", True)],
)
def test_replay_rejects_changed_candidate_evidence(field, value):
    plan = _plan()
    old_payload, manifest, _ = _snapshot(plan, 0)
    changed_row = _row(0)
    changed_row[field] = value
    replayed = _payload(plan, 0, rows=[changed_row])

    with pytest.raises(VideoExecutionContractFailure, match="replay_conflict"):
        require_video_window_replay_match(
            plan=plan,
            stored_manifest=manifest,
            stored_payload=old_payload,
            replayed_payload=replayed,
        )


def test_replay_rejects_changed_continuation_candidate_id_and_seed_lineage():
    plan = _plan()
    old_payload, manifest, _ = _snapshot(plan, 0)
    changed_state = make_video_window_checkpoint_payload(
        plan=plan,
        window_ordinal=0,
        raw_response_digest="0" * 64,
        frame_results=[_row(0)],
        continuation=_state(next_global=3),
    )
    with pytest.raises(VideoExecutionContractFailure, match="replay_conflict"):
        require_video_window_replay_match(
            plan=plan,
            stored_manifest=manifest,
            stored_payload=old_payload,
            replayed_payload=changed_state,
        )

    invalid_candidate = _row(0)
    invalid_candidate["candidate_key"] = "different:stable:key"
    with pytest.raises(VideoExecutionContractFailure):
        _payload(plan, 0, rows=[invalid_candidate])

    changed_seed = old_payload.model_dump(mode="json", by_alias=True)
    changed_seed["seed_input_digest"] = "9" * 64
    with pytest.raises(VideoExecutionContractFailure, match="seed_mismatch"):
        validate_video_window_checkpoint_payload(plan, changed_seed)

    changed_predecessor = old_payload.model_dump(mode="json", by_alias=True)
    changed_predecessor["predecessor_checkpoint_digest"] = "9" * 64
    with pytest.raises(VideoExecutionContractFailure, match="seed_mismatch"):
        validate_video_window_checkpoint_payload(plan, changed_predecessor)


def test_stateless_resume_reuses_completed_windows_and_rejects_gaps_or_divergence():
    plan = _plan(
        frame_count=6,
        windows=[
            _window(
                ordinal=0,
                start=0,
                end=1,
                is_seed_window=True,
                seed_source="prompt",
                seed_digest=_PROMPT,
            ),
            _window(ordinal=1, start=2, end=3, seed_source="continuation"),
            _window(ordinal=2, start=4, end=5, seed_source="continuation"),
        ],
    )
    payload0, manifest0, _ = _snapshot(plan, 0)
    decision = decide_video_window_resume(
        plan, [manifest0], closed_context_span_digests=[]
    )
    assert decision.reuse_ordinals == (0,)
    assert decision.execute_ordinals == (1, 2)
    assert decision.replay_ordinals == ()

    payload1, manifest1, _ = _snapshot(plan, 1, predecessor=manifest0)
    assert payload1.seed_input_digest == manifest0.continuation_digest
    assert payload1.predecessor_checkpoint_digest == manifest0.manifest_digest
    _, manifest2, _ = _snapshot(plan, 2, predecessor=manifest1)
    with pytest.raises(VideoExecutionContractFailure, match="window_checkpoint_gap"):
        decide_video_window_resume(
            plan,
            [manifest0, manifest2],
            closed_context_span_digests=[],
        )
    divergent = _payload(plan, 0, raw_digest="0" * 64)
    _, divergent_manifest, _ = _snapshot(plan, 0, payload=divergent)
    with pytest.raises(VideoExecutionContractFailure, match="divergent_duplicate"):
        decide_video_window_resume(
            plan,
            [manifest0, divergent_manifest],
            closed_context_span_digests=[],
        )
    assert payload0.window_ordinal == 0 and payload1.window_ordinal == 1


def test_session_checkpoint_prefix_replays_until_explicit_closure():
    plan = _session_plan()
    payload0, manifest0, _ = _snapshot(plan, 0)
    open_prefix = decide_video_window_resume(
        plan, [manifest0], closed_context_span_digests=[]
    )
    assert open_prefix.replay_ordinals == (0,)
    assert open_prefix.execute_ordinals == (1,)
    assert open_prefix.replay_context_span_digests == (
        plan.windows[0].context_span.span_digest,
    )

    payload1, manifest1, _ = _snapshot(plan, 1, predecessor=manifest0)
    full_but_open = decide_video_window_resume(
        plan, [manifest0, manifest1], closed_context_span_digests=[]
    )
    assert full_but_open.replay_ordinals == (0, 1)
    assert full_but_open.execute_ordinals == ()
    assert payload1.seed_input_digest is None
    assert payload1.predecessor_checkpoint_digest == manifest0.manifest_digest

    closed = decide_video_window_resume(
        plan,
        [manifest0, manifest1],
        closed_context_span_digests=[plan.windows[0].context_span.span_digest],
    )
    assert closed.reuse_ordinals == (0, 1)
    assert closed.replay_ordinals == ()
    assert payload0.window_ordinal == 0 and payload1.window_ordinal == 1

    with pytest.raises(
        VideoExecutionContractFailure,
        match="closed_context_span_missing_window",
    ):
        decide_video_window_resume(
            plan,
            [manifest0],
            closed_context_span_digests=[plan.windows[0].context_span.span_digest],
        )
    with pytest.raises(
        VideoExecutionContractFailure,
        match="unknown_or_duplicate_closed",
    ):
        decide_video_window_resume(plan, [], closed_context_span_digests=["f" * 64])


def test_missing_backend_identity_allows_fresh_execution_but_denies_resume():
    plan = _plan(backend=_backend(known=False))
    assert decide_video_window_resume(
        plan, [], closed_context_span_digests=[]
    ).execute_ordinals == (0, 1)
    _, manifest, _ = _snapshot(plan, 0)
    with pytest.raises(VideoExecutionContractFailure, match="backend_compatibility"):
        decide_video_window_resume(plan, [manifest], closed_context_span_digests=[])


def test_combo_discovery_checkpoint_binds_seeds_and_correction_branch_resets_seed():
    track = BackendCompatibilityIdentity(
        role="track",
        registry_id=_REGISTRY,
        backend_incarnation=_INCARNATION,
        model_key="sam3_video_interactive",
        model_id="pvs-model",
        weights_release_id="release-2026-10",
        weights_sha256="b" * 64,
        tracker_context_mode="none",
        max_context_frames=0,
        max_window_frames=2,
        capability_digest="c" * 64,
    )
    discovery = BackendCompatibilityIdentity(
        role="discovery",
        registry_id=_REGISTRY,
        backend_incarnation=_INCARNATION,
        model_key="sam3_video",
        model_id="discovery-model",
        weights_release_id="release-2026-10",
        weights_sha256="d" * 64,
        tracker_context_mode="none",
        max_context_frames=0,
        max_window_frames=5,
        capability_digest="e" * 64,
    )
    windows = [
        _window(
            ordinal=0,
            start=0,
            end=1,
            phase="discovery",
            backend_role="discovery",
            seed_source="none",
        ),
        _window(
            ordinal=1,
            start=0,
            end=1,
            is_seed_window=True,
            seed_source="discovery",
        ),
        _window(ordinal=2, start=2, end=3, seed_source="continuation"),
    ]
    settings = FrozenWindowSettings(
        platform_default_frames=4,
        platform_sam3_frames=4,
        track_backend_max_frames=2,
        effective_track_max_frames=2,
        combo_discovery_window_frames=5,
        discovery_backend_max_frames=5,
        effective_discovery_max_frames=4,
    )
    combo = _plan(
        windows=windows,
        requested_model_key="sam3_video_combo",
        backends=[track, discovery],
        settings=settings,
    )
    discovery_rows = [
        _row(0, instance_id="2"),
        _row(0, instance_id="1"),
        _row(1, instance_id="1"),
    ]
    discovery_payload = _payload(combo, 0, rows=discovery_rows)
    _, discovery_manifest, _ = _snapshot(combo, 0, payload=discovery_payload)
    track_payload = _payload(combo, 1, predecessor=discovery_manifest)
    assert track_payload.seed_input_digest == discovery_manifest.discovery_seeds_digest
    assert (
        track_payload.predecessor_checkpoint_digest
        == discovery_manifest.manifest_digest
    )
    validate_video_window_checkpoint_payload(
        combo, track_payload, predecessor_checkpoint=discovery_manifest
    )

    backward_windows = [
        _window(
            ordinal=0,
            start=4,
            end=5,
            phase="discovery",
            backend_role="discovery",
            seed_source="none",
        ),
        _window(
            ordinal=1,
            start=5,
            end=5,
            branch="backward",
            is_seed_window=True,
            seed_source="discovery",
        ),
        _window(
            ordinal=2,
            start=3,
            end=4,
            branch="backward",
            seed_source="continuation",
        ),
        _window(
            ordinal=3,
            start=1,
            end=2,
            branch="backward",
            seed_source="continuation",
        ),
        _window(
            ordinal=4,
            start=0,
            end=0,
            branch="backward",
            seed_source="continuation",
        ),
    ]
    backward_combo = _plan(
        windows=backward_windows,
        frame_count=6,
        direction="backward",
        requested_model_key="sam3_video_combo",
        backends=[track, discovery],
        settings=settings,
    )
    backward_discovery_payload = _payload(
        backward_combo,
        0,
        rows=[_row(4), _row(5, instance_id="2")],
    )
    assert [seed.geometry for seed in backward_discovery_payload.discovery_seeds] == [
        _row(5, instance_id="2")["geometry"]
    ]
    _, backward_discovery_manifest, _ = _snapshot(
        backward_combo, 0, payload=backward_discovery_payload
    )
    backward_track_payload = _payload(
        backward_combo,
        1,
        predecessor=backward_discovery_manifest,
    )
    assert (
        backward_track_payload.seed_input_digest
        == backward_discovery_manifest.discovery_seeds_digest
    )

    correction_windows = [
        _window(
            ordinal=0,
            start=2,
            end=3,
            branch="backward",
            is_seed_window=True,
            seed_source="correction",
            seed_digest="9" * 64,
        ),
        _window(
            ordinal=1,
            start=0,
            end=1,
            branch="backward",
            seed_source="continuation",
        ),
        _window(
            ordinal=2,
            start=3,
            end=4,
            branch="forward",
            is_seed_window=True,
            seed_source="correction",
            seed_digest="9" * 64,
        ),
        _window(
            ordinal=3,
            start=5,
            end=5,
            branch="forward",
            seed_source="continuation",
        ),
    ]
    correction = _plan(
        windows=correction_windows,
        frame_count=6,
        job_kind="correction",
        direction="bidirectional",
        correction_frame=3,
    )
    backward_payload = _payload(correction, 0, rows=[_row(2)])
    _, backward_manifest, _ = _snapshot(correction, 0, payload=backward_payload)
    forward_payload = _payload(correction, 2, rows=[_row(4)])
    assert forward_payload.predecessor_checkpoint_digest is None
    assert forward_payload.seed_input_digest == "9" * 64
    with pytest.raises(
        VideoExecutionContractFailure,
        match="unexpected_window_predecessor",
    ):
        make_video_window_checkpoint_payload(
            plan=correction,
            window_ordinal=2,
            raw_response_digest="f" * 64,
            frame_results=[_row(4)],
            continuation=_state(),
            predecessor_checkpoint=backward_manifest,
        )


def test_checkpoint_parts_enforce_exact_hash_order_and_64_mib_budget():
    plan = _plan()
    large_row = _row(
        0,
        geometry={
            "type": "bbox",
            "x": 0.1,
            "y": 0.2,
            "w": 0.3,
            "h": 0.4,
            "payload": "x" * (2 * 1024 * 1024 + 100),
        },
    )
    payload = _payload(plan, 0, rows=[large_row])
    with snapshot_video_window_checkpoint(plan, payload) as snapshot:
        manifest = snapshot.manifest
        parts = tuple(snapshot.iter_parts())
    assert manifest.result_byte_size > 2 * 1024 * 1024
    assert manifest.payload_part_count == len(parts)
    assert manifest.payload_part_count <= MAX_VIDEO_WINDOW_CHECKPOINT_PARTS
    with verify_video_window_checkpoint_parts(plan, manifest, parts) as restored:
        assert restored.result_digest == payload.result_digest
    with pytest.raises(VideoExecutionContractFailure):
        with assemble_video_window_checkpoint_parts(manifest, reversed(parts)):
            pass
    with pytest.raises(VideoExecutionContractFailure):
        with assemble_video_window_checkpoint_parts(manifest, parts[:-1]):
            pass
    short_first = parts[0].model_copy(
        update={
            "data_base64": "eA==",
            "part_digest": hashlib.sha256(b"x").hexdigest(),
        }
    )
    with pytest.raises(VideoExecutionContractFailure):
        with assemble_video_window_checkpoint_parts(
            manifest, [short_first, *parts[1:]]
        ):
            pass
    wrong_hash = parts[0].model_copy(update={"part_digest": "0" * 64})
    with pytest.raises(VideoExecutionContractFailure):
        with assemble_video_window_checkpoint_parts(manifest, [wrong_hash, *parts[1:]]):
            pass


def test_schema_document_is_closed_and_versioned():
    schema = video_window_checkpoint_schema()
    assert schema["$id"] == "aap-video-window-checkpoints/v1"
    assert schema["$schema"].endswith("2020-12/schema")
    assert (
        schema["$defs"]["VideoWindowCheckpointPayload"]["additionalProperties"] is False
    )
    assert schema["x-budgets"]["payload_bytes"] == 64 * 1024 * 1024
    assert schema["x-budgets"]["part_bytes"] == 512 * 1024


def test_manifest_and_payload_reject_coerced_integer_fields():
    plan = _plan()
    payload, manifest, _ = _snapshot(plan, 0)
    payload_document = payload.model_dump(mode="json", by_alias=True)
    payload_document["frame_results"][0]["outside"] = 1
    with pytest.raises(VideoExecutionContractFailure):
        decode_video_window_checkpoint_payload(payload_document)
    document = manifest.model_dump(mode="json", by_alias=True)
    document["window_ordinal"] = True
    with pytest.raises(VideoExecutionContractFailure):
        decode_video_window_checkpoint_manifest(document)


def test_resume_keeps_an_earlier_closed_span_when_later_session_needs_replay():
    windows = []
    for ordinal in range(4):
        first = (ordinal // 2) * 2
        windows.append(
            _window(
                ordinal=ordinal,
                start=ordinal * 2,
                end=ordinal * 2 + 1,
                is_seed_window=ordinal == 0,
                seed_source="prompt"
                if ordinal == 0
                else "continuation"
                if ordinal == 2
                else "session",
                seed_digest=_PROMPT if ordinal == 0 else None,
                span_start=first,
                span_end=first + 1,
                span_from=first * 2,
                span_to=first * 2 + 3,
                span_mode="session",
            )
        )
    plan = _plan(
        windows=windows, frame_count=8, context_mode="session", max_context_frames=4
    )
    manifests = []
    for ordinal in range(3):
        _, manifest, _ = _snapshot(
            plan, ordinal, predecessor=manifests[-1] if manifests else None
        )
        manifests.append(manifest)
    decision = decide_video_window_resume(
        plan,
        manifests,
        closed_context_span_digests=[plan.windows[0].context_span.span_digest],
    )
    assert decision.reuse_ordinals == (0, 1)
    assert decision.replay_ordinals == (2,)
    assert decision.execute_ordinals == (3,)


def test_a_closed_stateless_span_without_its_window_is_corrupt():
    plan = _plan()
    with pytest.raises(
        VideoExecutionContractFailure, match="closed_context_span_missing_window"
    ):
        decide_video_window_resume(
            plan,
            [],
            closed_context_span_digests=[plan.windows[0].context_span.span_digest],
        )


def test_manifest_rejects_a_different_stable_window_id_before_reading_parts():
    plan = _plan()
    _, manifest, _ = _snapshot(plan, 0)
    changed = manifest.model_dump(mode="json", by_alias=True)
    changed["checkpoint_id"] = "0" * 64
    with pytest.raises(VideoExecutionContractFailure):
        decode_video_window_checkpoint_manifest(changed)


@pytest.mark.parametrize("kind", ["video_track_bbox", "video_bbox"])
def test_empty_first_window_can_preserve_its_accepted_track_seed(kind):
    plan = _plan(output_geometry="mask")
    seed = (
        {"type": "video_track_bbox", "track_id": "seed", "keyframes": []}
        if kind == "video_track_bbox"
        else {
            "type": "video_bbox",
            "frame_index": 0,
            "x": 0.1,
            "y": 0.1,
            "w": 0.2,
            "h": 0.2,
        }
    )
    payload = make_video_window_checkpoint_payload(
        plan=plan,
        window_ordinal=0,
        raw_response_digest="f" * 64,
        frame_results=[],
        continuation=VideoContinuationPostState(
            last_geometry=seed,
            last_geom_by_instance={},
            mp_prev_boundary={},
            mp_next_global=1,
        ),
    )
    with snapshot_video_window_checkpoint(plan, payload) as snapshot:
        assert snapshot.manifest.continuation_digest is not None
        assert payload.continuation.last_geometry == seed


def test_packaged_window_schema_matches_its_generator():
    from aap_preannotation_results.video_window_checkpoint import (
        video_window_checkpoint_schema,
    )

    assert (
        json.loads(
            files("aap_preannotation_results")
            .joinpath("video-window-checkpoints.schema.json")
            .read_text()
        )
        == video_window_checkpoint_schema()
    )
