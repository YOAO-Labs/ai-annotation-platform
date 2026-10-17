"""Feedback thread, reply, patch, and soft-delete HTTP contracts."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError as ModelValidationError

from ai_annotation.errors import APIStatusError
from ai_annotation.models import (
    AnnotationFeedback,
    AnnotationFeedbackPatch,
    AnnotationFeedbackReply,
    FeedbackMention,
    FeedbackThreadPage,
)

PROJECT_ID, TASK_ID, ROOT_ID, REPLY_ID, USER_ID = [uuid4() for _ in range(5)]
CURSOR = "opaque:thread cursor=="


def _feedback(**extra):
    return {
        "id": str(extra.pop("id", uuid4())),
        "kind": "issue",
        "anchor_type": "task",
        "project_id": str(PROJECT_ID),
        "task_id": str(TASK_ID),
        "annotation_id": None,
        "anchor_position": None,
        "status": "open",
        "severity": None,
        "title": None,
        "body": "Review this task",
        "author_id": str(USER_ID),
        "author_name": "Reviewer",
        "attachments": [],
        "mentions": [],
        "canvas_drawing": None,
        "thread_parent_id": None,
        "is_active": True,
        "resolved_at": None,
        "resolved_by_id": None,
        "created_at": "2026-10-17T00:00:00Z",
        "updated_at": None,
        "actions": {
            "edit": True,
            "change_status": True,
            "delete": True,
            "reply": True,
        },
        **extra,
    }


def _mock_http(client, handler):
    client._http._client.close()
    client._http._client = httpx.Client(
        base_url="http://testserver/api/v1",
        headers={"Authorization": "Bearer ak_test"},
        transport=httpx.MockTransport(handler),
    )


def test_thread_forwards_opaque_root_bound_cursor_and_parses_page(client):
    root = _feedback(id=ROOT_ID)
    reply = _feedback(id=REPLY_ID, thread_parent_id=str(ROOT_ID), body="Reply")
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "root": root,
                "items": [reply],
                "next_cursor": CURSOR,
                "total": 3,
            },
        )

    _mock_http(client, handler)
    result = client.feedbacks.thread(ROOT_ID, cursor="old+/==", limit=7)

    assert isinstance(result, FeedbackThreadPage)
    assert result.root.id == ROOT_ID
    assert result.items[0].thread_parent_id == ROOT_ID
    assert result.next_cursor == CURSOR
    assert result.total == 3
    assert captured[0].method == "GET"
    assert captured[0].url.path == f"/api/v1/feedbacks/{ROOT_ID}/thread"
    assert dict(captured[0].url.params) == {"cursor": "old+/==", "limit": "7"}


@pytest.mark.parametrize("limit", [0, 201, True, 2.5])
def test_thread_rejects_invalid_limit_before_http(client, limit):
    requests = []
    _mock_http(client, lambda request: requests.append(request))

    with pytest.raises(ValueError, match="limit"):
        client.feedbacks.thread(ROOT_ID, limit=limit)

    assert requests == []


def test_reply_sends_only_typed_body_and_attachments_once(client):
    payload = AnnotationFeedbackReply(
        body="Reply body", attachments=[{"storageKey": "comment-attachments/a"}]
    )
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(
            200,
            json=_feedback(
                id=REPLY_ID, thread_parent_id=str(ROOT_ID), body="Reply body"
            ),
        )

    _mock_http(client, handler)
    result = client.feedbacks.reply(ROOT_ID, payload)

    assert isinstance(result, AnnotationFeedback)
    assert result.thread_parent_id == ROOT_ID
    assert captured[0].method == "POST"
    assert captured[0].url.path == f"/api/v1/feedbacks/{ROOT_ID}/replies"
    assert json.loads(captured[0].content) == {
        "body": "Reply body",
        "attachments": [{"storageKey": "comment-attachments/a"}],
    }
    assert len(captured) == 1


def test_reply_rejects_empty_content_and_non_reply_fields_before_http(client):
    requests = []
    _mock_http(client, lambda request: requests.append(request))

    with pytest.raises(ModelValidationError, match="text or an attachment"):
        AnnotationFeedbackReply(body="  ")
    for forbidden in (
        {"mentions": []},
        {"mentions": [{"userId": str(USER_ID)}]},
        {"canvas_drawing": {"shapes": []}},
    ):
        with pytest.raises(ModelValidationError):
            AnnotationFeedbackReply(body="reply", **forbidden)

    assert requests == []
    assert AnnotationFeedbackReply(body="", attachments=[{"file": "a"}]).body == ""


def test_reply_error_is_preserved_and_post_is_not_retried(client):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            503, json={"detail": {"reason": "temporarily unavailable"}}
        )

    _mock_http(client, handler)
    with pytest.raises(APIStatusError) as caught:
        client.feedbacks.reply(ROOT_ID, AnnotationFeedbackReply(body="reply"))

    assert caught.value.status_code == 503
    assert caught.value.detail == {"reason": "temporarily unavailable"}
    assert len(requests) == 1


def test_reply_does_not_retry_an_ambiguous_transport_failure(client):
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("response lost", request=request)

    _mock_http(client, handler)
    with pytest.raises(httpx.ReadTimeout):
        client.feedbacks.reply(ROOT_ID, AnnotationFeedbackReply(body="reply"))

    assert len(requests) == 1


def test_patch_preserves_omission_explicit_null_empty_mentions_and_aliases(client):
    payloads = []
    mention = FeedbackMention(
        user_id=USER_ID,
        display_name="Reviewer",
        offset=0,
        length=8,
    )

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=_feedback(id=ROOT_ID))

    _mock_http(client, handler)
    client.feedbacks.patch(ROOT_ID, AnnotationFeedbackPatch(status="resolved"))
    client.feedbacks.patch(
        ROOT_ID,
        AnnotationFeedbackPatch(title=None, mentions=[], body="Updated"),
    )
    client.feedbacks.patch(
        ROOT_ID,
        AnnotationFeedbackPatch(mentions=[mention]),
    )

    assert payloads == [
        {"status": "resolved"},
        {"title": None, "body": "Updated", "mentions": []},
        {
            "mentions": [
                {
                    "userId": str(USER_ID),
                    "displayName": "Reviewer",
                    "offset": 0,
                    "length": 8,
                }
            ]
        },
    ]


def test_patch_rejects_unknown_fields_before_http(client):
    requests = []
    _mock_http(client, lambda request: requests.append(request))

    with pytest.raises(ModelValidationError):
        AnnotationFeedbackPatch.model_validate({"unknown": "field"})

    assert requests == []


def test_response_declares_actions_and_mentions(client):
    payload = _feedback(
        mentions=[
            {
                "userId": str(USER_ID),
                "displayName": "Reviewer",
                "offset": 0,
                "length": 8,
            }
        ]
    )
    _mock_http(client, lambda request: httpx.Response(200, json=payload))

    result = client.feedbacks.patch(ROOT_ID, AnnotationFeedbackPatch(status="open"))

    assert result.mentions[0].user_id == USER_ID
    assert result.actions.reply is True


def test_delete_accepts_empty_204_without_parsing_body(client):
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(204)

    _mock_http(client, handler)
    result = client.feedbacks.delete(ROOT_ID)

    assert result is None
    assert len(captured) == 1
    assert captured[0].method == "DELETE"
    assert captured[0].url.path == f"/api/v1/feedbacks/{ROOT_ID}"


def test_delete_error_is_preserved(client):
    _mock_http(
        client,
        lambda request: httpx.Response(403, json={"detail": "not allowed"}),
    )

    with pytest.raises(APIStatusError) as caught:
        client.feedbacks.delete(ROOT_ID)

    assert caught.value.status_code == 403
    assert caught.value.detail == "not allowed"
