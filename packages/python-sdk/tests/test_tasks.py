import json
from uuid import uuid4

import httpx
import pytest

from yoao.errors import ConflictError
from yoao.models import ReviewClaim, Task, TaskActionResult, TaskPage

from .conftest import API

PROJECT_ID = str(uuid4())
TASK = {
    "id": str(uuid4()),
    "project_id": PROJECT_ID,
    "display_id": "T-1",
    "file_name": "img.jpg",
    "file_type": "image",
    "status": "pending",
    "created_at": "2026-06-11T00:00:00Z",
}


def test_list_tasks(client, respx_mock):
    route = respx_mock.get(f"{API}/tasks").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [TASK],
                "total": 1,
                "limit": 20,
                "offset": 0,
                "next_cursor": "abc",
            },
        )
    )
    page = client.tasks.list(PROJECT_ID, status="pending", limit=20, offset=0)
    req = route.calls.last.request
    assert req.url.params["project_id"] == PROJECT_ID
    assert req.url.params["status"] == "pending"
    assert req.url.params["limit"] == "20"
    assert "cursor" not in req.url.params
    assert isinstance(page, TaskPage)
    assert page.next_cursor == "abc"
    assert page.items[0].display_id == "T-1"


def test_get_task(client, respx_mock):
    tid = TASK["id"]
    respx_mock.get(f"{API}/tasks/{tid}").mock(
        return_value=httpx.Response(200, json=TASK)
    )
    t = client.tasks.get(tid)
    assert isinstance(t, Task)
    assert str(t.id) == tid


def test_next_task(client, respx_mock):
    route = respx_mock.get(f"{API}/tasks/next").mock(
        return_value=httpx.Response(200, json=TASK)
    )
    t = client.tasks.next(PROJECT_ID)
    assert route.calls.last.request.url.params["project_id"] == PROJECT_ID
    assert t is not None and t.display_id == "T-1"


def test_next_task_none(client, respx_mock):
    # 无可领任务时后端返回 null
    respx_mock.get(f"{API}/tasks/next").mock(
        return_value=httpx.Response(200, json=None)
    )
    assert client.tasks.next(PROJECT_ID, batch_id=uuid4()) is None


@pytest.mark.parametrize(
    ("suffix", "call", "expected_body", "status"),
    [
        ("submit", lambda c, tid: c.tasks.submit(tid), None, "submitted"),
        (
            "skip",
            lambda c, tid: c.tasks.skip(tid, "unclear", note="blurred"),
            {"reason": "unclear", "note": "blurred"},
            "skipped",
        ),
        ("withdraw", lambda c, tid: c.tasks.withdraw(tid), None, "withdrawn"),
        ("reopen", lambda c, tid: c.tasks.reopen(tid), None, "reopened"),
        (
            "accept-rejection",
            lambda c, tid: c.tasks.accept_rejection(tid),
            None,
            "in_progress",
        ),
        (
            "review/approve",
            lambda c, tid: c.tasks.approve_review(tid, note="looks good"),
            {"note": "looks good"},
            "approved",
        ),
        (
            "review/reject",
            lambda c, tid: c.tasks.reject_review(tid, "wrong_label", "fix class"),
            {"reason_type": "wrong_label", "reason": "fix class"},
            "rejected",
        ),
    ],
)
def test_task_workflow_actions(client, respx_mock, suffix, call, expected_body, status):
    task_id = TASK["id"]
    route = respx_mock.post(f"{API}/tasks/{task_id}/{suffix}").mock(
        return_value=httpx.Response(200, json={"status": status, "task_id": task_id})
    )
    result = call(client, task_id)
    assert isinstance(result, TaskActionResult)
    assert result.status == status
    if expected_body is not None:
        assert json.loads(route.calls.last.request.content) == expected_body


def test_claim_review(client, respx_mock):
    task_id = TASK["id"]
    reviewer_id = str(uuid4())
    respx_mock.post(f"{API}/tasks/{task_id}/review/claim").mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": task_id,
                "reviewer_id": reviewer_id,
                "reviewer_claimed_at": "2026-08-14T00:00:00Z",
                "is_self": True,
            },
        )
    )
    result = client.tasks.claim_review(task_id)
    assert isinstance(result, ReviewClaim)
    assert result.is_self is True


@pytest.mark.parametrize(
    ("suffix", "call"),
    [
        ("submit", lambda c, tid: c.tasks.submit(tid)),
        ("skip", lambda c, tid: c.tasks.skip(tid, "unclear")),
        ("withdraw", lambda c, tid: c.tasks.withdraw(tid)),
        ("reopen", lambda c, tid: c.tasks.reopen(tid)),
        ("accept-rejection", lambda c, tid: c.tasks.accept_rejection(tid)),
        ("review/claim", lambda c, tid: c.tasks.claim_review(tid)),
        ("review/approve", lambda c, tid: c.tasks.approve_review(tid)),
        (
            "review/reject",
            lambda c, tid: c.tasks.reject_review(tid, "wrong_label", "fix"),
        ),
    ],
)
def test_task_workflow_maps_conflict(client, respx_mock, suffix, call):
    task_id = TASK["id"]
    respx_mock.post(f"{API}/tasks/{task_id}/{suffix}").mock(
        return_value=httpx.Response(409, json={"detail": "wrong state"})
    )
    with pytest.raises(ConflictError):
        call(client, task_id)
