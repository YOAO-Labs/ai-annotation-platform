from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from aap_preannotation_results.video_execution_plan import (
    MAX_VIDEO_CHECKPOINT_BYTES,
    MAX_VIDEO_CHECKPOINT_PART_BASE64_CHARS,
    BackendCompatibilityIdentity,
    FrozenWindowSettings,
    VideoContinuationPostState,
    VideoExecutionContractFailure,
    VideoExecutionWindowSpec,
    VideoOutputIdentity,
    VideoSourceIdentity,
    VideoTimebaseIdentity,
    assemble_video_checkpoint_parts,
    canonical_video_json,
    combo_discovery_seeds,
    decide_video_resume,
    decode_video_execution_plan,
    make_video_checkpoint_payload,
    make_video_context_span,
    make_video_execution_plan,
    normalize_video_frame_results,
    require_checkpoint_replay_match,
    snapshot_video_checkpoint,
    video_execution_schema,
)

_REQUEST_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_INPUT_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_TASK_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_PROJECT_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
_REGISTRY_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
_INCARNATION_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
_PROMPT_DIGEST = "a" * 64


def test_builtin_identity_does_not_fabricate_a_registry_or_weights():
    identity = BackendCompatibilityIdentity(
        role="track",
        provider_kind="builtin",
        registry_id=None,
        backend_incarnation=None,
        builtin_implementation_id="aap-mock-bbox/v1",
        builtin_implementation_digest="a" * 64,
        model_key="mock_bbox",
        model_id="mock_bbox",
        weights_release_id=None,
        weights_sha256=None,
        tracker_context_mode="none",
        max_context_frames=0,
        max_window_frames=None,
        capability_digest="b" * 64,
    )
    assert identity.identity_complete
    incomplete = identity.model_dump()
    incomplete["builtin_implementation_digest"] = None
    assert not BackendCompatibilityIdentity.model_validate(incomplete).identity_complete
    for field, changed in (
        ("registry_id", uuid.uuid4()),
        ("weights_release_id", "invented-weights"),
        ("model_key", "sam2_video"),
    ):
        with pytest.raises(ValueError):
            BackendCompatibilityIdentity.model_validate(
                {**identity.model_dump(), field: changed}
            )


def test_registry_identity_rejects_an_absent_actual_registry():
    with pytest.raises(ValueError):
        BackendCompatibilityIdentity.model_validate(
            {**_compatibility().model_dump(), "registry_id": None}
        )


def _compatibility(
    *,
    role="track",
    model_key="sam2_video",
    context_mode="none",
    max_context_frames=0,
    max_window_frames=2,
    known=True,
):
    return BackendCompatibilityIdentity(
        role=role,
        registry_id=_REGISTRY_ID,
        backend_incarnation=_INCARNATION_ID if known else None,
        model_key=model_key,
        model_id="tracker-model" if known else None,
        weights_release_id="release-2026-10" if known else None,
        weights_sha256="b" * 64 if known else None,
        tracker_context_mode=context_mode,
        max_context_frames=max_context_frames,
        max_window_frames=max_window_frames,
        capability_digest="c" * 64 if known else None,
    )


def _source(frame_count=4):
    return VideoSourceIdentity(
        source_version_id="video-version-17",
        source_sha256="d" * 64,
        byte_size=1024,
        frame_count=frame_count,
        width=1920,
        height=1080,
        duration_ms=2500,
        video_stream=0,
    )


def _timebase(frame_count=4, *, step=1):
    return VideoTimebaseIdentity(
        timebase_digest="e" * 64,
        selected_frame_count=(frame_count + step - 1) // step,
        sampling_step=step,
        grid_step=step,
    )


def _settings(*, effective=2, backend_max=2, combo=False, discovery_max=None):
    return FrozenWindowSettings(
        platform_default_frames=2,
        platform_sam3_frames=2,
        track_backend_max_frames=backend_max,
        effective_track_max_frames=effective,
        combo_discovery_window_frames=5 if combo else None,
        discovery_backend_max_frames=discovery_max if combo else None,
        effective_discovery_max_frames=min(5, discovery_max, 2)
        if combo and discovery_max is not None
        else None,
    )


def _window(
    *,
    ordinal,
    start,
    end,
    phase="track",
    backend_role="track",
    branch="forward",
    is_seed_window=False,
    seed_source="continuation",
    seed_digest=None,
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
    frame_count=4,
    windows=None,
    job_kind="tracking",
    direction="forward",
    correction_frame=None,
    requested_model_key="sam2_video",
    backend_compatibility=None,
    settings=None,
):
    if windows is None:
        windows = [
            _window(
                ordinal=0,
                start=0,
                end=1,
                is_seed_window=True,
                seed_source="prompt",
                seed_digest=_PROMPT_DIGEST,
            ),
            _window(
                ordinal=1,
                start=2,
                end=3,
                seed_source="continuation",
            ),
        ]
    if backend_compatibility is None:
        backend_compatibility = [_compatibility()]
    return make_video_execution_plan(
        request_id=_REQUEST_ID,
        input_id=_INPUT_ID,
        task_id=_TASK_ID,
        project_id=_PROJECT_ID,
        request_digest="1" * 64,
        prompt_digest=_PROMPT_DIGEST,
        source_plan_digest="2" * 64,
        source=_source(frame_count),
        timebase=_timebase(frame_count),
        requested_model_key=requested_model_key,
        job_kind=job_kind,
        direction=direction,
        from_frame=0,
        to_frame=frame_count - 1,
        correction_frame=correction_frame,
        segment_id=None,
        window_settings=settings or _settings(),
        backend_compatibility=backend_compatibility,
        output=VideoOutputIdentity(geometry="bbox", grid_step=1),
        windows=windows,
    )


def _row(frame, *, geometry=None, instance_id="1", primary=True):
    return {
        "frame_index": frame,
        "geometry": geometry
        or {"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        "confidence": 0.9,
        "outside": False,
        "instance_id": instance_id,
        "primary": primary,
    }


def _state():
    return VideoContinuationPostState(
        last_geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        last_geom_by_instance={
            "1": {"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
        },
        mp_prev_boundary={},
        mp_next_global=2,
    )


def _payload(plan, *, span_ordinal=0, revision=1, raw_digest="f" * 64, rows=None):
    span_digest = plan.windows[span_ordinal].context_span.span_digest
    span = tuple(w for w in plan.windows if w.context_span.span_digest == span_digest)
    if rows is None:
        rows = [
            _row(frame) for frame in range(span[0].from_frame, span[-1].to_frame + 1)
        ]
    return make_video_checkpoint_payload(
        plan=plan,
        context_span_digest=span_digest,
        span_revision=revision,
        raw_response_digests=[raw_digest] * len(span),
        frame_results=rows,
        continuation=None if span[0].phase == "discovery" else _state(),
    )


def test_plan_binds_exact_input_timebase_backend_and_windows():
    plan = _plan()
    encoded = canonical_video_json(plan)

    assert decode_video_execution_plan(encoded) == plan
    assert (
        plan.plan_digest
        == hashlib.sha256(
            canonical_video_json(
                plan.model_dump(mode="json", by_alias=True, exclude={"plan_digest"})
            )
        ).hexdigest()
    )
    assert plan.windows[1].context_span.mode == "none"
    assert plan.compatibility_complete is True
    assert b"Authorization" not in encoded
    assert b"session_token" not in encoded

    duplicate = b'{"schema":"aap-video-execution-plan/v1","schema":"aap-video-execution-plan/v1"}'
    with pytest.raises(VideoExecutionContractFailure):
        decode_video_execution_plan(duplicate)

    altered = plan.model_dump(mode="json", by_alias=True)
    altered["backend_compatibility"][0]["session_token"] = "must-not-be-stored"
    with pytest.raises(VideoExecutionContractFailure):
        decode_video_execution_plan(altered)

    bool_integer = plan.model_dump(mode="json", by_alias=True)
    bool_integer["from_frame"] = True
    with pytest.raises(VideoExecutionContractFailure):
        decode_video_execution_plan(bool_integer)


def test_full_source_timeline_is_independent_of_annotation_output_grid():
    wire = _plan().model_dump(mode="json", by_alias=True)
    wire["timebase"]["grid_step"] = 2
    wire["output"]["grid_step"] = 2
    wire["plan_digest"] = hashlib.sha256(
        canonical_video_json(
            {key: value for key, value in wire.items() if key != "plan_digest"}
        )
    ).hexdigest()
    plan = decode_video_execution_plan(wire)
    assert plan.timebase.selected_frame_count == plan.source.frame_count == 4
    assert plan.timebase.sampling_step == 1
    assert plan.output.grid_step == 2


def test_model_instance_cannot_bypass_plan_identity_validation():
    forged = _plan().model_copy(update={"from_frame": True})
    with pytest.raises(VideoExecutionContractFailure):
        decode_video_execution_plan(forged)


@pytest.mark.parametrize("geometry", [False, 0, "", []])
def test_falsy_scalar_geometry_is_not_normalized_to_an_empty_object(geometry):
    row = {**_row(0), "geometry": geometry}
    with pytest.raises(VideoExecutionContractFailure, match="invalid_frame_result"):
        normalize_video_frame_results([row], branch="forward")


def test_correction_plan_freezes_each_direction_and_its_seed_window():
    mask_digest = "9" * 64
    windows = [
        _window(
            ordinal=0,
            start=2,
            end=3,
            branch="backward",
            is_seed_window=True,
            seed_source="correction",
            seed_digest=mask_digest,
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
            seed_digest=mask_digest,
        ),
        _window(
            ordinal=3,
            start=5,
            end=5,
            branch="forward",
            seed_source="continuation",
        ),
    ]
    plan = _plan(
        frame_count=6,
        windows=windows,
        job_kind="correction",
        direction="bidirectional",
        correction_frame=3,
    )

    assert [(window.branch, window.is_seed_window) for window in plan.windows] == [
        ("backward", True),
        ("backward", False),
        ("forward", True),
        ("forward", False),
    ]


def test_combo_plan_binds_discovery_and_pvs_compatibility_separately():
    backends = [
        _compatibility(
            role="track",
            model_key="sam3_video_interactive",
            context_mode="session",
            max_context_frames=16,
        ),
        _compatibility(
            role="discovery",
            model_key="sam3_video",
            max_window_frames=16,
        ),
    ]
    windows = [
        _window(
            ordinal=0,
            start=0,
            end=4,
            phase="discovery",
            backend_role="discovery",
            seed_source="none",
            span_start=0,
            span_end=0,
            span_from=0,
            span_to=4,
        ),
        _window(
            ordinal=1,
            start=0,
            end=1,
            is_seed_window=True,
            seed_source="discovery",
            span_start=1,
            span_end=1,
            span_from=0,
            span_to=1,
        ),
        _window(
            ordinal=2,
            start=2,
            end=3,
            seed_source="continuation",
            span_start=2,
            span_end=2,
            span_from=2,
            span_to=3,
        ),
        _window(
            ordinal=3,
            start=4,
            end=5,
            seed_source="continuation",
            span_start=3,
            span_end=3,
            span_from=4,
            span_to=5,
        ),
    ]
    plan = _plan(
        frame_count=6,
        windows=windows,
        requested_model_key="sam3_video_combo",
        backend_compatibility=backends,
        settings=FrozenWindowSettings(
            platform_default_frames=2,
            platform_sam3_frames=5,
            track_backend_max_frames=2,
            effective_track_max_frames=2,
            combo_discovery_window_frames=5,
            discovery_backend_max_frames=16,
            effective_discovery_max_frames=5,
        ),
    )

    discovery_rows = normalize_video_frame_results(
        [_row(0, instance_id="2"), _row(0, instance_id="1"), _row(1, instance_id="1")],
        branch="forward",
    )
    seeds = combo_discovery_seeds(discovery_rows, seed_frame=0)
    assert [(item.obj_id, item.geometry["type"]) for item in seeds] == [
        (1, "bbox"),
        (2, "bbox"),
    ]
    assert plan.windows[0].phase == "discovery"
    assert plan.windows[1].seed_source == "discovery"


def test_closed_span_checkpoint_is_reusable_and_divergence_requires_new_revision():
    span = make_video_context_span(
        start_ordinal=0,
        end_ordinal=1,
        from_frame=0,
        to_frame=3,
        mode="session",
    )
    windows = [
        _window(
            ordinal=0,
            start=0,
            end=1,
            is_seed_window=True,
            seed_source="prompt",
            seed_digest=_PROMPT_DIGEST,
            span_start=0,
            span_end=1,
            span_from=0,
            span_to=3,
            span_mode="session",
        ),
        _window(
            ordinal=1,
            start=2,
            end=3,
            seed_source="session",
            span_start=0,
            span_end=1,
            span_from=0,
            span_to=3,
            span_mode="session",
        ),
    ]
    plan = _plan(
        windows=windows,
        backend_compatibility=[
            _compatibility(context_mode="session", max_context_frames=4)
        ],
    )
    payload = _payload(plan, raw_digest="f" * 64)
    with snapshot_video_checkpoint(plan, payload) as snapshot:
        manifest = snapshot.manifest
        parts = tuple(snapshot.iter_parts())
    assert manifest.context_span_closed is True
    assert manifest.span_start_ordinal == 0 and manifest.span_end_ordinal == 1
    assert (
        decide_video_resume(plan, [manifest], target_ordinal=0).action == "reuse_span"
    )
    assert (
        decide_video_resume(
            plan,
            [],
            next_span_revisions={span.span_digest: 2},
        ).action
        == "replay_span"
    )
    with assemble_video_checkpoint_parts(manifest, parts) as assembled:
        assert assembled.tell() == 0
        assert len(assembled.read()) == manifest.payload_byte_size

    divergent = _payload(plan, raw_digest="0" * 64)
    with snapshot_video_checkpoint(plan, divergent) as alternate:
        with pytest.raises(VideoExecutionContractFailure) as error:
            require_checkpoint_replay_match(manifest, alternate.manifest)
    assert error.value.code == "immutable_checkpoint_conflict"
    assert error.value.next_span_revision == 2


def test_large_checkpoint_uses_bounded_parts_above_the_control_message_limit():
    plan = _plan(
        frame_count=2,
        windows=[
            _window(
                ordinal=0,
                start=0,
                end=1,
                is_seed_window=True,
                seed_source="prompt",
                seed_digest=_PROMPT_DIGEST,
            )
        ],
    )
    large_geometry = {
        "type": "polygon",
        "points_blob": "x" * (2 * 1024 * 1024 + 31),
    }
    payload = _payload(plan, rows=[_row(0, geometry=large_geometry)])
    with snapshot_video_checkpoint(plan, payload) as snapshot:
        manifest = snapshot.manifest
        parts = tuple(snapshot.iter_parts())

    assert manifest.result_byte_size > 2 * 1024 * 1024
    assert manifest.result_byte_size <= MAX_VIDEO_CHECKPOINT_BYTES
    assert len(parts) == manifest.payload_part_count
    assert all(
        len(part.content) <= MAX_VIDEO_CHECKPOINT_PART_BASE64_CHARS for part in parts
    )
    with assemble_video_checkpoint_parts(manifest, parts) as assembled:
        assert assembled.read(1) == b"{"


def test_missing_release_identity_allows_fresh_execution_but_blocks_reuse():
    plan = _plan(backend_compatibility=[_compatibility(known=False)])
    assert plan.compatibility_complete is False
    assert decide_video_resume(plan, []).action == "execute_span"
    payload = _payload(plan)
    with snapshot_video_checkpoint(plan, payload) as snapshot:
        manifest = snapshot.manifest
    with pytest.raises(VideoExecutionContractFailure) as error:
        decide_video_resume(plan, [manifest])
    assert error.value.code == "backend_compatibility_identity_unavailable"


def test_schema_snapshot_matches_the_portable_value_contract():
    from importlib.resources import files

    schema_path = files("aap_preannotation_results").joinpath(
        "video-execution-plan.schema.json"
    )
    document = json.loads(schema_path.read_text(encoding="utf-8"))
    assert document == video_execution_schema()
    assert document["x-aap-schema"] == "aap-video-execution-plan/v1"
