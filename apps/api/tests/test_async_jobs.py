"""async_jobs 所有者命令与 API 行为回归。

覆盖：
- service: create_job / mark_running / update_payload /
  update_progress / mark_complete / mark_failed / track_job（happy + raise）
- API: GET /async-jobs owner-scoped、super_admin 看全部、cancel kind 白名单 + 终态拒绝
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.notification import Notification
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

    @pytest.mark.parametrize(
        "status", ["pending", "running", "completed", "cancelled", "failed"]
    )
    async def test_dispatch_failure_uses_current_state_despite_cached_pending(
        self, db_session, status
    ):
        job = await async_job_svc.create_job(db_session, kind="batch_predict")
        settled_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        # Model a worker's update without refreshing the API's cached pending row.
        await db_session.execute(
            update(AsyncJob)
            .where(AsyncJob.id == job.id)
            .values(
                status=status,
                completed_at=settled_at,
                error_message="worker outcome",
                result={"retained": True},
            )
            .execution_options(synchronize_session=False)
        )
        assert job.status == "pending"

        changed = await async_job_svc.mark_dispatch_failed(
            db_session, job.id, error="x" * 4001
        )
        await db_session.refresh(job)

        assert changed is (status == "pending")
        assert job.result == {"retained": True}
        if status == "pending":
            assert job.status == "failed"
            assert job.completed_at > settled_at
            assert job.error_message == "x" * 4000
        else:
            assert job.status == status
            assert job.completed_at == settled_at
            assert job.error_message == "worker outcome"

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

    async def test_update_payload_shallow_merge_preserves_other_fields(
        self, db_session
    ):
        job = await async_job_svc.create_job(
            db_session,
            kind="batch_predict",
            payload={"retained": True, "nested": {"old": True}},
        )

        await async_job_svc.update_payload(
            db_session, job.id, {"added": 2, "nested": {"new": True}}
        )
        await db_session.flush()
        await db_session.refresh(job)
        assert job.payload == {
            "retained": True,
            "added": 2,
            "nested": {"new": True},
        }

    async def test_delete_terminal_jobs_before_preserves_cutoff_and_status(
        self, db_session
    ):
        cutoff = datetime(2000, 1, 1, tzinfo=timezone.utc)
        old = cutoff - timedelta(seconds=1)
        jobs = {}
        for name, status, created_at in (
            ("completed-old", AsyncJobStatus.COMPLETED.value, old),
            ("failed-old", AsyncJobStatus.FAILED.value, old),
            ("cancelled-old", AsyncJobStatus.CANCELLED.value, old),
            ("pending-old", AsyncJobStatus.PENDING.value, old),
            ("running-old", AsyncJobStatus.RUNNING.value, old),
            ("completed-at-cutoff", AsyncJobStatus.COMPLETED.value, cutoff),
        ):
            job = await async_job_svc.create_job(db_session, kind="batch_predict")
            job.status = status
            job.created_at = created_at
            jobs[name] = job

        removed_names = {"completed-old", "failed-old", "cancelled-old"}
        removed_ids = await async_job_svc.delete_terminal_jobs_before(
            db_session,
            cutoff,
            statuses=(
                AsyncJobStatus.COMPLETED.value,
                AsyncJobStatus.FAILED.value,
                AsyncJobStatus.CANCELLED.value,
            ),
        )
        assert set(removed_ids) == {jobs[name].id for name in removed_names}

        job_ids = [job.id for job in jobs.values()]
        remaining_ids = await db_session.scalars(
            select(AsyncJob.id).where(AsyncJob.id.in_(job_ids))
        )
        assert set(remaining_ids) == {
            jobs[name].id for name in jobs.keys() - removed_names
        }

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
            db_session, kind="dataset_import", user_id=user.id
        )
        # 别人的 job
        await async_job_svc.create_job(db_session, kind="dataset_import")
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
            db_session, kind="dataset_import", user_id=anno_user.id
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
        self, httpx_client, db_session, project_admin
    ):
        user, token = project_admin
        project = await create_project(db_session, owner_id=user.id)
        batch_job = await async_job_svc.create_job(
            db_session,
            kind="batch_predict",
            user_id=user.id,
            project_id=project.id,
        )
        retry_job = await async_job_svc.create_job(
            db_session,
            kind="prediction_retry",
            user_id=user.id,
            project_id=project.id,
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
            type_label="Point Cloud",
            type_key="point-cloud",
            data_type="point_cloud",
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
            "apply_async",
            lambda **kwargs: queued.append(tuple(kwargs["args"])),
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

    @pytest.mark.parametrize("fail_at", [0, 1])
    async def test_retry_publish_uncertainty_keeps_all_durable_claims(
        self, db_session, project_admin, monkeypatch, fail_at
    ):
        from app.application.async_job_contracts import RetryCredentialAuthority
        from app.application.prediction_retry import SqlPredictionRetry
        from app.workers.prediction_retry_dispatch import (
            CeleryPredictionRetryDispatcher,
        )
        from app.workers import predictions_retry

        user, _ = project_admin
        project = await create_project(db_session, owner_id=user.id)
        project.data_type = "point_cloud"
        context = {"type": "text", "text": "car"}
        failures = [
            FailedPrediction(
                project_id=project.id,
                error_type="TimeoutError",
                message="timeout",
                extra={"request_context": context},
            )
            for _ in range(3)
        ]
        db_session.add_all(failures)
        await db_session.flush()
        job = await async_job_svc.create_job(
            db_session,
            kind="batch_predict",
            user_id=user.id,
            project_id=project.id,
        )
        await async_job_svc.mark_complete(
            db_session,
            job.id,
            result={"failed_prediction_ids": [str(row.id) for row in failures]},
        )
        await db_session.flush()

        def publish(**kwargs):
            failed_id, _user_id = kwargs["args"]
            if failed_id == str(failures[fail_at].id):
                raise RuntimeError("publication interrupted")

        monkeypatch.setattr(
            predictions_retry.retry_failed_prediction, "apply_async", publish
        )
        result = await SqlPredictionRetry(
            db_session, dispatcher=CeleryPredictionRetryDispatcher()
        ).retry_job_items(
            job_id=job.id,
            actor_id=user.id,
            credential=RetryCredentialAuthority(
                actor_id=user.id, authorization="Bearer test"
            ),
        )
        assert result.queued == 3

        for row in failures:
            await db_session.refresh(row)
            assert row.extra.get("retry_pending") is True
            assert row.extra.get("retry_job_id")
            assert row.extra["request_context"] == context

    async def test_retry_failed_batch_predict_items_requires_recorded_ids(
        self, httpx_client, db_session, project_admin
    ):
        user, token = project_admin
        project = await create_project(db_session, owner_id=user.id)
        aj = await async_job_svc.create_job(
            db_session,
            kind="batch_predict",
            user_id=user.id,
            project_id=project.id,
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
        self, httpx_client, db_session, project_admin
    ):
        user, token = project_admin
        project = await create_project(db_session, owner_id=user.id)
        aj = await async_job_svc.create_job(
            db_session,
            kind="video_tracker",
            user_id=user.id,
            project_id=project.id,
        )
        await async_job_svc.mark_running(db_session, aj.id)
        await db_session.flush()

        r = await httpx_client.post(
            f"/api/v1/async-jobs/{aj.id}/cancel", headers=_bearer(token)
        )
        assert r.status_code == 400
        assert "not cancellable" in r.json()["detail"]

    async def test_cancel_running_batch_predict_requests_cooperative_cancel(
        self, httpx_client, db_session, project_admin, monkeypatch
    ):
        user, token = project_admin
        project = await create_project(db_session, owner_id=user.id)
        aj = await async_job_svc.create_job(
            db_session,
            kind="batch_predict",
            user_id=user.id,
            project_id=project.id,
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
        assert r.json() == {"status": "cancel_requested", "id": str(aj.id)}
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
        self, httpx_client, db_session, project_admin, monkeypatch
    ):
        user, token = project_admin
        project = await create_project(db_session, owner_id=user.id)
        aj = await async_job_svc.create_job(
            db_session,
            kind="batch_predict",
            user_id=user.id,
            project_id=project.id,
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
        assert r.json() == {"status": "cancelled", "id": str(aj.id)}
        await db_session.refresh(aj)
        assert aj.status == AsyncJobStatus.CANCELLED.value
        assert aj.result["done_count"] == 0
        assert aj.result["skipped_count"] == 5
        assert called == {"task_id": "celery-batch-pending", "terminate": False}

    async def test_cancel_supported_kind_succeeds(
        self, httpx_client, db_session, project_admin
    ):
        user, token = project_admin
        project = await create_project(db_session, owner_id=user.id)
        aj = await async_job_svc.create_job(
            db_session,
            kind="predictions_import",
            user_id=user.id,
            project_id=project.id,
        )
        await async_job_svc.mark_running(db_session, aj.id)
        await db_session.flush()

        r = await httpx_client.post(
            f"/api/v1/async-jobs/{aj.id}/cancel", headers=_bearer(token)
        )
        assert r.status_code == 200
        assert r.json() == {"status": "cancelled", "id": str(aj.id)}
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
        self, httpx_client, db_session, project_admin
    ):
        user, token = project_admin
        project = await create_project(db_session, owner_id=user.id)
        aj = await async_job_svc.create_job(
            db_session,
            kind="predictions_import",
            user_id=user.id,
            project_id=project.id,
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


@pytest.mark.parametrize("kind", ["mask_repair", "mask_format_import"])
@pytest.mark.parametrize("status", ["pending", "running"])
@pytest.mark.parametrize("broker_fails", [False, True])
async def test_domain_cancellation_keeps_pending_and_running_semantics_on_revoke_failure(
    httpx_client, db_session, super_admin, monkeypatch, kind, status, broker_fails
):
    import uuid
    from datetime import datetime, timedelta, timezone

    from app.db.models.mask_format_import import MaskFormatImport
    from app.db.models.mask_repair_batch import MaskRepairBatch
    from app.workers.celery_app import celery_app

    user, token = super_admin
    project = await create_project(db_session, owner_id=user.id)
    job = await async_job_svc.create_job(
        db_session,
        kind=kind,
        user_id=user.id,
        project_id=project.id,
        celery_task_id=str(uuid.uuid4()),
    )
    job.status = status
    common = {
        "project_id": project.id,
        "requested_by_id": user.id,
        "async_job_id": job.id,
        "token_hash": "a" * 64,
        "plan_digest": "b" * 64,
        "plan_json": {},
        "receipt_expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
        "status": status,
    }
    if kind == "mask_repair":
        ledger = MaskRepairBatch(**common, request_json={})
    else:
        ledger = MaskFormatImport(
            **common,
            format_id="coco-rle",
            adapter_version="1.0",
            manifest_version="1.0",
            staged_object_key=f"fixture/{uuid.uuid4()}.json",
            staged_sha256="c" * 64,
            mapping_json={},
            options_json={},
            mapping_digest="d" * 64,
            options_digest="e" * 64,
        )
    db_session.add(ledger)
    await db_session.flush()

    def request_stop(*args, **kwargs):
        if broker_fails:
            raise RuntimeError("Broker unavailable")

    monkeypatch.setattr(celery_app.control, "revoke", request_stop)
    response = await httpx_client.post(
        f"/api/v1/async-jobs/{job.id}/cancel", headers=_bearer(token)
    )
    assert response.status_code == 200
    expected = "cancelled" if status == "pending" else "cancel_requested"
    assert response.json() == {"status": expected, "id": str(job.id)}
    await db_session.refresh(job)
    await db_session.refresh(ledger)
    notices = list(
        (
            await db_session.scalars(
                select(Notification).where(Notification.target_id == job.id)
            )
        ).all()
    )
    if status == "pending":
        assert job.status == ledger.status == "cancelled"
        assert job.result == {"reason": "cancelled_by_user"}
        assert [notice.type for notice in notices] == ["job.cancelled"]
    else:
        assert job.status == ledger.status == "running"
        assert job.payload["cancel_requested"] is True
        assert notices == []
