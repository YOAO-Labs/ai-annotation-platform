from __future__ import annotations

import re
import uuid
from types import SimpleNamespace

import pytest

from app.services.video_tracking import backend_identity
from app.services.video_tracking.backend_identity import (
    VideoBackendIdentityError,
    bind_video_normalization_policy,
    build_video_backend_compatibility,
)


def _model(
    *,
    model_id: str = "tracker-v1",
    tracker: str = "sam2_video",
    max_window_frames: int = 12,
    max_context_frames: int = 48,
    weights: bool = True,
) -> dict:
    value = {
        "id": model_id,
        "supported_trackers": [tracker],
        "supported_prompts": ["bbox"],
        "supported_inputs": ["video", "bbox_prompt"],
        "supported_geometric_outputs": ["bbox", "polygon"],
        "text_driven_trackers": [],
        "tracker_context_mode": "session",
        "max_context_frames": max_context_frames,
        "max_window_frames": max_window_frames,
    }
    if weights:
        value.update(
            {
                "weights_release_id": "sam2-release-7",
                "weights_sha256": "a" * 64,
            }
        )
    return value


def _backend(*models: dict) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "health_meta": {"capabilities": {"models": list(models)}},
    }


def _runtime_facts(backend_id: str, *, epoch: str | None = None) -> tuple[dict, dict]:
    authority_epoch = epoch or str(uuid.uuid4())
    backend_incarnation = str(uuid.uuid4())
    return (
        {
            "registries": [
                {
                    "registry_id": backend_id,
                    "continuity": "ready",
                    "authority_epoch": authority_epoch,
                    "backend_incarnation": backend_incarnation,
                }
            ]
        },
        {
            "registry_id": backend_id,
            "authority_epoch": authority_epoch,
        },
    )


def test_registry_identity_uses_selected_backend_model_and_matching_epoch() -> None:
    backend = _backend(_model())
    runtime, scope = _runtime_facts(backend["id"])

    identity = build_video_backend_compatibility(
        model_key="sam2_video",
        selected_backend=backend,
        runtime_snapshot=runtime,
        bound_scope=scope,
    )[0]

    assert identity.provider_kind == "registry"
    assert str(identity.registry_id) == backend["id"]
    assert (
        str(identity.backend_incarnation)
        == runtime["registries"][0]["backend_incarnation"]
    )
    assert identity.model_id == "tracker-v1"
    assert identity.weights_release_id == "sam2-release-7"
    assert identity.weights_sha256 == "a" * 64
    assert identity.max_window_frames == 12
    assert identity.tracker_context_mode == "session"
    assert identity.max_context_frames == 48
    assert identity.identity_complete is True


def test_unmatched_runtime_epoch_keeps_registry_identity_incomplete() -> None:
    backend = _backend(_model())
    runtime, scope = _runtime_facts(backend["id"])
    scope["authority_epoch"] = str(uuid.uuid4())

    identity = build_video_backend_compatibility(
        model_key="sam2_video",
        selected_backend=backend,
        runtime_snapshot=runtime,
        bound_scope=scope,
    )[0]

    assert identity.registry_id == uuid.UUID(backend["id"])
    assert identity.backend_incarnation is None
    assert identity.identity_complete is False


def test_registry_identity_does_not_invent_missing_weight_facts() -> None:
    backend = _backend(_model(weights=False))

    identity = build_video_backend_compatibility(
        model_key="sam2_video", selected_backend=backend
    )[0]

    assert identity.weights_release_id is None
    assert identity.weights_sha256 is None
    assert identity.backend_incarnation is None
    assert identity.identity_complete is False


def test_capability_digest_tracks_actual_window_and_session_limits() -> None:
    small = _backend(_model(max_window_frames=8, max_context_frames=32))
    large = _backend(_model(max_window_frames=16, max_context_frames=64))

    small_identity = build_video_backend_compatibility(
        model_key="sam2_video", selected_backend=small
    )[0]
    large_identity = build_video_backend_compatibility(
        model_key="sam2_video", selected_backend=large
    )[0]

    assert small_identity.capability_digest is not None
    assert large_identity.capability_digest is not None
    assert small_identity.capability_digest != large_identity.capability_digest


def test_combo_identity_keeps_discovery_and_tracking_model_facts_separate() -> None:
    backend = _backend(
        _model(model_id="interactive-v2", tracker="sam3_video_interactive"),
        _model(model_id="multiplex-v4", tracker="sam3_video", max_window_frames=5),
    )

    track, discovery = build_video_backend_compatibility(
        model_key="sam3_video_interactive",
        selected_backend=backend,
        discovery_backend=backend,
    )

    assert track.role == "track"
    assert track.model_id == "interactive-v2"
    assert track.max_window_frames == 12
    assert discovery.role == "discovery"
    assert discovery.model_key == "sam3_video"
    assert discovery.model_id == "multiplex-v4"
    assert discovery.max_window_frames == 5
    assert track.registry_id == discovery.registry_id
    assert track.capability_digest != discovery.capability_digest


def test_ambiguous_model_capabilities_are_not_resolved_by_guessing() -> None:
    backend = _backend(_model(model_id="one"), _model(model_id="two"))

    with pytest.raises(VideoBackendIdentityError, match="ambiguous"):
        build_video_backend_compatibility(
            model_key="sam2_video", selected_backend=backend
        )


def test_builtin_mock_identity_hashes_loaded_source_without_registry() -> None:
    identity = build_video_backend_compatibility(
        model_key="mock_bbox", selected_backend=None
    )[0]

    assert identity.provider_kind == "builtin"
    assert identity.registry_id is None
    assert identity.backend_incarnation is None
    assert identity.builtin_implementation_id == "aap-mock-bbox/v1"
    assert identity.builtin_implementation_digest is not None
    assert re.fullmatch(r"[0-9a-f]{64}", identity.builtin_implementation_digest)
    assert identity.model_key == "mock_bbox"
    assert identity.model_id == "mock_bbox"


def test_builtin_source_unavailable_leaves_identity_incomplete(monkeypatch) -> None:
    def unavailable(_artifact):
        raise OSError("source unavailable")

    monkeypatch.setattr(backend_identity.inspect, "getsource", unavailable)
    identity = build_video_backend_compatibility(
        model_key="mock_bbox", selected_backend=None
    )[0]

    assert identity.builtin_implementation_digest is None
    assert identity.identity_complete is False


def test_registry_identity_uses_actual_row_id_not_capability_metadata() -> None:
    backend = _backend(_model())
    backend["health_meta"]["registry_id"] = str(uuid.uuid4())

    identity = build_video_backend_compatibility(
        model_key="sam2_video", selected_backend=backend
    )[0]

    assert str(identity.registry_id) == backend["id"]
    assert str(identity.registry_id) != backend["health_meta"]["registry_id"]


def test_builtin_provider_cannot_be_used_for_registry_tracker_keys() -> None:
    with pytest.raises(VideoBackendIdentityError):
        build_video_backend_compatibility(model_key="sam2_video", selected_backend=None)


def test_legacy_aggregate_can_run_without_claiming_a_model_or_weight_identity():
    backend = SimpleNamespace(
        id=uuid.uuid4(),
        health_meta={
            "capabilities": {
                "supported_trackers": ["sam2_video"],
                "max_window_frames": 7,
                "weights_release_id": "aggregate-label",
                "weights_sha256": "a" * 64,
            }
        },
    )
    (identity,) = build_video_backend_compatibility(
        model_key="sam2_video", selected_backend=backend
    )
    assert identity.registry_id == backend.id
    assert identity.model_id is None
    assert identity.weights_release_id is None and identity.weights_sha256 is None
    assert identity.max_window_frames == 7
    assert identity.identity_complete is False


def test_real_postgres_driver_uuid_keeps_the_selected_registry_identity():
    from asyncpg.pgproto.pgproto import UUID as DriverUUID

    backend = _backend(_model())
    backend["id"] = DriverUUID(backend["id"])
    (identity,) = build_video_backend_compatibility(
        model_key="sam2_video", selected_backend=backend
    )
    assert str(identity.registry_id) == str(backend["id"])


def test_capability_identity_covers_model_defaults_and_registry_dispatch_parameters():
    backend = _backend(_model())
    (baseline,) = build_video_backend_compatibility(
        model_key="sam2_video", selected_backend=backend
    )
    backend["health_meta"]["capabilities"]["models"][0]["default_thresholds"] = {
        "score_threshold": 0.4
    }
    (changed_defaults,) = build_video_backend_compatibility(
        model_key="sam2_video", selected_backend=backend
    )
    assert changed_defaults.capability_digest != baseline.capability_digest
    backend["extra_params"] = {"score_threshold": 0.5}
    (changed_dispatch,) = build_video_backend_compatibility(
        model_key="sam2_video", selected_backend=backend
    )
    assert changed_dispatch.capability_digest != changed_defaults.capability_digest


def test_worker_normalization_policy_changes_the_compatibility_identity():
    identities = build_video_backend_compatibility(
        model_key="mock_bbox", selected_backend=None
    )
    first = bind_video_normalization_policy(identities, outside_threshold=0.2)
    assert first == bind_video_normalization_policy(identities, outside_threshold=0.20)
    assert first != bind_video_normalization_policy(identities, outside_threshold=0.3)
    unknown = (identities[0].model_copy(update={"capability_digest": None}),)
    assert (
        bind_video_normalization_policy(unknown, outside_threshold=0.2)[
            0
        ].capability_digest
        is None
    )
    with pytest.raises(VideoBackendIdentityError, match="normalization_policy_invalid"):
        bind_video_normalization_policy(identities, outside_threshold=float("nan"))


def test_frozen_outside_threshold_survives_a_runtime_setting_change(monkeypatch):
    from app.config import settings
    from app.services.video_tracking.adapters import _frame_result_from_payload

    payload = {
        "frame_index": 1,
        "confidence": 0.5,
        "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
    }
    monkeypatch.setattr(settings, "video_tracker_low_confidence_outside_threshold", 0.9)
    assert _frame_result_from_payload(payload).outside is True
    assert _frame_result_from_payload(payload, outside_threshold=0.2).outside is False
