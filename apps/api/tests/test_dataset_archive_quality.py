"""Archive fences quality writes without hiding historical report evidence."""

from datetime import timedelta

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select

from app.db.models.async_job import AsyncJob
from app.db.models.dataset_quality import (
    DatasetQualityRun as Run,
    DatasetQualityResult as Result,
)
from app.db.models.dataset_quality_review import DatasetQualityReviewBatch as Batch
from app.schemas.dataset_quality import DatasetQualityConfig, DatasetQualityReview
from app.schemas.dataset_quality_review import QualityReviewExecute
from app.services import (
    dataset_content as content,
    dataset_quality as quality,
    dataset_quality_review as review,
)
from app.workers import (
    dataset_quality as quality_worker,
    dataset_quality_review as review_worker,
)
from tests.test_dataset_archive_sources import transition
from tests.test_dataset_content import headers
from tests.test_dataset_quality import world as world
from tests.test_dataset_quality_review import (
    ready as ready,
    make_batch,
    confirm,
    audits,
    execute_all,
)


def forbid_work(monkeypatch):
    async def unexpected(*_args, **_kwargs):
        pytest.fail("An archive fence must reject before quality I/O or dispatch")

    monkeypatch.setattr(content, "quality_identity", unexpected)
    monkeypatch.setattr(content, "quality_snapshot", unexpected)
    monkeypatch.setattr(quality_worker, "dispatch", unexpected)
    monkeypatch.setattr(review_worker, "dispatch", unexpected)


@pytest.mark.parametrize(
    "operation", ["start", "resume", "review", "preview", "execute", "batch_resume"]
)
async def test_archived_quality_write_routes_reject_before_io_or_dispatch(
    ready, httpx_client, super_admin, monkeypatch, operation
):
    db, dataset, actor, run = ready
    batch = await make_batch(ready)
    row = await db.scalar(select(Result).where(Result.run_id == run.id))
    await transition(db, dataset, archived=True)
    forbid_work(monkeypatch)
    base = f"/api/v1/datasets/{dataset.id}/quality-runs"
    path, body = {
        "start": (base, {}),
        "resume": (f"{base}/{run.id}/resume", None),
        "review": (
            f"{base}/{run.id}/results/{row.id}/review",
            {"state": "confirmed", "note": "人工确认", "expected_revision": 0},
        ),
        "preview": (
            f"{base}/{run.id}/review-batches/preview",
            {"scope": "filtered", "state": "confirmed", "note": "人工确认"},
        ),
        "execute": (
            f"{base}/{run.id}/review-batches/{batch.id}/execute",
            {"scope_digest": batch.scope_digest, "range_policy": "snapshot"},
        ),
        "batch_resume": (f"{base}/{run.id}/review-batches/{batch.id}/resume", None),
    }[operation]
    response = await httpx_client.post(path, headers=headers(super_admin), json=body)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "dataset_archived"
    await db.refresh(row)
    await db.refresh(batch)
    assert row.review_revision == 0 and row.review_state == "open"
    assert batch.status == "preview" and batch.async_job_id is None
    assert await db.scalar(select(func.count()).select_from(Run)) == 1
    assert await db.scalar(select(func.count()).select_from(Batch)) == 1
    assert await audits(db) == 0


async def test_archived_quality_history_results_and_batch_members_remain_readable(
    ready, httpx_client, super_admin
):
    db, dataset, actor, run = ready
    batch = await make_batch(ready)
    await transition(db, dataset, archived=True)
    base = f"/api/v1/datasets/{dataset.id}/quality-runs"
    for path in [
        base,
        f"{base}/{run.id}",
        f"{base}/{run.id}/results",
        f"{base}/{run.id}/review-batches",
        f"{base}/{run.id}/review-batches/{batch.id}",
        f"{base}/{run.id}/review-batches/{batch.id}/members",
    ]:
        response = await httpx_client.get(path, headers=headers(super_admin))
        assert response.status_code == 200, response.text
        assert "dataset_lifecycle_revision" not in response.text


@pytest.mark.parametrize("operation", ["execute", "recover", "resume"])
@pytest.mark.parametrize("restored", [False, True])
async def test_queued_quality_work_cannot_cross_an_archive_transition(
    world, monkeypatch, operation, restored
):
    db, dataset, actor, *_ = world
    run = await quality.start(db, dataset.id, actor, DatasetQualityConfig())
    job = await db.get(AsyncJob, run.async_job_id)
    assert run.authority["dataset_lifecycle_revision"] == 1
    assert job.payload["dataset_lifecycle_revision"] == 1
    run.lease_until = quality.now() - timedelta(seconds=1)
    await transition(db, dataset, archived=True)
    if restored:
        await transition(db, dataset, archived=False)
    forbid_work(monkeypatch)
    code = "dataset_lifecycle_changed" if restored else "dataset_archived"
    if operation == "resume":
        with pytest.raises(HTTPException) as exc:
            await quality.resume(db, dataset.id, run.id, actor)
        assert exc.value.detail["code"] == code
        assert run.async_job_id == job.id
    else:
        if operation == "execute":
            await quality_worker.execute(db, run.id, job.celery_task_id, "worker:test")
        else:
            await quality_worker.recover(db)
        await db.refresh(run)
        await db.refresh(job)
        assert run.status == job.status == "failed"
        assert run.error_code == code and job.error_message == code
    assert run.processed == 0
    assert not await db.scalar(
        select(Result.id).where(Result.run_id == run.id, Result.state != "pending")
    )


@pytest.mark.parametrize("operation", ["execute", "recover", "resume"])
@pytest.mark.parametrize("restored", [False, True])
async def test_queued_review_work_cannot_cross_an_archive_transition(
    ready, monkeypatch, operation, restored
):
    db, dataset, actor, run = ready
    batch = await make_batch(ready)
    assert batch.authority == {"dataset_lifecycle_revision": 1}
    await confirm(ready, batch)
    job = await db.get(AsyncJob, batch.async_job_id)
    assert batch.authority["dataset_lifecycle_revision"] == 1
    assert job.payload["dataset_lifecycle_revision"] == 1
    if operation == "resume":
        await review.cancel_job(db, job, actor)
    else:
        batch.lease_until = quality.now() - timedelta(seconds=1)
    await transition(db, dataset, archived=True)
    if restored:
        await transition(db, dataset, archived=False)
    forbid_work(monkeypatch)
    code = "dataset_lifecycle_changed" if restored else "dataset_archived"
    if operation == "resume":
        with pytest.raises(HTTPException) as exc:
            await review.resume(db, dataset.id, run.id, batch.id, actor)
        assert exc.value.detail["code"] == code
        assert batch.async_job_id == job.id
    else:
        if operation == "execute":
            await review_worker.execute(db, batch.id, job.celery_task_id)
        else:
            await review_worker.recover(db)
        await db.refresh(batch)
        await db.refresh(job)
        assert batch.status == job.status == "failed"
        assert batch.error_code == code and job.error_message == code
    assert batch.processed == 0 and await audits(db) == 0
    assert not await db.scalar(
        select(Result.id).where(Result.run_id == run.id, Result.review_revision != 0)
    )


@pytest.mark.parametrize("owner", ["quality", "review"])
async def test_legacy_quality_authority_defaults_to_initial_lifecycle_revision(
    ready, monkeypatch, owner
):
    db, dataset, actor, run = ready
    if owner == "quality":
        target = await quality.start(db, dataset.id, actor, DatasetQualityConfig())
        worker = quality_worker
    else:
        target = await make_batch(ready)
        await confirm(ready, target)
        worker = review_worker
    target.authority = {
        key: value
        for key, value in target.authority.items()
        if key != "dataset_lifecycle_revision"
    }
    job = await db.get(AsyncJob, target.async_job_id)
    await transition(db, dataset, archived=True)
    await transition(db, dataset, archived=False)
    forbid_work(monkeypatch)
    args = (db, target.id, job.celery_task_id)
    await worker.execute(*args, *(["worker:test"] if owner == "quality" else []))
    await db.refresh(target)
    assert (
        target.status == "failed" and target.error_code == "dataset_lifecycle_changed"
    )
    assert target.processed == 0 and await audits(db) == 0


@pytest.mark.parametrize("restored", [False, True])
async def test_quality_final_evidence_write_rechecks_lifecycle_after_source_io(
    world, monkeypatch, restored
):
    db, dataset, actor, *_ = world
    run = await quality.start(db, dataset.id, actor, DatasetQualityConfig())
    await db.commit()
    job = await db.get(AsyncJob, run.async_job_id)
    capture = content.quality_snapshot
    checkpoint = {}

    async def transition_after_snapshot(*args, **kwargs):
        snapshot = await capture(*args, **kwargs)
        checkpoint["processed"] = run.processed
        checkpoint["item_id"] = args[1]
        await transition(db, dataset, archived=True)
        if restored:
            await transition(db, dataset, archived=False)
        return snapshot

    monkeypatch.setattr(content, "quality_snapshot", transition_after_snapshot)
    await quality_worker.execute(db, run.id, job.celery_task_id, "worker:test")
    await db.refresh(run)
    assert run.status == "failed" and run.error_code == (
        "dataset_lifecycle_changed" if restored else "dataset_archived"
    )
    assert run.processed == checkpoint["processed"]
    row = await db.scalar(
        select(Result).where(
            Result.run_id == run.id, Result.item_id == checkpoint["item_id"]
        )
    )
    assert row.state == "pending" and not row.bytes_verified and row.observed_at is None


@pytest.mark.parametrize("checkpoint", ["report", "member", "batch"])
@pytest.mark.parametrize("restored", [False, True])
async def test_quality_business_checkpoints_fence_old_authority_independently(
    ready, checkpoint, restored
):
    db, dataset, actor, run = ready
    batch = await make_batch(ready)
    await confirm(ready, batch)
    pending = await quality.start(db, dataset.id, actor, DatasetQualityConfig())
    await transition(db, dataset, archived=True)
    if restored:
        await transition(db, dataset, archived=False)
    with pytest.raises(HTTPException) as exc:
        if checkpoint == "report":
            await quality.finish(db, pending, "completed")
        elif checkpoint == "member":
            await review.process_member(db, batch, actor, run)
        else:
            await review.finish(db, batch, "completed")
    assert exc.value.detail["code"] == (
        "dataset_lifecycle_changed" if restored else "dataset_archived"
    )
    assert pending.status == batch.status == "pending"
    assert pending.processed == batch.processed == 0
    assert await audits(db) == 0


@pytest.mark.parametrize("owner", ["quality", "review"])
async def test_archived_quality_cancellation_and_terminal_cleanup_remain_available(
    ready, owner
):
    db, dataset, actor, run = ready
    if owner == "quality":
        target = await quality.start(db, dataset.id, actor, DatasetQualityConfig())
        service = quality
    else:
        target = await make_batch(ready)
        await confirm(ready, target)
        service = review
    job = await db.get(AsyncJob, target.async_job_id)
    task_id = job.celery_task_id
    await transition(db, dataset, archived=True)
    await service.cancel_job(db, job, actor)
    await db.commit()
    await service.signal_interrupted(db, job.id, task_id, cancelled=False)
    await db.commit()
    assert target.status == job.status == "cancelled"


async def test_old_preview_is_rejected_but_new_human_review_uses_restored_revision(
    ready,
):
    db, dataset, actor, run = ready
    old = await make_batch(ready)
    await transition(db, dataset, archived=True)
    await transition(db, dataset, archived=False)
    with pytest.raises(HTTPException) as exc:
        await review.execute_preview(
            db,
            dataset.id,
            run.id,
            old.id,
            actor,
            QualityReviewExecute(
                scope_digest=old.scope_digest, range_policy="snapshot"
            ),
        )
    assert exc.value.detail["code"] == "dataset_lifecycle_changed"
    assert old.status == "preview" and old.async_job_id is None
    row = await db.scalar(select(Result).where(Result.run_id == run.id))
    await quality.review_result(
        db,
        run,
        row.id,
        actor,
        DatasetQualityReview(
            state="dismissed", note="恢复后人工确认", expected_revision=0
        ),
    )
    await db.commit()
    new = await make_batch(ready)
    assert new.authority["dataset_lifecycle_revision"] == 3
    await confirm(ready, new)
    await execute_all(db, new)
    assert new.status == "completed" and new.processed == new.total
    assert await audits(db) == new.total + 1
