"""TUI 面板 headless 测试: Textual Pilot 驱动 + stub client (不发真实网络)。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import sleep
from uuid import uuid4

import pytest
from textual.widgets import DataTable, Static, TabbedContent, TabPane

from ai_annotation.models import (
    Batch,
    Dataset,
    DatasetItem,
    HealthMeta,
    Job,
    JobPage,
    JobRetryResult,
    Me,
    MLBackend,
    Member,
    MyPerformance,
    Page,
    PersonStat,
    Project,
    ProjectServicePool,
    ProjectStats,
    ServicePoolSummary,
    UserBrief,
)
from ai_annotation.tui.app import (
    AapTuiApp,
    BatchDetailScreen,
    DatasetDetailScreen,
    DetailScreen,
    ExportConfigModal,
    MlBackendDetailScreen,
    PathInputModal,
    ProjectDetailScreen,
)

# pyproject 未配 asyncio_mode=auto, 用模块级 marker 驱动 async 测试
pytestmark = pytest.mark.asyncio

BASE = "http://testserver"
JOB_ID = uuid4()


def _project() -> Project:
    # total_tasks / completed_tasks 是服务端附加字段, 靠 extra="allow" 透传
    return Project(
        id=uuid4(),
        display_id="P-1",
        name="demo-project",
        type_key="detection",
        data_type="image",
        status="active",
        total_tasks=10,
        completed_tasks=3,
    )


def _dataset() -> Dataset:
    return Dataset(
        id=uuid4(),
        display_id="D-1",
        name="demo-dataset",
        data_type="image",
        file_count=5,
        total_size=2048,
    )


def _job(status: str, **extra) -> Job:
    kind = extra.pop("kind", "export")
    return Job(
        id=JOB_ID,
        kind=kind,
        status=status,
        progress_pct=40,
        created_at=datetime(2026, 6, 11, tzinfo=timezone.utc),
        **extra,
    )


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


class _StubProjects:
    def __init__(self, items, stats=None):
        self._items = items
        self._stats = stats or ProjectStats(
            total_data=100,
            completed=60,
            ai_rate=0.4,
            pending_review=8,
            total_data_series=[10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 100, 100],
            completed_series=[5, 10, 18, 25, 33, 40, 46, 50, 55, 58, 59, 60],
            ai_rate_series=[
                0.1,
                0.2,
                0.25,
                0.3,
                0.32,
                0.35,
                0.36,
                0.38,
                0.39,
                0.4,
                0.4,
                0.4,
            ],
            pending_review_series=[2, 3, 4, 5, 6, 7, 8, 9, 8, 8, 8, 8],
        )

    def list(self, **kw):
        return self._items

    def stats(self):
        return self._stats


class _StubDatasets:
    def __init__(self, items):
        self._items = items

    def list(self, **kw):
        return Page[Dataset](
            items=self._items,
            total=len(self._items),
            limit=kw.get("limit", 50),
            offset=kw.get("offset", 0),
        )

    def list_items(self, dataset_id, limit=50, offset=0):
        item = DatasetItem(
            id=uuid4(),
            dataset_id=dataset_id,
            file_name="a.jpg",
            file_path="a.jpg",
            file_type="image/jpeg",
            file_size=2048,
        )
        return Page[DatasetItem](items=[item], total=1, limit=limit, offset=offset)

    def list_projects(self, dataset_id):
        return []


class _StubJobs:
    """每次 list 前进一页, 停在最后一页 — 模拟轮询时状态推进。"""

    def __init__(self, pages):
        self._pages = list(pages)
        self.cancelled: list = []
        self.retried: list = []
        self.calls: list[dict] = []

    def list(self, **kw):
        self.calls.append(kw)
        if len(self._pages) > 1:
            page = self._pages.pop(0)
        else:
            page = self._pages[0]
        project_id = kw.get("project_id")
        if project_id is None:
            return page
        items = [j for j in page.items if str(j.project_id) == str(project_id)]
        return JobPage(items=items, total=len(items))

    def cancel(self, job_id):
        self.cancelled.append(job_id)

    def retry_failed(self, job_id):
        self.retried.append(job_id)
        return JobRetryResult(status="queued", job_id=job_id, queued=2, skipped=1)


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


class _StubExports:
    def __init__(self, job=None):
        self.created: list = []
        self.downloaded: list = []
        self._job = job

    def create(self, project_id, targets, **kwargs):
        self.created.append((project_id, targets, kwargs))
        return str(uuid4())

    def wait(self, job_id, **kw):
        return self._job

    def download(self, job_or_id, dest):
        self.downloaded.append((job_or_id, str(dest)))
        return Path(dest)


class _StubMLBackends:
    """按 project 返回 backend (聚合时逐项目调用)。"""

    def __init__(self, by_project, pools_by_project=None):
        self._by_project = by_project
        self._pools_by_project = pools_by_project or {}

    def list(self, project_id):
        return self._by_project.get(project_id, [])

    def list_available_pools(self, project_id):
        return self._pools_by_project.get(project_id, [])


class _StubBatches:
    def __init__(self, by_project=None):
        self._by_project = by_project or {}

    def list(self, project_id, status=None):
        return self._by_project.get(project_id, [])

    def get(self, project_id, batch_id):
        return next(b for b in self._by_project.get(project_id, []) if b.id == batch_id)


class _StubMembers:
    def __init__(self, by_project=None):
        self._by_project = by_project or {}

    def list(self, project_id):
        return self._by_project.get(project_id, [])


class _StubDashboard:
    def __init__(self, people=None):
        self._people = people or []

    def people(self, **kw):
        return self._people

    def me_performance(self, **kw):
        return MyPerformance(
            user_id=str(uuid4()),
            name="Me",
            throughput=12,
            quality_score=90,
            first_pass_yield=0.8,
        )


class _StubClient:
    def __init__(
        self,
        projects,
        datasets,
        job_pages,
        ml_by_project=None,
        pools_by_project=None,
        batches_by_project=None,
        members_by_project=None,
        role="employee",
        people=None,
        stats=None,
    ):
        self.projects = _StubProjects(projects, stats=stats)
        self.datasets = _StubDatasets(datasets)
        self.jobs = _StubJobs(job_pages)
        self.exports = _StubExports()
        self.ml_backends = _StubMLBackends(
            ml_by_project or {}, pools_by_project=pools_by_project
        )
        self.batches = _StubBatches(batches_by_project)
        self.members = _StubMembers(members_by_project)
        self.dashboard = _StubDashboard(people)
        self._role = role

    def me(self):
        if self._role is None:
            raise RuntimeError("no /auth/me")
        return Me(id=uuid4(), email="me@x.io", name="Me", role=self._role)

    def close(self):
        pass


def _make_app(
    job_statuses=("running",),
    with_ml=False,
    role="employee",
    job_kind="export",
) -> AapTuiApp:
    pages = [JobPage(items=[_job(s, kind=job_kind)], total=1) for s in job_statuses]
    project = _project()
    ml_by_project = {project.id: [_ml_backend(project.id)]} if with_ml else {}
    client = _StubClient([project], [_dataset()], pages, ml_by_project, role=role)
    return AapTuiApp(client, base_url=BASE)


async def _settle(app, pilot):
    """等 thread worker 落地并刷新一帧。"""
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_renders_tabs_and_status_bar():
    app = _make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        assert [t.id for t in app.query(TabPane)] == [
            "tab-projects",
            "tab-datasets",
            "tab-jobs",
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
        # 切到 Jobs tab 后 r 触发立即刷新 (与 3s 轮询同一条加载路径)
        app.query_one("#tabs", TabbedContent).active = "tab-jobs"
        await pilot.pause()
        await pilot.press("r")
        await _settle(app, pilot)
        assert "completed" in jobs.get_row_at(0)[1].plain
        # 翻转行高亮 + 状态栏提示
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
    # 同一全局 backend 被多个项目启用 → 各项目作用域端点返回同一 registry id;
    # 聚合须按 id 去重为一行 (否则 DataTable 同 key add_row 崩溃), 项目列示「N 个项目」。
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
    shared = _ml_backend(proj_a.id)  # 固定 id 的同一实例, 两项目共享
    client = _StubClient(
        [proj_a, proj_b],
        [_dataset()],
        [JobPage(items=[_job("running")], total=1)],
        {proj_a.id: [shared], proj_b.id: [shared]},
    )
    app = AapTuiApp(client, base_url=BASE)
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
    # 回车 push 实时详情屏 MlBackendDetailScreen; 顶部 #ml-static 仍是 REST 快照。
    # 测试无 api_key (base_url 给了但 api_key 缺), WS 不启动, 仅展示静态体 + 降级提示, 不崩。
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
        # esc 返回主屏 (栈回到只剩主屏)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, MlBackendDetailScreen)
        assert app.query_one("#tabs", TabbedContent) is not None


async def test_export_action_opens_modal_and_create():
    app = _make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        # 默认在 Projects tab, 首行已高亮
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, ExportConfigModal)  # 弹导出配置框, 非简单确认
        await pilot.click("#modal-ok")  # image 项目默认勾 coco
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
        await pilot.click("#modal-cancel")  # 取消按钮
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
    app = AapTuiApp(client, base_url=BASE)
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, ExportConfigModal)
        # lidar: 有坐标系 RadioSet, 无帧模式
        assert app.screen.query("#export-axis")
        assert not app.screen.query("#export-frame")
        await pilot.click("#modal-ok")
        await _settle(app, pilot)
        _pid, targets, kwargs = app._client.exports.created[0]
        assert targets == ["aap_json"]  # lidar 默认勾选
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
        # 终态 job 不弹确认框、不发请求, 状态栏给提示
        assert app._client.jobs.cancelled == []
        assert "不可取消" in str(app.query_one("#status-bar", Static).render())


async def test_quit_key():
    app = _make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        await pilot.press("q")
    assert app.return_code == 0


async def _settle_screen(app, pilot):
    """等子屏 worker 落地 (push 详情屏后调用)。"""
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


# ---- 下钻子路由 + 动作按钮 ----


async def test_open_project_pushes_detail_with_subtabs():
    project = _project()
    job = _job("running", project_id=project.id)  # 命中本项目的 job
    pages = [JobPage(items=[job], total=1)]
    client = _StubClient(
        [project], [_dataset()], pages, {project.id: [_ml_backend(project.id)]}
    )
    app = AapTuiApp(client, base_url=BASE)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        await pilot.press("o")  # 打开 Projects 高亮首行
        await _settle_screen(app, pilot)
        assert isinstance(app.screen, ProjectDetailScreen)
        # 六个 scoped 子 tab（含项目可用 Pool）
        assert [t.id for t in app.screen.query(TabPane)] == [
            "pd-overview",
            "pd-batches",
            "pd-members",
            "pd-jobs",
            "pd-backends",
            "pd-pools",
        ]
        # 任务表按 project_id 过滤后命中 1 条; backend 表 project-scoped 1 条
        assert app.screen.query_one("#pd-jobs-table", DataTable).row_count == 1
        assert app.screen.query_one("#pd-backends-table", DataTable).row_count == 1


async def test_project_detail_jobs_filtered_excludes_other_project():
    project = _project()
    other = _job("running")  # project_id=None → 应被过滤掉
    pages = [JobPage(items=[other], total=1)]
    client = _StubClient([project], [_dataset()], pages, {})
    app = AapTuiApp(client, base_url=BASE)
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
        await pilot.press("e")  # 项目详情屏内发起导出
        await pilot.pause()
        assert isinstance(app.screen, ExportConfigModal)
        await pilot.click("#modal-ok")  # 导出配置框确认
        await _settle(app, pilot)
        assert len(app._client.exports.created) == 1


async def test_action_bar_open_button_pushes_project_detail():
    app = _make_app()
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        await pilot.click("#proj-open")  # 点击「打开」按钮
        await _settle_screen(app, pilot)
        assert isinstance(app.screen, ProjectDetailScreen)


async def test_dataset_detail_loads_items_and_linked_projects():
    app = _make_app()
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-datasets"
        await _settle(app, pilot)
        app.query_one("#datasets-table", DataTable).focus()
        await pilot.press("enter")
        await _settle_screen(app, pilot)
        assert isinstance(app.screen, DatasetDetailScreen)
        assert app.screen.query_one("#dd-items-table", DataTable).row_count == 1
        assert app.screen.query_one("#dd-projects-table", DataTable).row_count == 0


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
    app = AapTuiApp(client, base_url=BASE)
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
        await pilot.press("enter")  # 下钻 job 详情
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        # 运行中 job → 详情屏带取消按钮
        assert app.screen.query("#cancel")
        await pilot.click("#cancel")
        await pilot.pause()
        await pilot.press("y")  # 确认取消
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
        # 终态 (无 download_url) job 既不提供取消也不提供下载
        assert not app.screen.query("#cancel")
        assert not app.screen.query("#download")


# ---- 导出对齐 + 闭环下载 + 悬浮框按钮化 ----


async def test_cancel_confirm_via_button_triggers_cancel():
    # ConfirmModal 按钮通道: 点「确认取消」按钮等价于按 y
    app = _make_app(job_statuses=("running",))
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-jobs"
        await pilot.pause()
        app.query_one("#jobs-table", DataTable).focus()
        await pilot.press("c")
        await pilot.pause()
        await pilot.click("#modal-ok")  # 点确认按钮 (而非按 y)
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
    job = _job("completed", result=result)  # _job 默认 kind="export"
    client = _StubClient([_project()], [_dataset()], [JobPage(items=[job], total=1)])
    app = AapTuiApp(client, base_url=BASE)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-jobs"
        await pilot.pause()
        app.query_one("#jobs-table", DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        # 完成态导出 job → 详情屏有下载按钮 + 结构化 result 摘要
        assert app.screen.query("#download")
        body = str(app.screen.query_one("#detail-body", Static).render())
        assert "文件数 3" in body
        # 点下载 → 弹路径框 → 确认 → stub download 落地
        await pilot.click("#download")
        await pilot.pause()
        assert isinstance(app.screen, PathInputModal)
        await pilot.click("#modal-ok")
        await _settle(app, pilot)
        assert len(app._client.exports.downloaded) == 1


# ---- 项目详情 批次 / 成员 子 tab ----


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


def _member() -> Member:
    return Member(
        id=uuid4(),
        user_id=uuid4(),
        user_name="张三",
        user_email="zhang@x.io",
        role="annotator",
        platform_role="employee",
        assigned_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
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
    app = AapTuiApp(client, base_url=BASE)
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
        assert "60%" in row[2]  # progress_cell
        assert row[5] == "标注员甲"
        assert row[6] == "审核员乙"
        mt = app.screen.query_one("#pd-members-table", DataTable)
        assert mt.row_count == 1
        assert mt.get_row_at(0)[0] == "张三"
        assert mt.get_row_at(0)[2] == "annotator"


async def test_project_detail_degrades_when_batches_endpoint_unavailable():
    # 批次端点抛错 (旧后端/无权限) → 空表降级, 不拖垮整个详情屏
    project = _project()
    client = _StubClient([project], [_dataset()], [JobPage(items=[], total=0)])

    def _boom(*_a, **_k):
        raise RuntimeError("404 batches not found")

    client.batches.list = _boom  # type: ignore[assignment]
    app = AapTuiApp(client, base_url=BASE)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        await pilot.press("o")
        await _settle_screen(app, pilot)
        assert isinstance(app.screen, ProjectDetailScreen)
        # 批次表空, 但详情屏正常 (成员表仍在)
        assert app.screen.query_one("#pd-batches-table", DataTable).row_count == 0
        assert app.screen.query_one("#pd-members-table", DataTable) is not None


# ---- 看板趋势 + 角色门控绩效 ----


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
    from ai_annotation.tui.app import AxisChart

    app = _make_app()
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-stats"
        await _settle(app, pilot)
        headline = str(app.query_one("#stats-headline", Static).render())
        assert "总量 100" in headline
        assert "AI率 40%" in headline
        # 4 条折线图都拿到了序列数据
        assert app.query_one("#spark-total", AxisChart)._data
        assert len(app.query_one("#spark-completed", AxisChart)._data) == 12
        assert app.query_one("#spark-airate", AxisChart)._data


async def test_axis_chart_renders_axes_and_line():
    from ai_annotation.tui.app import _render_axis_chart

    plain = _render_axis_chart(
        [50, 60, 55, 80, 40, 95],
        width=60,
        height=8,
        unit="%",
        x_left="-12w",
        x_right="now",
    ).plain
    # 自适应纵轴: 顶=max 底=min; 横轴: 首末标签 + └ 基线; braille 连线
    assert "95%" in plain and "40%" in plain
    assert "-12w" in plain and "now" in plain
    assert "└" in plain
    assert any(0x2800 <= ord(c) <= 0x28FF and c != "⠀" for c in plain)
    # 数据不足时降级为占位, 不抛
    assert "等待数据" in _render_axis_chart([], 60, 8, "%", "-12w", "now").plain


async def test_fmt_pool_summarizes_protocol_fields():
    from ai_annotation.tui.app import _fmt_pool

    # 协议 PoolStatus / video_pool 字段 → 简洁摘要, 不退化打印原始长 dict
    img = _fmt_pool(
        {"cap": 1, "current_size": 0, "loaded_keys": [], "last_evict": None}
    )
    assert img == "cap=1 · loaded=0"
    vid = _fmt_pool(
        {"cap": 1, "loaded_variants": [], "active_sessions": 0, "idle_seconds": 600}
    )
    assert vid == "cap=1 · loaded=0 · active=0 · idle=600s"
    # current_size 缺失 → 回落到 loaded_keys 长度
    assert (
        _fmt_pool({"cap": 2, "loaded_keys": [{"key": "a"}, {"key": "b"}]})
        == "cap=2 · loaded=2"
    )
    assert _fmt_pool({}) == "-"
    assert "loaded_keys" not in img  # 不出现原始键名


async def test_people_tab_loads_for_super_admin():
    project = _project()
    client = _StubClient(
        [project],
        [_dataset()],
        [JobPage(items=[], total=0)],
        role="super_admin",
        people=[_person()],
    )
    app = AapTuiApp(client, base_url=BASE)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-people"
        await _settle(app, pilot)
        table = app.query_one("#people-table", DataTable)
        assert table.row_count == 1
        row = table.get_row_at(0)
        assert row[0] == "标注员甲"
        assert row[2] == "82"  # 产出分
        assert "5%" in row[4]  # 退回率


async def test_people_tab_gated_for_employee():
    # 非 super_admin 仍加载本人摘要，但不拉全员 people。
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
    # me() 不可用 (老后端) → 角色未知, 绩效不拉数, 主流程不崩
    app = _make_app(role=None)
    async with app.run_test(size=(120, 32)) as pilot:
        await _settle(app, pilot)
        app.query_one("#tabs", TabbedContent).active = "tab-people"
        await _settle(app, pilot)
        assert app.query_one("#people-table", DataTable).row_count == 0
        assert "本人 Me" in str(app.query_one("#people-self", Static).render())
        assert "角色未知" in str(app.query_one("#people-note", Static).render())
