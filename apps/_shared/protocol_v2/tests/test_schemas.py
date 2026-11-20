"""共享 schema 单测."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from yoao_protocol_v2 import (
    BatchPredictResponse,
    COMPAT_PROTOCOL_VERSIONS,
    CocoRlePayload,
    CorrectionFramePrompt,
    EvictRecord,
    LoadedKey,
    MaskInteractionDiagnostic,
    MaskInteractionFallback,
    MaskPromptPayload,
    ModelUnavailableError,
    NativeMaskCandidate,
    PoolStateSnapshot,
    PoolStatus,
    PROTOCOL_VERSION,
    PredictionResult,
    ScribblePrompt,
    TaskItem,
    TrackerContextControl,
    VariantNotSupportedError,
    WarmupResponse,
    canonical_rle_bytes,
    native_mask_candidate_id,
    normalize_context_model_variants,
)


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/native_mask_interaction.json").read_text()
)


def test_task_item_minimal() -> None:
    item = TaskItem(id="t1", file_path="/tmp/a.jpg")
    assert item.id == "t1"
    assert item.file_path == "/tmp/a.jpg"


def test_task_item_int_id_accepted() -> None:
    item = TaskItem(id=42, file_path="x.jpg")
    assert item.id == 42


def test_prediction_result_defaults() -> None:
    r = PredictionResult()
    assert r.task is None
    assert r.result == []
    assert r.score is None
    assert r.diagnostic is None
    assert r.cache_hit is None
    assert r.model_load_ms is None
    assert r.pool_state is None


def test_tracker_context_control_contract() -> None:
    started = TrackerContextControl(
        action="start",
        job_id="job-1",
        span_start_frame=10,
        span_end_frame=20,
        direction="forward",
    )
    assert started.token is None
    with pytest.raises(ValueError):
        TrackerContextControl(
            action="continue",
            job_id="job-1",
            direction="forward",
        )


def test_prediction_result_keeps_empty_mask_diagnostic() -> None:
    result = PredictionResult(
        result=[],
        diagnostic={"reason": "empty_mask", "retryable": False},
    )
    assert result.model_dump(mode="json", exclude_none=True)["diagnostic"] == {
        "reason": "empty_mask",
        "retryable": False,
    }


def test_batch_predict_response_round_trip() -> None:
    r = BatchPredictResponse(
        results=[
            PredictionResult(
                task="t1",
                result=[{"type": "rectanglelabels", "value": {"x": 10, "y": 10}}],
                score=0.9,
                model_version="yolo11s",
                inference_time_ms=42,
            )
        ]
    )
    dumped = r.model_dump()
    assert dumped["results"][0]["task"] == "t1"
    assert dumped["results"][0]["model_version"] == "yolo11s"
    assert dumped["results"][0]["inference_time_ms"] == 42


def test_batch_predict_response_empty() -> None:
    r = BatchPredictResponse(results=[])
    assert r.results == []


def test_task_item_missing_file_path_rejects() -> None:
    with pytest.raises(ValidationError) as exc:
        TaskItem(id="t1")  # type: ignore[call-arg]
    assert exc.value.errors()[0]["loc"] == ("file_path",)


def test_native_mask_interaction_fixture_contract() -> None:
    candidate = NativeMaskCandidate.model_validate(FIXTURE["candidate"])
    mask_prompt = MaskPromptPayload.model_validate(FIXTURE["mask_prompt"])
    scribble = ScribblePrompt.model_validate(
        {**FIXTURE["scribble"], "mask_prompt": FIXTURE["mask_prompt"]}
    )
    correction = CorrectionFramePrompt.model_validate(
        {**FIXTURE["correction_frame"], "mask_prompt": FIXTURE["mask_prompt"]}
    )
    diagnostic = MaskInteractionDiagnostic.model_validate(
        FIXTURE["empty_mask_diagnostic"]
    )
    fallback = MaskInteractionFallback.model_validate(FIXTURE["mask_prompt_fallback"])
    assert candidate.value.rle.size == [3, 4]
    assert candidate.value.preview is not None
    assert len(candidate.value.preview.points) == 4
    assert canonical_rle_bytes(candidate.value.rle) == (
        b'{"encoding":"coco_rle","size":[3,4],"counts":[0,1,11]}'
    )
    assert candidate.candidate_id == native_mask_candidate_id(
        candidate.value.rle,
        prompt_revision=FIXTURE["prompt_revision"],
        candidate_index=FIXTURE["candidate_index"],
    )
    assert mask_prompt.source_version == 3
    assert len(scribble.scribbles) == 2
    assert correction.frame_index == 12
    assert diagnostic.reason == "empty_mask"
    assert fallback.fallback_reason == "mask_prompt_unsupported"


def test_native_mask_preview_rejects_non_normalized_points() -> None:
    raw = json.loads(json.dumps(FIXTURE["candidate"]))
    raw["value"]["preview"]["points"][0] = [-0.1, 0.0]

    with pytest.raises(ValidationError) as exc:
        NativeMaskCandidate.model_validate(raw)
    assert exc.value.errors()[0]["loc"] == ("value", "preview")


def test_native_mask_candidate_id_binds_prompt_revision_and_index() -> None:
    rle = CocoRlePayload.model_validate(FIXTURE["candidate"]["value"]["rle"])
    first = native_mask_candidate_id(rle, prompt_revision="rev-1", candidate_index=0)
    assert first.startswith("sha256:") and len(first) == 71
    assert first != native_mask_candidate_id(
        rle, prompt_revision="rev-2", candidate_index=0
    )
    assert first != native_mask_candidate_id(
        rle, prompt_revision="rev-1", candidate_index=1
    )


def test_native_mask_candidate_rejects_empty_foreground() -> None:
    payload = {
        **FIXTURE["candidate"],
        "value": {
            **FIXTURE["candidate"]["value"],
            "rle": {"encoding": "coco_rle", "size": [3, 4], "counts": [12]},
        },
    }
    with pytest.raises(ValidationError) as exc:
        NativeMaskCandidate.model_validate(payload)
    assert exc.value.errors()[0]["loc"] == ("value",)


def test_unsupported_output_diagnostic_accepts_bbox_capability() -> None:
    diagnostic = MaskInteractionDiagnostic(
        reason="unsupported_output_geometry",
        supported_geometric_outputs=["bbox", "polygon"],
    )
    assert diagnostic.supported_geometric_outputs == ["bbox", "polygon"]


@pytest.mark.parametrize(
    "payload",
    [
        {"encoding": "coco_rle", "size": [3, 4], "counts": [13]},
        {"encoding": "coco_rle", "size": [3, 4], "counts": [12, -1, 1]},
        {"encoding": "coco_rle", "size": [4097, 1], "counts": [4097]},
    ],
)
def test_native_mask_rle_rejects_invalid_bounds(payload: dict) -> None:
    with pytest.raises(ValidationError):
        CocoRlePayload.model_validate(payload)


@pytest.mark.parametrize(
    ("polarity", "points", "location"),
    [
        (1, [[0.0, 0.0], [1.1, 0.5]], ("scribbles", 0)),
        (True, [[0.0, 0.0], [0.5, 0.5]], ("scribbles", 0, "polarity")),
    ],
    ids=["out-of-bounds-point", "boolean-polarity"],
)
def test_scribble_rejects_invalid_stroke(polarity, points, location) -> None:
    with pytest.raises(ValidationError) as exc:
        ScribblePrompt.model_validate(
            {"scribbles": [{"polarity": polarity, "points": points, "width": 0.01}]}
        )
    assert exc.value.errors()[0]["loc"] == location


# ---------- 运行时状态与预热 ----------


def test_prediction_result_with_cache_hit_and_pool_state() -> None:
    r = PredictionResult(
        cache_hit=True,
        model_load_ms=0,
        pool_state=PoolStateSnapshot(current_size=2, cap=4),
    )
    assert r.cache_hit is True
    assert r.model_load_ms == 0
    assert r.pool_state is not None
    assert r.pool_state.current_size == 2
    assert r.pool_state.cap == 4


def test_pool_status_minimal_empty() -> None:
    s = PoolStatus(cap=4, current_size=0)
    assert s.loaded_keys == []
    assert s.last_evict is None


def test_pool_status_with_loaded_keys_and_evict() -> None:
    now = datetime(2026, 6, 8, tzinfo=timezone.utc)
    s = PoolStatus(
        cap=4,
        current_size=1,
        loaded_keys=[
            LoadedKey(
                key="yolov11/s/detection", loaded_at=now, last_used_at=now, hit_count=3
            ),
        ],
        last_evict=EvictRecord(key="yolov8/x/detection", at=now, reason="lru"),
    )
    dumped = s.model_dump()
    assert dumped["loaded_keys"][0]["key"] == "yolov11/s/detection"
    assert dumped["loaded_keys"][0]["hit_count"] == 3
    assert dumped["last_evict"]["reason"] == "lru"


def test_evict_record_rejects_unknown_reason() -> None:
    with pytest.raises(ValidationError) as exc:
        EvictRecord(
            key="k",
            at=datetime(2026, 6, 8, tzinfo=timezone.utc),
            reason="nope",  # type: ignore[arg-type]
        )
    assert exc.value.errors()[0]["loc"] == ("reason",)


def test_warmup_response_defaults() -> None:
    r = WarmupResponse()
    assert r.ok is True
    assert r.model_load_ms is None
    assert r.cache_hit is False
    assert r.evicted is None


def test_warmup_response_with_evicted() -> None:
    r = WarmupResponse(
        model_load_ms=4500, cache_hit=False, evicted="yolov8/n/detection"
    )
    assert r.model_load_ms == 4500
    assert r.evicted == "yolov8/n/detection"


# ---------- Protocol v2.1 ----------


def test_protocol_version_constants() -> None:
    assert PROTOCOL_VERSION == "2.2"
    assert COMPAT_PROTOCOL_VERSIONS == ["2.1", "2.0"]


def test_normalize_context_model_variants_keeps_new_field() -> None:
    ctx, deprecated = normalize_context_model_variants(
        {"model_variants": {"series": "yolo11", "size": "s"}}
    )
    assert ctx["model_variants"] == {"series": "yolo11", "size": "s"}
    assert deprecated == []


def test_normalize_context_model_variants_merges_legacy_fields() -> None:
    ctx, deprecated = normalize_context_model_variants(
        {
            "model_variants": {"sam_variant": "small"},
            "variants": {"sam_variant": "tiny", "dino_variant": "T"},
            "model_variant": "sam3.1",
        }
    )
    assert ctx["model_variants"] == {
        "sam_variant": "small",
        "dino_variant": "T",
        "model_variant": "sam3.1",
    }
    assert set(deprecated) == {"context.variants", "context.model_variant"}


def test_shared_variant_error_body() -> None:
    err = VariantNotSupportedError("size", "z", ["s", "m"])
    assert err.status_code == 422
    assert err.detail["error_code"] == "variant_not_supported"
    assert err.detail["axis"] == "size"


def test_shared_model_unavailable_error_body_and_retry_after() -> None:
    err = ModelUnavailableError("sam=tiny/dino=T", "missing checkpoint")
    assert err.status_code == 503
    assert err.headers == {"Retry-After": "30"}
    assert err.detail["error_code"] == "model_unavailable"
