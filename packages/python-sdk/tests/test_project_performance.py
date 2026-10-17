"""Project performance cursor, filter and streaming contracts."""

from __future__ import annotations

from uuid import uuid4

import httpx

from ai_annotation.project_performance import ProjectPerformance
from ai_annotation.project_performance_models import ProjectPerformanceQuery

from .conftest import API, BASE


def _metric() -> dict:
    return {"value": None, "unit": "tasks"}


def _member(user_id: str) -> dict:
    metric_names = (
        "submitted_tasks",
        "resubmissions",
        "contributed_tasks",
        "annotated_images",
        "retained_objects",
        "approved_task_outcomes",
        "first_review_pass_rate",
        "recorded_time_minutes",
        "current_backlog",
        "review_decisions",
        "approvals",
        "rejections",
        "reviewed_tasks",
        "recorded_review_minutes",
        "review_backlog",
    )
    return {
        "user_id": user_id,
        "name": "Ada",
        "email": "ada@example.test",
        "project_role": "reviewer",
        "account_status": "active",
        "is_owner": False,
        "is_current_member": True,
        "member_since": None,
        "avatar_ref": None,
        "metrics": {name: _metric() for name in metric_names},
    }


def _scope() -> dict:
    return {
        "from": "2026-09-01T00:00:00Z",
        "to": "2026-09-08T00:00:00Z",
        "timezone": "Asia/Shanghai",
        "as_of": "2026-09-08T00:00:00Z",
    }


def _coverage() -> dict:
    return {"state": "partial", "source": "task_events", "detail": None}


def _totals() -> dict:
    return {
        name: _metric()
        for name in (
            "submitted_tasks",
            "approved_task_outcomes",
            "first_review_pass_rate",
            "recorded_time_minutes",
            "current_backlog",
            "review_decisions",
            "approvals",
            "rejections",
            "review_backlog",
            "annotated_images",
            "retained_objects",
        )
    }


def test_performance_filters_alias_cursor_paths_and_detail_responses(
    client, respx_mock
):
    project_id = str(uuid4())
    member_id = str(uuid4())
    query = ProjectPerformanceQuery(
        **{
            "from": "2026-09-01",
            "to": "2026-09-08",
            "timezone": "Asia/Shanghai",
            "work_type": "review",
            "account_status": "inactive",
            "include_historical": True,
            "q": "Ada & Grace",
            "sort": "-review_decisions",
            "cursor": "next/1",
            "limit": 20,
        }
    )
    list_route = respx_mock.get(
        f"{API}/projects/{project_id}/performance/members"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "scope": _scope(),
                "coverage": _coverage(),
                "project_totals": _totals(),
                "items": [_member(member_id)],
                "next_cursor": "next/2",
            },
        )
    )
    detail_route = respx_mock.get(
        f"{API}/projects/{project_id}/performance/members/{member_id}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "scope": _scope(),
                "coverage": _coverage(),
                "member": _member(member_id),
                "trend": [],
                "reject_reasons": [],
                "class_distribution": [],
                "source_distribution": [],
                "geometry_distribution": [],
                "evidence": [],
                "evidence_next_cursor": "evidence/2",
            },
        )
    )
    events_route = respx_mock.get(
        f"{API}/projects/{project_id}/performance/members/{member_id}/events"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"scope": _scope(), "items": [], "next_cursor": None},
        )
    )
    perf = ProjectPerformance(client._http)

    members = perf.members(project_id, query)
    assert members.items[0].name == "Ada"
    params = list_route.calls.last.request.url.params
    assert params["from"] == "2026-09-01"
    assert params["work_type"] == "review"
    assert params["include_historical"] == "true"
    assert params["q"] == "Ada & Grace"
    assert params["cursor"] == "next/1"

    detail = perf.member(
        project_id,
        member_id,
        ProjectPerformanceQuery(cursor="evidence/1", limit=10),
    )
    assert detail.evidence_next_cursor == "evidence/2"
    assert detail_route.calls.last.request.url.params["cursor"] == "evidence/1"

    events = perf.events(
        project_id, member_id, ProjectPerformanceQuery(cursor="events/1")
    )
    assert events.next_cursor is None
    assert events_route.calls.last.request.url.params["cursor"] == "events/1"


def test_performance_export_streams_full_csv_and_ignores_page_cursor(
    client, respx_mock, tmp_path
):
    project_id = str(uuid4())
    content = b"\xef\xbb\xbf# Scope from: 2026-09-01\nuser_id,name\n"
    route = respx_mock.get(
        f"{BASE}/api/v1/projects/{project_id}/performance/export"
    ).mock(return_value=httpx.Response(200, content=content))
    dest = tmp_path / "performance.csv"
    query = ProjectPerformanceQuery(
        **{
            "from": "2026-09-01",
            "to": "2026-09-08",
            "cursor": "page/2",
            "limit": 10,
            "work_type": "annotation",
            "q": "Ada",
        }
    )

    result = ProjectPerformance(client._http).export_members(project_id, dest, query)

    assert result == dest
    assert dest.read_bytes() == content
    params = route.calls.last.request.url.params
    assert params["from"] == "2026-09-01"
    assert params["q"] == "Ada"
    assert "cursor" not in params and "limit" not in params
