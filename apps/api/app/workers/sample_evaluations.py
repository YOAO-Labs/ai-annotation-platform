"""Short fenced commits around exact routed inference and immutable report IO."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.sample_evaluation import (
    SampleEvaluationAttempt as Attempt,
    SampleEvaluationRun as Run,
    SampleEvaluationSample as Sample,
)
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services import (
    async_job as jobs,
    sample_evaluations as service,
    managed_inference as managed,
)
from app.services.training_dataset_bridge import TrainingDatasetError
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


class FencedOut(Exception):
    pass


RemoteUncertain = managed.RemoteUncertain


async def fenced(db, attempt_id, claim_id, *, check_authority=False):
    attempt = await db.get(Attempt, attempt_id)
    run = await db.get(Run, attempt.run_id) if attempt else None
    if not run:
        raise FencedOut
    if check_authority:
        await service.authority(db, run)
    run = await service.load_run(db, run.project_id, run.id, lock=True)
    attempt = await db.get(Attempt, attempt_id, populate_existing=True)
    if (
        run.current_attempt_id != attempt.id
        or run.generation != attempt.generation
        or attempt.claim_id != claim_id
        or run.deletion_requested_at is not None
        or run.status not in {"running", "cancelling"}
    ):
        raise FencedOut
    return run, attempt


async def claim(factory, attempt_id, celery_task_id):
    async with factory() as db:
        attempt = await db.get(Attempt, attempt_id)
        run = await db.get(Run, attempt.run_id) if attempt else None
        if not run:
            return None
        try:
            await service.authority(db, run)
            service.owners.resolve(run)
        except TrainingDatasetError as exc:
            run = await service.load_run(db, run.project_id, run.id, lock=True)
            if run.current_attempt_id == attempt_id and run.status == "queued":
                await service.request_cancel(db, run)
                run.error_code = attempt.error_code = exc.code
                run.error_message = attempt.error_message = str(exc)[:1000]
                await db.commit()
            return None
        run = await service.load_run(db, run.project_id, run.id, lock=True)
        attempt = await db.get(Attempt, attempt_id, populate_existing=True)
        if (
            run.current_attempt_id != attempt_id
            or run.status != "queued"
            or attempt.claim_id
            or run.deletion_requested_at is not None
        ):
            return None
        claim_id = uuid.uuid4()
        run.status = attempt.status = "running"
        attempt.claim_id = claim_id
        attempt.heartbeat_at = attempt.started_at = service.now()
        await jobs.mark_running(db, attempt.async_job_id, celery_task_id=celery_task_id)
        await db.commit()
        return claim_id


async def heartbeat(factory, attempt_id, claim_id, stop):
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
            break
        except asyncio.TimeoutError:
            pass
        try:
            async with factory() as db:
                _run, attempt = await fenced(db, attempt_id, claim_id)
                attempt.heartbeat_at = service.now()
                await db.commit()
        except FencedOut:
            return
        except Exception:
            log.warning("Evaluation heartbeat unavailable attempt=%s", attempt_id)


async def private_client(db, run):
    return await managed.private_client(db, run.spec["model_ref"])


def recovered_result(receipt, run):
    return managed.recovered_result(receipt, run.spec["model_ref"])


async def remote_receipt(client, row, *, seal=False):
    return await managed.remote_receipt(client, row, seal=seal)


async def obtain_result(factory, run, row, png, attempt_id, claim_id):
    async with factory() as db:
        client = await private_client(db, run)
        await db.commit()
    # Read cached evidence first, including when a previous GPU grant is uncertain.
    for _ in range(120):
        try:
            receipt = await remote_receipt(client, row)
            break
        except HTTPException as exc:
            if (
                exc.status_code != 409
                or not isinstance(exc.detail, dict)
                or exc.detail.get("reason") != "evaluation_request_busy"
            ):
                raise RemoteUncertain from exc
            await asyncio.sleep(1)
            async with factory() as db:
                current, _attempt = await fenced(db, attempt_id, claim_id)
                if current.status == "cancelling":
                    raise FencedOut
    else:
        raise RemoteUncertain
    if receipt.get("state") == "completed":
        return recovered_result(receipt, run)
    if receipt.get("state") not in {"missing", "bound"}:
        raise RemoteUncertain
    async with factory() as db:
        current, _attempt = await fenced(db, attempt_id, claim_id, check_authority=True)
        if current.status != "running":
            raise FencedOut
        await db.commit()
        try:
            return await managed.predict(
                db,
                factory,
                project_id=run.project_id,
                spec=run.spec,
                row=row,
                png=png,
                owner=f"sample-evaluation:{attempt_id}",
                operation="sample_evaluation",
            )
        except (TrainingDatasetError, FencedOut):
            raise
        except Exception as exc:
            # A transport error is not proof that native work has stopped.
            try:
                receipt = await remote_receipt(client, row)
                if receipt.get("state") == "completed":
                    return recovered_result(receipt, run)
            except Exception:
                pass
            raise RemoteUncertain from exc


async def checkpoint(factory, attempt_id, claim_id, ordinal, result):
    async with factory() as db:
        run, attempt = await fenced(db, attempt_id, claim_id, check_authority=True)
        row = await db.get(Sample, (run.id, ordinal))
        if row.evidence:
            return
        evidence = service.successful_evidence(run, attempt, row, result)
        row.evidence, row.status, row.produced_attempt_id = (
            evidence,
            "succeeded",
            attempt.id,
        )
        row.error_code = row.error_message = None
        rows = await service.sample_rows(db, run)
        await jobs.update_progress(
            db,
            attempt.async_job_id,
            int(95 * sum(s.status == "succeeded" for s in rows) / len(rows)),
        )
        await db.commit()


async def mark_lost(factory, attempt_id, claim_id):
    async with factory() as db:
        run, attempt = await fenced(db, attempt_id, claim_id)
        if run.status == "cancelling":
            return
        run.status = attempt.status = "lost"
        run.error_code = attempt.error_code = "remote_execution_uncertain"
        run.error_message = attempt.error_message = (
            "推理结果或退出状态尚未确认；恢复时会先读取同一请求的收据"
        )
        attempt.claim_id = None
        await jobs.mark_failed(db, attempt.async_job_id, error=run.error_message)
        await service.notify_job_terminal(db, job_id=attempt.async_job_id)
        await db.commit()


async def cleanup_candidate(factory, run_id, attempt_id, artifact, *, storage_ref):
    """Never infer rollback from a lost COMMIT acknowledgement."""
    try:
        owner = service.owners.resolve_ref(storage_ref)
        owner = service.owners.resolve_ref(
            storage_ref,
            expected_prefix=owner.key_prefix
            + service.owners.prefix_for(owner.logical_prefix.split("/")[1], run_id),
        )
        service.owners.expect_report(
            owner, attempt_id, artifact["key"], artifact["sha256"]
        )
        async with factory() as db:
            run = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            attempt = await db.get(Attempt, attempt_id)
            if run and service.owners.resolve(run).ref != owner.ref:
                return
            if attempt and attempt.run_id != run_id:
                return
            if (
                attempt
                and attempt.report
                and attempt.report.get("key") == artifact["key"]
            ):
                return
            if (
                run
                and not run.deletion_requested_at
                and run.current_attempt_id == attempt_id
                and run.status in {"running", "cancelling"}
            ):
                return
            # This attempt can never become the current active writer again.
            await db.commit()
        async with factory() as db:
            await service.storage_io.run(
                db,
                owner,
                "sample_evaluation_cleanup",
                lambda transport: transport.client.delete_object(
                    Bucket=owner.bucket, Key=artifact["key"]
                ),
            )
    except Exception:
        # Retain uncertain candidates under the evaluation's cleanup prefix.
        log.warning("Evaluation report cleanup deferred attempt=%s", attempt_id)


async def finish(factory, attempt_id, claim_id):
    async with factory() as db:
        run, attempt = await fenced(db, attempt_id, claim_id, check_authority=True)
        if run.status != "running":
            return
        storage_ref = service.owners.resolve(run).ref
        rows = await service.sample_rows(db, run)
        status = "failed" if any(s.status == "failed" for s in rows) else "succeeded"
        report = service.build_report(run, attempt, rows, status)
        await db.commit()
    async with factory() as db:
        artifact = await service.storage_io.run(
            db,
            service.owners.resolve(run),
            "sample_evaluation_write",
            lambda transport: service.upload_report(
                run, attempt, report, transport=transport
            ),
        )
    try:
        async with factory() as db:
            current, current_attempt = await fenced(
                db, attempt_id, claim_id, check_authority=True
            )
            if current.status != "running":
                return
            if service.owners.resolve(current).ref != storage_ref:
                raise FencedOut
            service.verify_report(
                report, current, current_attempt, await service.sample_rows(db, current)
            )
            await service.mark_terminal(
                db,
                current,
                current_attempt,
                status,
                code="sample_failed" if status == "failed" else None,
                message="部分样本失败，未生成成功汇总" if status == "failed" else None,
                report=artifact,
            )
            await db.commit()
    finally:
        await cleanup_candidate(
            factory, run.id, attempt_id, artifact, storage_ref=storage_ref
        )


async def execute(factory, attempt_id, celery_task_id=None):
    claim_id = await claim(factory, attempt_id, celery_task_id)
    if not claim_id:
        return
    stop = asyncio.Event()
    pulse = asyncio.create_task(heartbeat(factory, attempt_id, claim_id, stop))
    try:
        async with factory() as db:
            run, _attempt = await fenced(db, attempt_id, claim_id, check_authority=True)
            spec = service.validate_spec(run)
            await db.commit()
        for frozen in spec["samples"]:
            async with factory() as db:
                run, attempt = await fenced(
                    db, attempt_id, claim_id, check_authority=True
                )
                if run.status == "cancelling":
                    return
                row = await db.get(Sample, (run.id, frozen["ordinal"]))
                if row.evidence:
                    continue
                version = await db.get(TrainingDatasetVersion, run.dataset_id)
                if (
                    not version
                    or version.snapshot_digest != spec["snapshot_digest"]
                    or service.checked_dataset(
                        version, "segment" if spec["schema"] == 2 else "detect"
                    )["content_digest"]
                    != spec["manifest_digest"]
                ):
                    raise ValueError("Frozen dataset identity differs")
                await db.commit()
            try:
                from app.services import training_dataset_storage

                async with factory() as db:
                    png, preprocessing = await service.storage_io.run(
                        db,
                        training_dataset_storage.resolve(version),
                        "sample_evaluation_source",
                        lambda transport: service.prepare_input(
                            run, version, frozen, transport=transport
                        ),
                    )
            except Exception:
                async with factory() as db:
                    current, _attempt = await fenced(
                        db, attempt_id, claim_id, check_authority=True
                    )
                    row = await db.get(Sample, (current.id, frozen["ordinal"]))
                    if row.request:
                        raise RemoteUncertain
                    row.status, row.error_code, row.error_message = (
                        "failed",
                        "frozen_input_unavailable",
                        "冻结图片未通过完整性、尺寸或解码检查；未运行推理",
                    )
                    await db.commit()
                continue
            async with factory() as db:
                run, attempt = await fenced(
                    db, attempt_id, claim_id, check_authority=True
                )
                if run.status != "running":
                    return
                row = await db.get(Sample, (run.id, frozen["ordinal"]))
                request = service.request_identity(spec, row, preprocessing)
                if row.request and row.request != request:
                    raise RemoteUncertain
                row.request, row.status = request, "inflight"
                await db.commit()
            result = await obtain_result(factory, run, row, png, attempt_id, claim_id)
            await checkpoint(factory, attempt_id, claim_id, frozen["ordinal"], result)
        await finish(factory, attempt_id, claim_id)
    except FencedOut:
        return
    except TrainingDatasetError as exc:
        async with factory() as db:
            try:
                run, attempt = await fenced(db, attempt_id, claim_id)
                await service.request_cancel(db, run)
                run.error_code = attempt.error_code = exc.code
                run.error_message = attempt.error_message = str(exc)[:1000]
                await db.commit()
            except FencedOut:
                pass
    except Exception:
        try:
            await mark_lost(factory, attempt_id, claim_id)
        except FencedOut:
            pass
    finally:
        stop.set()
        await pulse


async def settle_cancel(factory, run_id):
    async with factory() as db:
        run = await db.get(Run, run_id)
        if not run or run.status != "cancelling":
            return False
        attempt = await db.get(Attempt, run.current_attempt_id)
        rows = await service.sample_rows(db, run)
        client = (
            await private_client(db, run) if any(row.request for row in rows) else None
        )
        await db.commit()
    recovered = {}
    for row in rows:
        if not row.request:
            continue
        try:
            receipt = await remote_receipt(client, row, seal=True)
            if receipt.get("state") not in {"sealed", "completed"}:
                return False
            if receipt["state"] == "completed" and not row.evidence:
                result = recovered_result(receipt, run)
                recovered[row.ordinal] = service.successful_evidence(
                    run, attempt, row, result
                )
        except Exception:
            return False
    async with factory() as db:
        authorized = True
        try:
            await service.authority(db, run)
        except TrainingDatasetError:
            authorized = False
        current = await service.load_run(db, run.project_id, run.id, lock=True)
        if current.status != "cancelling" or current.current_attempt_id != attempt.id:
            return False
        for ordinal, evidence in recovered.items() if authorized else []:
            row = await db.get(Sample, (run.id, ordinal))
            if not row.evidence:
                row.evidence, row.status, row.produced_attempt_id = (
                    evidence,
                    "succeeded",
                    attempt.id,
                )
        attempt = await db.get(Attempt, attempt.id, populate_existing=True)
        status = (
            "failed"
            if current.error_code in {"authority_revoked", "api_key_revoked"}
            else "cancelled"
        )
        rows = await service.sample_rows(db, current)
        report = (
            service.build_report(current, attempt, rows, status) if authorized else None
        )
        storage_ref = None
        if report:
            try:
                storage_ref = service.owners.resolve(current).ref
            except TrainingDatasetError:
                # Confirming remote stop must not require a retained output owner.
                report = None
        await db.commit()
    artifact = None
    if report:
        async with factory() as db:
            artifact = await service.storage_io.run(
                db,
                service.owners.resolve(current),
                "sample_evaluation_write",
                lambda transport: service.upload_report(
                    current, attempt, report, transport=transport
                ),
            )
    candidate = artifact
    try:
        async with factory() as db:
            # Stop confirmation survives revoked authority; report publication does not.
            try:
                if artifact:
                    await service.authority(db, current)
            except TrainingDatasetError:
                await db.rollback()
                artifact = None
            current = await service.load_run(db, run.project_id, run.id, lock=True)
            if (
                current.status != "cancelling"
                or current.current_attempt_id != attempt.id
            ):
                return False
            attempt = await db.get(Attempt, attempt.id, populate_existing=True)
            if artifact:
                try:
                    if service.owners.resolve(current).ref != storage_ref:
                        artifact = None
                except TrainingDatasetError:
                    artifact = None
            if artifact:
                service.verify_report(
                    report, current, attempt, await service.sample_rows(db, current)
                )
            await service.mark_terminal(
                db,
                current,
                attempt,
                status,
                code=current.error_code,
                message=current.error_message,
                report=artifact,
            )
            attempt.claim_id = None
            await db.commit()
            return True
    finally:
        if candidate:
            await cleanup_candidate(
                factory, run.id, attempt.id, candidate, storage_ref=storage_ref
            )


async def dispatch(db, run):
    attempt = await db.get(Attempt, run.current_attempt_id)
    if run.status != "queued" or not attempt or attempt.claim_id:
        return
    task_id = str(uuid.uuid4())
    attempt.dispatched_at = service.now()
    job = await db.get(AsyncJob, attempt.async_job_id)
    if job:
        job.celery_task_id = task_id
    await db.commit()
    try:
        run_sample_evaluation.apply_async(args=[str(attempt.id)], task_id=task_id)
    except Exception:
        log.warning("Evaluation enqueue unavailable attempt=%s", attempt.id)


async def maintain(factory):
    from app.services import training_storage_cleanup

    await training_storage_cleanup.maintain(factory)
    cutoff = service.now()
    async with factory() as db:
        ids = list(
            (
                await db.scalars(
                    select(Run.id)
                    .join(Attempt, Attempt.id == Run.current_attempt_id)
                    .where(
                        or_(
                            Run.deletion_requested_at.is_not(None),
                            Run.status == "cancelling",
                            and_(
                                Run.status == "queued",
                                or_(
                                    Attempt.dispatched_at.is_(None),
                                    Attempt.dispatched_at
                                    < cutoff - timedelta(seconds=30),
                                ),
                            ),
                            and_(
                                Run.status == "running",
                                or_(
                                    Attempt.heartbeat_at.is_(None),
                                    Attempt.heartbeat_at
                                    < cutoff - timedelta(seconds=45),
                                ),
                            ),
                        )
                    )
                    .order_by(Attempt.maintenance_at.asc().nulls_first(), Run.id)
                    .limit(100)
                )
            ).all()
        )
    for run_id in ids:
        async with factory() as db:
            run = await db.scalar(
                select(Run).where(Run.id == run_id).with_for_update(skip_locked=True)
            )
            if not run:
                continue
            attempt = await db.get(Attempt, run.current_attempt_id)
            attempt.maintenance_at = cutoff
            if run.deletion_requested_at:
                await db.commit()
                try:
                    await service.purge_requested(db, run.id)
                except Exception:
                    log.warning("Evaluation deletion deferred evaluation=%s", run.id)
                continue
            if run.status == "running" and (
                not attempt.heartbeat_at
                or attempt.heartbeat_at < service.now() - timedelta(seconds=45)
            ):
                run.status = attempt.status = "lost"
                run.error_code = attempt.error_code = "execution_lost"
                run.error_message = attempt.error_message = (
                    "执行心跳超时；旧请求可能仍在运行，恢复将核对收据"
                )
                attempt.claim_id = None
                await jobs.mark_failed(
                    db, attempt.async_job_id, error=run.error_message
                )
                await service.notify_job_terminal(db, job_id=attempt.async_job_id)
            elif run.status == "queued" and (
                not attempt.dispatched_at
                or attempt.dispatched_at < service.now() - timedelta(seconds=30)
            ):
                await dispatch(db, run)
            cancelling = run.status == "cancelling"
            await db.commit()
        if cancelling:
            await settle_cancel(factory, run_id)


async def with_factory(callback, *args):
    engine = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await callback(factory, *args)
    finally:
        await engine.dispose()


@celery_app.task(bind=True, name="app.workers.sample_evaluations.run_sample_evaluation")
def run_sample_evaluation(self, attempt_id):
    return asyncio.run(with_factory(execute, uuid.UUID(attempt_id), self.request.id))


@celery_app.task(name="app.workers.sample_evaluations.maintain_evaluations")
def maintain_evaluations():
    return asyncio.run(with_factory(maintain))
