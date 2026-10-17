"""Video review SDK contracts: versioned edits, digests and destructive CLI guards."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ai_annotation.cli import video_review as video_review_cli
from ai_annotation.video_review import VideoReview
from ai_annotation.video_review_models import (
    CocoRleContent,
    VideoMaskKeyframeOperationRequest,
    VideoMaskKeyframeSaveRequest,
    VideoMaskContentUploadRequest,
    VideoTrackCompositionRequest,
    VideoTrackConvertToBboxesRequest,
    VideoTrackQualityAcceptRequest,
    VideoTrackQualityRunRequest,
)

runner = CliRunner()
TASK_ID = uuid4()
ANN_ID = uuid4()
RUN_ID = uuid4()
LEFT_SEGMENT_ID = uuid4()
RIGHT_SEGMENT_ID = uuid4()


class _Response:
    def __init__(self, value):
        self._value = value

    def json(self):
        return self._value


class _Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        value = self.responses.pop(0)
        return _Response(value)


def _annotation(annotation_id=ANN_ID):
    return {
        "id": str(annotation_id),
        "task_id": str(TASK_ID),
        "source": "manual",
        "annotation_type": "video_track_mask",
        "class_name": "person",
        "geometry": {"type": "video_track_mask", "keyframes": []},
        "version": 3,
    }


def _quality_run():
    return {
        "id": str(RUN_ID),
        "task_id": str(TASK_ID),
        "left_segment_id": str(LEFT_SEGMENT_ID),
        "right_segment_id": str(RIGHT_SEGMENT_ID),
        "async_job_id": None,
        "status": "completed",
        "progress_pct": 100,
        "input_digest": "a" * 64,
        "sampling_digest": "b" * 64,
        "fragments": [],
        "metrics": {},
        "pairs": [],
        "error_message": None,
        "accepted_by_id": None,
        "accepted_at": None,
        "stale_at": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "issues": [],
    }


def _mask_body():
    sha256 = "c" * 64
    return VideoMaskKeyframeSaveRequest.model_validate(
        {
            "mask": {
                "size": [5, 5],
                "object_key": f"raster-masks/sha256/cc/cc/{sha256}.json",
                "sha256": sha256,
                "runs": 3,
                "bytes": 19,
            }
        }
    )


def _mask_reference(*, gzip=False):
    sha256 = "d" * 64
    return {
        "encoding": "coco_rle_ref",
        "storage_encoding": "gzip" if gzip else "identity",
        "size": [2, 2],
        "object_key": (
            f"raster-masks/sha256/dd/dd/{sha256}.json{'.gz' if gzip else ''}"
        ),
        "sha256": sha256,
        "runs": 2,
        "bytes": 20,
    }


def _mask_content():
    return {"encoding": "coco_rle", "size": [2, 2], "counts": [0, 4]}


def test_mask_keyframe_edits_send_version_precondition_and_return_annotation():
    transport = _Transport([_annotation(), _annotation()])
    review = VideoReview(transport)

    saved = review.save_mask_keyframe(TASK_ID, ANN_ID, 4, 2, _mask_body())
    operated = review.operate_mask_keyframe(
        TASK_ID,
        ANN_ID,
        4,
        3,
        VideoMaskKeyframeOperationRequest(operation="restore_held"),
    )

    assert saved.version == operated.version == 3
    assert [(method, path) for method, path, _ in transport.calls] == [
        (
            "PUT",
            f"/tasks/{TASK_ID}/video/tracks/{ANN_ID}/mask-keyframes/4",
        ),
        (
            "PATCH",
            f"/tasks/{TASK_ID}/video/tracks/{ANN_ID}/mask-keyframes/4",
        ),
    ]
    assert transport.calls[0][2]["headers"] == {"If-Match": 'W/"2"'}
    assert transport.calls[1][2]["headers"] == {"If-Match": 'W/"3"'}
    assert transport.calls[0][2]["json"]["mask"]["sha256"] == "c" * 64
    assert transport.calls[1][2]["json"] == {"operation": "restore_held"}


def test_mask_keyframe_edits_reject_invalid_preconditions_before_http():
    transport = _Transport([])
    review = VideoReview(transport)

    with pytest.raises(ValueError, match="frame_index"):
        review.save_mask_keyframe(TASK_ID, ANN_ID, -1, 1, _mask_body())
    with pytest.raises(ValueError, match="expected_version"):
        review.operate_mask_keyframe(
            TASK_ID,
            ANN_ID,
            0,
            0,
            VideoMaskKeyframeOperationRequest(operation="delete_keyframe"),
        )
    assert transport.calls == []


def test_mask_content_upload_and_json_reads_use_all_four_contracts():
    reference = _mask_reference(gzip=True)
    content = _mask_content()
    transport = _Transport([reference, content, content, content])
    review = VideoReview(transport)

    uploaded = review.upload_mask_content(
        TASK_ID,
        VideoMaskContentUploadRequest.model_validate(
            {**content, "storage_encoding": "gzip"}
        ),
    )
    static = review.get_annotation_mask_content(ANN_ID)
    frame = review.get_annotation_frame_mask_content(ANN_ID, 2)
    predicted = review.get_prediction_mask_content(TASK_ID, RUN_ID, 1, 2)

    assert uploaded.object_key == reference["object_key"]
    assert static.counts == frame.counts == predicted.counts == [0, 4]
    assert [(method, path) for method, path, _ in transport.calls] == [
        ("POST", f"/tasks/{TASK_ID}/mask-content"),
        ("GET", f"/annotations/{ANN_ID}/mask-content"),
        ("GET", f"/annotations/{ANN_ID}/mask-content/2"),
        (
            "GET",
            f"/tasks/{TASK_ID}/predictions/{RUN_ID}/mask-content/1/2",
        ),
    ]
    assert transport.calls[0][2]["json"] == {
        **content,
        "storage_encoding": "gzip",
    }
    assert all("headers" not in call[2] for call in transport.calls[1:])


@pytest.mark.parametrize(
    "body",
    [
        {"size": [2, 2], "counts": [0, 3]},
        {"size": [2, 2], "counts": [0, -1, 5]},
        {"size": [2, 2], "counts": [0.0, 4]},
        {"size": [8193, 1], "counts": [8193]},
    ],
)
def test_coco_rle_content_rejects_invalid_or_oversized_payloads(body):
    with pytest.raises(ValidationError):
        CocoRleContent.model_validate(body)


def test_mask_reference_rejects_mismatched_content_address():
    body = _mask_body().model_dump(mode="python")
    body["mask"]["object_key"] = "raster-masks/sha256/dd/dd/" + "d" * 64 + ".json"
    with pytest.raises(ValidationError, match="content address"):
        VideoMaskKeyframeSaveRequest.model_validate(body)


def test_quality_endpoints_use_concrete_run_contracts():
    run = _quality_run()
    transport = _Transport([[run], run, run, run])
    review = VideoReview(transport)

    listed = review.list_track_quality_runs(TASK_ID)
    created = review.run_track_quality(
        TASK_ID,
        VideoTrackQualityRunRequest(
            left_segment_id=LEFT_SEGMENT_ID,
            right_segment_id=RIGHT_SEGMENT_ID,
        ),
    )
    fetched = review.get_track_quality_run(TASK_ID, RUN_ID)
    accepted = review.accept_track_quality_run(
        TASK_ID,
        RUN_ID,
        VideoTrackQualityAcceptRequest(input_digest="a" * 64, pairs=[]),
    )

    assert [item.id for item in listed] == [RUN_ID]
    assert [created.id, fetched.id, accepted.id] == [RUN_ID] * 3
    assert [method for method, _, _ in transport.calls] == [
        "GET",
        "POST",
        "GET",
        "POST",
    ]
    assert transport.calls[1][1] == f"/tasks/{TASK_ID}/video/track-quality/run"
    assert transport.calls[1][2]["json"] == {
        "left_segment_id": str(LEFT_SEGMENT_ID),
        "right_segment_id": str(RIGHT_SEGMENT_ID),
    }
    assert transport.calls[3][1] == (
        f"/tasks/{TASK_ID}/video/track-quality/{RUN_ID}:accept"
    )
    assert transport.calls[3][2]["json"] == {"input_digest": "a" * 64, "pairs": []}


def test_track_composition_and_conversion_keep_api_response_shapes():
    composition = {"operation": "merge_tracks", "updated_annotations": [_annotation()]}
    conversion = {
        "source_annotation": None,
        "created_annotations": [_annotation(uuid4())],
        "deleted_source": True,
        "removed_frame_indexes": [0, 2],
    }
    transport = _Transport([composition, conversion])
    review = VideoReview(transport)

    composed = review.compose_tracks(
        TASK_ID,
        VideoTrackCompositionRequest(
            operation="merge_tracks", annotation_ids=[ANN_ID, uuid4()]
        ),
    )
    converted = review.convert_track_to_bboxes(
        TASK_ID,
        ANN_ID,
        VideoTrackConvertToBboxesRequest(
            operation="split", scope="track", frame_mode="all_frames"
        ),
    )

    assert composed.updated_annotations[0].id == ANN_ID
    assert composed.created_annotations == []
    assert converted.source_annotation is None and converted.deleted_source
    assert converted.removed_frame_indexes == [0, 2]
    assert (
        transport.calls[0][1]
        == f"/tasks/{TASK_ID}/annotations/video/track-compositions"
    )
    assert transport.calls[0][2]["json"].get("delete_sources", True) is True
    assert transport.calls[1][1] == (
        f"/tasks/{TASK_ID}/annotations/{ANN_ID}/video/convert-to-bboxes"
    )


@pytest.mark.parametrize(
    "body, expected",
    [
        (
            {"operation": "split_track", "annotation_ids": [str(ANN_ID)]},
            "frame_index",
        ),
        (
            {
                "operation": "merge_tracks",
                "annotation_ids": [str(ANN_ID)],
            },
            "exactly two",
        ),
        (
            {
                "operation": "aggregate_bboxes",
                "annotation_ids": [str(ANN_ID), str(ANN_ID)],
            },
            "unique",
        ),
    ],
)
def test_composition_request_checks_action_preconditions(body, expected):
    with pytest.raises(ValidationError, match=expected):
        VideoTrackCompositionRequest.model_validate(body)


def test_conversion_requires_a_frame_for_frame_scope():
    with pytest.raises(ValidationError, match="frame_index"):
        VideoTrackConvertToBboxesRequest(operation="copy", scope="frame")


@pytest.mark.parametrize(
    "command, body",
    [
        (
            ["mask-upload", str(TASK_ID)],
            {"size": [2, 2], "counts": [0, 4]},
        ),
        (
            ["compose", str(TASK_ID)],
            {"operation": "aggregate_bboxes", "annotation_ids": [str(ANN_ID)]},
        ),
        (
            ["convert-to-bboxes", str(TASK_ID), str(ANN_ID)],
            {"operation": "split", "scope": "track", "frame_mode": "all_frames"},
        ),
    ],
)
def test_cli_json_destructive_actions_require_confirmation_before_client(
    monkeypatch, tmp_path, command, body
):
    body_path = tmp_path / "body.json"
    body_path.write_text(json.dumps(body), encoding="utf-8")

    def fail_if_opened(*args, **kwargs):
        raise AssertionError("must confirm before opening the API client")

    monkeypatch.setattr(video_review_cli, "get_client", fail_if_opened)
    result = runner.invoke(
        video_review_cli.app,
        [*command, "--body", str(body_path), "--json"],
    )

    assert result.exit_code != 0
    assert "--yes" in result.output


def test_cli_reads_complex_request_from_stdin(monkeypatch):
    run = _quality_run()
    transport = _Transport([run])
    review = VideoReview(transport)

    @contextmanager
    def fake_client(_json_mode):
        yield type("ClientStub", (), {"video_review": review})()

    monkeypatch.setattr(video_review_cli, "get_client", fake_client)
    result = runner.invoke(
        video_review_cli.app,
        ["quality", "run", str(TASK_ID), "--body", "-", "--json"],
        input=json.dumps(
            {
                "left_segment_id": str(LEFT_SEGMENT_ID),
                "right_segment_id": str(RIGHT_SEGMENT_ID),
            }
        ),
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["id"] == str(RUN_ID)
    assert transport.calls[0][2]["json"] == {
        "left_segment_id": str(LEFT_SEGMENT_ID),
        "right_segment_id": str(RIGHT_SEGMENT_ID),
    }
