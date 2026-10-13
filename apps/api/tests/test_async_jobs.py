"""v0.10.16 · async_jobs 表 + service + API 端点单测（ROADMAP §1.7）。

覆盖：
- service: create_job / mark_running / update_progress / mark_complete / mark_failed
  / track_job 上下文（happy + raise）
- API: GET /async-jobs owner-scoped、super_admin 看全部、cancel kind 白名单 + 终态拒绝
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models.notification import Notification
from app.db.models.async_job import AsyncJobStatus
from app.db.models.prediction import FailedPrediction
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.services import async_job as async_job_svc
from app.services.async_job_notify import notify_job_terminal
from tests.factory import create_project


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestAsyncJobService:
    async def test_create_run_complete_flow(self, db_session, annotator):
        user, _ = annotator
        aj = await async_job_svc.create_job(
            db_session,
            kind="batch_predict",
            user_id=user.id,
            payload={"total": 10},
            celery_task_id="celery-1",
        )
        assert aj.status == AsyncJobStatus.PENDING.value
        await async_job_svc.mark_running(db_session, aj.id)
        await db_session.flush()
        await db_session.refresh(aj)
        assert aj.status == AsyncJobStatus.RUNNING.value
        assert aj.started_at is not None

        await async_job_svc.update_progress(db_session, aj.id, 47)
        await db_session.flush()
        await db_session.refresh(aj)
        assert aj.progress_pct == 47

        await async_job_svc.mark_complete(
            db_session, aj.id, result={"success": 9, "fail": 1}
        )
        await db_session.flush()
        await db_session.refresh(aj)
        assert aj.status == AsyncJobStatus.COMPLETED.value
        assert aj.progress_pct == 100
        assert aj.result == {"success": 9, "fail": 1}
        assert aj.completed_at is not None

    async def test_mark_failed_with_error_message(self, db_session, annotator):
        user, _ = annotator
        aj = await async_job_svc.create_job(
            db_session, kind="predictions_import", user_id=user.id
        )
        await async_job_svc.mark_failed(db_session, aj.id, error="boom")
        await db_session.flush()
        await db_session.refresh(aj)
        assert aj.status == AsyncJobStatus.FAILED.value
        assert aj.error_message == "boom"

    async def test_mark_complete_does_not_overwrite_failed(self, db_session, annotator):
        user, _ = annotator
        aj = await async_job_svc.create_job(
            db_session, kind="audit_archive", user_id=user.id
        )
        await async_job_svc.mark_failed(db_session, aj.id, error="db down")
        await async_job_svc.mark_complete(db_session, aj.id)
        await db_session.flush()
        await db_session.refresh(aj)
        # 仍是 failed，complete 是幂等的
        assert aj.status == AsyncJobStatus.FAILED.value

    async def test_progress_pct_clamps(self, db_session):
        aj = await async_job_svc.create_job(db_session, kind="batch_predict")
        await async_job_svc.update_progress(db_session, aj.id, 150)
        await db_session.flush()
        await db_session.refresh(aj)
        assert aj.progress_pct == 100
        await async_job_svc.update_progress(db_session, aj.id, -5)
        await db_session.flush()
        await db_session.refresh(aj)
        assert aj.progress_pct == 0

    async def test_track_job_context_marks_complete_on_success(
        self, db_session, annotator
    ):
        user, _ = annotator
        async with async_job_svc.track_job(
            db_session, kind="audit_archive", user_id=user.id
        ) as aj:
            assert aj.status == AsyncJobStatus.RUNNING.value
        await db_session.refresh(aj)
        assert aj.status == AsyncJobStatus.COMPLETED.value

    async def test_track_job_context_marks_failed_on_raise(self, db_session, annotator):
        user, _ = annotator
        with pytest.raises(ValueError):
            async with async_job_svc.track_job(
                db_session, kind="audit_archive", user_id=user.id
            ) as aj:
                raise ValueError("bad")
        await db_session.refresh(aj)
        assert aj.status == AsyncJobStatus.FAILED.value
        assert "bad" in (aj.error_message or "")


async def test_removed_project_member_cannot_read_or_list_export_result(
    httpx_client, db_session, super_admin, annotator
):
    owner, owner_token = super_admin
    member, member_token = annotator
    project = await create_project(db_session, owner_id=owner.id)
    membership = ProjectMember(
        project_id=project.id,
        user_id=member.id,
        # Export result/URL access is a reviewer/manager capability; a reviewer
        # membership is required before the revocation half of this test.
        role="reviewer",
        assigned_by=owner.id,
    )
    db_session.add(membership)
    job = await async_job_svc.create_job(
        db_session,
        kind="export",
        user_id=member.id,
        project_id=project.id,
        payload={"targets": ["aap_json"]},
    )
    await async_job_svc.mark_complete(
        db_session,
        job.id,
        result={"download_url": "https://download.invalid/export.zip"},
    )
    await db_session.commit()

    member_headers = _bearer(member_token)
    await db_session.refresh(job)
    visible = await httpx_client.get(
        f"/api/v1/async-jobs/{job.id}", headers=member_headers
    )
    assert visible.status_code == 200, visible.text
    listed = await httpx_client.get(
        f"/api/v1/async-jobs?project_id={project.id}", headers=member_headers
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1

    removed = await httpx_client.delete(
        f"/api/v1/projects/{project.id}/members/{membership.id}",
        headers=_bearer(owner_token),
    )
    assert removed.status_code == 204, removed.text

    denied = await httpx_client.get(
        f"/api/v1/async-jobs/{job.id}", headers=member_headers
    )
    assert denied.status_code == 403, denied.text
    listed_after = await httpx_client.get(
        f"/api/v1/async-jobs?project_id={project.id}", headers=member_headers
    )
    assert listed_after.status_code == 200, listed_after.text
    assert listed_after.json()["items"] == []
    assert listed_after.json()["total"] == 0


class TestAsyncJobTerminalNotifications:
    async def _notification_rows(self, db_session, user_id):
        return list(
            (
                await db_session.execute(
                    select(Notification)
                    .where(Notification.user_id == user_id)
                    .order_by(Notification.created_at.asc())
                )
            )
            .scalars()
            .all()
        )

    async def test_complete_failed_and_cancelled_emit_job_notifications(
        self, db_session, annotator
    ):
        user, _ = annotator

        completed = await async_job_svc.create_job(
            db_session,
            kind="batch_predict",
            user_id=user.id,
            payload={"batch_display_id": "BATCH-1", "total_tasks": 3},
        )
        await async_job_svc.mark_complete(
            db_session,
            completed.id,
            result={"success_count": 2, "failed_count": 1},
        )
        await notify_job_terminal(db_session, job_id=completed.id)

        failed = await async_job_svc.create_job(
            db_session,
            kind="video_tracker",
            user_id=user.id,
            payload={"task_display_id": "TASK-1", "model_key": "sam2_video"},
        )
        await async_job_svc.mark_failed(db_session, failed.id, error="tracker failed")
        await notify_job_terminal(db_session, job_id=failed.id)

        cancelled = await async_job_svc.create_job(
            db_session,
            kind="predictions_import",
            user_id=user.id,
            payload={"format": "aap_json"},
        )
        await async_job_svc.mark_running(db_session, cancelled.id)
        await async_job_svc.mark_cancelled(db_session, cancelled.id)
        await notify_job_terminal(db_session, job_id=cancelled.id)

        rows = await self._notification_rows(db_session, user.id)
        assert [row.type for row in rows] == [
            "job.completed",
            "job.failed",
            "job.cancelled",
        ]
        assert rows[0].target_type == "async_job"
        assert rows[0].target_id == completed.id
        assert rows[0].payload["kind"] == "batch_predict"
        assert rows[0].payload["batch_display_id"] == "BATCH-1"
        assert rows[0].payload["success_count"] == 2
        assert rows[1].payload["error_message"] == "tracker failed"

    async def test_terminal_notification_skips_export_and_system_jobs(
        self, db_session, annotator
    ):
        user, _ = annotator

        export_job = await async_job_svc.create_job(
            db_session,
            kind="export",
            user_id=user.id,
        )
        await async_job_svc.mark_complete(db_session, export_job.id)
        await notify_job_terminal(db_session, job_id=export_job.id)

        system_job = await async_job_svc.create_job(
            db_session,
            kind="audit_archive",
            user_id=None,
        )
        await async_job_svc.mark_complete(db_session, system_job.id)
        await notify_job_terminal(db_session, job_id=system_job.id)

        rows = await self._notification_rows(db_session, user.id)
        assert rows == []

    async def test_terminal_notification_is_idempotent(self, db_session, annotator):
        user, _ = annotator
        job = await async_job_svc.create_job(
            db_session,
            kind="batch_predict",
            user_id=user.id,
        )
        await async_job_svc.mark_complete(db_session, job.id)

        await notify_job_terminal(db_session, job_id=job.id)
        await notify_job_terminal(db_session, job_id=job.id)

        rows = await self._notification_rows(db_session, user.id)
        assert len(rows) == 1
        assert rows[0].type == "job.completed"


class TestAsyncJobsAPI:
    async def test_list_owner_scoped(self, httpx_client, db_session, annotator):
        user, token = annotator
        # 自己的 job
        own = await async_job_svc.create_job(
            db_session, kind="batch_predict", user_id=user.id
        )
        # 别人的 job
        await async_job_svc.create_job(db_session, kind="batch_predict")
        await db_session.flush()

        r = await httpx_client.get("/api/v1/async-jobs", headers=_bearer(token))
        assert r.status_code == 200
        body = r.json()
        ids = {item["id"] for item in body["items"]}
        assert str(own.id) in ids
        # annotator 看不到他人的 job
        assert all(item["user_id"] == str(user.id) for item in body["items"])

    async def test_super_admin_sees_all(
        self, httpx_client, db_session, super_admin, annotator
    ):
        _, admin_token = super_admin
        anno_user, _ = annotator
        await async_job_svc.create_job(
            db_session, kind="batch_predict", user_id=anno_user.id
        )
        await db_session.flush()

        r = await httpx_client.get("/api/v1/async-jobs", headers=_bearer(admin_token))
        assert r.status_code == 200
        assert r.json()["total"] >= 1

    async def test_list_filters_and_project_meta(
        self, httpx_client, db_session, project_admin
    ):
        # Project-scoped list access needs current management of the owned
        # project; use the administrative owner so the scope matches.
        user, token = project_admin
        project = Project(
            display_id="PROJ-AJ-1",
            name="Async Jobs Project",
            type_label="Image Classification",
            type_key="image_classification",
            owner_id=user.id,
        )
        other_project = Project(
            display_id="PROJ-AJ-2",
            name="Other Project",
            type_label="Image Classification",
            type_key="image_classification",
            owner_id=user.id,
        )
        db_session.add_all([project, other_project])
        await db_session.flush()

        matching = await async_job_svc.create_job(
            db_session,
            kind="batch_predict",
            user_id=user.id,
            project_id=project.id,
            payload={
                "prompt": "detect buses",
                "batch_display_id": "BATCH-AJ-1",
            },
        )
        await async_job_svc.mark_running(db_session, matching.id)
        await async_job_svc.create_job(
            db_session,
            kind="batch_predict",
            user_id=user.id,
            project_id=project.id,
            payload={"prompt": "detect cars", "batch_display_id": "BATCH-AJ-2"},
        )
        await async_job_svc.create_job(
            db_session,
            kind="batch_predict",
            user_id=user.id,
            project_id=other_project.id,
            payload={"prompt": "detect buses", "batch_display_id": "BATCH-AJ-3"},
        )
        await db_session.flush()

        r = await httpx_client.get(
            "/api/v1/async-jobs",
            params=[
                ("project_id", str(project.id)),
                ("search", "BATCH-AJ-1"),
                ("status", "pending"),
                ("status", "running"),
            ],
            headers=_bearer(token),
        )

        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["id"] == str(matching.id)
        assert item["project_display_id"] == "PROJ-AJ-1"
        assert item["project_name"] == "Async Jobs Project"

    async def test_list_filters_multiple_kinds(
        self, httpx_client, db_session, annotator
    ):
        user, token = annotator
        batch_job = await async_job_svc.create_job(
            db_session, kind="batch_predict", user_id=user.id
        )
        retry_job = await async_job_svc.create_job(
            db_session, kind="prediction_retry", user_id=user.id
        )
        await async_job_svc.create_job(
            db_session, kind="audit_archive", user_id=user.id
        )
        await db_session.flush()

        r = await httpx_client.get(
            "/api/v1/async-jobs",
            params=[("kind", "batch_predict"), ("kind", "prediction_retry")],
            headers=_bearer(token),
        )

        assert r.status_code == 200
        ids = {item["id"] for item in r.json()["items"]}
        assert str(batch_job.id) in ids
        assert str(retry_job.id) in ids
        assert r.json()["total"] == 2

    async def test_retry_failed_batch_predict_items_queues_recorded_failures(
        self, httpx_client, db_session, project_admin, monkeypatch
    ):
        user, token = project_admin
        project = Project(
            display_id="PROJ-AJ-RETRY",
            name="Retry Project",
            type_label="Image Classification",
            type_key="image_classification",
            owner_id=user.id,
        )
        db_session.add(project)
        await db_session.flush()

        retryable = FailedPrediction(
            project_id=project.id,
            task_id=None,
            ml_backend_id=None,
            error_type="TimeoutError",
            message="timeout",
        )
        skipped = FailedPrediction(
            project_id=project.id,
            task_id=None,
            ml_backend_id=None,
            error_type="ValueError",
            message="bad",
            retry_count=3,
        )
        db_session.add_all([retryable, skipped])
        await db_session.flush()

        aj = await async_job_svc.create_job(
            db_session,
            kind="batch_predict",
            user_id=user.id,
            project_id=project.id,
        )
        await async_job_svc.mark_complete(
            db_session,
            aj.id,
            result={
                "failed_count": 2,
                "failed_prediction_ids": [str(retryable.id), str(skipped.id)],
            },
        )
        await db_session.flush()

        from app.workers import predictions_retry

        queued: list[tuple[str, str]] = []
        monkeypatch.setattr(
            predictions_retry.retry_failed_prediction,
            "delay",
            lambda failed_id, user_id: queued.append((failed_id, user_id)),
        )

        r = await httpx_client.post(
            f"/api/v1/async-jobs/{aj.id}/retry-failed", headers=_bearer(token)
        )

        assert r.status_code == 202
        assert r.json()["queued"] == 1
        assert r.json()["skipped"] == 1
        assert queued == [(str(retryable.id), str(user.id))]

        repeated = await httpx_client.post(
            f"/api/v1/async-jobs/{aj.id}/retry-failed", headers=_bearer(token)
        )
        assert repeated.status_code == 409
        assert queued == [(str(retryable.id), str(user.id))]

    async def test_retry_failed_batch_predict_items_requires_recorded_ids(
        self, httpx_client, db_session, project_admin
    ):
        user, token = project_admin
        aj = await async_job_svc.create_job(
            db_session,
            kind="batch_predict",
            user_id=user.id,
        )
        await async_job_svc.mark_complete(
            db_session,
            aj.id,
            result={"failed_count": 1},
        )
        await db_session.flush()

        r = await httpx_client.post(
            f"/api/v1/async-jobs/{aj.id}/retry-failed", headers=_bearer(token)
        )

        assert r.status_code == 409
        assert "no retryable failed prediction ids" in r.json()["detail"]

    async def test_cancel_unsupported_kind_rejected(
        self, httpx_client, db_session, annotator
    ):
        user, token = annotator
        aj = await async_job_svc.create_job(
            db_session, kind="video_tracker", user_id=user.id
        )
        await async_job_svc.mark_running(db_session, aj.id)
        await db_session.flush()

        r = await httpx_client.post(
            f"/api/v1/async-jobs/{aj.id}/cancel", headers=_bearer(token)
        )
        assert r.status_code == 400
        assert "not cancellable" in r.json()["detail"]

    async def test_cancel_running_batch_predict_requests_cooperative_cancel(
        self, httpx_client, db_session, annotator, monkeypatch
    ):
        user, token = annotator
        aj = await async_job_svc.create_job(
            db_session,
            kind="batch_predict",
            user_id=user.id,
            payload={"total_tasks": 5},
            celery_task_id="celery-batch-running",
        )
        await async_job_svc.mark_running(db_session, aj.id)
        await db_session.flush()

        from app.workers.celery_app import celery_app

        called: dict = {}

        def fake_revoke(task_id: str, terminate: bool = False):
            called["task_id"] = task_id
            called["terminate"] = terminate

        monkeypatch.setattr(celery_app.control, "revoke", fake_revoke)

        r = await httpx_client.post(
            f"/api/v1/async-jobs/{aj.id}/cancel", headers=_bearer(token)
        )

        assert r.status_code == 200
        assert r.json()["status"] == "cancel_requested"
        await db_session.refresh(aj)
        assert aj.status == AsyncJobStatus.RUNNING.value
        assert aj.payload["cancel_requested"] is True
        assert called == {"task_id": "celery-batch-running", "terminate": False}
        rows = (
            (
                await db_session.execute(
                    select(Notification).where(Notification.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
        assert rows == []

    async def test_cancel_pending_batch_predict_marks_cancelled(
        self, httpx_client, db_session, annotator, monkeypatch
    ):
        user, token = annotator
        aj = await async_job_svc.create_job(
            db_session,
            kind="batch_predict",
            user_id=user.id,
            payload={"total_tasks": 5},
            celery_task_id="celery-batch-pending",
        )
        await db_session.flush()

        from app.workers.celery_app import celery_app

        called: dict = {}
        monkeypatch.setattr(
            celery_app.control,
            "revoke",
            lambda task_id, terminate=False: called.update(
                {"task_id": task_id, "terminate": terminate}
            ),
        )

        r = await httpx_client.post(
            f"/api/v1/async-jobs/{aj.id}/cancel", headers=_bearer(token)
        )

        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"
        await db_session.refresh(aj)
        assert aj.status == AsyncJobStatus.CANCELLED.value
        assert aj.result["done_count"] == 0
        assert aj.result["skipped_count"] == 5
        assert called == {"task_id": "celery-batch-pending", "terminate": False}

    async def test_cancel_supported_kind_succeeds(
        self, httpx_client, db_session, annotator
    ):
        user, token = annotator
        aj = await async_job_svc.create_job(
            db_session, kind="predictions_import", user_id=user.id
        )
        await async_job_svc.mark_running(db_session, aj.id)
        await db_session.flush()

        r = await httpx_client.post(
            f"/api/v1/async-jobs/{aj.id}/cancel", headers=_bearer(token)
        )
        assert r.status_code == 200
        # 刷新看库
        await db_session.refresh(aj)
        assert aj.status == AsyncJobStatus.CANCELLED.value
        rows = (
            (
                await db_session.execute(
                    select(Notification).where(Notification.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].type == "job.cancelled"
        assert rows[0].target_id == aj.id

    async def test_cancel_already_terminal_rejected(
        self, httpx_client, db_session, annotator
    ):
        user, token = annotator
        aj = await async_job_svc.create_job(
            db_session, kind="predictions_import", user_id=user.id
        )
        await async_job_svc.mark_complete(db_session, aj.id)
        await db_session.flush()

        r = await httpx_client.post(
            f"/api/v1/async-jobs/{aj.id}/cancel", headers=_bearer(token)
        )
        assert r.status_code == 409

    async def test_export_job_without_project_is_hidden(
        self, httpx_client, db_session, project_admin
    ):
        """A projectless export row has no legitimate global mode."""

        user, token = project_admin
        aj = await async_job_svc.create_job(
            db_session, kind="export", user_id=user.id, payload={}
        )
        await async_job_svc.mark_complete(
            db_session, aj.id, result={"download_url": "https://download.invalid/x"}
        )
        await db_session.commit()

        detail = await httpx_client.get(
            f"/api/v1/async-jobs/{aj.id}", headers=_bearer(token)
        )
        assert detail.status_code == 403, detail.text

        listed = await httpx_client.get("/api/v1/async-jobs", headers=_bearer(token))
        assert listed.status_code == 200, listed.text
        assert str(aj.id) not in {item["id"] for item in listed.json()["items"]}
