"""TUI lazy-load、逐视图错误、筛选分页与窄终端回归。"""

from __future__ import annotations

from collections import defaultdict
from uuid import uuid4

import pytest
from textual.widgets import Button, DataTable, Input, Static, TabbedContent

from yoao.models import Dataset, JobPage, Page
from yoao.tui.app import TuiApp, ExportConfigModal, ProjectDetailScreen

from .conftest import BASE
from .support_tui import (
    _StubClient,
    _dataset,
    _job,
    _member,
    _project,
    _settle,
    _settle_screen,
)

pytestmark = pytest.mark.asyncio


def _app(client) -> TuiApp:
    return TuiApp(client, base_url=BASE, poll_interval=999)


async def test_mount_only_loads_projects_jobs_and_principal():
    project = _project()
    client = _StubClient(
        [project], [_dataset()], [JobPage(items=[_job("running")], total=1)]
    )
    calls = defaultdict(int)

    def counted(owner, name, label):
        original = getattr(owner, name)

        def call(*args, **kwargs):
            calls[label] += 1
            return original(*args, **kwargs)

        setattr(owner, name, call)

    counted(client.projects, "list", "projects")
    counted(client.datasets, "list", "datasets")
    counted(client.jobs, "list", "jobs")
    counted(client.ml_backends, "list_available_pools", "pools")
    counted(client.projects, "stats", "stats")
    counted(client.dashboard, "me_performance", "me_performance")
    original_me = client.me

    def me():
        calls["me"] += 1
        return original_me()

    client.me = me

    app = _app(client)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        assert calls["projects"] == 1
        assert calls["jobs"] == 1
        assert calls["datasets"] == calls["pools"] == 0
        assert calls["me"] == 1
        assert calls["stats"] == 0
        assert calls["me_performance"] == 0
        assert app.query_one("#datasets-table", DataTable).row_count == 0

        app.query_one("#tabs", TabbedContent).active = "tab-datasets"
        await _settle(app, pilot)
        assert calls["datasets"] == 1
        assert app.query_one("#datasets-table", DataTable).row_count == 1
        app.query_one("#tabs", TabbedContent).active = "tab-projects"
        app.query_one("#tabs", TabbedContent).active = "tab-datasets"
        await pilot.pause()
        assert calls["datasets"] == 1


async def test_dataset_error_preserves_rows_and_jobs_refresh_does_not_clear_it():
    project = _project()
    client = _StubClient(
        [project], [_dataset()], [JobPage(items=[_job("running")], total=1)]
    )
    original = client.datasets.list
    calls = 0

    def flaky(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(**kwargs)
        raise RuntimeError("HTTP 503: backend unavailable")

    client.datasets.list = flaky
    app = _app(client)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-datasets"
        await _settle(app, pilot)
        table = app.query_one("#datasets-table", DataTable)
        assert table.row_count == 1

        await pilot.press("r")
        await _settle(app, pilot)
        assert table.row_count == 1
        assert "503" in str(app.query_one("#status-bar", Static).render())

        app._refresh_jobs()
        await _settle(app, pilot)
        assert "503" in str(app.query_one("#status-bar", Static).render())


async def test_filters_debounce_and_dataset_pagination_reset_offset():
    project = _project()
    client = _StubClient(
        [project], [_dataset()], [JobPage(items=[_job("running")], total=1)]
    )
    project_calls: list[dict] = []
    dataset_calls: list[dict] = []
    original_projects = client.projects.list

    def projects(**kwargs):
        project_calls.append(kwargs)
        return original_projects(**kwargs)

    def datasets(**kwargs):
        dataset_calls.append(kwargs)
        item = Dataset(
            id=uuid4(),
            display_id=f"D-{kwargs['offset']}",
            name="page",
            data_type="image",
        )
        return Page[Dataset](items=[item], total=120, limit=50, offset=kwargs["offset"])

    client.projects.list = projects
    client.datasets.list = datasets
    app = _app(client)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        search = app.query_one("#projects-search", Input)
        search.value = "a"
        search.value = "ab"
        search.value = "final"
        await pilot.pause(0.4)
        await _settle(app, pilot)
        assert project_calls[-1]["search"] == "final"
        assert sum(call.get("search") == "final" for call in project_calls) == 1

        app.query_one("#tabs", TabbedContent).active = "tab-datasets"
        await _settle(app, pilot)
        assert dataset_calls[-1]["offset"] == 0
        assert app.query_one("#datasets-prev", Button).disabled
        assert not app.query_one("#datasets-next", Button).disabled
        await pilot.press("n")
        await _settle(app, pilot)
        assert dataset_calls[-1]["offset"] == 50

        app.query_one("#datasets-search", Input).value = "reset"
        await pilot.pause(0.4)
        await _settle(app, pilot)
        assert dataset_calls[-1]["offset"] == 0
        assert "1–50" in str(app.query_one("#datasets-page", Static).render())


async def test_project_panes_report_errors_independently_and_scope_jobs():
    project = _project()
    client = _StubClient(
        [project],
        [_dataset()],
        [JobPage(items=[_job("running", project_id=project.id)], total=1)],
        members_by_project={project.id: [_member()]},
    )

    def denied(*args, **kwargs):
        raise RuntimeError("HTTP 403: forbidden")

    client.batches.list = denied
    app = _app(client)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        await pilot.press("o")
        await _settle_screen(app, pilot)
        assert isinstance(app.screen, ProjectDetailScreen)
        assert "403" in str(
            app.screen.query_one("#pd-batches-table", DataTable).border_title
        )
        assert app.screen.query_one("#pd-members-table", DataTable).row_count == 1
        scoped = [call for call in client.jobs.calls if call.get("project_id")]
        assert scoped[-1]["project_id"] == project.id
        assert scoped[-1]["limit"] == 50


@pytest.mark.parametrize("size", [(80, 24), (120, 32)])
async def test_responsive_main_and_export_modal(size):
    client = _StubClient(
        [_project()], [_dataset()], [JobPage(items=[_job("running")], total=1)]
    )
    app = _app(client)
    async with app.run_test(size=size) as pilot:
        await _settle(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, ExportConfigModal)
        assert app.screen.query_one("#modal-box").size.width <= 72
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one("#projects-table", DataTable).row_count == 1
