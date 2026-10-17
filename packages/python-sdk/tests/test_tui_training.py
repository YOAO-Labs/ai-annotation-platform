"""Training monitor Pilot tests use stub namespaces and never issue HTTP requests."""

from __future__ import annotations

from threading import Lock
from time import sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
from textual.widgets import DataTable, Select, Static, TabbedContent

from ai_annotation.models import JobPage
from ai_annotation.tui.app import AapTuiApp, TrainingRunDetailScreen
from tests.test_tui_app import (
    BASE,
    _StubClient,
    _dataset,
    _job,
    _project,
    _settle,
    _settle_screen,
)

pytestmark = pytest.mark.asyncio


def _run(project_id, *, run_id=None, attempt_id=None, status="running"):
    return {
        "id": str(run_id or uuid4()),
        "project_id": str(project_id),
        "dataset_id": str(uuid4()),
        "creator_id": None,
        "initialization_model_id": None,
        "async_job_id": None,
        "auto_trigger_id": None,
        "study_id": None,
        "name": "训练-run-一",
        "task": "detect",
        "media_type": "image",
        "status": status,
        "config": {},
        "config_digest": "0" * 64,
        "origin": None,
        "current_attempt_id": str(attempt_id) if attempt_id else None,
        "metrics": {"loss": 0.32},
        "error_message": None,
        "created_at": "2026-10-16T00:00:00Z",
        "completed_at": None,
    }


def _queue_page(runs, total):
    return SimpleNamespace(
        items=[
            SimpleNamespace(
                run=run,
                project_name="demo-project",
                project_display_id="P-Queue",
            )
            for run in runs
        ],
        total=total,
    )


class _Training:
    def __init__(self, pages=None, *, error=None):
        self.pages = pages or []
        self.error = error
        self.calls = []

    def runs(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        if callable(self.pages):
            return self.pages(kwargs)
        return self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]


class _TrainingRuns:
    def __init__(self, details, attempt_ids):
        self.details = list(details)
        self.attempt_ids = attempt_ids
        self.get_calls = []
        self.telemetry_calls = []
        self.logs_calls = []
        self.recovery_calls = []

    def get(self, project_id, run_id):
        self.get_calls.append((str(project_id), str(run_id)))
        if len(self.details) > 1:
            return self.details.pop(0)
        return self.details[0]

    def recovery(self, project_id, run_id):
        self.recovery_calls.append((str(project_id), str(run_id)))
        return SimpleNamespace(eligible=False, reasons=["checkpoint is stale"])

    def telemetry(self, project_id, run_id, attempt_id, **kwargs):
        attempt_id = str(attempt_id)
        self.telemetry_calls.append((attempt_id, kwargs))
        n = self.attempt_ids.index(attempt_id) + 1
        return SimpleNamespace(
            items=[
                {
                    "sequence": n,
                    "captured_at": f"2026-10-16T0{n}:00:00Z",
                    "phase": "train",
                    "phase_elapsed_seconds": n * 10,
                    "metrics": {"loss": 0.4 / n},
                    "progress": {"epoch": n},
                }
            ],
            latest_cursor=n,
            earliest_cursor=1,
            has_more=False,
        )

    def logs(self, project_id, run_id, attempt_id, **kwargs):
        attempt_id = str(attempt_id)
        self.logs_calls.append((attempt_id, kwargs))
        n = self.attempt_ids.index(attempt_id) + 1
        return SimpleNamespace(
            items=[
                {
                    "sequence": n,
                    "captured_at": f"2026-10-16T0{n}:00:00Z",
                    "text": f"attempt {n} training started",
                }
            ],
            earliest_cursor=n,
            latest_cursor=n,
            has_more=False,
        )


def _detail(run, attempt_id):
    return {
        "run": run,
        "attempts": [
            {
                "id": attempt_id,
                "ordinal": 1,
                "status": "running",
                "runtime": {"framework": "ultralytics"},
                "failure": None,
            }
        ],
        "registration_eligibility": {
            "qualified": False,
            "reason": "evaluation required",
        },
    }


def _app(client):
    return AapTuiApp(client, base_url=BASE, poll_interval=999)


@pytest.mark.parametrize("size", [(80, 24), (120, 32)])
async def test_training_tab_is_lazy_and_renders_at_terminal_sizes(size):
    project = _project()
    run = _run(project.id, status="succeeded")
    training = _Training([_queue_page([run], 1)])
    client = _StubClient(
        [project],
        [_dataset()],
        [JobPage(items=[_job("running")], total=1)],
    )
    client.training = training
    client.training_runs = _TrainingRuns([], [])
    app = _app(client)
    async with app.run_test(size=size) as pilot:
        await _settle(app, pilot)
        assert training.calls == []
        app.query_one("#tabs", TabbedContent).active = "tab-training"
        await _settle(app, pilot)
        assert training.calls == [
            {
                "state": "all",
                "project_id": None,
                "framework": None,
                "limit": 20,
                "offset": 0,
            }
        ]
        assert training.calls[0]["state"] in {"all", "active", "finished", "failed"}
        row = app.query_one("#training-table", DataTable).get_row_at(0)
        assert row[0] == "P-Queue"
        assert row[1] == "训练-run-一"
        assert row[3].plain == "succeeded"
        assert row[3].style == "green"
        assert "1–1" in str(app.query_one("#training-page", Static).render())


async def test_training_project_state_filters_and_pagination():
    project = _project()
    runs = [_run(project.id) for _ in range(25)]
    training = _Training(
        [
            _queue_page(runs[:20], 25),
            _queue_page(runs[20:], 25),
        ]
    )
    client = _StubClient(
        [project], [_dataset()], [JobPage(items=[_job("running")], total=1)]
    )
    client.training = training
    client.training_runs = _TrainingRuns([], [])
    app = _app(client)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-training"
        await _settle(app, pilot)
        app.query_one("#training-project", Select).value = str(project.id)
        app.query_one("#training-state", Select).value = "failed"
        await _settle(app, pilot)
        assert training.calls[-1] == {
            "state": "failed",
            "project_id": str(project.id),
            "framework": None,
            "limit": 20,
            "offset": 0,
        }
        assert {call["state"] for call in training.calls} <= {
            "all",
            "active",
            "finished",
            "failed",
        }
        await pilot.click("#training-next")
        await _settle(app, pilot)
        assert training.calls[-1]["offset"] == 20
        assert training.calls[-1]["limit"] == 20


async def test_slow_training_refreshes_coalesce_without_overlap():
    project = _project()
    client = _StubClient(
        [project], [_dataset()], [JobPage(items=[_job("running")], total=1)]
    )
    active = peak = 0
    lock = Lock()
    calls = []

    def slow(kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            calls.append(kwargs)
            sleep(0.12)
            return _queue_page([], 0)
        finally:
            with lock:
                active -= 1

    client.training = _Training(slow)
    client.training_runs = _TrainingRuns([], [])
    app = _app(client)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-training"
        await pilot.pause(0.02)
        for _ in range(5):
            app._refresh_training()
        await _settle(app, pilot)
    assert peak == 1
    assert len(calls) == 2


async def test_training_error_retains_last_queue_and_shows_authorization_failure():
    project = _project()
    run = _run(project.id)
    training = _Training([_queue_page([run], 1)])
    client = _StubClient(
        [project], [_dataset()], [JobPage(items=[_job("running")], total=1)]
    )
    client.training = training
    client.training_runs = _TrainingRuns([], [])
    app = _app(client)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        assert training.calls == []
        app.query_one("#tabs", TabbedContent).active = "tab-training"
        await _settle(app, pilot)
        table = app.query_one("#training-table", DataTable)
        assert table.row_count == 1
        training.error = RuntimeError("HTTP 403: platform role required")
        app.action_refresh()
        await _settle(app, pilot)
        assert table.row_count == 1
        assert "403" in table.border_title
        assert "403" in str(app.query_one("#status-bar", Static).render())


async def test_run_detail_refresh_tracks_changed_attempt_and_bounded_observations():
    project = _project()
    run_id = str(uuid4())
    attempt_a, attempt_b = str(uuid4()), str(uuid4())
    run_a = _run(project.id, run_id=run_id, attempt_id=attempt_a)
    run_b = _run(project.id, run_id=run_id, attempt_id=attempt_b)
    training = _Training([_queue_page([run_a], 1)])
    training_runs = _TrainingRuns(
        [_detail(run_a, attempt_a), _detail(run_b, attempt_b)],
        [attempt_a, attempt_b],
    )
    client = _StubClient(
        [project], [_dataset()], [JobPage(items=[_job("running")], total=1)]
    )
    client.training = training
    client.training_runs = training_runs
    app = _app(client)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-training"
        await _settle(app, pilot)
        table = app.query_one("#training-table", DataTable)
        table.focus()
        await pilot.press("enter")
        await _settle_screen(app, pilot)
        assert isinstance(app.screen, TrainingRunDetailScreen)
        detail = app.screen
        assert detail._observed_attempt_id == attempt_a
        summary = str(detail.query_one("#training-run-summary", Static).render())
        assert attempt_a in summary
        eligibility = str(
            detail.query_one("#training-run-eligibility", Static).render()
        )
        assert "evaluation required" in eligibility
        assert "checkpoint is stale" in eligibility
        telemetry_table = detail.query_one("#training-telemetry-table", DataTable)
        logs_table = detail.query_one("#training-logs-table", DataTable)
        assert telemetry_table.row_count == logs_table.row_count == 1
        assert telemetry_table.get_row_at(0)[0] == "1"
        assert logs_table.get_row_at(0)[2] == "attempt 1 training started"
        assert training_runs.telemetry_calls[0][1] == {
            "after": None,
            "limit": 120,
        }
        assert training_runs.logs_calls[0][1] == {
            "after": None,
            "before": None,
            "limit": 120,
        }

        await pilot.press("r")
        await _settle_screen(app, pilot)
        assert detail._observed_attempt_id == attempt_b
        assert [call[0] for call in training_runs.telemetry_calls] == [
            attempt_a,
            attempt_b,
        ]
        assert [call[0] for call in training_runs.logs_calls] == [attempt_a, attempt_b]
        assert telemetry_table.get_row_at(0)[0] == "2"
        assert logs_table.get_row_at(0)[2] == "attempt 2 training started"
