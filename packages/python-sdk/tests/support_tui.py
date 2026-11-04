from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from yoao.models import (
    Dataset,
    DatasetItem,
    Job,
    JobPage,
    JobRetryResult,
    Me,
    Member,
    MyPerformance,
    Page,
    Project,
    ProjectStats,
)


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


async def _settle(app, pilot):
    """等 thread worker 落地并刷新一帧。"""
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _settle_screen(app, pilot):
    """等子屏 worker 落地 (push 详情屏后调用)。"""
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


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
