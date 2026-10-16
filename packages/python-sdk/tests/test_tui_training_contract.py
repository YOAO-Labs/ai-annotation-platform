"""Exercise training UI through the real SDK's typed HTTP responses."""

from uuid import uuid4

import httpx
import pytest
from textual.widgets import DataTable, TabbedContent

from ai_annotation.tui.app import AapTuiApp, TrainingRunDetailScreen

from .conftest import API, BASE
from .test_training_runs import _run
from .test_tui_app import _settle, _settle_screen


@pytest.mark.asyncio
async def test_typed_training_queue_and_detail_are_openable(client, respx_mock):
    project, attempt = str(uuid4()), str(uuid4())
    run = _run(project, str(uuid4()))
    run["current_attempt_id"] = attempt
    respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": project,
                    "display_id": "P-list",
                    "name": "训练项目",
                    "type_key": "image",
                    "status": "in_progress",
                }
            ],
        )
    )
    respx_mock.get(f"{API}/auth/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": str(uuid4()),
                "name": "SDK smoke",
                "email": "sdk@example.test",
                "role": "project_admin",
            },
        )
    )
    respx_mock.get(f"{API}/async-jobs").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0})
    )
    queue = respx_mock.get(f"{API}/training/runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "run": run,
                        "project_name": "训练项目",
                        "project_display_id": "P-envelope",
                    }
                ],
                "total": 1,
                "counts": {
                    "active": 0,
                    "queued": 0,
                    "running": 0,
                    "succeeded": 1,
                    "failed": 0,
                    "cancelled": 0,
                },
            },
        )
    )
    respx_mock.get(f"{API}/projects/{project}/training-runs/{run['id']}").mock(
        return_value=httpx.Response(
            200,
            json={
                "run": run,
                "attempts": [
                    {
                        "id": attempt,
                        "ordinal": 1,
                        "status": "succeeded",
                        "resume_from_id": None,
                        "executor_id": None,
                        "resource_id": None,
                        "runtime": {},
                        "artifacts": {},
                        "error_message": None,
                        "heartbeat_at": None,
                        "started_at": None,
                        "completed_at": None,
                    }
                ],
                "registration_eligibility": {
                    "qualified": False,
                    "reason": "evaluation required",
                    "qualification": None,
                },
            },
        )
    )
    respx_mock.get(f"{API}/projects/{project}/training-runs/{run['id']}/recovery").mock(
        return_value=httpx.Response(
            200,
            json={
                "attempt_id": attempt,
                "eligible": False,
                "reasons": ["completed"],
                "executor_online": False,
                "runtime_compatible": False,
                "checkpoint_epoch": None,
            },
        )
    )
    observation = {
        "attempt_id": attempt,
        "stream_id": None,
        "earliest_cursor": None,
        "latest_cursor": 1,
        "next_cursor": 1,
        "has_more": False,
        "gaps": [],
        "summary": None,
        "items": [],
        "metric_definitions": {},
    }
    for stream in ["logs", "telemetry"]:
        item = {
            "sequence": 1,
            "producer_boot_id": str(uuid4()),
            "captured_at": "2026-10-16T00:00:00Z",
            "received_at": "2026-10-16T00:00:01Z",
        }
        if stream == "logs":
            item["text"] = "训练完成"
        else:
            item.update(
                phase="training",
                phase_elapsed_seconds=1.5,
                callback_age_seconds=None,
                metrics={"cpu_cores": {"value": 0.5, "reason": None}},
                progress={"completed_epoch": 1, "total_epochs": 1},
            )
        respx_mock.get(
            f"{API}/projects/{project}/training-runs/{run['id']}/attempts/{attempt}/{stream}"
        ).mock(return_value=httpx.Response(200, json={**observation, "items": [item]}))
    app = AapTuiApp(client, base_url=BASE, poll_interval=999)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        assert not queue.called
        app.query_one("#tabs", TabbedContent).active = "tab-training"
        await _settle(app, pilot)
        assert queue.calls.last.request.url.params["state"] == "all"
        table = app.query_one("#training-table", DataTable)
        assert table.row_count == 1
        assert table.get_row_at(0)[:2] == ["P-envelope", run["name"]]
        table.focus()
        await pilot.press("enter")
        await _settle_screen(app, pilot)
        assert isinstance(app.screen, TrainingRunDetailScreen)
        assert app.screen.query_one("#training-run-attempts", DataTable).row_count == 1
        assert (
            app.screen.query_one("#training-telemetry-table", DataTable).row_count == 1
        )
        logs = app.screen.query_one("#training-logs-table", DataTable)
        assert logs.row_count == 1 and "训练完成" in str(logs.get_row_at(0)[2])
    assert all(call.request.method == "GET" for call in respx_mock.calls)
