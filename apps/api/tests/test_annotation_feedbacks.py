"""I18 · AnnotationFeedback 统一表 service + schema 测试."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.annotation_feedback import AnnotationFeedbackCreate
from app.services.feedback import FeedbackService
from tests.factory import create_project, create_task


def test_pixel_anchor_requires_position():
    with pytest.raises(ValidationError):
        AnnotationFeedbackCreate(
            kind="issue",
            anchor_type="pixel",
            project_id="00000000-0000-0000-0000-000000000001",
            task_id="00000000-0000-0000-0000-000000000002",
            body="missing position",
        )


def test_project_anchor_rejects_task_id():
    with pytest.raises(ValidationError):
        AnnotationFeedbackCreate(
            kind="comment",
            anchor_type="project",
            project_id="00000000-0000-0000-0000-000000000001",
            task_id="00000000-0000-0000-0000-000000000002",
            body="should fail",
        )


def test_annotation_anchor_requires_both_ids():
    with pytest.raises(ValidationError):
        AnnotationFeedbackCreate(
            kind="comment",
            anchor_type="annotation",
            project_id="00000000-0000-0000-0000-000000000001",
            task_id="00000000-0000-0000-0000-000000000002",
            body="missing annotation_id",
        )


@pytest.mark.asyncio
async def test_feedback_create_and_list(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    svc = FeedbackService(db_session)
    issue = await svc.create(
        author_id=user.id,
        kind="issue",
        anchor_type="pixel",
        project_id=proj.id,
        task_id=task.id,
        annotation_id=None,
        anchor_position={"x": 0.5, "y": 0.3},
        severity="warn",
        title="漏标了一片区域",
        body="左上角缺一个人",
        attachments=[],
        thread_parent_id=None,
    )
    await db_session.flush()

    rows, cursor = await svc.list_paged(project_id=proj.id, limit=10)
    assert len(rows) == 1
    assert rows[0].id == issue.id
    assert rows[0].anchor_position["x"] == 0.5
    assert cursor is None


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [0, 17])
async def test_video_issue_preserves_source_frame_and_task_anchor(
    db_session, super_admin, frame
):
    user, _ = super_admin
    project = await create_project(db_session, owner_id=user.id, type_key="video-det")
    task = await create_task(db_session, project_id=project.id)
    service = FeedbackService(db_session)
    for position in ({"x": 0.5, "y": 0.25, "frame": frame}, None):
        await service.create(
            author_id=user.id,
            kind="issue",
            anchor_type="pixel" if position else "task",
            project_id=project.id,
            task_id=task.id,
            annotation_id=None,
            anchor_position=position,
            severity="warn",
            title=None,
            body="Video frame anchor regression",
            attachments=[],
            thread_parent_id=None,
        )
    await db_session.flush()
    project_id, task_id = project.id, task.id
    db_session.expire_all()
    rows, _ = await service.list_paged(project_id=project_id, task_id=task_id, limit=10)
    assert len(rows) == 2
    pixel = next(row for row in rows if row.anchor_type == "pixel")
    assert pixel.anchor_position == {"x": 0.5, "y": 0.25, "frame": frame}
    assert (
        next(row for row in rows if row.anchor_type == "task").anchor_position is None
    )


@pytest.mark.asyncio
async def test_feedback_patch_status_sets_resolved_at(db_session, super_admin):
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    svc = FeedbackService(db_session)
    issue = await svc.create(
        author_id=user.id,
        kind="issue",
        anchor_type="task",
        project_id=proj.id,
        task_id=task.id,
        annotation_id=None,
        anchor_position=None,
        severity="info",
        title="t",
        body="b",
        attachments=[],
        thread_parent_id=None,
    )
    await db_session.flush()

    updated = await svc.patch(issue.id, actor_id=user.id, status="resolved")
    assert updated.status == "resolved"
    assert updated.resolved_at is not None
    assert updated.resolved_by_id == user.id

    reopened = await svc.patch(issue.id, actor_id=user.id, status="open")
    assert reopened.status == "open"
    assert reopened.resolved_at is None
    assert reopened.resolved_by_id is None


@pytest.mark.asyncio
async def test_mirror_bug_report_creates_feedback(db_session, super_admin):
    """BugReportService.create 同事务 mirror 到 annotation_feedbacks (kind=bug)."""
    from sqlalchemy import select

    from app.db.models.annotation_feedback import AnnotationFeedback
    from app.services.bug_report import BugReportService

    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    bug_svc = BugReportService(db_session)
    await bug_svc.create(
        reporter_id=user.id,
        user_role="super_admin",
        project_id=proj.id,
        task_id=task.id,
        route="/workbench",
        title="按钮无响应",
        description="点保存没反应",
        severity="high",
        status="new",
    )
    await db_session.flush()

    rows = (
        (
            await db_session.execute(
                select(AnnotationFeedback).where(AnnotationFeedback.kind == "bug")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    mirror = rows[0]
    assert mirror.anchor_type == "task"
    assert mirror.task_id == task.id
    assert mirror.project_id == proj.id
    assert mirror.title == "按钮无响应"
    assert mirror.body == "点保存没反应"
    assert mirror.severity == "high"
    assert mirror.author_id == user.id


@pytest.mark.asyncio
async def test_mirror_bug_report_skips_when_project_null(db_session, super_admin):
    """无 project_id 的 bug (登录页等) 不 mirror, 仅写 bug_reports."""
    from sqlalchemy import select

    from app.db.models.annotation_feedback import AnnotationFeedback
    from app.services.bug_report import BugReportService

    user, _ = super_admin

    bug_svc = BugReportService(db_session)
    await bug_svc.create(
        reporter_id=user.id,
        user_role="super_admin",
        route="/login",
        title="登录失败",
        description="点击登录无响应",
        severity="medium",
        status="new",
    )
    await db_session.flush()

    rows = (
        (
            await db_session.execute(
                select(AnnotationFeedback).where(AnnotationFeedback.kind == "bug")
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_mirror_task_reject_creates_feedback(db_session, super_admin):
    """FeedbackService.mirror_task_reject 同事务 mirror (kind=reject)."""
    from sqlalchemy import select

    from app.db.models.annotation_feedback import AnnotationFeedback

    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    task.reject_reason = "标错了"
    task.reject_reason_type = "wrong_label"
    await db_session.flush()

    svc = FeedbackService(db_session)
    await svc.mirror_task_reject(task, reviewer_id=user.id)
    await db_session.flush()

    rows = (
        (
            await db_session.execute(
                select(AnnotationFeedback).where(AnnotationFeedback.kind == "reject")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    mirror = rows[0]
    assert mirror.anchor_type == "task"
    assert mirror.task_id == task.id
    assert mirror.body == "标错了"
    assert mirror.severity == "wrong_label"


@pytest.mark.asyncio
async def test_view_unions_sources(db_session, super_admin):
    """v_annotation_feedback_unified UNION ALL 4 数据源, source_table 列正确分组."""
    from sqlalchemy import text

    from app.services.bug_report import BugReportService

    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    svc = FeedbackService(db_session)
    await svc.create(
        author_id=user.id,
        kind="issue",
        anchor_type="task",
        project_id=proj.id,
        task_id=task.id,
        annotation_id=None,
        anchor_position=None,
        severity="info",
        title="t",
        body="b",
        attachments=[],
        thread_parent_id=None,
    )
    await BugReportService(db_session).create(
        reporter_id=user.id,
        user_role="super_admin",
        project_id=proj.id,
        task_id=task.id,
        route="/workbench",
        title="bug",
        description="desc",
        severity="medium",
        status="new",
    )
    task.status = "rejected"
    task.reject_reason = "标错了"
    task.reject_reason_type = "wrong_label"
    from datetime import datetime, timezone

    task.reviewed_at = datetime.now(timezone.utc)
    await svc.mirror_task_reject(task, reviewer_id=user.id)
    await db_session.flush()

    rows = (
        await db_session.execute(
            text(
                "SELECT source_table, COUNT(*) FROM v_annotation_feedback_unified "
                "WHERE project_id = :pid GROUP BY source_table ORDER BY source_table"
            ),
            {"pid": proj.id},
        )
    ).all()
    counts = {r[0]: r[1] for r in rows}
    assert counts.get("annotation_feedbacks") == 3
    assert counts.get("bug_reports") == 1
    assert counts.get("tasks_reject") == 1


@pytest.mark.asyncio
async def test_compute_feedback_drift_zero_when_mirrored(db_session, super_admin):
    """正常态：bug/reject 均经双写 mirror → compute_feedback_drift 全 0。"""
    from datetime import datetime, timezone

    from app.services.bug_report import BugReportService
    from app.services.feedback_reconcile import compute_feedback_drift

    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    task = await create_task(db_session, project_id=proj.id)
    await db_session.flush()

    await BugReportService(db_session).create(
        reporter_id=user.id,
        user_role="super_admin",
        project_id=proj.id,
        task_id=task.id,
        route="/workbench",
        title="bug",
        description="desc",
        severity="medium",
        status="new",
    )
    task.status = "rejected"
    task.reject_reason = "标错了"
    task.reject_reason_type = "wrong_label"
    task.reviewed_at = datetime.now(timezone.utc)
    await FeedbackService(db_session).mirror_task_reject(task, reviewer_id=user.id)
    await db_session.flush()

    drift = await compute_feedback_drift(db_session)
    assert sum(len(v["missing_ids"]) for v in drift.values()) == 0
    assert drift["bug_reports"]["missing_ids"] == []
    assert drift["tasks_reject"]["missing_ids"] == []
    assert drift["bug_reports"]["expected"] == 1
    assert drift["bug_reports"]["actual"] == 1


@pytest.mark.asyncio
async def test_reconcile_detects_drift_and_notifies(
    db_session, super_admin, monkeypatch
):
    """人为制造 drift（插 bug 不 mirror）→ 检出 missing=1 + notify_many 被调用。"""
    import uuid as _uuid

    from app.db.models.bug_report import BugReport
    from app.services.feedback_reconcile import compute_feedback_drift
    from app.services.notification import NotificationService
    from app.workers.feedback_reconcile import run_reconcile

    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    await db_session.flush()

    db_session.add(
        BugReport(
            id=_uuid.uuid4(),
            display_id="B-DRIFT-1",
            reporter_id=user.id,
            route="/workbench",
            user_role="super_admin",
            project_id=proj.id,
            task_id=None,
            title="orphan bug",
            description="never mirrored",
            severity="high",
            status="new",
        )
    )
    await db_session.flush()

    drift = await compute_feedback_drift(db_session)
    assert len(drift["bug_reports"]["missing_ids"]) == 1
    assert drift["bug_reports"]["expected"] == 1
    assert drift["bug_reports"]["actual"] == 0

    calls: list[dict] = []
    orig = NotificationService.notify_many

    async def _spy(self, **kwargs):
        calls.append(kwargs)
        return await orig(self, **kwargs)

    monkeypatch.setattr(NotificationService, "notify_many", _spy)

    result = await run_reconcile(db_session)
    assert result["total_missing"] == 1
    assert len(calls) == 1
    assert calls[0]["type"] == "feedback.reconcile_drift"
    assert user.id in calls[0]["user_ids"]
    assert calls[0]["payload"]["total_missing"] == 1
    assert calls[0]["payload"]["missing_by_source"]["bug_reports"] == 1


@pytest.mark.asyncio
async def test_drift_detects_under_mirrored_duplicates(db_session, super_admin):
    """内容完全相同的 2 条旧表行只 mirror 了 1 条 → 必须检出缺失 1 条。

    回归 PR #21 审查发现的 false-negative：旧 NOT EXISTS 写法在重复内容下会把
    两条都判为「已覆盖」漏报缺失。差额计数应精确报出 1 条。
    """
    import uuid as _uuid

    from app.db.models.annotation_feedback import AnnotationFeedback
    from app.db.models.bug_report import BugReport
    from app.services.feedback_reconcile import compute_feedback_drift

    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id, type_key="image-det")
    await db_session.flush()

    for i in range(2):
        db_session.add(
            BugReport(
                id=_uuid.uuid4(),
                display_id=f"B-DUP-{i}",
                reporter_id=user.id,
                route="/workbench",
                user_role="super_admin",
                project_id=proj.id,
                task_id=None,
                title="same title",
                description="same body",
                severity="low",
                status="new",
            )
        )
    db_session.add(
        AnnotationFeedback(
            id=_uuid.uuid4(),
            kind="bug",
            anchor_type="project",
            project_id=proj.id,
            title="same title",
            body="same body",
            author_id=user.id,
        )
    )
    await db_session.flush()

    drift = await compute_feedback_drift(db_session)
    assert drift["bug_reports"]["expected"] == 2
    assert len(drift["bug_reports"]["missing_ids"]) == 1
    assert drift["bug_reports"]["actual"] == 1
