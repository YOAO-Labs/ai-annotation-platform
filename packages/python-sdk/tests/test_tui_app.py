"""TUI 面板 headless 测试: Textual Pilot 驱动 + stub client (不发真实网络)。"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from time import sleep
from uuid import uuid4

import pytest
from textual.widgets import DataTable, Static, TabbedContent, TabPane

from yoao.models import (
    Batch,
    HealthMeta,
    JobPage,
    MLBackend,
    PersonStat,
    Project,
    ProjectServicePool,
    ServicePoolSummary,
    UserBrief,
)
from yoao.tui.app import (
    TuiApp,
    BatchDetailScreen,
    DatasetDetailScreen,
    DetailScreen,
    ExportConfigModal,
    MlBackendDetailScreen,
    PathInputModal,
    ProjectDetailScreen,
)

from .conftest import API, BASE
from .support_tui import (
    JOB_ID,
    _StubClient,
    _StubJobs,
    _dataset,
    _job,
    _member,
    _project,
    _settle,
    _settle_screen,
)

pytestmark = pytest.mark.asyncio


def _ml_backend(project_id, state="connected") -> MLBackend:
    return MLBackend(
        id=uuid4(),
        project_id=project_id,
        name="sam2-backend",
        url="http://gpu-host:9000",
        state=state,
        health_meta=HealthMeta(
            model_version="v1.2",
            gpu_info={
                "gpu_utilization_percent": 73,
                "memory_used_mb": 8000,
                "memory_total_mb": 24000,
            },
        ),
        last_checked_at=datetime(2026, 6, 11, tzinfo=timezone.utc),
    )


class _SlowStubJobs(_StubJobs):
    def __init__(self, pages):
        super().__init__(pages)
        self.active = 0
        self.peak = 0
        self._lock = Lock()

    def list(self, **kw):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            sleep(0.1)
            return super().list(**kw)
        finally:
            with self._lock:
                self.active -= 1


def _make_app(
    job_statuses=("running",),
    with_ml=False,
    role="employee",
    job_kind="export",
) -> TuiApp:
    pages = [JobPage(items=[_job(s, kind=job_kind)], total=1) for s in job_statuses]
    project = _project()
    ml_by_project = {project.id: [_ml_backend(project.id)]} if with_ml else {}
    client = _StubClient([project], [_dataset()], pages, ml_by_project, role=role)
    return TuiApp(client, base_url=BASE)


async def test_renders_tabs_and_status_bar():
    app = _make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        assert [t.id for t in app.query(TabPane)] == [
            "tab-projects",
            "tab-datasets",
            "tab-jobs",
            "tab-training",
            "tab-ml-backends",
            "tab-stats",
            "tab-people",
        ]
        bar = app.query_one("#status-bar", Static)
        assert BASE in str(bar.render())


async def test_tables_show_stub_rows():
    app = _make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        projects = app.query_one("#projects-table", DataTable)
        assert projects.row_count == 1
        assert projects.get_row_at(0)[:4] == ["P-1", "demo-project", "active", "3/10"]
        app.query_one("#tabs", TabbedContent).active = "tab-datasets"
        await _settle(app, pilot)
        datasets = app.query_one("#datasets-table", DataTable)
        assert datasets.get_row_at(0)[1] == "demo-dataset"
        jobs = app.query_one("#jobs-table", DataTable)
        assert jobs.row_count == 1
        row = jobs.get_row_at(0)
        assert row[0].plain == "export"
        assert row[1].plain == "running"
        assert "40%" in row[2].plain


async def test_refresh_reflects_status_flip():
    app = _make_app(job_statuses=("running", "completed"))
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        jobs = app.query_one("#jobs-table", DataTable)
        assert jobs.get_row_at(0)[1].plain == "running"
        app.query_one("#tabs", TabbedContent).active = "tab-jobs"
        await pilot.pause()
        await pilot.press("r")
        await _settle(app, pilot)
        assert "completed" in jobs.get_row_at(0)[1].plain
        assert "✔" in jobs.get_row_at(0)[1].plain
        bar = app.query_one("#status-bar", Static)
        assert "完成" in str(bar.render())


async def test_jobs_refresh_does_not_overlap_slow_requests():
    app = _make_app()
    slow_jobs = _SlowStubJobs([JobPage(items=[_job("running")], total=1)])
    app._client.jobs = slow_jobs
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.01)
        for _ in range(5):
            app._refresh_jobs()
        await pilot.pause(0.3)
    assert slow_jobs.peak == 1


async def test_ml_backends_tab_renders_and_colors():
    app = _make_app(with_ml=True)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-ml-backends"
        await _settle(app, pilot)
        table = app.query_one("#ml-backends-table", DataTable)
        assert table.row_count == 1
        row = table.get_row_at(0)
        assert row[0] == "sam2-backend"
        assert row[1] == "P-1"
        assert row[2].plain == "connected"
        assert row[2].style == "green"
        assert row[3] == "v1.2"
        assert "73%" in row[4]


async def test_ml_backends_dedup_shared_backend_merges_one_row():
    proj_a = _project()
    proj_b = Project(
        id=uuid4(),
        display_id="P-2",
        name="proj-b",
        type_key="detection",
        data_type="image",
        status="active",
        total_tasks=0,
        completed_tasks=0,
    )
    shared = _ml_backend(proj_a.id)
    client = _StubClient(
        [proj_a, proj_b],
        [_dataset()],
        [JobPage(items=[_job("running")], total=1)],
        {proj_a.id: [shared], proj_b.id: [shared]},
    )
    app = TuiApp(client, base_url=BASE)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-ml-backends"
        await _settle(app, pilot)
        table = app.query_one("#ml-backends-table", DataTable)
        assert table.row_count == 1
        row = table.get_row_at(0)
        assert row[0] == "sam2-backend"
        assert row[1] == "2 个项目"


async def test_ml_backend_detail_on_enter_pushes_screen():
    app = _make_app(with_ml=True)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-ml-backends"
        await pilot.pause()
        table = app.query_one("#ml-backends-table", DataTable)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, MlBackendDetailScreen)
        detail = str(app.screen.query_one("#ml-static", Static).render())
        assert "model_version: v1.2" in detail
        assert "util 73%" in detail
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, MlBackendDetailScreen)
        assert app.query_one("#tabs", TabbedContent) is not None


async def test_export_action_opens_modal_and_create():
    app = _make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, ExportConfigModal)
        await pilot.click("#modal-ok")
        await _settle(app, pilot)
        assert len(app._client.exports.created) == 1
        _pid, targets, kwargs = app._client.exports.created[0]
        assert targets == ["coco"]
        assert kwargs.get("include_attributes") is True


async def test_export_modal_cancel_does_not_create():
    app = _make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        await pilot.click("#modal-cancel")
        await _settle(app, pilot)
        assert app._client.exports.created == []


async def test_export_modal_lidar_has_axis_frame():
    project = Project(
        id=uuid4(),
        display_id="P-2",
        name="lidar-proj",
        type_key="lidar",
        data_type="lidar",
        status="active",
    )
    client = _StubClient([project], [_dataset()], [JobPage(items=[], total=0)])
    app = TuiApp(client, base_url=BASE)
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, ExportConfigModal)
        assert app.screen.query("#export-axis")
        assert not app.screen.query("#export-frame")
        await pilot.click("#modal-ok")
        await _settle(app, pilot)
        _pid, targets, kwargs = app._client.exports.created[0]
        assert targets == ["native_json"]
        assert kwargs.get("axis_frame") == "iso"


async def test_cancel_action_confirm_triggers_cancel():
    app = _make_app(job_statuses=("running",))
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-jobs"
        await pilot.pause()
        app.query_one("#jobs-table", DataTable).focus()
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("y")
        await _settle(app, pilot)
        assert app._client.jobs.cancelled == [JOB_ID]


async def test_cancel_action_blocked_on_terminal_job():
    app = _make_app(job_statuses=("completed",))
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-jobs"
        await pilot.pause()
        app.query_one("#jobs-table", DataTable).focus()
        await pilot.press("c")
        await pilot.pause()
        assert app._client.jobs.cancelled == []
        assert "不可取消" in str(app.query_one("#status-bar", Static).render())


async def test_quit_key():
    app = _make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        await pilot.press("q")
    assert app.return_code == 0


async def test_open_project_pushes_detail_with_subtabs():
    project = _project()
    job = _job("running", project_id=project.id)
    pages = [JobPage(items=[job], total=1)]
    client = _StubClient(
        [project], [_dataset()], pages, {project.id: [_ml_backend(project.id)]}
    )
    app = TuiApp(client, base_url=BASE)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        await pilot.press("o")
        await _settle_screen(app, pilot)
        assert isinstance(app.screen, ProjectDetailScreen)
        assert [t.id for t in app.screen.query(TabPane)] == [
            "pd-overview",
            "pd-batches",
            "pd-members",
            "pd-jobs",
            "pd-backends",
            "pd-pools",
        ]
        assert app.screen.query_one("#pd-jobs-table", DataTable).row_count == 1
        assert app.screen.query_one("#pd-backends-table", DataTable).row_count == 1


async def test_project_detail_jobs_filtered_excludes_other_project():
    project = _project()
    other = _job("running")
    pages = [JobPage(items=[other], total=1)]
    client = _StubClient([project], [_dataset()], pages, {})
    app = TuiApp(client, base_url=BASE)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        await pilot.press("o")
        await _settle_screen(app, pilot)
        assert isinstance(app.screen, ProjectDetailScreen)
        assert app.screen.query_one("#pd-jobs-table", DataTable).row_count == 0


async def test_project_detail_export_via_key_confirms_and_creates():
    app = _make_app(with_ml=True)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        await pilot.press("o")
        await _settle_screen(app, pilot)
        assert isinstance(app.screen, ProjectDetailScreen)
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, ExportConfigModal)
        await pilot.click("#modal-ok")
        await _settle(app, pilot)
        assert len(app._client.exports.created) == 1


async def test_action_bar_open_button_pushes_project_detail():
    app = _make_app()
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        await pilot.click("#proj-open")
        await _settle_screen(app, pilot)
        assert isinstance(app.screen, ProjectDetailScreen)


async def test_dataset_detail_loads_items_and_linked_projects(client, respx_mock):
    dataset = _dataset()
    project = _project().model_dump(mode="json") | {
        "display_id": "P-linked",
        "name": "linked-only",
    }
    root = f"{API}/datasets/{dataset.id}"
    respx_mock.get(f"{API}/datasets").respond(
        200,
        json={
            "items": [dataset.model_dump(mode="json")],
            "total": 1,
            "limit": 50,
            "offset": 0,
        },
    )
    respx_mock.get(f"{root}/items").respond(
        200,
        json={
            "items": [
                {
                    "id": str(uuid4()),
                    "dataset_id": str(dataset.id),
                    "file_name": "detail.jpg",
                    "file_path": "images/detail.jpg",
                    "file_type": "image/jpeg",
                    "width": 19,
                    "height": 7,
                }
            ],
            "total": 1,
            "limit": 50,
            "offset": 0,
        },
    )
    respx_mock.get(f"{root}/projects").respond(200, json=[project])
    shell = _StubClient([], [], [JobPage(items=[], total=0)])
    shell.datasets = client.datasets
    app = TuiApp(shell, base_url=BASE)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-datasets"
        await _settle(app, pilot)
        app.query_one("#datasets-table", DataTable).focus()
        await pilot.press("enter")
        await _settle_screen(app, pilot)
        assert isinstance(app.screen, DatasetDetailScreen)
        item = app.screen.query_one("#dd-items-table", DataTable).get_row_at(0)
        assert (item[0], item[3]) == ("detail.jpg", "19×7")
        linked = app.screen.query_one("#dd-projects-table", DataTable).get_row_at(0)
        assert linked[:2] == ["P-linked", "linked-only"]


async def test_batch_detail_and_project_pool_tab():
    project = _project()
    batch = _batch(project.id)
    pool = ProjectServicePool(
        pool=ServicePoolSummary(
            id=uuid4(), name="detector-pool", enabled=True, member_count=2
        ),
        enabled=True,
        default_variants={"image": "sam2"},
    )
    client = _StubClient(
        [project],
        [_dataset()],
        [JobPage(items=[], total=0)],
        pools_by_project={project.id: [pool]},
        batches_by_project={project.id: [batch]},
    )
    app = TuiApp(client, base_url=BASE)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        await pilot.press("o")
        await _settle_screen(app, pilot)
        assert isinstance(app.screen, ProjectDetailScreen)
        assert app.screen.query_one("#pd-pools-table", DataTable).row_count == 1
        app.screen.query_one("#pd-tabs", TabbedContent).active = "pd-batches"
        app.screen.query_one("#pd-batches-table", DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, BatchDetailScreen)
        body = str(app.screen.query_one("#batch-detail-body", Static).render())
        assert "batch-alpha" in body


async def test_failed_job_detail_retry_confirm():
    app = _make_app(job_statuses=("failed",), job_kind="batch_predict")
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-jobs"
        app.query_one("#jobs-table", DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query("#retry")
        await pilot.click("#retry")
        await pilot.pause()
        await pilot.press("y")
        await _settle(app, pilot)
        assert app._client.jobs.retried == [JOB_ID]
        assert "queued=2" in str(app.query_one("#status-bar", Static).render())


async def test_failed_non_prediction_job_has_no_retry_action():
    app = _make_app(job_statuses=("failed",))
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-jobs"
        app.query_one("#jobs-table", DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert not app.screen.query("#retry")


async def test_job_detail_cancel_button_triggers_cancel():
    app = _make_app(job_statuses=("running",))
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-jobs"
        await pilot.pause()
        app.query_one("#jobs-table", DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        assert app.screen.query("#cancel")
        await pilot.click("#cancel")
        await pilot.pause()
        await pilot.press("y")
        await _settle(app, pilot)
        assert app._client.jobs.cancelled == [JOB_ID]


async def test_job_detail_terminal_has_no_cancel_button():
    app = _make_app(job_statuses=("completed",))
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-jobs"
        await pilot.pause()
        app.query_one("#jobs-table", DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        assert not app.screen.query("#cancel")
        assert not app.screen.query("#download")


async def test_cancel_confirm_via_button_triggers_cancel():
    app = _make_app(job_statuses=("running",))
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-jobs"
        await pilot.pause()
        app.query_one("#jobs-table", DataTable).focus()
        await pilot.press("c")
        await pilot.pause()
        await pilot.click("#modal-ok")
        await _settle(app, pilot)
        assert app._client.jobs.cancelled == [JOB_ID]


async def test_completed_export_job_detail_download_closure():
    result = {
        "download_url": "http://x/out.zip",
        "file_count": 3,
        "size_bytes": 2048,
        "cache_hit": False,
        "expires_at": "2026-06-20T00:00:00Z",
    }
    job = _job("completed", result=result)
    client = _StubClient([_project()], [_dataset()], [JobPage(items=[job], total=1)])
    app = TuiApp(client, base_url=BASE)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-jobs"
        await pilot.pause()
        app.query_one("#jobs-table", DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        assert app.screen.query("#download")
        body = str(app.screen.query_one("#detail-body", Static).render())
        assert "文件数 3" in body
        await pilot.click("#download")
        await pilot.pause()
        assert isinstance(app.screen, PathInputModal)
        await pilot.click("#modal-ok")
        await _settle(app, pilot)
        assert len(app._client.exports.downloaded) == 1


def _batch(project_id) -> Batch:
    return Batch(
        id=uuid4(),
        project_id=project_id,
        display_id="B-1",
        name="batch-alpha",
        status="active",
        total_tasks=20,
        completed_tasks=12,
        review_tasks=3,
        rejected_tasks=1,
        progress_pct=60.0,
        annotator=UserBrief(
            id=uuid4(), name="标注员甲", email="a@x.io", avatar_initial="甲"
        ),
        reviewer=UserBrief(
            id=uuid4(), name="审核员乙", email="b@x.io", avatar_initial="乙"
        ),
    )


async def test_project_detail_batches_and_members_subtabs():
    project = _project()
    client = _StubClient(
        [project],
        [_dataset()],
        [JobPage(items=[], total=0)],
        batches_by_project={project.id: [_batch(project.id)]},
        members_by_project={project.id: [_member()]},
    )
    app = TuiApp(client, base_url=BASE)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        await pilot.press("o")
        await _settle_screen(app, pilot)
        assert isinstance(app.screen, ProjectDetailScreen)
        bat = app.screen.query_one("#pd-batches-table", DataTable)
        assert bat.row_count == 1
        row = bat.get_row_at(0)
        assert row[0] == "batch-alpha"
        assert row[1] == "active"
        assert "60%" in row[2]
        assert row[5] == "标注员甲"
        assert row[6] == "审核员乙"
        mt = app.screen.query_one("#pd-members-table", DataTable)
        assert mt.row_count == 1
        assert mt.get_row_at(0)[0] == "张三"
        assert mt.get_row_at(0)[2] == "annotator"


async def test_project_detail_degrades_when_batches_endpoint_unavailable():
    project = _project()
    client = _StubClient([project], [_dataset()], [JobPage(items=[], total=0)])

    def _boom(*_a, **_k):
        raise RuntimeError("404 batches not found")

    client.batches.list = _boom  # type: ignore[assignment]
    app = TuiApp(client, base_url=BASE)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        await pilot.press("o")
        await _settle_screen(app, pilot)
        assert isinstance(app.screen, ProjectDetailScreen)
        assert app.screen.query_one("#pd-batches-table", DataTable).row_count == 0
        assert app.screen.query_one("#pd-members-table", DataTable) is not None


def _person() -> PersonStat:
    return PersonStat(
        user_id=str(uuid4()),
        name="标注员甲",
        email="a@x.io",
        role="annotator",
        throughput_score=82,
        quality_score=91,
        rejected_rate=0.05,
        sparkline_7d=[3, 5, 4, 8, 6, 9, 7],
    )


async def test_stats_tab_renders_sparklines():
    from yoao.tui.app import AxisChart

    app = _make_app()
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-stats"
        await _settle(app, pilot)
        headline = str(app.query_one("#stats-headline", Static).render())
        assert "总量 100" in headline
        assert "AI率 40%" in headline
        assert app.query_one("#spark-total", AxisChart)._data
        assert len(app.query_one("#spark-completed", AxisChart)._data) == 12
        assert app.query_one("#spark-airate", AxisChart)._data


async def test_axis_chart_renders_axes_and_line():
    from yoao.tui.app import _render_axis_chart

    plain = _render_axis_chart(
        [50, 60, 55, 80, 40, 95],
        width=60,
        height=8,
        unit="%",
        x_left="-12w",
        x_right="now",
    ).plain
    assert "95%" in plain and "40%" in plain
    assert "-12w" in plain and "now" in plain
    assert "└" in plain
    assert any(0x2800 <= ord(c) <= 0x28FF and c != "⠀" for c in plain)
    assert "等待数据" in _render_axis_chart([], 60, 8, "%", "-12w", "now").plain


async def test_fmt_pool_summarizes_protocol_fields():
    from yoao.tui.app import _fmt_pool

    img = _fmt_pool(
        {"cap": 1, "current_size": 0, "loaded_keys": [], "last_evict": None}
    )
    assert img == "cap=1 · loaded=0"
    vid = _fmt_pool(
        {"cap": 1, "loaded_variants": [], "active_sessions": 0, "idle_seconds": 600}
    )
    assert vid == "cap=1 · loaded=0 · active=0 · idle=600s"
    assert (
        _fmt_pool({"cap": 2, "loaded_keys": [{"key": "a"}, {"key": "b"}]})
        == "cap=2 · loaded=2"
    )
    assert _fmt_pool({}) == "-"
    assert "loaded_keys" not in img


async def test_people_tab_loads_for_super_admin():
    project = _project()
    client = _StubClient(
        [project],
        [_dataset()],
        [JobPage(items=[], total=0)],
        role="super_admin",
        people=[_person()],
    )
    app = TuiApp(client, base_url=BASE)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-people"
        await _settle(app, pilot)
        table = app.query_one("#people-table", DataTable)
        assert table.row_count == 1
        row = table.get_row_at(0)
        assert row[0] == "标注员甲"
        assert row[2] == "82"
        assert "5%" in row[4]


async def test_people_tab_gated_for_employee():
    app = _make_app(role="employee")
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-people"
        await _settle(app, pilot)
        assert app.query_one("#people-table", DataTable).row_count == 0
        assert "本人 Me" in str(app.query_one("#people-self", Static).render())
        note = str(app.query_one("#people-note", Static).render())
        assert "super_admin" in note


async def test_me_failure_degrades_people_tab():
    app = _make_app(role=None)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-people"
        await _settle(app, pilot)
        assert app.query_one("#people-table", DataTable).row_count == 0
        assert "本人 Me" in str(app.query_one("#people-self", Static).render())
        assert "角色未知" in str(app.query_one("#people-note", Static).render())
