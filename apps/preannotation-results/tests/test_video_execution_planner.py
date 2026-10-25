from uuid import uuid4

import pytest

from aap_preannotation_results.video_execution_plan import (
    BackendCompatibilityIdentity,
    VideoExecutionContractFailure,
)
from aap_preannotation_results.video_execution_planner import (
    VideoExecutionInputFacts,
    build_video_execution_plan,
    freeze_video_window_settings,
)


def _facts(**changes):
    values = dict(
        job_id=uuid4(),
        input_id=uuid4(),
        task_id=uuid4(),
        project_id=uuid4(),
        source_item_id=None,
        request_digest="a" * 64,
        prompt_digest="b" * 64,
        source_plan_digest="c" * 64,
        requested_model_key="sam2_video",
        job_kind="tracking",
        direction="forward",
        from_frame=0,
        to_frame=9,
        grid_step=2,
        correction_frame=None,
        segment_id=None,
        source=dict(
            source_version_id="source-version-1",
            source_sha256="d" * 64,
            byte_size=1024,
            frame_count=10,
            width=1280,
            height=720,
            duration_ms=1000,
            video_stream=0,
        ),
        timebase=dict(
            timebase_digest="e" * 64,
            selected_frame_count=10,
            sampling_step=1,
            grid_step=2,
        ),
    )
    values.update(changes)
    return VideoExecutionInputFacts.model_validate(values)


def _backend(*, role="track", model_key="sam2_video", window=3, context=0):
    return BackendCompatibilityIdentity(
        role=role,
        registry_id=uuid4(),
        backend_incarnation=uuid4(),
        model_key=model_key,
        model_id="selected-model",
        weights_release_id="release-1",
        weights_sha256="f" * 64,
        tracker_context_mode="session" if context else "none",
        max_context_frames=context,
        max_window_frames=window,
        capability_digest="1" * 64,
    )


def _build(facts=None, backends=None, **kwargs):
    facts = facts or _facts()
    backends = backends or [_backend()]
    combo = facts.requested_model_key == "sam3_video_combo"
    settings = freeze_video_window_settings(
        requested_model_key=facts.requested_model_key,
        platform_default_frames=8,
        platform_sam3_frames=4,
        backend_compatibility=backends,
        combo_discovery_window_frames=5 if combo else None,
    )
    source = (
        "none"
        if combo
        else "correction"
        if facts.job_kind == "correction"
        else "prompt"
    )
    return build_video_execution_plan(
        job_id=facts.job_id,
        facts=facts,
        window_settings=settings,
        backend_compatibility=backends,
        output_geometry="bbox",
        initial_seed_source=source,
        initial_seed_digest=None if combo else "2" * 64,
        **kwargs,
    )


def test_actual_selected_model_limit_controls_windows_and_keeps_full_source_timeline():
    plan = _build()
    assert [(w.from_frame, w.to_frame) for w in plan.windows] == [
        (0, 2),
        (3, 5),
        (6, 8),
        (9, 9),
    ]
    assert plan.window_settings.effective_track_max_frames == 3
    assert plan.timebase.sampling_step == 1 and plan.output.grid_step == 2
    assert [w.seed_source for w in plan.windows] == [
        "prompt",
        "continuation",
        "continuation",
        "continuation",
    ]


def test_session_spans_are_frozen_with_seed_reset_at_each_context_boundary():
    plan = _build(backends=[_backend(window=2, context=6)])
    assert [
        (w.context_span.from_frame, w.context_span.to_frame) for w in plan.windows
    ] == [(0, 5), (0, 5), (0, 5), (6, 9), (6, 9)]
    assert [w.seed_source for w in plan.windows] == [
        "prompt",
        "session",
        "session",
        "continuation",
        "session",
    ]
    assert not plan.windows[1].seed_required and plan.windows[3].seed_required


def test_context_bound_smaller_than_window_bound_still_produces_valid_windows():
    plan = _build(backends=[_backend(window=10, context=3)])
    assert plan.window_settings.effective_track_max_frames == 3
    assert all(w.to_frame - w.from_frame + 1 <= 3 for w in plan.windows)
    assert all(
        w.context_span.to_frame - w.context_span.from_frame + 1 <= 3
        for w in plan.windows
    )


def test_backward_plan_preserves_directional_window_order():
    plan = _build(_facts(direction="backward"))
    assert [(w.from_frame, w.to_frame) for w in plan.windows] == [
        (9, 9),
        (6, 8),
        (3, 5),
        (0, 2),
    ]
    assert all(w.branch == "backward" for w in plan.windows)


def test_correction_separates_backward_then_forward_seed_branches():
    plan = _build(
        _facts(job_kind="correction", direction="bidirectional", correction_frame=4)
    )
    assert [
        (w.from_frame, w.to_frame, w.branch, w.is_seed_window) for w in plan.windows
    ] == [
        (3, 4, "backward", True),
        (0, 2, "backward", False),
        (4, 6, "forward", True),
        (7, 9, "forward", False),
    ]
    assert plan.windows[0].seed_source == plan.windows[2].seed_source == "correction"


def test_native_mask_correction_does_not_silently_split_its_seed_context():
    with pytest.raises(
        VideoExecutionContractFailure, match="invalid_video_execution_plan"
    ):
        _build(
            _facts(job_kind="correction", direction="forward", correction_frame=4),
            native_correction=True,
        )


@pytest.mark.parametrize(
    "direction,discovery", [("forward", (0, 1)), ("backward", (8, 9))]
)
def test_combo_freezes_both_roles_and_discovers_the_directional_seed_frame(
    direction, discovery
):
    plan = _build(
        _facts(requested_model_key="sam3_video_combo", direction=direction),
        [
            _backend(model_key="sam3_video_interactive", context=10),
            _backend(role="discovery", model_key="sam3_video", window=2),
        ],
    )
    assert (plan.windows[0].from_frame, plan.windows[0].to_frame) == discovery
    assert plan.windows[0].phase == "discovery" and plan.windows[0].branch == "forward"
    assert plan.windows[1].seed_source == "discovery"
    assert all(w.context_span.mode == "none" for w in plan.windows)
    assert plan.window_settings.effective_discovery_max_frames == 2


def test_accepted_correction_bound_is_applied_before_a_plan_can_be_frozen():
    settings = freeze_video_window_settings(
        requested_model_key="sam2_video",
        platform_default_frames=128,
        platform_sam3_frames=16,
        backend_compatibility=[_backend(window=32)],
        accepted_correction_max_frames=4,
    )
    assert settings.effective_track_max_frames == 4


@pytest.mark.parametrize("bound", [True, 0, "8", 8.0])
def test_window_settings_reject_coerced_platform_bounds(bound):
    with pytest.raises(VideoExecutionContractFailure, match="invalid_window_settings"):
        freeze_video_window_settings(
            requested_model_key="sam2_video",
            platform_default_frames=bound,
            platform_sam3_frames=16,
            backend_compatibility=[_backend()],
        )


def test_mutated_typed_facts_are_revalidated_before_planning():
    facts = _facts()
    invalid = facts.model_copy(
        update={"source": facts.source.model_copy(update={"width": -1})}
    )
    with pytest.raises(VideoExecutionContractFailure):
        _build(invalid)
