import json

import httpx
from typer.testing import CliRunner

from ai_annotation.cli.dashboard import app

from .conftest import API, BASE

runner = CliRunner()
ENV = {
    "AAP_BASE_URL": BASE,
    "AAP_API_KEY": "ak_test",
    "COLUMNS": "120",
    "NO_COLOR": "1",
    "TERM": "dumb",
}


def test_dashboard_reviewer_and_annotator_commands_json(respx_mock):
    respx_mock.get(f"{API}/dashboard/reviewer/today-mini").mock(
        return_value=httpx.Response(
            200,
            json={
                "approved_today": 4,
                "rejected_today": 1,
                "avg_review_seconds": 8.0,
            },
        )
    )
    respx_mock.get(f"{API}/dashboard/annotator/projects/project-1/onboarding").mock(
        return_value=httpx.Response(
            200,
            json={
                "project_id": "d3575cd3-f87b-4353-8ac8-3b14cfa533f0",
                "assigned_task_count": 2,
                "opened_task_count": 1,
                "saved_annotation_count": 1,
                "reviewed_task_count": 0,
            },
        )
    )

    mini = runner.invoke(app, ["reviewer-today-mini", "--json"], env=ENV)
    onboarding = runner.invoke(
        app, ["annotator-onboarding", "project-1", "--json"], env=ENV
    )

    assert mini.exit_code == 0, mini.output
    assert json.loads(mini.stdout)["approved_today"] == 4
    assert onboarding.exit_code == 0, onboarding.output
    assert json.loads(onboarding.stdout)["opened_task_count"] == 1


def test_dashboard_people_export_requires_confirmation_before_overwrite(
    respx_mock, tmp_path
):
    out = tmp_path / "people.csv"
    out.write_text("old", encoding="utf-8")
    route = respx_mock.get(f"{API}/dashboard/admin/people/export").mock(
        return_value=httpx.Response(200, content=b"\xef\xbb\xbfuser_id,name\r\n")
    )

    refused = runner.invoke(
        app, ["people-export", "--out", str(out), "--json"], env=ENV
    )
    assert refused.exit_code == 2
    assert "--yes" in refused.output
    assert not route.called
    assert out.read_text(encoding="utf-8") == "old"

    saved = runner.invoke(
        app,
        ["people-export", "--out", str(out), "--yes", "--json"],
        env=ENV,
    )
    assert saved.exit_code == 0, saved.output
    assert json.loads(saved.stdout) == {"path": str(out)}
    assert out.read_bytes().startswith(b"\xef\xbb\xbf")
    assert len(route.calls) == 1


def test_dashboard_prediction_cost_command(respx_mock):
    route = respx_mock.get(f"{API}/dashboard/admin/prediction-cost-stats").mock(
        return_value=httpx.Response(
            200,
            json={"range": "7d", "total_predictions": 10, "failure_rate": 0.2},
        )
    )
    result = runner.invoke(
        app, ["prediction-costs", "--range", "7d", "--json"], env=ENV
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["total_predictions"] == 10
    assert route.calls.last.request.url.params["range"] == "7d"
