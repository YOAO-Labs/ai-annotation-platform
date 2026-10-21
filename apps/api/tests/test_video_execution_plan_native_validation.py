"""Native plan validation rejects structurally rehashed invalid video plans."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import uuid

import pytest
from sqlalchemy import text

from aap_preannotation_results.video_execution_plan import (
    BackendCompatibilityIdentity,
    VideoSourceIdentity,
    VideoTimebaseIdentity,
    canonical_video_json,
)
from aap_preannotation_results.video_execution_planner import (
    VideoExecutionInputFacts,
    build_video_execution_plan,
    freeze_video_window_settings,
)

pytestmark = pytest.mark.asyncio


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_video_json(value)).hexdigest()


def _random_digest() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _rehash_plan(plan: dict) -> dict:
    for window in plan["windows"]:
        span = window["context_span"]
        span["span_digest"] = _digest(
            {key: value for key, value in span.items() if key != "span_digest"}
        )
        window["window_digest"] = _digest(
            {key: value for key, value in window.items() if key != "window_digest"}
        )
    plan["plan_digest"] = _digest(
        {key: value for key, value in plan.items() if key != "plan_digest"}
    )
    return plan


def _facts(
    *,
    frame_count: int = 6,
    requested_model_key: str = "sam2_video",
    job_kind: str = "tracking",
    direction: str = "forward",
    correction_frame: int | None = None,
) -> VideoExecutionInputFacts:
    source = VideoSourceIdentity(
        source_version_id="video-version-17",
        source_sha256=_random_digest(),
        byte_size=8192,
        frame_count=frame_count,
        width=640,
        height=480,
        duration_ms=6000,
        video_stream=0,
    )
    timebase = VideoTimebaseIdentity(
        timebase_digest=_random_digest(),
        selected_frame_count=frame_count,
        sampling_step=1,
        grid_step=1,
    )
    return VideoExecutionInputFacts(
        job_id=uuid.uuid4(),
        input_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        source_item_id=None,
        request_digest=_random_digest(),
        prompt_digest=_random_digest(),
        source_plan_digest=_random_digest(),
        requested_model_key=requested_model_key,
        job_kind=job_kind,
        direction=direction,
        from_frame=0,
        to_frame=frame_count - 1,
        grid_step=1,
        correction_frame=correction_frame,
        segment_id=None,
        source=source,
        timebase=timebase,
    )


def _backend(
    *,
    role: str = "track",
    model_key: str = "sam2_video",
    context_mode: str = "none",
    max_context_frames: int = 0,
    max_window_frames: int = 2,
) -> BackendCompatibilityIdentity:
    return BackendCompatibilityIdentity(
        role=role,
        registry_id=uuid.uuid4(),
        backend_incarnation=uuid.uuid4(),
        model_key=model_key,
        model_id=f"{model_key}-model",
        weights_release_id="release-2026-10",
        weights_sha256=_random_digest(),
        tracker_context_mode=context_mode,
        max_context_frames=max_context_frames,
        max_window_frames=max_window_frames,
        capability_digest=_random_digest(),
    )


def _plan(
    facts: VideoExecutionInputFacts,
    backends: list[BackendCompatibilityIdentity] | None = None,
):
    if backends is None:
        backends = [_backend(model_key=facts.requested_model_key)]
    settings = freeze_video_window_settings(
        requested_model_key=facts.requested_model_key,
        platform_default_frames=2,
        platform_sam3_frames=5,
        backend_compatibility=backends,
        combo_discovery_window_frames=(
            5 if facts.requested_model_key == "sam3_video_combo" else None
        ),
    )
    correction = facts.job_kind == "correction"
    combo = facts.requested_model_key == "sam3_video_combo"
    return build_video_execution_plan(
        job_id=facts.job_id,
        facts=facts,
        window_settings=settings,
        backend_compatibility=backends,
        output_geometry="bbox",
        initial_seed_source="none"
        if combo
        else "correction"
        if correction
        else "prompt",
        initial_seed_digest=None if combo else _random_digest(),
    )


def _wire(value) -> dict:
    return value.model_dump(mode="json", by_alias=True)


def _scope_for(plan) -> dict:
    track = plan.backend_compatibility[0]
    return {
        "owner": f"tracker:{plan.request_id}",
        "operation": "tracker",
        "closed": False,
        "state": "active",
        "registry_id": str(track.registry_id),
    }


async def _native_valid(db, *, plan: dict, facts: VideoExecutionInputFacts, scope):
    result = await db.scalar(
        text(
            "SELECT aap_jobs_v1._video_checkpoint_plan_valid("
            "CAST(:plan AS jsonb),CAST(:facts AS jsonb),:job_id,CAST(:scope AS jsonb))"
        ),
        {
            "plan": canonical_video_json(plan).decode("utf-8"),
            "facts": canonical_video_json(facts).decode("utf-8"),
            "job_id": facts.job_id,
            "scope": canonical_video_json(scope).decode("utf-8"),
        },
    )
    return result is True


async def test_native_plan_validator_rejects_rehashed_gaps_overlaps_and_truncation(
    db_session,
):
    facts = _facts()
    plan = _plan(facts)
    scope = _scope_for(plan)
    assert await _native_valid(db_session, plan=_wire(plan), facts=facts, scope=scope)

    truncated = _wire(plan)
    truncated["windows"].pop()
    assert not await _native_valid(
        db_session, plan=_rehash_plan(truncated), facts=facts, scope=scope
    )

    for start, end in ((3, 3), (1, 2)):
        altered = deepcopy(_wire(plan))
        window = altered["windows"][1]
        window["from_frame"] = start
        window["to_frame"] = end
        window["context_span"]["from_frame"] = start
        window["context_span"]["to_frame"] = end
        assert not await _native_valid(
            db_session, plan=_rehash_plan(altered), facts=facts, scope=scope
        )

    for effective_limit in (0, 3):
        altered = deepcopy(_wire(plan))
        altered["window_settings"]["effective_track_max_frames"] = effective_limit
        assert not await _native_valid(
            db_session, plan=_rehash_plan(altered), facts=facts, scope=scope
        )


async def test_native_plan_validator_requires_backward_then_forward_correction_branches(
    db_session,
):
    facts = _facts(job_kind="correction", direction="bidirectional", correction_frame=3)
    plan = _plan(facts)
    scope = _scope_for(plan)
    assert await _native_valid(db_session, plan=_wire(plan), facts=facts, scope=scope)
    altered = _wire(plan)
    backward = [
        window for window in altered["windows"] if window["branch"] == "backward"
    ]
    forward = [window for window in altered["windows"] if window["branch"] == "forward"]
    reordered = forward + backward
    for ordinal, window in enumerate(reordered):
        window["ordinal"] = ordinal
        span = window["context_span"]
        span["start_ordinal"] = ordinal
        span["end_ordinal"] = ordinal
    altered["windows"] = reordered

    assert not await _native_valid(
        db_session,
        plan=_rehash_plan(altered),
        facts=facts,
        scope=scope,
    )


async def test_native_plan_validator_requires_identical_context_span_documents(
    db_session,
):
    facts = _facts()
    backend = _backend(context_mode="session", max_context_frames=6)
    plan = _plan(facts, [backend])
    scope = _scope_for(plan)
    assert await _native_valid(db_session, plan=_wire(plan), facts=facts, scope=scope)
    altered = _wire(plan)
    assert len(altered["windows"]) == 3
    span = altered["windows"][1]["context_span"]
    span["from_frame"] = 2

    assert not await _native_valid(
        db_session,
        plan=_rehash_plan(altered),
        facts=facts,
        scope=scope,
    )

    over_context_limit = _wire(plan)
    over_context_limit["backend_compatibility"][0]["max_context_frames"] = 4
    assert not await _native_valid(
        db_session,
        plan=_rehash_plan(over_context_limit),
        facts=facts,
        scope=scope,
    )


async def test_native_plan_validator_binds_combo_discovery_to_direction_anchor(
    db_session,
):
    facts = _facts(
        frame_count=6,
        requested_model_key="sam3_video_combo",
        direction="backward",
    )
    backends = [
        _backend(role="track", model_key="sam3_video_interactive"),
        _backend(role="discovery", model_key="sam3_video", max_window_frames=5),
    ]
    plan = _plan(facts, backends)
    scope = _scope_for(plan)
    assert await _native_valid(db_session, plan=_wire(plan), facts=facts, scope=scope)
    altered = _wire(plan)
    discovery = altered["windows"][0]
    discovery["from_frame"] = 3
    discovery["to_frame"] = 4
    discovery["context_span"]["from_frame"] = 3
    discovery["context_span"]["to_frame"] = 4

    assert not await _native_valid(
        db_session,
        plan=_rehash_plan(altered),
        facts=facts,
        scope=scope,
    )

    invalid_discovery_cap = _wire(plan)
    invalid_discovery_cap["window_settings"]["effective_discovery_max_frames"] = 0
    assert not await _native_valid(
        db_session,
        plan=_rehash_plan(invalid_discovery_cap),
        facts=facts,
        scope=scope,
    )
