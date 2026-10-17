import httpx
import pytest

from ai_annotation.errors import APIStatusError, PermissionDeniedError
from ai_annotation.management_operation_models import (
    AdminPersonDetail,
    MyBatchItem,
    OnboardingProjectSummary,
    PredictionCostStats,
    RecentReviewItem,
    ReviewerMiniStats,
)
from ai_annotation.models import MyPerformance, PersonStat, ProjectStats

from .conftest import API


def test_projects_stats(client, respx_mock):
    respx_mock.get(f"{API}/projects/stats").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_data": 100,
                "completed": 60,
                "ai_rate": 0.4,
                "pending_review": 8,
                "total_data_series": [10, 50, 100],
                "completed_series": [5, 30, 60],
                "ai_rate_series": [0.1, 0.3, 0.4],
                "pending_review_series": [2, 5, 8],
            },
        )
    )
    s = client.projects.stats()
    assert isinstance(s, ProjectStats)
    assert s.total_data == 100
    assert s.ai_rate == 0.4
    assert s.completed_series == [5, 30, 60]


def test_dashboard_people(client, respx_mock):
    route = respx_mock.get(f"{API}/dashboard/admin/people").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "user_id": "u1",
                        "name": "甲",
                        "email": "a@x.io",
                        "role": "annotator",
                        "status": "online",
                        "project_count": 2,
                        "main_metric": 30,
                        "main_metric_label": "本周完成",
                        "throughput_score": 82,
                        "quality_score": 91,
                        "activity_score": 70,
                        "sparkline_7d": [3, 5, 4, 8, 6, 9, 7],
                        "rejected_rate": 0.05,
                        "alerts": [],
                    }
                ],
                "total": 1,
                "period": "7d",
            },
        )
    )
    people = client.dashboard.people(period="7d")
    assert route.called
    assert route.calls.last.request.url.params["period"] == "7d"
    assert len(people) == 1
    assert isinstance(people[0], PersonStat)
    assert people[0].throughput_score == 82


def test_dashboard_people_project_scope(client, respx_mock):
    route = respx_mock.get(f"{API}/dashboard/admin/people").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0, "period": "7d"})
    )
    client.dashboard.people(project="P-1")
    assert route.calls.last.request.url.params["project"] == "P-1"


def test_dashboard_people_forbidden(client, respx_mock):
    respx_mock.get(f"{API}/dashboard/admin/people").mock(
        return_value=httpx.Response(403, json={"detail": "forbidden"})
    )
    with pytest.raises(PermissionDeniedError):
        client.dashboard.people()


def test_dashboard_me_performance(client, respx_mock):
    respx_mock.get(f"{API}/dashboard/me/performance").mock(
        return_value=httpx.Response(
            200,
            json={
                "user_id": "u1",
                "name": "甲",
                "period": "4w",
                "throughput": 120,
                "quality_score": 88,
                "weekly_compare_pct": 5.0,
                "trend_throughput": [20, 30, 35, 35],
                "trend_quality": [80, 85, 87, 88],
                "team_trend_throughput": [25.0, 28.0, 30.0, 31.0],
                "p50_duration_ms": 1200,
                "p95_duration_ms": 4500,
                "reject_reason_breakdown": [],
                "class_distribution": [],
                "first_pass_yield": 0.92,
            },
        )
    )
    perf = client.dashboard.me_performance()
    assert isinstance(perf, MyPerformance)
    assert perf.throughput == 120
    assert perf.team_trend_throughput == [25.0, 28.0, 30.0, 31.0]
    assert perf.first_pass_yield == 0.92


def test_dashboard_reviewer_and_annotator_operations(client, respx_mock):
    mini = respx_mock.get(f"{API}/dashboard/reviewer/today-mini").mock(
        return_value=httpx.Response(
            200,
            json={
                "approved_today": 3,
                "rejected_today": 1,
                "avg_review_seconds": 16.5,
            },
        )
    )
    recent = respx_mock.get(f"{API}/dashboard/me/recent-reviews").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "task_id": "task-1",
                    "task_display_id": "T-1",
                    "file_name": "frame.jpg",
                    "project_id": "project-1",
                    "project_name": "Demo",
                    "status": "completed",
                    "reviewed_at": "2026-10-16T12:00:00Z",
                }
            ],
        )
    )
    onboarding = respx_mock.get(
        f"{API}/dashboard/annotator/projects/project-1/onboarding"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "project_id": "b03f81d7-58e8-4eb2-9d3a-d82d92a5a47f",
                "assigned_task_count": 4,
                "opened_task_count": 2,
                "saved_annotation_count": 1,
                "reviewed_task_count": 1,
                "reviewed_task_id": None,
                "reviewed_task_status": None,
            },
        )
    )
    batches = respx_mock.get(f"{API}/dashboard/annotator/batches").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "batch_id": "batch-1",
                    "batch_display_id": "B-1",
                    "batch_name": "Batch",
                    "project_id": "project-1",
                    "project_name": "Demo",
                    "status": "active",
                    "total_tasks": 5,
                    "completed_tasks": 2,
                    "review_tasks": 1,
                    "in_progress_tasks": 2,
                    "approved_tasks": 1,
                    "rejected_tasks": 1,
                    "progress_pct": 40.0,
                    "reviewer": None,
                }
            ],
        )
    )

    mini_result = client.dashboard.reviewer_today_mini()
    recent_result = client.dashboard.recent_reviews(limit=12)
    onboarding_result = client.dashboard.annotator_onboarding("project-1")
    batches_result = client.dashboard.annotator_batches()

    assert isinstance(mini_result, ReviewerMiniStats)
    assert mini_result.approved_today == 3
    assert len(recent_result) == 1
    assert isinstance(recent_result[0], RecentReviewItem)
    assert recent_result[0].task_display_id == "T-1"
    assert recent.calls.last.request.url.params["limit"] == "12"
    assert isinstance(onboarding_result, OnboardingProjectSummary)
    assert onboarding_result.saved_annotation_count == 1
    assert isinstance(batches_result[0], MyBatchItem)
    assert batches_result[0].progress_pct == 40.0
    assert mini.called and onboarding.called and batches.called


def test_dashboard_admin_detail_cost_and_csv_export(client, respx_mock, tmp_path):
    user_id = "5fb5f59a-1796-4c09-ad6d-6cf1f59a0a94"
    person = respx_mock.get(f"{API}/dashboard/admin/people/{user_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "user_id": user_id,
                "name": "甲",
                "email": "a@example.com",
                "role": "employee",
                "project_count": 1,
                "throughput": 20,
                "quality_score": 93,
                "active_minutes": 14,
                "composite_score": 85,
                "weekly_compare_pct": 5.0,
                "trend_throughput": [3, 4, 5, 8],
                "trend_quality": [88, 89, 91, 93],
                "project_distribution": [{"project_id": "p1", "count": 20}],
                "duration_histogram": [{"upper_ms": 1000, "count": 3}],
                "p50_duration_ms": 900,
                "p95_duration_ms": 2500,
                "timeline": [{"action": "annotation.created"}],
                "reject_reason_breakdown": [],
                "class_distribution": [],
                "first_pass_yield": 0.91,
            },
        )
    )
    costs = respx_mock.get(f"{API}/dashboard/admin/prediction-cost-stats").mock(
        return_value=httpx.Response(
            200,
            json={
                "range": "7d",
                "total_predictions": 10,
                "failed_predictions": 2,
                "failure_rate": 0.1667,
                "total_cost": 1.5,
                "total_tokens": 100,
                "by_backend": [
                    {
                        "backend_id": None,
                        "backend_name": "legacy",
                        "predictions": 10,
                        "failures": 2,
                        "total_cost": 1.5,
                        "avg_inference_time_ms": 42.0,
                    }
                ],
            },
        )
    )
    csv_route = respx_mock.get(f"{API}/dashboard/admin/people/export").mock(
        return_value=httpx.Response(
            200,
            content="\ufeffuser_id,name\r\nu1,甲\r\n".encode("utf-8"),
            headers={"content-type": "text/csv; charset=utf-8"},
        )
    )

    detail = client.dashboard.person_detail(user_id, project="project-1", period="7d")
    stats = client.dashboard.prediction_cost_stats(range="7d")
    out = tmp_path / "people.csv"
    saved = client.dashboard.people_export(out, project="project-1", role="reviewer")

    assert isinstance(detail, AdminPersonDetail)
    assert dict(person.calls.last.request.url.params) == {
        "period": "7d",
        "project": "project-1",
    }
    assert detail.timeline == [{"action": "annotation.created"}]
    assert isinstance(stats, PredictionCostStats)
    assert stats.by_backend[0].avg_inference_time_ms == 42.0
    assert saved == out
    assert out.read_bytes().startswith(b"\xef\xbb\xbf")
    assert dict(csv_route.calls.last.request.url.params) == {
        "project": "project-1",
        "role": "reviewer",
        "period": "7d",
        "sort": "throughput",
    }
    assert costs.called


def test_dashboard_recent_reviews_limit_is_bounded_before_request(client, respx_mock):
    with pytest.raises(ValueError, match="between 1 and 100"):
        client.dashboard.recent_reviews(limit=101)
    assert not respx_mock.calls


def test_dashboard_csv_export_is_single_request_and_preserves_target_on_failure(
    client, respx_mock, tmp_path
):
    out = tmp_path / "people.csv"
    out.write_bytes(b"previous")
    route = respx_mock.get(f"{API}/dashboard/admin/people/export").mock(
        return_value=httpx.Response(503, json={"detail": "temporarily unavailable"})
    )

    with pytest.raises(APIStatusError):
        client.dashboard.people_export(out)

    assert len(route.calls) == 1
    assert out.read_bytes() == b"previous"
