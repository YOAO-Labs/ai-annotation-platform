"""Feedback resources use the real transport with an in-memory HTTP backend."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError as ModelValidationError

from yoao import (
    AnnotationFeedback,
    FeedbackAnchorPosition,
    FeedbackPage,
    FeedbackVideoContext,
    FeedbackVideoFrameRange,
    FeedbackVideoTimelineWindow,
    FeedbackVideoViewport,
)
from yoao.errors import NotFoundError, PermissionDeniedError, ValidationError


PROJECT_ID, TASK_ID, ANNOTATION_ID = uuid4(), uuid4(), uuid4()


def feedback(anchor=None):
    return {
        "id": str(uuid4()),
        "kind": "issue",
        "anchor_type": "pixel" if anchor is not None else "task",
        "project_id": str(PROJECT_ID),
        "task_id": str(TASK_ID),
        "annotation_id": str(ANNOTATION_ID) if anchor else None,
        "anchor_position": anchor,
        "body": "Review source frame",
        "status": "open",
        "author_id": str(uuid4()),
        "created_at": "2026-09-08T00:00:00Z",
    }


def mock_http(client, handler):
    client._http._client.close()
    client._http._client = httpx.Client(
        base_url="http://testserver/api/v1",
        headers={"Authorization": "Bearer ak_test"},
        transport=httpx.MockTransport(handler),
    )


def test_create_video_issue_preserves_source_frame_context_and_wire_alias(client):
    context = FeedbackVideoContext(
        schema_version=1,
        track_id="car-track/left",
        annotation_version=2,
        frame_range=FeedbackVideoFrameRange(from_frame=120, to_frame=160),
        viewport=FeedbackVideoViewport(center_x=-0.25, center_y=1.25, zoom=2.5),
        timeline_window=FeedbackVideoTimelineWindow(from_=110.5, to=170.25),
    )
    anchor = FeedbackAnchorPosition(x=0.25, y=0.5, frame=140, video_context=context)
    captured = []

    def handler(request):
        assert request.method == "POST" and request.url.path == "/api/v1/feedbacks"
        assert request.headers["authorization"] == "Bearer ak_test"
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=feedback(captured[-1]["anchor_position"]))

    mock_http(client, handler)
    result = client.feedbacks.create(
        PROJECT_ID,
        "Review source frame",
        anchor_type="pixel",
        task_id=TASK_ID,
        annotation_id=ANNOTATION_ID,
        anchor_position=anchor,
        severity="warn",
    )
    assert isinstance(result, AnnotationFeedback)
    assert captured[0]["annotation_id"] == str(ANNOTATION_ID)
    assert captured[0]["anchor_position"]["frame"] == 140
    assert captured[0]["anchor_position"]["video_context"] == context.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    assert captured[0]["anchor_position"]["video_context"]["timeline_window"] == {
        "from": 110.5,
        "to": 170.25,
    }
    assert result.video_context.annotation_version == 2


@pytest.mark.parametrize(
    "anchor",
    [None, {"x": 0.2, "y": 0.4}, {"x": 0.2, "y": 0.4, "frame": 0}],
)
def test_create_legacy_task_image_and_f0_anchors(client, anchor):
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=feedback(anchor))

    mock_http(client, handler)
    result = client.feedbacks.create(
        str(PROJECT_ID),
        "Legacy anchor",
        task_id=str(TASK_ID),
        anchor_type="task" if anchor is None else "pixel",
        anchor_position=anchor,
    )
    assert captured[0]["anchor_type"] == result.anchor_type
    if anchor is None:
        assert captured[0]["anchor_position"] is None
    else:
        assert {key: captured[0]["anchor_position"][key] for key in anchor} == anchor
        assert "video_context" not in captured[0]["anchor_position"]


def test_list_uses_items_cursor_and_preserves_unknown_context_without_interpreting_it(
    client,
):
    anchor = {
        "x": 0.2,
        "y": 0.4,
        "frame": 0,
        "video_context": {"schema_version": 99, "future": [1, 2]},
    }
    item = feedback(anchor)
    captured = []

    def handler(request):
        assert request.method == "GET" and request.url.path == "/api/v1/feedbacks"
        captured.append(dict(request.url.params))
        return httpx.Response(
            200, json={"items": [item], "next_cursor": "opaque:next=="}
        )

    mock_http(client, handler)
    page = client.feedbacks.list(
        PROJECT_ID,
        task_id=TASK_ID,
        kind="issue",
        status="open",
        cursor="opaque:old==",
        limit=2,
    )
    assert isinstance(page, FeedbackPage)
    assert page.next_cursor == "opaque:next=="
    assert page.items[0].anchor_position == anchor
    assert page.items[0].video_context is None
    assert captured == [
        {
            "project_id": str(PROJECT_ID),
            "task_id": str(TASK_ID),
            "kind": "issue",
            "status": "open",
            "cursor": "opaque:old==",
            "limit": "2",
        }
    ]


@pytest.mark.parametrize("version", [2, 1.0, True, "1"])
def test_create_rejects_unknown_or_non_integer_schema_before_http(client, version):
    requests = []
    mock_http(client, lambda request: requests.append(request))
    with pytest.raises(ModelValidationError):
        client.feedbacks.create(
            PROJECT_ID,
            "Invalid context",
            task_id=TASK_ID,
            anchor_type="pixel",
            anchor_position={
                "x": 0.2,
                "y": 0.4,
                "frame": 0,
                "video_context": {"schema_version": version},
            },
        )
    assert requests == []


@pytest.mark.parametrize(
    "context",
    [
        {"schema_version": 1, "annotation_version": 1},
        {"schema_version": 1, "annotation_version": 1.5},
        {"schema_version": 1, "frame_range": {"from_frame": 1, "to_frame": 4}},
        {"schema_version": 1, "frame_range": {"from_frame": 0.0, "to_frame": 4}},
        {
            "schema_version": 1,
            "viewport": {"center_x": float("nan"), "center_y": 0, "zoom": 1},
        },
        {"schema_version": 1, "viewport": {"center_x": 0, "center_y": 0, "zoom": 0}},
        {"schema_version": 1, "timeline_window": {"from": 4.5, "to": 0}},
        {"schema_version": 1, "timeline_window": {"from": 0, "to": float("inf")}},
        {"schema_version": 1, "unknown": "field"},
    ],
)
def test_invalid_context_never_sends_a_mutation(client, context):
    requests = []
    mock_http(client, lambda request: requests.append(request))
    with pytest.raises(ModelValidationError):
        client.feedbacks.create(
            PROJECT_ID,
            "Invalid context",
            task_id=TASK_ID,
            anchor_type="pixel",
            anchor_position={
                "x": 0.2,
                "y": 0.4,
                "frame": 0,
                "video_context": context,
            },
        )
    assert requests == []


@pytest.mark.parametrize("frame", [None, True, 0.0, "0"])
def test_context_requires_an_existing_strict_frame(client, frame):
    with pytest.raises(ModelValidationError):
        client.feedbacks.create(
            PROJECT_ID,
            "Invalid frame",
            task_id=TASK_ID,
            anchor_type="pixel",
            anchor_position={
                "x": 0.2,
                "y": 0.4,
                "frame": frame,
                "video_context": {"schema_version": 1},
            },
        )


@pytest.mark.parametrize(
    ("status", "error"),
    [(403, PermissionDeniedError), (404, NotFoundError), (422, ValidationError)],
)
def test_feedback_errors_keep_transport_mapping(client, status, error):
    mock_http(
        client,
        lambda request: httpx.Response(status, json={"detail": "rejected anchor"}),
    )
    with pytest.raises(error) as caught:
        client.feedbacks.create(PROJECT_ID, "Review", task_id=TASK_ID)
    assert caught.value.status_code == status
    assert caught.value.detail == "rejected anchor"


@pytest.mark.parametrize("limit", [0, 201, True, 2.5])
def test_list_rejects_invalid_page_size_without_http(client, limit):
    with pytest.raises(ValueError, match="limit"):
        client.feedbacks.list(PROJECT_ID, limit=limit)


def test_feedback_public_models_import_with_core_dependencies_only():
    source = Path(__file__).resolve().parents[1] / "src"
    script = """
import importlib.abc
import sys
class BlockExtras(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'typer', 'rich', 'textual', 'websockets'}:
            raise ImportError('optional dependency is unavailable: ' + fullname)
sys.meta_path.insert(0, BlockExtras())
from yoao import Client, FeedbackVideoContext, FeedbackPage
assert FeedbackVideoContext(schema_version=1).schema_version == 1
assert FeedbackPage(items=[]).next_cursor is None
with Client(base_url='http://testserver', api_key='ak_test') as client:
    assert callable(client.feedbacks.create) and callable(client.feedbacks.list)
"""
    subprocess.run(
        [sys.executable, "-B", "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1"},
    )
