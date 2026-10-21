from __future__ import annotations

import httpx
import pytest
import uuid
from copy import deepcopy
from types import SimpleNamespace
from aap_protocol_v2 import (
    CocoRlePayload,
    NativeMaskCandidate,
    NativeMaskCandidateValue,
    encode_low_res_mask,
    native_mask_candidate_id,
)
from fastapi import HTTPException

from aap_inference.backend_transport import bounded_stream_response
from app.services.ml_interaction_proxy import (
    normalize_native_mask_response,
    prepare_interactive_context,
)
from app.services.ai_mask_session import issue_ai_mask_session


def _capabilities(*, interactive_outputs: list[str]) -> dict:
    return {
        "models": [
            {
                "id": "image-interactive",
                "task": "interactive_seg",
                "is_interactive": True,
                "supported_prompts": [
                    "exemplar",
                    "point",
                    "interactive_box",
                    "mask",
                    "scribble",
                ],
                "supported_inputs": [
                    "full_image",
                    "point_prompt",
                    "bbox_prompt",
                    "mask_prompt",
                    "scribble_prompt",
                ],
                "supported_geometric_outputs": interactive_outputs,
            },
            {
                "id": "video-tracker",
                "task": "tracker",
                "is_interactive": True,
                "supported_prompts": ["bbox"],
                "supported_geometric_outputs": ["bbox", "polygon", "mask"],
            },
        ]
    }


def _candidate(revision: str, candidate_index: int = 0) -> dict:
    rle = CocoRlePayload(
        encoding="coco_rle",
        size=[2, 3],
        counts=[1, 2, 2, 1],
    )
    return NativeMaskCandidate(
        value=NativeMaskCandidateValue(
            rle=rle,
            masklabels=["object"],
            preview={
                "points": [
                    [0.0, 0.0],
                    [1.0, 0.0],
                    [1.0, 1.0],
                    [0.0, 1.0],
                ]
            },
        ),
        score=0.9,
        candidate_id=native_mask_candidate_id(
            rle,
            prompt_revision=revision,
            candidate_index=candidate_index,
        ),
    ).model_dump(mode="json")


def test_tracker_mask_capability_does_not_authorize_image_model() -> None:
    with pytest.raises(HTTPException) as raised:
        prepare_interactive_context(
            {"type": "point", "output_geometry": "mask"},
            _capabilities(interactive_outputs=["polygon"]),
            task_id="task-1",
        )
    assert raised.value.status_code == 422
    assert raised.value.detail["reason"] == "unsupported_output_geometry"
    assert raised.value.detail["model_id"] == "image-interactive"


def test_native_context_resolves_model_and_replaces_client_revision() -> None:
    context, model_id = prepare_interactive_context(
        {
            "type": "point",
            "points": [[0.25, 0.5]],
            "output_geometry": "mask",
            "prompt_revision": "client-controlled",
        },
        _capabilities(interactive_outputs=["polygon", "mask"]),
        task_id="task-1",
    )
    assert model_id == "image-interactive"
    assert context["model_id"] == model_id
    assert context["prompt_revision"] != "client-controlled"
    assert len(context["prompt_revision"]) == 64


def test_signed_mask_session_is_unwrapped_and_bound_to_context() -> None:
    import numpy as np

    raw = encode_low_res_mask(np.zeros((256, 256), dtype=np.float32))
    token = issue_ai_mask_session(
        raw,
        {
            "task_id": "task-1",
            "frame_index": 4,
            "requested_backend_id": "backend-1",
            "model_id": "image-interactive",
            "source": None,
            "model_variants": {},
            "origin_prompt_revision": "origin",
            "candidate_id": "sha256:" + "1" * 64,
            "candidate_index": 0,
        },
    )

    context, _ = prepare_interactive_context(
        {
            "type": "point",
            "points": [[0.25, 0.5]],
            "output_geometry": "mask",
            "mask_input": token,
        },
        _capabilities(interactive_outputs=["polygon", "mask"]),
        task_id="task-1",
        frame_index=4,
        requested_backend_id="backend-1",
    )

    assert context["mask_input"] == raw
    assert token not in context["prompt_revision"]


def test_signed_mask_session_rejects_cross_frame_reuse() -> None:
    import numpy as np

    raw = encode_low_res_mask(np.zeros((256, 256), dtype=np.float32))
    token = issue_ai_mask_session(
        raw,
        {
            "task_id": "task-1",
            "frame_index": 4,
            "requested_backend_id": "backend-1",
            "model_id": "image-interactive",
            "source": None,
            "model_variants": {},
        },
    )
    with pytest.raises(HTTPException) as caught:
        prepare_interactive_context(
            {
                "type": "point",
                "points": [[0.25, 0.5]],
                "output_geometry": "mask",
                "mask_input": token,
            },
            _capabilities(interactive_outputs=["polygon", "mask"]),
            task_id="task-1",
            frame_index=5,
            requested_backend_id="backend-1",
        )
    assert caught.value.status_code == 409
    assert caught.value.detail["reason"] == "mask_session_mismatch"


def test_scribble_schema_rejects_negative_only_without_seed() -> None:
    with pytest.raises(HTTPException) as caught:
        prepare_interactive_context(
            {
                "type": "scribble",
                "scribbles": [
                    {
                        "polarity": 0,
                        "points": [[0.2, 0.2], [0.4, 0.4]],
                        "width": 0.01,
                    }
                ],
                "output_geometry": "mask",
            },
            _capabilities(interactive_outputs=["polygon", "mask"]),
            task_id="task-1",
        )
    assert caught.value.status_code == 422
    assert caught.value.detail["reason"] == "negative_scribble_requires_seed"


def test_scribble_schema_enforces_total_point_budget_before_backend() -> None:
    with pytest.raises(HTTPException) as caught:
        prepare_interactive_context(
            {
                "type": "scribble",
                "scribbles": [
                    {
                        "polarity": 1,
                        "points": [[0.5, 0.5]] * 8_193,
                        "width": 0.01,
                    }
                ],
                "output_geometry": "mask",
            },
            _capabilities(interactive_outputs=["polygon", "mask"]),
            task_id="task-1",
        )
    assert caught.value.status_code == 422
    assert caught.value.detail["reason"] == "invalid_scribble_prompt"


def test_mask_prompt_requires_declared_model_input() -> None:
    capabilities = _capabilities(interactive_outputs=["polygon", "mask"])
    capabilities["models"][0]["supported_inputs"].remove("mask_prompt")
    with pytest.raises(HTTPException) as caught:
        prepare_interactive_context(
            {
                "type": "point",
                "points": [[0.25, 0.5]],
                "output_geometry": "mask",
                "mask_prompt": {
                    "rle": {
                        "encoding": "coco_rle",
                        "size": [1, 1],
                        "counts": [0, 1],
                    },
                    "source_annotation_id": "source-1",
                    "source_version": 1,
                    "source_digest": "0" * 64,
                },
            },
            capabilities,
            task_id="task-1",
        )
    assert caught.value.status_code == 422
    assert caught.value.detail["reason"] == "unsupported_input"


def test_automatic_model_selection_uses_required_inputs_before_ambiguity() -> None:
    capabilities = _capabilities(interactive_outputs=["polygon", "mask"])
    first = capabilities["models"][0]
    capabilities["models"].insert(
        1,
        {
            **first,
            "id": "point-only",
            "supported_inputs": ["full_image", "point_prompt"],
        },
    )
    context, model_id = prepare_interactive_context(
        {
            "type": "point",
            "points": [[0.25, 0.5]],
            "output_geometry": "mask",
            "mask_prompt": {
                "rle": {
                    "encoding": "coco_rle",
                    "size": [1, 1],
                    "counts": [0, 1],
                },
                "source_annotation_id": "source-1",
                "source_version": 1,
                "source_digest": "0" * 64,
            },
        },
        capabilities,
        task_id="task-1",
    )
    assert model_id == "image-interactive"
    assert context["model_id"] == "image-interactive"


def test_equivalent_variant_forms_share_revision_and_sessions_reject_switches() -> None:
    import numpy as np

    capabilities = _capabilities(interactive_outputs=["polygon", "mask"])
    capabilities["models"][0]["default_variants"] = {"sam_variant": "tiny"}
    legacy, _ = prepare_interactive_context(
        {
            "type": "point",
            "points": [[0.25, 0.5]],
            "output_geometry": "mask",
            "sam_variant": "small",
        },
        capabilities,
        task_id="task-1",
        requested_backend_id="backend-1",
    )
    canonical, _ = prepare_interactive_context(
        {
            "type": "point",
            "points": [[0.25, 0.5]],
            "output_geometry": "mask",
            "model_variants": {"sam_variant": "small"},
        },
        capabilities,
        task_id="task-1",
        requested_backend_id="backend-1",
    )
    assert legacy["prompt_revision"] == canonical["prompt_revision"]
    assert legacy["model_variants"] == {"sam_variant": "small"}
    assert "sam_variant" not in legacy

    raw = encode_low_res_mask(np.zeros((256, 256), dtype=np.float32))
    token = issue_ai_mask_session(
        raw,
        {
            "task_id": "task-1",
            "frame_index": None,
            "requested_backend_id": "backend-1",
            "model_id": "image-interactive",
            "source": None,
            "model_variants": {"sam_variant": "small"},
        },
    )
    with pytest.raises(HTTPException) as caught:
        prepare_interactive_context(
            {
                "type": "point",
                "points": [[0.25, 0.5]],
                "output_geometry": "mask",
                "model_variants": {"sam_variant": "large"},
                "mask_input": token,
            },
            capabilities,
            task_id="task-1",
            requested_backend_id="backend-1",
        )
    assert caught.value.status_code == 409
    assert caught.value.detail["reason"] == "mask_session_mismatch"


def test_native_response_preserves_non_square_rle() -> None:
    context, _ = prepare_interactive_context(
        {"type": "point", "output_geometry": "mask"},
        _capabilities(interactive_outputs=["polygon", "mask"]),
        task_id="task-1",
    )
    raw = _candidate(context["prompt_revision"])
    result, diagnostic = normalize_native_mask_response(
        [raw],
        None,
        context=context,
        expected_size=(3, 2),
    )
    assert result == [raw]
    assert diagnostic is None


def _native_pairs(revision: str) -> list[dict]:
    results = []
    for index in (1, 3):
        results.extend(
            [
                {
                    "type": "rectanglelabels",
                    "value": {
                        "x": 0.1,
                        "y": 0.2,
                        "width": 0.6,
                        "height": 0.7,
                        "rectanglelabels": ["object"],
                    },
                    "score": 0.9,
                },
                _candidate(revision, index),
            ]
        )
    return results


def test_native_exemplar_both_preserves_pairs_and_full_array_indices() -> None:
    raw = _native_pairs("revision")
    normalized, diagnostic = normalize_native_mask_response(
        raw,
        None,
        context={
            "type": "exemplar",
            "output": "both",
            "output_geometry": "mask",
            "prompt_revision": "revision",
        },
        expected_size=(3, 2),
    )
    assert normalized == raw
    assert diagnostic is None


@pytest.mark.parametrize("output_geometry", ["polygon", "mask"])
def test_native_companion_boxes_are_negotiated_by_platform(output_geometry) -> None:
    context, _ = prepare_interactive_context(
        {
            "type": "exemplar",
            "output": "both",
            "output_geometry": output_geometry,
            "native_mask_companion_boxes": "client-controlled",
        },
        _capabilities(interactive_outputs=["polygon", "mask"]),
        task_id="task-1",
    )
    if output_geometry == "mask":
        assert context["native_mask_companion_boxes"] is True
    else:
        assert "native_mask_companion_boxes" not in context


@pytest.mark.parametrize("frame_index", [None, 7])
def test_native_exemplar_both_signs_only_masks_at_original_indices(frame_index) -> None:
    from app.api.v1.ml_backends import _interactive_response
    from app.services.ai_mask_receipt import verify_ai_mask_receipt
    from aap_inference.backend_transport import PredictionResult

    raw = _native_pairs("revision")
    task_id, backend_id = uuid.uuid4(), uuid.uuid4()
    response = _interactive_response(
        PredictionResult(task_id=str(task_id), result=raw),
        context={
            "type": "exemplar",
            "output": "both",
            "output_geometry": "mask",
            "prompt_revision": "revision",
        },
        expected_size=(3, 2),
        client=SimpleNamespace(pool_id=None, last_instance_id=backend_id),
        requested_backend_id=backend_id,
        model_id="sam3-interactive-seg",
        task_id=task_id,
        frame_index=frame_index,
    )
    assert response["result"] == raw
    assert len(response["accept_receipts"]) == 2
    assert response["mask_input_next"] is None
    for index in (1, 3):
        candidate_id = raw[index]["candidate_id"]
        receipt = verify_ai_mask_receipt(response["accept_receipts"][candidate_id])
        assert receipt["candidate_index"] == index
        assert receipt["candidate_id"] == candidate_id
        assert receipt["frame_index"] == frame_index


@pytest.mark.parametrize(
    "case",
    [
        "point",
        "mask_only",
        "unpaired",
        "wrong_order",
        "wrong_label",
        "wrong_score",
        "zero_width",
        "out_of_bounds",
        "nan",
        "boolean",
        "extra",
        "mask_id",
        "mask_rle",
    ],
)
def test_native_exemplar_both_rejects_invalid_pairs(case: str) -> None:
    raw = deepcopy(_native_pairs("revision"))
    context = {
        "type": "exemplar",
        "output": "both",
        "output_geometry": "mask",
        "prompt_revision": "revision",
    }
    if case == "point":
        context["type"] = "point"
    elif case == "mask_only":
        context["output"] = "mask"
    elif case == "unpaired":
        raw.pop()
    elif case == "wrong_order":
        raw[0], raw[1] = raw[1], raw[0]
    elif case == "wrong_label":
        raw[0]["value"]["rectanglelabels"] = ["other"]
    elif case == "wrong_score":
        raw[0]["score"] = 0.5
    elif case == "zero_width":
        raw[0]["value"]["width"] = 0
    elif case == "out_of_bounds":
        raw[0]["value"]["width"] = 1
    elif case == "nan":
        raw[0]["value"]["x"] = float("nan")
    elif case == "boolean":
        raw[0]["value"]["x"] = True
    elif case == "extra":
        raw[0]["candidate_id"] = raw[1]["candidate_id"]
    elif case == "mask_id":
        raw[1]["candidate_id"] = _candidate("revision", 0)["candidate_id"]
    elif case == "mask_rle":
        raw[1]["value"]["rle"]["counts"] = [1]
    with pytest.raises(HTTPException) as raised:
        normalize_native_mask_response(raw, None, context=context, expected_size=(3, 2))
    assert raised.value.status_code == 502
    assert raised.value.detail["reason"] == "invalid_mask_payload"


def test_native_response_rejects_candidate_id_mismatch() -> None:
    context, _ = prepare_interactive_context(
        {"type": "point", "output_geometry": "mask"},
        _capabilities(interactive_outputs=["polygon", "mask"]),
        task_id="task-1",
    )
    raw = _candidate(context["prompt_revision"])
    raw["candidate_id"] = "sha256:" + "0" * 64
    with pytest.raises(HTTPException) as raised:
        normalize_native_mask_response([raw], None, context=context)
    assert raised.value.status_code == 502
    assert raised.value.detail["reason"] == "invalid_mask_payload"


def test_native_response_classifies_oversized_dimension_as_413() -> None:
    context = {"output_geometry": "mask", "prompt_revision": "revision"}
    raw = {
        "type": "mask",
        "value": {
            "rle": {
                "encoding": "coco_rle",
                "size": [1, 4097],
                "counts": [4097],
            },
            "masklabels": ["object"],
        },
        "score": 0.5,
        "candidate_id": "sha256:" + "0" * 64,
    }
    with pytest.raises(HTTPException) as raised:
        normalize_native_mask_response([raw], None, context=context)
    assert raised.value.status_code == 413
    assert raised.value.detail["reason"] == "mask_payload_too_large"


def test_empty_native_response_requires_and_keeps_diagnostic() -> None:
    context = {"output_geometry": "mask", "prompt_revision": "revision"}
    result, diagnostic = normalize_native_mask_response(
        [],
        {"reason": "empty_mask", "retryable": False},
        context=context,
    )
    assert result == []
    assert diagnostic == {"reason": "empty_mask", "retryable": False}

    with pytest.raises(HTTPException) as raised:
        normalize_native_mask_response([], None, context=context)
    assert raised.value.status_code == 502
    assert raised.value.detail["reason"] == "invalid_mask_payload"


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_bounded_response_rejects_declared_size_before_read() -> None:
    response = httpx.Response(
        200,
        headers={"content-length": "5"},
        stream=_ChunkStream([b"12345"]),
        request=httpx.Request("POST", "http://backend/predict"),
    )
    with pytest.raises(HTTPException) as raised:
        await bounded_stream_response(
            response, max_bytes=4, error_factory=HTTPException
        )
    assert raised.value.status_code == 413
    assert raised.value.detail["reason"] == "mask_response_too_large"


@pytest.mark.asyncio
async def test_bounded_response_rejects_chunked_overflow() -> None:
    response = httpx.Response(
        200,
        stream=_ChunkStream([b"12", b"345"]),
        request=httpx.Request("POST", "http://backend/predict"),
    )
    with pytest.raises(HTTPException) as raised:
        await bounded_stream_response(
            response, max_bytes=4, error_factory=HTTPException
        )
    assert raised.value.status_code == 413
    assert raised.value.detail["reason"] == "mask_response_too_large"


@pytest.mark.asyncio
async def test_bounded_response_accepts_exact_limit() -> None:
    response = httpx.Response(
        200,
        stream=_ChunkStream([b"12", b"34"]),
        request=httpx.Request("POST", "http://backend/predict"),
    )
    bounded = await bounded_stream_response(
        response, max_bytes=4, error_factory=HTTPException
    )
    assert bounded.content == b"1234"
