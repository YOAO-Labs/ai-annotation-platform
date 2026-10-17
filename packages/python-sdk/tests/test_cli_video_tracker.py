from __future__ import annotations

import json
from uuid import uuid4

import httpx
from ai_annotation.cli.video_tracker import app
from typer.testing import CliRunner

from .conftest import API, BASE

runner = CliRunner()
ENV = {"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1"}


def _job(task_id: str, *, status: str = "pending_review") -> dict:
    return {
        "id": str(uuid4()),
        "task_id": task_id,
        "dataset_item_id": str(uuid4()),
        "status": status,
        "model_key": "sam3_video_custom",
        "direction": "forward",
        "from_frame": 1,
        "to_frame": 4,
        "event_channel": "video-tracker:cli",
        "created_at": "2026-10-17T00:00:00Z",
    }


def test_tracker_accept_requires_confirmation_before_http(respx_mock):
    job = _job(str(uuid4()), status="accepted")
    route = respx_mock.post(f"{API}/video-tracker-jobs/{job['id']}/accept").mock(
        return_value=httpx.Response(200, json=job)
    )

    declined = runner.invoke(app, ["jobs", "accept", job["id"], "--json"], env=ENV)
    assert declined.exit_code == 2
    assert route.call_count == 0

    accepted = runner.invoke(
        app, ["jobs", "accept", job["id"], "--yes", "--json"], env=ENV
    )
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout)["status"] == "accepted"
    assert route.call_count == 1


def test_tracker_cli_reads_typed_track_body_and_emits_job_json(tmp_path, respx_mock):
    task_id = str(uuid4())
    body_file = tmp_path / "track.json"
    body_file.write_text(
        json.dumps(
            {
                "from_frame": 2,
                "to_frame": 6,
                "model_key": "sam3_video_custom",
                "text": "red car",
            }
        ),
        encoding="utf-8",
    )
    job = _job(task_id)
    route = respx_mock.post(f"{API}/tasks/{task_id}/video:track").mock(
        return_value=httpx.Response(202, json=job)
    )

    result = runner.invoke(
        app,
        ["tasks", "track", task_id, "--body-file", str(body_file), "--json"],
        env=ENV,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["task_id"] == task_id
    assert json.loads(route.calls.last.request.content) == {
        "from_frame": 2,
        "to_frame": 6,
        "model_key": "sam3_video_custom",
        "text": "red car",
    }


def test_tracker_task_recovery_serializes_model_lists(respx_mock):
    task_id = str(uuid4())
    job = _job(task_id, status="running")
    respx_mock.get(f"{API}/tasks/{task_id}/video/tracker-jobs/active").mock(
        return_value=httpx.Response(200, json=[job])
    )

    result = runner.invoke(app, ["tasks", "active", task_id, "--json"], env=ENV)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["status"] == "running"
