"""Bulk review snapshots cannot expand or overwrite a concurrent decision."""

from datetime import timedelta
import importlib.util
from pathlib import Path
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, func, inspect, select
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.enums import UserRole
from app.db.models.async_job import AsyncJob
from app.db.models.audit_log import AuditLog
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_quality import DatasetQualityResult as Result
from app.db.models.dataset_quality_review import (
    DatasetQualityReviewBatch as Batch,
    DatasetQualityReviewMember as Member,
)
from app.schemas.dataset_quality import DatasetQualityConfig, DatasetQualityReview
from app.schemas.dataset_quality_review import (
    QualityReviewPreview,
    QualityReviewExecute,
)
from app.services import dataset_quality as quality, dataset_quality_review as svc
from app.workers import dataset_quality_review as worker
from tests.test_dataset_quality import world as world, run_all
from tests.test_dataset_content import headers


@pytest.fixture
async def ready(world, monkeypatch):
    db, ds, actor, items, blobs = world
    run = await quality.start(db, ds.id, actor, DatasetQualityConfig())
    await db.commit()
    await run_all(db, run)

    async def dispatch(db, batch):
        await db.commit()

    monkeypatch.setattr(worker, "dispatch", dispatch)
    return db, ds, actor, run


async def make_batch(ready, **kwargs):
    db, ds, actor, run = ready
    payload = QualityReviewPreview.model_validate(
        {"scope": "filtered", "state": "confirmed", "note": "人工核验", **kwargs}
    )
    batch = await svc.preview(db, ds.id, run.id, actor, payload)
    await db.commit()
    return batch


async def confirm(ready, batch):
    db, ds, actor, run = ready
    value, started = await svc.execute_preview(
        db,
        ds.id,
        run.id,
        batch.id,
        actor,
        QualityReviewExecute(scope_digest=batch.scope_digest, range_policy="snapshot"),
    )
    await db.commit()
    return value, started


async def execute_all(db, batch):
    job = await db.get(AsyncJob, batch.async_job_id)
    await worker.execute(db, batch.id, job.celery_task_id)
    await db.refresh(batch)


async def audits(db):
    return await db.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "dataset_quality.review")
    )


async def test_filtered_snapshot_matches_all_pages_and_never_expands(ready):
    db, ds, actor, run = ready
    page = await quality.result_page(
        db, run, limit=1, offset=1, finding="exact_duplicate"
    )
    batch = await make_batch(ready, query={"finding": "exact_duplicate"})
    assert batch.total == page["total"] == 2 and not batch.async_job_id
    snapshots = list(
        await db.scalars(select(Member).where(Member.batch_id == batch.id))
    )
    assert page["items"][0].id in {m.result_id for m in snapshots}
    original = await db.get(Result, snapshots[0].result_id)
    late = Result(
        run_id=run.id,
        item_id=uuid.uuid4(),
        file_name="late.png",
        state="checked",
        bytes_verified=True,
        sha256=original.sha256,
        size_bytes=original.size_bytes,
    )
    db.add(late)
    await db.commit()
    _, started = await confirm(ready, batch)
    assert started
    await execute_all(db, batch)
    assert batch.status == "completed" and batch.summary["applied_count"] == 2
    await db.refresh(late)
    assert late.review_revision == 0 and await audits(db) == 2
    assert batch.total == 2


async def test_selected_scope_ignores_current_filter_but_rejects_foreign_ids(ready):
    db, ds, actor, run = ready
    row = await db.scalar(select(Result).where(Result.run_id == run.id))
    batch = await make_batch(
        ready,
        scope="selected",
        result_ids=[row.id],
        query={"finding": "exact_duplicate"},
    )
    assert batch.total == 1
    with pytest.raises(HTTPException) as exc:
        await make_batch(ready, scope="selected", result_ids=[uuid.uuid4()])
    assert exc.value.status_code == 404


async def test_revision_conflicts_preserve_other_members_and_do_not_repeat_audits(
    ready,
):
    db, ds, actor, run = ready
    batch = await make_batch(ready)
    row = await db.scalar(select(Result).where(Result.run_id == run.id))
    await quality.review_result(
        db,
        run,
        row.id,
        actor,
        DatasetQualityReview(
            state="dismissed", note="另一位复核人", expected_revision=0
        ),
    )
    await db.commit()
    await confirm(ready, batch)
    await execute_all(db, batch)
    assert batch.status == "partial"
    assert batch.summary["applied_count"] == 4 and batch.summary["conflict_count"] == 1
    await db.refresh(row)
    assert row.review_state == "dismissed" and row.review_revision == 1
    assert await audits(db) == 5
    old_job = batch.async_job_id
    _, started = await confirm(ready, batch)
    assert not started and batch.async_job_id == old_job
    await db.execute(delete(AsyncJob).where(AsyncJob.id == old_job))
    await db.commit()
    await db.refresh(batch)
    assert batch.async_job_id is None
    _, started = await confirm(ready, batch)
    assert not started and await audits(db) == 5


async def test_evidence_change_conflicts_but_historical_binding_can_be_reviewed(ready):
    db, ds, actor, run = ready
    rows = list(
        await db.scalars(
            select(Result).where(Result.run_id == run.id).order_by(Result.id)
        )
    )
    await db.execute(delete(DatasetItem).where(DatasetItem.id == rows[0].item_id))
    await db.commit()
    batch = await make_batch(ready)
    assert batch.historical_count == 1
    rows[1].findings = (
        ["decode_failed"] if rows[1].findings != ["decode_failed"] else []
    )
    await db.commit()
    await confirm(ready, batch)
    await execute_all(db, batch)
    assert batch.summary["applied_count"] == 4 and batch.summary["conflict_count"] == 1
    await db.refresh(rows[0])
    assert rows[0].review_revision == 1


async def test_cancel_resume_checkpoints_and_stale_callbacks(ready):
    db, ds, actor, run = ready
    batch = await make_batch(ready)
    await confirm(ready, batch)
    job = await db.get(AsyncJob, batch.async_job_id)
    old_id, old_task, old_generation = job.id, job.celery_task_id, batch.generation
    assert await worker.process_one(db, batch.id, job.id, old_task, old_generation)
    assert batch.processed == 1 and await audits(db) == 1
    await svc.cancel_job(db, job, actor)
    await db.commit()
    assert batch.status == "cancelled"
    await svc.resume(db, ds.id, run.id, batch.id, actor)
    await db.commit()
    new_job = batch.async_job_id
    assert new_job != old_id
    assert not await worker.process_one(db, batch.id, old_id, old_task, old_generation)
    await svc.signal_interrupted(db, old_id, old_task, cancelled=False)
    await db.commit()
    await db.refresh(batch)
    assert batch.status == "pending" and batch.async_job_id == new_job
    await execute_all(db, batch)
    assert batch.status == "completed" and await audits(db) == 5
    revisions = list(
        await db.scalars(select(Result.review_revision).where(Result.run_id == run.id))
    )
    assert revisions == [1] * 5


async def test_recovery_rotates_task_and_old_signal_is_fenced(ready):
    db, ds, actor, run = ready
    batch = await make_batch(ready)
    await confirm(ready, batch)
    job = await db.get(AsyncJob, batch.async_job_id)
    old_task, generation = job.celery_task_id, batch.generation
    batch.lease_until = svc.now() - timedelta(seconds=1)
    await db.commit()
    await worker.recover(db)
    await db.refresh(batch)
    await db.refresh(job)
    assert job.celery_task_id != old_task and batch.generation > generation
    await svc.signal_interrupted(db, job.id, old_task, cancelled=False)
    await db.commit()
    assert batch.status == "pending"
    await execute_all(db, batch)
    assert batch.status == "completed"


async def test_pending_members_are_blocked_and_not_upgraded(ready):
    db, ds, actor, run = ready
    row = await db.scalar(select(Result).where(Result.run_id == run.id))
    row.state = "pending"
    run.status = "cancelled"
    await db.commit()
    batch = await make_batch(ready)
    assert batch.actionable_count == 4 and batch.processed == 1
    row.state = "checked"
    await db.commit()
    await confirm(ready, batch)
    await execute_all(db, batch)
    assert batch.status == "partial" and batch.summary["blocked_count"] == 1
    await db.refresh(row)
    assert row.review_revision == 0


async def test_expiry_digest_owner_and_parent_guards(ready, project_admin):
    db, ds, actor, run = ready
    batch = await make_batch(ready)
    with pytest.raises(HTTPException) as exc:
        await svc.execute_preview(
            db,
            ds.id,
            run.id,
            batch.id,
            actor,
            QualityReviewExecute(scope_digest="0" * 64, range_policy="snapshot"),
        )
    assert exc.value.status_code == 409
    batch.expires_at = svc.now() - timedelta(seconds=1)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await confirm(ready, batch)
    assert exc.value.status_code == 409
    run.status = "running"
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await make_batch(ready)
    assert exc.value.status_code == 409


async def test_authority_loss_stops_attempt_and_preserves_prior_checkpoint(ready):
    db, ds, actor, run = ready
    batch = await make_batch(ready)
    await confirm(ready, batch)
    job = await db.get(AsyncJob, batch.async_job_id)
    await worker.process_one(db, batch.id, job.id, job.celery_task_id, batch.generation)
    actor.role = UserRole.ANNOTATOR
    await db.commit()
    await execute_all(db, batch)
    assert batch.status == "failed" and batch.error_code == "access_revoked"
    assert batch.processed == 1 and await audits(db) == 1


async def test_failed_checkpoint_rolls_back_decision_audit_and_member_together(
    ready, monkeypatch
):
    db, ds, actor, run = ready
    batch = await make_batch(ready)
    await confirm(ready, batch)
    run_id = run.id
    original = quality.review_result

    async def fail_after_review(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("checkpoint interrupted")

    monkeypatch.setattr(quality, "review_result", fail_after_review)
    await execute_all(db, batch)
    assert batch.status == "failed" and batch.processed == 0
    assert await audits(db) == 0
    assert (
        list(
            await db.scalars(
                select(Result.review_revision).where(Result.run_id == run_id)
            )
        )
        == [0] * 5
    )
    assert (
        await db.scalar(
            select(func.count())
            .select_from(Member)
            .where(Member.batch_id == batch.id, Member.state == "pending")
        )
        == 5
    )


async def test_api_confirmation_is_idempotent_even_after_job_retention(
    ready, httpx_client, super_admin, monkeypatch
):
    db, ds, actor, run = ready
    base = f"/api/v1/datasets/{ds.id}/quality-runs/{run.id}/review-batches"
    auth = headers(super_admin)
    response = await httpx_client.post(
        base + "/preview",
        headers=auth,
        json={"scope": "filtered", "state": "dismissed", "note": "保留历史证据"},
    )
    assert response.status_code == 201, response.text
    value = response.json()
    assert "authority" not in value
    calls = []

    async def dispatch(db, batch):
        calls.append(batch.id)
        await db.commit()

    monkeypatch.setattr(worker, "dispatch", dispatch)
    body = {"scope_digest": value["scope_digest"], "range_policy": "snapshot"}
    path = base + f"/{value['id']}/execute"
    response = await httpx_client.post(path, headers=auth, json=body)
    assert response.status_code == 200, response.text
    job_id = uuid.UUID(response.json()["async_job_id"])
    assert (await httpx_client.post(path, headers=auth, json=body)).status_code == 200
    assert len(calls) == 1
    batch = await db.get(Batch, uuid.UUID(value["id"]))
    await execute_all(db, batch)
    await db.execute(delete(AsyncJob).where(AsyncJob.id == job_id))
    await db.commit()
    response = await httpx_client.post(path, headers=auth, json=body)
    assert response.status_code == 200 and response.json()["async_job_id"] is None
    assert len(calls) == 1


async def test_migration_round_trip(db_session):
    path = Path(__file__).parents[1] / "alembic/versions/0208_dataset_quality_review.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def round_trip(connection):
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
        assert (
            "dataset_quality_review_batches"
            not in inspect(connection).get_table_names()
        )
        migration.upgrade()
        assert "dataset_quality_review_members" in inspect(connection).get_table_names()
        fks = inspect(connection).get_foreign_keys("dataset_quality_review_batches")
        assert any(
            fk["referred_table"] == "async_jobs"
            and fk["options"]["ondelete"] == "SET NULL"
            for fk in fks
        )

    await (await db_session.connection()).run_sync(round_trip)


async def test_job_and_parent_access_and_preview_visibility(
    ready, httpx_client, super_admin, project_admin, annotator
):
    db, ds, actor, run = ready
    client, auth = httpx_client, headers(super_admin)
    batch = await make_batch(ready)
    base = f"/api/v1/datasets/{ds.id}/quality-runs/{run.id}/review-batches"
    assert (
        await client.get(base + f"/{batch.id}", headers=headers(annotator))
    ).status_code == 403
    assert (
        await client.get(base + f"/{batch.id}", headers=headers(project_admin))
    ).status_code == 404
    assert (
        await client.get(
            base.replace(str(ds.id), str(uuid.uuid4())) + f"/{batch.id}", headers=auth
        )
    ).status_code == 404
    # A second manager of the dataset still cannot consume another person's preview.
    from app.db.models.user import User

    other = User(
        email=f"second-{uuid.uuid4()}@example.test",
        password_hash="unused",
        role=UserRole.SUPER_ADMIN,
        is_active=True,
        name="Second",
    )
    db.add(other)
    await db.commit()
    assert (
        await db.scalar(
            select(Batch.id).where(Batch.id == batch.id, svc.visible_clause(other))
        )
        is None
    )
    await confirm(ready, batch)
    with pytest.raises(HTTPException) as exc:
        await svc.execute_preview(
            db,
            ds.id,
            run.id,
            batch.id,
            other,
            QualityReviewExecute(
                scope_digest=batch.scope_digest, range_policy="snapshot"
            ),
        )
    assert exc.value.status_code == 403
    job_url = f"/api/v1/async-jobs/{batch.async_job_id}"
    assert (await client.get(job_url, headers=auth)).status_code == 200
    assert (await client.get(job_url, headers=headers(annotator))).status_code == 403
    listed = await client.get(
        "/api/v1/async-jobs?kind=dataset_quality_review", headers=headers(project_admin)
    )
    assert listed.status_code == 200 and listed.json()["total"] == 0
    assert (await client.post(job_url + "/cancel", headers=auth)).status_code == 200
    await db.refresh(batch)
    assert batch.status == "cancelled"


async def test_api_key_scope_and_rotation_prevent_review(
    ready, httpx_client, super_admin
):
    from app.db.models.api_key import ApiKey

    db, ds, actor, run = ready
    client, auth = httpx_client, headers(super_admin)
    base = f"/api/v1/datasets/{ds.id}/quality-runs/{run.id}/review-batches"
    batch = await make_batch(ready)
    keys = []
    for scopes in [
        ["datasets:write"],
        ["datasets:read"],
        ["datasets:read", "datasets:write"],
    ]:
        created = await client.post(
            "/api/v1/me/api-keys",
            headers=auth,
            json={"name": "review", "scopes": scopes},
        )
        assert created.status_code == 201
        keys.append(created.json())

    def token(key):
        return {"Authorization": f"Bearer {key['plaintext']}"}

    assert (await client.get(base, headers=token(keys[0]))).status_code == 403
    assert (
        await client.post(
            base + "/preview",
            headers=token(keys[1]),
            json={"scope": "filtered", "state": "open", "note": "原因"},
        )
    ).status_code == 403
    key = await db.get(ApiKey, uuid.UUID(keys[2]["id"]))
    await svc.execute_preview(
        db,
        ds.id,
        run.id,
        batch.id,
        actor,
        QualityReviewExecute(scope_digest=batch.scope_digest, range_policy="snapshot"),
        key.id,
    )
    await db.commit()
    job_url = f"/api/v1/async-jobs/{batch.async_job_id}"
    assert (await client.get(job_url, headers=token(keys[0]))).status_code == 403
    listed = await client.get(
        "/api/v1/async-jobs?kind=dataset_quality_review", headers=token(keys[0])
    )
    assert listed.status_code == 200 and listed.json()["total"] == 0
    key.key_hash = "rotated-proof"
    await db.commit()
    await execute_all(db, batch)
    assert batch.status == "failed" and batch.error_code == "access_revoked"
    assert await audits(db) == 0
