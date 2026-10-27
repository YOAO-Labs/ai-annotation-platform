"""Fenced bounded inference; only confirmed remote stop permits termination."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.db.models.active_learning import (
    ActiveLearningAttempt as Attempt,
    ActiveLearningCandidate as Candidate,
    ActiveLearningRound as Round,
)
from app.db.models.async_job import AsyncJob
from app.services import (
    active_learning as service,
    async_job as jobs,
    managed_inference as managed,
)
from app.services.training_dataset_bridge import TrainingDatasetError
from app.services.training_datasets import bind_training_authority
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)
RemoteUncertain = managed.RemoteUncertain


class FencedOut(Exception):
    pass


class RoundFailure(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


async def fenced(db, attempt_id, claim_id, *, check_authority=False):
    attempt = await db.get(Attempt, attempt_id)
    round = await db.get(Round, attempt.round_id) if attempt else None
    if not round:
        raise FencedOut
    if check_authority:
        await bind_training_authority(db).assert_authority(
            project_id=round.project_id,
            actor_id=round.creator_id,
            api_key_id=round.authorization.get("api_key_id"),
            required_scopes=service.pool.SCOPES,
        )
    round = await service.load_round(db, round.project_id, round.id, lock=True)
    attempt = await db.get(Attempt, attempt_id, populate_existing=True)
    if (
        round.current_attempt_id != attempt.id
        or round.generation != attempt.generation
        or attempt.claim_id != claim_id
        or round.status not in {"running", "cancelling"}
    ):
        raise FencedOut
    return round, attempt


async def claim(factory, attempt_id, celery_task_id):
    async with factory() as db:
        attempt = await db.get(Attempt, attempt_id)
        round = await db.get(Round, attempt.round_id) if attempt else None
        if not round:
            return None
        try:
            await bind_training_authority(db).assert_authority(
                project_id=round.project_id,
                actor_id=round.creator_id,
                api_key_id=round.authorization.get("api_key_id"),
                required_scopes=service.pool.SCOPES,
            )
            service.owners.resolve(round)
        except TrainingDatasetError as exc:
            round = await service.load_round(db, round.project_id, round.id, lock=True)
            if round.current_attempt_id == attempt_id and round.status == "queued":
                await service.request_cancel(
                    db, round, code=exc.code, message=str(exc)[:1000]
                )
                await db.commit()
            return None
        round = await service.load_round(db, round.project_id, round.id, lock=True)
        attempt = await db.get(Attempt, attempt_id, populate_existing=True)
        if (
            round.current_attempt_id != attempt_id
            or round.status != "queued"
            or attempt.claim_id
        ):
            return None
        claim_id = service.claim_attempt(round, attempt)
        await jobs.mark_running(db, attempt.async_job_id, celery_task_id=celery_task_id)
        await db.commit()
        return claim_id


async def heartbeat(factory, attempt_id, claim_id, stop):
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
        try:
            async with factory() as db:
                _round, attempt = await fenced(db, attempt_id, claim_id)
                service.record_heartbeat(attempt)
                await db.commit()
        except FencedOut:
            return
        except Exception:
            log.warning("Active learning heartbeat unavailable attempt=%s", attempt_id)


async def debit(factory, attempt_id, claim_id):
    async with factory() as db:
        round, _attempt = await fenced(db, attempt_id, claim_id, check_authority=True)
        if round.status != "running":
            raise FencedOut
        if time.time() >= round.spec["expires_at"]:
            raise RoundFailure(
                "request_expired", "原始推理窗口已结束；须新建选样轮次，不能延长旧请求"
            )
        if round.inference_dispatches >= round.spec["max_inference_dispatches"]:
            raise RoundFailure(
                "inference_budget_exhausted",
                "推理派发预算已用尽；未退还超时或失败的派发",
            )
        service.record_inference_dispatch(round)
        await db.commit()


async def obtain_result(factory, round, row, png, attempt_id, claim_id):
    async with factory() as db:
        client = await managed.backend_control_handle(
            db, round.spec["model_ref"], session_factory=factory
        )
        await db.commit()
    for _ in range(120):
        try:
            receipt = managed.checked_receipt(
                row, await managed.remote_receipt(client, row)
            )
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
                current, _attempt = await fenced(
                    db, attempt_id, claim_id, check_authority=True
                )
                if current.status != "running":
                    raise FencedOut
        except ValueError as exc:
            raise RoundFailure(
                "receipt_invalid", "远端收据与本轮冻结请求身份不一致"
            ) from exc
    else:
        raise RemoteUncertain
    if receipt["state"] == "completed":
        return managed.recovered_result(receipt, round.spec["model_ref"])
    if receipt["state"] == "sealed":
        raise RoundFailure(
            "request_sealed", "原始请求已封存，不能重新推理；请新建选样轮次"
        )
    # The committed predebit also charges conservative admission/transport failures.
    await debit(factory, attempt_id, claim_id)
    async with factory() as db:
        current, _attempt = await fenced(db, attempt_id, claim_id, check_authority=True)
        if current.status != "running":
            raise FencedOut
        await db.commit()
        try:
            return await managed.predict(
                db,
                factory,
                project_id=round.project_id,
                spec=round.spec,
                row=row,
                png=png,
                owner=f"active-learning:{attempt_id}",
                operation="active_learning",
            )
        except (TrainingDatasetError, FencedOut):
            raise
        except Exception as exc:
            # Transport failure is never a refund or confirmation of native exit.
            try:
                receipt = managed.checked_receipt(
                    row, await managed.remote_receipt(client, row)
                )
                if receipt["state"] == "completed":
                    return managed.recovered_result(receipt, round.spec["model_ref"])
            except Exception:
                pass
            raise RemoteUncertain from exc


async def checkpoint(factory, attempt_id, claim_id, ordinal, result):
    async with factory() as db:
        round, attempt = await fenced(db, attempt_id, claim_id, check_authority=True)
        if round.status != "running":
            raise FencedOut
        row = await db.get(Candidate, (round.id, ordinal))
        if row.evidence:
            service.verified_evidence(round, row)
            return
        evidence = service.successful_evidence(round, attempt, row, result)
        service.record_candidate_success(row, attempt, evidence)
        rows = await service.candidate_rows(db, round)
        await jobs.update_progress(
            db,
            attempt.async_job_id,
            int(
                95
                * sum(candidate.status == "succeeded" for candidate in rows)
                / len(rows)
            ),
        )
        await db.commit()


async def mark_lost(factory, attempt_id, claim_id):
    async with factory() as db:
        round, attempt = await fenced(db, attempt_id, claim_id)
        if round.status != "running":
            return
        service.mark_lost(
            round,
            attempt,
            code="remote_execution_uncertain",
            message="远端执行或结果尚未确认；恢复将先读取同一请求的原始收据",
        )
        service.release_claim(attempt)
        await jobs.mark_failed(db, attempt.async_job_id, error=round.error_message)
        await service.notify_job_terminal(db, job_id=attempt.async_job_id)
        await db.commit()


async def fail_and_stop(factory, attempt_id, claim_id, code, message):
    async with factory() as db:
        round, _attempt = await fenced(db, attempt_id, claim_id)
        await service.request_cancel(db, round, code=code, message=message)
        round_id = round.id
        await db.commit()
    await settle_cancel(factory, round_id)


async def finish(factory, attempt_id, claim_id):
    async with factory() as db:
        round, attempt = await fenced(db, attempt_id, claim_id, check_authority=True)
        if round.status != "running":
            return
        report = await service.build_report(db, round, attempt)
        await service.mark_terminal(db, round, attempt, "succeeded", report=report)
        await db.commit()


async def execute(factory, attempt_id, celery_task_id=None):
    claim_id = await claim(factory, attempt_id, celery_task_id)
    if not claim_id:
        return
    stop = asyncio.Event()
    pulse = asyncio.create_task(heartbeat(factory, attempt_id, claim_id, stop))
    try:
        async with factory() as db:
            round, _attempt = await fenced(
                db, attempt_id, claim_id, check_authority=True
            )
            spec = service.validate_spec(round)
            await db.commit()
        for sample in spec["samples"]:
            async with factory() as db:
                round, _attempt = await fenced(
                    db, attempt_id, claim_id, check_authority=True
                )
                if round.status != "running":
                    return
                row = await db.get(Candidate, (round.id, sample["ordinal"]))
                if row.evidence:
                    service.verified_evidence(round, row)
                    continue
                await db.commit()
                expected_spec = round.spec_digest

                async def input_guard():
                    current, _ = await fenced(db, attempt_id, claim_id)
                    if (
                        current.status != "running"
                        or current.spec_digest != expected_spec
                    ):
                        raise FencedOut

                try:
                    png = await service.prepare_image(
                        db, round, sample, guard=input_guard
                    )
                except (TrainingDatasetError, FencedOut):
                    raise
                except HTTPException as exc:
                    if exc.status_code == 409:
                        raise
                    raise RoundFailure(
                        "frozen_input_unavailable",
                        "冻结图片未通过来源、字节或解码核验；未生成评分",
                    ) from exc
                except Exception as exc:
                    raise RoundFailure(
                        "frozen_input_unavailable",
                        "冻结图片未通过来源、字节或解码核验；未生成评分",
                    ) from exc
            async with factory() as db:
                round, _attempt = await fenced(
                    db, attempt_id, claim_id, check_authority=True
                )
                if round.status != "running":
                    return
                row = await db.get(Candidate, (round.id, sample["ordinal"]))
                request = managed.request_identity(spec, row, sample["preprocessing"])
                if row.request and row.request != request:
                    raise RoundFailure(
                        "request_invalid", "持久化请求与本轮冻结输入身份不一致"
                    )
                service.record_candidate_inflight(
                    row, request, service.media_descriptor(round, sample)
                )
                await db.commit()
            result = await obtain_result(factory, round, row, png, attempt_id, claim_id)
            await checkpoint(factory, attempt_id, claim_id, sample["ordinal"], result)
        await finish(factory, attempt_id, claim_id)
    except FencedOut:
        return
    except TrainingDatasetError as exc:
        try:
            await fail_and_stop(
                factory, attempt_id, claim_id, exc.code, str(exc)[:1000]
            )
        except FencedOut:
            pass
    except RoundFailure as exc:
        try:
            await fail_and_stop(factory, attempt_id, claim_id, exc.code, exc.message)
        except FencedOut:
            pass
    except ValueError:
        try:
            await fail_and_stop(
                factory,
                attempt_id,
                claim_id,
                "inference_evidence_invalid",
                "推理输出未通过模型、类别、坐标或评分身份检查；未发布排序",
            )
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


async def settle_cancel(factory, round_id):
    async with factory() as db:
        round = await db.get(Round, round_id)
        if not round or round.status != "cancelling":
            return False
        attempt_id = round.current_attempt_id
        rows = await service.candidate_rows(db, round)
        client = (
            await managed.backend_control_handle(
                db, round.spec["model_ref"], session_factory=factory
            )
            if any(row.request for row in rows)
            else None
        )
        await db.commit()
    for row in rows:
        if row.request:
            try:
                receipt = managed.checked_receipt(
                    row, await managed.remote_receipt(client, row, seal=True)
                )
                if receipt["state"] not in {"sealed", "completed"}:
                    return False
            except Exception:
                return False
    async with factory() as db:
        current = await service.load_round(db, round.project_id, round_id, lock=True)
        if current.status != "cancelling" or current.current_attempt_id != attempt_id:
            return False
        attempt = await db.get(Attempt, attempt_id)
        await service.mark_terminal(
            db,
            current,
            attempt,
            "failed" if current.error_code else "cancelled",
            code=current.error_code,
            message=current.error_message,
        )
        await db.commit()
        return True


async def dispatch(db, round):
    attempt = await db.get(Attempt, round.current_attempt_id)
    if round.status != "queued" or not attempt or attempt.claim_id:
        return
    task_id = str(uuid.uuid4())
    service.record_dispatched(attempt)
    job = await db.get(AsyncJob, attempt.async_job_id)
    if job:
        jobs.bind_execution_id(job, task_id)
    await db.commit()
    try:
        run_round.apply_async(args=[str(attempt.id)], task_id=task_id)
    except Exception:
        log.warning("Active learning enqueue unavailable attempt=%s", attempt.id)


async def maintain(factory):
    cutoff = service.now()
    async with factory() as db:
        ids = list(
            (
                await db.scalars(
                    select(Round.id)
                    .join(Attempt, Attempt.id == Round.current_attempt_id)
                    .where(
                        or_(
                            Round.status == "cancelling",
                            and_(
                                Round.status == "queued",
                                or_(
                                    Attempt.dispatched_at.is_(None),
                                    Attempt.dispatched_at
                                    < cutoff - timedelta(seconds=30),
                                ),
                            ),
                            and_(
                                Round.status == "running",
                                or_(
                                    Attempt.heartbeat_at.is_(None),
                                    Attempt.heartbeat_at
                                    < cutoff - timedelta(seconds=45),
                                ),
                            ),
                        )
                    )
                    .order_by(Attempt.maintenance_at.asc().nulls_first(), Round.id)
                    .limit(100)
                )
            ).all()
        )
    for identity in ids:
        async with factory() as db:
            round = await db.scalar(
                select(Round)
                .where(Round.id == identity)
                .with_for_update(skip_locked=True)
            )
            if not round:
                continue
            attempt = await db.get(Attempt, round.current_attempt_id)
            service.record_maintenance(attempt, cutoff)
            if round.status == "running" and (
                not attempt.heartbeat_at
                or attempt.heartbeat_at < cutoff - timedelta(seconds=45)
            ):
                service.mark_lost(
                    round,
                    attempt,
                    code="execution_lost",
                    message="执行心跳超时；原请求可能仍在运行，恢复须核对收据",
                )
                service.release_claim(attempt)
                await jobs.mark_failed(
                    db, attempt.async_job_id, error=round.error_message
                )
                await service.notify_job_terminal(db, job_id=attempt.async_job_id)
            elif round.status == "queued" and (
                not attempt.dispatched_at
                or attempt.dispatched_at < cutoff - timedelta(seconds=30)
            ):
                await dispatch(db, round)
            cancelling = round.status == "cancelling"
            await db.commit()
        if cancelling:
            try:
                await settle_cancel(factory, identity)
            except Exception:
                log.warning(
                    "Active learning stop confirmation deferred round=%s", identity
                )


async def with_factory(callback, *args):
    engine = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await callback(factory, *args)
    finally:
        await engine.dispose()


@celery_app.task(bind=True, name="app.workers.active_learning.run_round")
def run_round(self, attempt_id):
    return asyncio.run(with_factory(execute, uuid.UUID(attempt_id), self.request.id))


@celery_app.task(name="app.workers.active_learning.maintain_rounds")
def maintain_rounds():
    return asyncio.run(with_factory(maintain))
