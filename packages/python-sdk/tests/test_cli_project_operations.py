"""Project-operation CLI contracts without live mutations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from typer.testing import CliRunner

from ai_annotation.cli import project_performance as performance_cli
from ai_annotation.cli import projects as projects_cli
from ai_annotation.cli import search as search_cli
from ai_annotation.models import Project
from ai_annotation.project_operation_models import LidarExportPreflight, ProjectPage
from ai_annotation.project_performance_models import (
    ProjectMembersPerformance,
    ProjectPerformanceCoverage,
    ProjectPerformanceMetric,
    ProjectPerformanceTotals,
    ProjectPerformanceScope,
)
from ai_annotation.search_models import SearchResponse

runner = CliRunner()


class _ClientContext:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *_exc):
        return False


def test_rejected_destructive_project_commands_never_open_client(monkeypatch):
    calls = []

    def fail_if_opened(_json_mode):
        calls.append(True)
        raise AssertionError("confirmation must happen before opening the API client")

    monkeypatch.setattr(projects_cli, "get_client", fail_if_opened)
    project_id = str(uuid4())
    commands = [
        ["rename-class", project_id, "--old-name", "car", "--new-name", "vehicle"],
        ["cleanup-orphans", project_id, "--apply"],
        ["cleanup-orphan-tasks", project_id],
        ["transfer", project_id, "--new-owner-id", str(uuid4())],
    ]

    for command in commands:
        result = runner.invoke(projects_cli.app, [*command, "--json"])
        assert result.exit_code != 0
        assert "--yes" in result.output
    assert calls == []


def test_projects_query_cli_forwards_repeated_filters(monkeypatch):
    expected_project = Project(
        id=uuid4(),
        display_id="P-1",
        name="demo",
        type_key="object_detection",
        data_type="image",
        status="active",
    )
    expected = ProjectPage(
        items=[expected_project], total=1, page=1, page_size=20, pages=1
    )

    class _Projects:
        received = None

        def query(self, **kwargs):
            self.received = kwargs
            return expected

    projects = _Projects()
    client = type("C", (), {"projects": projects})()
    monkeypatch.setattr(
        projects_cli, "get_client", lambda _json: _ClientContext(client)
    )
    result = runner.invoke(
        projects_cli.app,
        [
            "query",
            "--type-key",
            "object_detection",
            "--type-key",
            "segmentation",
            "--data-type",
            "image",
            "--data-type",
            "lidar",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert projects.received["type_key"] == ["object_detection", "segmentation"]
    assert projects.received["data_type"] == ["image", "lidar"]
    assert json.loads(result.stdout)["total"] == 1


def test_lidar_preflight_cli_reads_typed_json_body(monkeypatch, tmp_path):
    request_file = tmp_path / "lidar-preflight.json"
    request_file.write_text(
        '{"targets":["kitti"],"lidar":{"kitti_camera_role":"camera_front"}}',
        encoding="utf-8",
    )

    class _Projects:
        def lidar_export_preflight(self, _project_id, request):
            assert request.targets == ["kitti"]
            assert request.lidar.kitti_camera_role == "camera_front"
            return LidarExportPreflight(
                ready=True,
                camera_roles=["camera_front"],
                selected_camera_role="camera_front",
                checked_tasks=4,
                issue_count=0,
                issues_truncated=False,
                issues=[],
            )

    client = type("C", (), {"projects": _Projects()})()
    monkeypatch.setattr(
        projects_cli, "get_client", lambda _json: _ClientContext(client)
    )
    result = runner.invoke(
        projects_cli.app,
        ["lidar-export-preflight", str(uuid4()), str(request_file), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ready"] is True


def test_performance_and_search_cli_use_typed_resource_methods(monkeypatch):
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    metrics = ProjectPerformanceMetric(value=2, unit="tasks")
    totals = ProjectPerformanceTotals(
        submitted_tasks=metrics,
        approved_task_outcomes=metrics,
        first_review_pass_rate=ProjectPerformanceMetric(value=None, unit="percent"),
        recorded_time_minutes=ProjectPerformanceMetric(value=None, unit="minutes"),
        current_backlog=metrics,
        review_decisions=metrics,
        approvals=metrics,
        rejections=metrics,
        review_backlog=metrics,
        annotated_images=ProjectPerformanceMetric(value=None, unit="images"),
        retained_objects=ProjectPerformanceMetric(value=None, unit="objects"),
    )
    performance_result = ProjectMembersPerformance(
        scope=ProjectPerformanceScope(
            **{"from": now, "to": now, "timezone": "UTC", "as_of": now}
        ),
        coverage=ProjectPerformanceCoverage(state="complete", source="audit"),
        project_totals=totals,
        items=[],
        next_cursor=None,
    )

    class _Performance:
        received = None

        def members(self, _project_id, query):
            self.received = query
            return performance_result

    class _Search:
        received = None

        def query(self, q, *, limit):
            self.received = (q, limit)
            return SearchResponse(projects=[], tasks=[], datasets=[], members=[])

    performance = _Performance()
    search = _Search()
    client = type("C", (), {"project_performance": performance, "search": search})()
    monkeypatch.setattr(
        performance_cli,
        "get_client",
        lambda _json: _ClientContext(client),
    )
    monkeypatch.setattr(search_cli, "get_client", lambda _json: _ClientContext(client))

    perf_result = runner.invoke(
        performance_cli.app,
        [
            "members",
            str(uuid4()),
            "--from",
            "2026-10-01",
            "--work-type",
            "review",
            "--json",
        ],
    )
    assert perf_result.exit_code == 0, perf_result.output
    assert performance.received.from_ == "2026-10-01"
    assert performance.received.work_type == "review"

    search_result = runner.invoke(
        search_cli.app,
        ["query", "--q", "Ada & Grace", "--limit", "7", "--json"],
    )
    assert search_result.exit_code == 0, search_result.output
    assert search.received == ("Ada & Grace", 7)
