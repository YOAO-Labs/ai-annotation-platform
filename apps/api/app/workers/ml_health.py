"""Periodic ML backend health and GPU arbitration maintenance workers."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import secrets
import uuid
from datetime import datetime, timezone
from time import perf_counter

import redis as redis_sync

from app.config import GPUArbiterMode, settings
from app.services.inference_backend_control import backend_control_service
from app.services.inference_gpu import gpu_authority_client
from app.services.inference_gpu_data import open_gpu_authority_data
from app.services.inference_topology_data import InferenceTopologyData
from app.workers._db import task_session, task_session_factory
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)
_PERFHUD_META_KEYS = (
    "gpu_info",
    "host",
    "cache",
    "model_version",
    "loaded",
    "idle_unload_seconds",
    "last_request_age_seconds",
    "pool",
    "video_pool",
    "compute",
    "residency",
)

_HEALTH_TASK_LOCK_KEY = "celery-lock:check-ml-backends-health"
_GPU_REPAIR_TASK_LOCK_KEY = "celery-lock:repair-gpu-arbiter-resources"
_PERIODIC_HEALTH_TASK_LOCK_SECONDS = 720
_MANUAL_REPAIR_TASK_LOCK_SECONDS = 90
_GPU_REPAIR_MAX_CONCURRENCY = 4
_GPU_REPAIR_BATCH_TIMEOUT_SECONDS = 50
_GPU_REPAIR_WORK_BUDGET_SECONDS = 45
_GPU_PROMOTION_RESOURCE_TIMEOUT_SECONDS = 12
_GPU_REPAIR_FAIL_CLOSED_TIMEOUT_SECONDS = 2
_RELEASE_TASK_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def _run_with_task_lock(
    operation,
    *,
    lock_key: str,
    lock_seconds: int,
) -> dict:
    owner = secrets.token_hex(32)
    redis = redis_sync.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        acquired = redis.set(
            lock_key,
            owner,
            nx=True,
            ex=lock_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed on lock uncertainty
        log.warning("ml_health_task_lock_failed: %s", exc)
        redis.close()
        return {"skipped": True, "reason": "health_task_lock_unavailable"}
    if not acquired:
        redis.close()
        return {"skipped": True, "reason": "health_task_already_running"}
    try:
        return asyncio.run(operation())
    finally:
        try:
            redis.eval(_RELEASE_TASK_LOCK_LUA, 1, lock_key, owner)
        except Exception as exc:  # noqa: BLE001 - TTL remains the crash fallback
            log.warning("ml_health_task_lock_release_failed: %s", exc)
        redis.close()


@celery_app.task(
    name="app.workers.ml_health.check_ml_backends_health",
    soft_time_limit=650,
    time_limit=680,
)
def check_ml_backends_health() -> dict:
    return _run_with_task_lock(
        _run_async,
        lock_key=_HEALTH_TASK_LOCK_KEY,
        lock_seconds=_PERIODIC_HEALTH_TASK_LOCK_SECONDS,
    )


async def _run_async() -> dict:
    return await check_all_backends()


@celery_app.task(
    name="app.workers.ml_health.repair_gpu_arbiter_resources",
    soft_time_limit=65,
    time_limit=75,
)
def repair_gpu_arbiter_resources() -> dict:
    """Advance the isolated GPU repair loop."""

    return _run_with_task_lock(
        _repair_gpu_arbiter_resources,
        lock_key=_GPU_REPAIR_TASK_LOCK_KEY,
        lock_seconds=_MANUAL_REPAIR_TASK_LOCK_SECONDS,
    )


async def _resource_memberships(factory, resource_id: str):
    async with open_gpu_authority_data(factory) as data:
        return await data.membership_candidates(
            resource_id,
            states=("retiring",),
        )


async def _begin_gpu_rollout(factory, resource_id: str, target_mode: str):
    async with open_gpu_authority_data(factory) as data:
        return await data.begin_rollout(resource_id, target_mode)


async def _complete_gpu_rollout(factory, resource_id: str, transition_id: uuid.UUID):
    async with open_gpu_authority_data(factory) as data:
        return await data.complete_rollout(resource_id, transition_id)


async def _block_gpu_rollout(
    factory,
    resource_id: str,
    transition_id: uuid.UUID,
    reason: str,
):
    async with open_gpu_authority_data(factory) as data:
        return await data.block_rollout(resource_id, transition_id, reason)


def _collection_document(result) -> dict:
    return {
        "backend_id": str(result.backend_id),
        "resource_id": result.resource_id,
        "membership_epoch": result.membership_epoch,
        "status": result.status,
        "reason": result.reason,
        "redis_idempotent": result.idempotent,
    }


def _rollout_document(rollout) -> dict:
    return {
        "state": rollout.state,
        "effective_mode": rollout.effective_mode,
        "target_mode": rollout.target_mode,
        "transition_id": (
            str(rollout.transition_id) if rollout.transition_id is not None else None
        ),
        "last_transition_id": (
            str(rollout.last_transition_id)
            if rollout.last_transition_id is not None
            else None
        ),
        "blocker_reason": rollout.blocker_reason,
        "revision": rollout.revision,
    }


async def _latch_gpu_resource_not_ready(
    client,
    resource_id: str,
    allocatable_mb: int | None,
    *,
    reason: str,
):
    result = await client.repair_resource(
        resource_id,
        allocatable_mb,
        readiness_blocker=reason,
    )
    if result.status != "not_ready":
        raise RuntimeError(f"GPU readiness demotion was not confirmed: {result.status}")
    return result


async def _settle_gpu_resource_promotion(
    factory,
    rollout,
    repair_result: dict,
) -> dict:
    settled = rollout
    if rollout.state == "promoting":
        if rollout.transition_id is None:
            raise RuntimeError("GPU rollout promotion identity is missing")
        if repair_result.get("transition_pending") is True:
            pass
        elif repair_result["ready"] is True:
            settled = await _complete_gpu_rollout(
                factory,
                rollout.resource_id,
                rollout.transition_id,
            )
        else:
            settled = await _block_gpu_rollout(
                factory,
                rollout.resource_id,
                rollout.transition_id,
                repair_result.get("reason") or "gpu_resource_not_ready",
            )
    repair_result["effective_mode"] = settled.effective_mode
    repair_result["rollout"] = _rollout_document(settled)
    return repair_result


async def _settle_gpu_resource_demotion(
    factory,
    rollout,
    result: dict,
) -> dict:
    settled = rollout
    if rollout.state != "demoting" or rollout.transition_id is None:
        raise RuntimeError("GPU rollout demotion identity is missing")
    if result.get("transition_pending") is True:
        pass
    elif result.get("demotion_complete") is True:
        settled = await _complete_gpu_rollout(
            factory,
            rollout.resource_id,
            rollout.transition_id,
        )
    else:
        settled = await _block_gpu_rollout(
            factory,
            rollout.resource_id,
            rollout.transition_id,
            result.get("reason") or "gpu_resource_demotion_unconfirmed",
        )
    result["effective_mode"] = settled.effective_mode
    result["rollout"] = _rollout_document(settled)
    return result


async def _repair_one_gpu_resource(
    factory,
    client,
    resource_id: str,
    allocatable_mb: int,
    rollout,
    *,
    promotion_timeout_seconds: float = _GPU_PROMOTION_RESOURCE_TIMEOUT_SECONDS,
) -> dict:
    started = perf_counter()
    collections: list[dict] = []
    rollout_transition_id = rollout.transition_id

    # Finish DB-pending collection receipts before a reset can reintroduce the
    # still-durable tombstone into the closed domain.
    memberships = await _resource_memberships(factory, resource_id)
    for membership in memberships:
        finalized = await client.collect_tombstone(
            membership.backend_id,
            resource_id,
            membership.membership_epoch,
        )
        if finalized.status == "collected":
            collections.append(_collection_document(finalized))

    force_proof_reset = False
    promotion_failed = False
    promotion_results = ()
    if rollout.state != "enforcing":
        try:
            async with asyncio.timeout(promotion_timeout_seconds):
                promotion_reply = await client.activate_resource_memberships(
                    resource_id,
                    pending_only=rollout_transition_id is not None,
                )
            promotion_results = tuple(promotion_reply.results)
            promotions = [item.model_dump(mode="json") for item in promotion_results]
            force_proof_reset = any(
                item.requires_proof_reset for item in promotion_results
            )
        except TimeoutError:
            force_proof_reset = True
            promotion_failed = True
            promotions = [
                {
                    "backend_id": None,
                    "resource_id": resource_id,
                    "membership_epoch": None,
                    "status": "unavailable",
                    "reason": "membership_promotion_timeout",
                    "runtime_epoch": None,
                    "control_epoch": None,
                    "requires_proof_reset": True,
                }
            ]
        except Exception:  # noqa: BLE001 - P3 repair must still run fail-closed
            force_proof_reset = True
            promotion_failed = True
            promotions = [
                {
                    "backend_id": None,
                    "resource_id": resource_id,
                    "membership_epoch": None,
                    "status": "unavailable",
                    "reason": "membership_promotion_unavailable",
                    "runtime_epoch": None,
                    "control_epoch": None,
                    "requires_proof_reset": True,
                }
            ]
    else:
        promotions = []

    controls: list[dict] = []
    transition_pending = False
    control_blocker: str | None = None
    if rollout_transition_id is not None:
        if promotion_failed:
            control_blocker = "membership_promotion_unconfirmed"
        elif promotion_results and {item.status for item in promotion_results} <= {
            "promoted",
            "acknowledged",
        }:
            transition_pending = True
        elif promotion_results:
            control_blocker = "membership_promotion_unconfirmed"
        else:
            try:
                async with asyncio.timeout(promotion_timeout_seconds):
                    control_reply = await client.advance_resource_rollout_control(
                        resource_id,
                        rollout_transition_id,
                        "enforce",
                    )
                control_results = tuple(control_reply.results)
                controls = [item.model_dump(mode="json") for item in control_results]
                control_statuses = {item.status for item in control_results}
                transition_pending = bool(control_statuses & {"issued", "pending"})
                if control_statuses & {"blocked", "unavailable"}:
                    transition_pending = False
                    control_blocker = next(
                        item.reason
                        for item in control_results
                        if item.status in {"blocked", "unavailable"}
                    )
            except TimeoutError:
                control_blocker = "rollout_control_timeout"
            except Exception:  # noqa: BLE001 - rollout remains fail-closed
                control_blocker = "rollout_control_unavailable"

    if control_blocker is not None:
        readiness_blocker = control_blocker
    elif transition_pending:
        readiness_blocker = "rollout_control_awaiting_fresh_health"
    elif force_proof_reset:
        readiness_blocker = "membership_promotion_unconfirmed"
    else:
        readiness_blocker = None

    repair = await client.repair_resource(
        resource_id,
        allocatable_mb,
        force_proof_reset=force_proof_reset,
        readiness_blocker=readiness_blocker,
    )

    memberships = await _resource_memberships(factory, resource_id)
    collected_now = False
    for membership in memberships:
        probe = await client.probe_retired_membership(
            membership.backend_id,
            resource_id,
            membership.membership_epoch,
        )
        if not probe.proof_available:
            collections.append(
                {
                    "backend_id": str(membership.backend_id),
                    "resource_id": resource_id,
                    "membership_epoch": membership.membership_epoch,
                    "status": "blocked",
                    "reason": probe.reason,
                    "redis_idempotent": False,
                }
            )
            continue
        collected = await client.collect_tombstone(
            membership.backend_id,
            resource_id,
            membership.membership_epoch,
        )
        collections.append(_collection_document(collected))
        collected_now = collected_now or collected.status == "collected"

    if collected_now:
        repair = await client.repair_resource(
            resource_id,
            allocatable_mb,
            force_proof_reset=force_proof_reset,
            readiness_blocker=readiness_blocker,
        )

    observation = await client.observe_resource(
        resource_id,
        allocatable_mb=allocatable_mb,
    )
    observation_document = observation.model_dump(mode="json")
    observation_document.pop("protocol", None)
    result = {
        "resource_id": resource_id,
        "desired_mode": "enforce",
        "effective_mode": rollout.effective_mode,
        "action": repair.action,
        "status": repair.status,
        "ready": repair.ready,
        "reason": repair.reason,
        "ledger_revision": repair.ledger_revision,
        "ledger_incarnation": repair.ledger_incarnation,
        "committed_mb": repair.committed_mb,
        "collections": collections,
        "promotions": promotions,
        "controls": controls,
        "transition_pending": transition_pending,
        "observation": observation_document,
        "duration_ms": round((perf_counter() - started) * 1000),
    }
    log.info(
        "gpu_arbiter_resource_repair: %s",
        json.dumps(result, sort_keys=True, separators=(",", ":")),
    )
    return result


async def _demote_one_gpu_resource(
    factory,
    client,
    resource_id: str,
    allocatable_mb: int | None,
    rollout,
    *,
    control_timeout_seconds: float = _GPU_PROMOTION_RESOURCE_TIMEOUT_SECONDS,
) -> dict:
    started = perf_counter()
    assert rollout.transition_id is not None
    await _latch_gpu_resource_not_ready(
        client,
        resource_id,
        allocatable_mb,
        reason="rollout_demotion_in_progress",
    )
    observation = await client.observe_resource(
        resource_id,
        allocatable_mb=allocatable_mb,
    )
    runtime_quiescent = (
        observation.lease_count == 0
        and observation.card_queue_count == 0
        and observation.backend_queue_count == 0
        and observation.transition_present is False
    )
    controls: list[dict] = []
    transition_pending = not runtime_quiescent
    demotion_complete = False
    reason = "demotion_waiting_for_runtime_quiescence"

    if runtime_quiescent:
        try:
            async with asyncio.timeout(control_timeout_seconds):
                control_reply = await client.advance_resource_rollout_control(
                    resource_id,
                    rollout.transition_id,
                    "legacy",
                )
            control_results = tuple(control_reply.results)
            controls = [item.model_dump(mode="json") for item in control_results]
            statuses = {item.status for item in control_results}
            if statuses & {"blocked", "unavailable"}:
                failed = next(
                    item
                    for item in control_results
                    if item.status in {"blocked", "unavailable"}
                )
                transition_pending = False
                reason = failed.reason
            elif statuses & {"issued", "pending"}:
                transition_pending = True
                reason = "legacy_gate_awaiting_fresh_health"
            else:
                transition_pending = False
                demotion_complete = True
                reason = ""
        except TimeoutError:
            transition_pending = False
            reason = "rollout_control_timeout"
        except Exception:  # noqa: BLE001 - demotion remains fail-closed
            transition_pending = False
            reason = "rollout_control_unavailable"

    result = {
        "resource_id": resource_id,
        "desired_mode": rollout.target_mode,
        "effective_mode": rollout.effective_mode,
        "action": "demotion",
        "status": (
            "legacy_acknowledged"
            if demotion_complete
            else "pending"
            if transition_pending
            else "not_ready"
        ),
        "ready": False,
        "reason": reason,
        "ledger_revision": observation.ledger_revision,
        "ledger_incarnation": observation.ledger_incarnation,
        "committed_mb": observation.committed_mb,
        "collections": [],
        "promotions": [],
        "controls": controls,
        "transition_pending": transition_pending,
        "demotion_complete": demotion_complete,
        "observation": None,
        "duration_ms": round((perf_counter() - started) * 1000),
    }
    log.info(
        "gpu_arbiter_resource_demotion: %s",
        json.dumps(result, sort_keys=True, separators=(",", ":")),
    )
    return result


async def _repair_gpu_arbiter_resources() -> dict:
    """Advance desired-enforce cards and settle active demotions after health."""

    if settings.gpu_arbiter_config_errors:
        return {
            "skipped": True,
            "reason": "gpu_resources_config_invalid",
            "resources": [],
        }
    if not settings.gpu_arbiter_rollout_enabled:
        return {
            "skipped": True,
            "reason": "gpu_rollout_disabled",
            "resources": [],
        }

    client = gpu_authority_client()
    async with task_session_factory() as factory:
        try:
            async with open_gpu_authority_data(factory) as data:
                durable_rollouts = await data.read_rollouts()
        except Exception as exc:  # noqa: BLE001 - DB loss must not touch GPU owner
            return {
                "skipped": True,
                "reason": "gpu_rollout_state_unavailable",
                "detail": str(exc) or type(exc).__name__,
                "resources": [],
            }

        durable_by_resource = {item.resource_id: item for item in durable_rollouts}
        resource_ids = {
            resource_id
            for resource_id in settings.gpu_arbiter_resources
            if settings.gpu_arbiter_desired_mode(resource_id) is GPUArbiterMode.ENFORCE
        }
        resource_ids.update(
            item.resource_id for item in durable_rollouts if item.state != "off"
        )
        resources = [
            (
                resource_id,
                settings.gpu_arbiter_desired_mode(resource_id),
                settings.gpu_arbiter_resources.get(resource_id),
            )
            for resource_id in sorted(resource_ids)
        ]
        if not resources:
            return {
                "skipped": True,
                "reason": "no_active_gpu_rollouts",
                "resources": [],
            }

        semaphore = asyncio.Semaphore(_GPU_REPAIR_MAX_CONCURRENCY)
        repair_waves = max(
            1,
            (len(resources) + _GPU_REPAIR_MAX_CONCURRENCY - 1)
            // _GPU_REPAIR_MAX_CONCURRENCY,
        )
        repair_wave_budget_seconds = _GPU_REPAIR_WORK_BUDGET_SECONDS / repair_waves
        fail_closed_timeout_seconds = max(
            0.001,
            min(
                _GPU_REPAIR_FAIL_CLOSED_TIMEOUT_SECONDS,
                repair_wave_budget_seconds / 4,
            ),
        )
        per_resource_timeout_seconds = min(
            30.0,
            max(0.001, repair_wave_budget_seconds - fail_closed_timeout_seconds),
        )
        promotion_timeout_seconds = min(
            _GPU_PROMOTION_RESOURCE_TIMEOUT_SECONDS,
            per_resource_timeout_seconds / 3,
        )

        def failure_result(
            resource_id: str,
            desired_mode: GPUArbiterMode,
            reason: str,
            started: float,
            rollout,
        ) -> dict:
            result = {
                "resource_id": resource_id,
                "desired_mode": desired_mode.value,
                "effective_mode": (
                    rollout.effective_mode
                    if rollout is not None
                    else GPUArbiterMode.OFF.value
                ),
                "action": "error",
                "status": "unavailable",
                "ready": False,
                "reason": reason[:256],
                "ledger_revision": None,
                "ledger_incarnation": None,
                "committed_mb": None,
                "collections": [],
                "promotions": [],
                "controls": [],
                "transition_pending": False,
                "observation": None,
                "duration_ms": round((perf_counter() - started) * 1000),
            }
            result["rollout"] = (
                _rollout_document(rollout) if rollout is not None else None
            )
            return result

        rollouts_by_resource = {}

        async def fail_closed_failure_result(
            resource_id: str,
            desired_mode: GPUArbiterMode,
            allocatable_mb: int | None,
            *,
            reason: str,
            readiness_reason: str,
            started: float,
            rollout,
        ) -> dict:
            try:
                async with asyncio.timeout(fail_closed_timeout_seconds):
                    await _latch_gpu_resource_not_ready(
                        client,
                        resource_id,
                        allocatable_mb,
                        reason=readiness_reason,
                    )
            except Exception as exc:  # noqa: BLE001 - report uncertain latch
                reason = f"{reason}: {str(exc) or type(exc).__name__}"
            settled_rollout = rollout
            if rollout is not None and rollout.state in {"promoting", "demoting"}:
                assert rollout.transition_id is not None
                try:
                    async with asyncio.timeout(fail_closed_timeout_seconds):
                        settled_rollout = await _block_gpu_rollout(
                            factory,
                            resource_id,
                            rollout.transition_id,
                            reason,
                        )
                except Exception as exc:  # noqa: BLE001 - transition remains fail-closed
                    reason = f"{reason}: {str(exc) or type(exc).__name__}"
            result = failure_result(
                resource_id,
                desired_mode,
                reason,
                started,
                settled_rollout,
            )
            log.warning(
                "gpu_arbiter_resource_repair_failed: %s",
                json.dumps(result, sort_keys=True, separators=(",", ":")),
            )
            return result

        async def run_one(resource_id: str, desired_mode: GPUArbiterMode, resource):
            async with semaphore:
                started = perf_counter()
                rollout = None
                try:
                    async with asyncio.timeout(per_resource_timeout_seconds):
                        current = durable_by_resource.get(resource_id)
                        target_mode = desired_mode
                        if (
                            desired_mode is GPUArbiterMode.ENFORCE
                            and current is not None
                            and current.effective_mode == GPUArbiterMode.ENFORCE.value
                            and current.state in {"demoting", "blocked"}
                            and current.target_mode != GPUArbiterMode.ENFORCE.value
                        ):
                            target_mode = GPUArbiterMode(current.target_mode)
                        rollout = await _begin_gpu_rollout(
                            factory,
                            resource_id,
                            target_mode.value,
                        )
                        rollouts_by_resource[resource_id] = rollout
                        if target_mode is GPUArbiterMode.ENFORCE:
                            if resource is None:
                                raise RuntimeError(
                                    "enforce resource configuration is missing"
                                )
                            repair_result = await _repair_one_gpu_resource(
                                factory,
                                client,
                                resource_id,
                                resource.allocatable_mb,
                                rollout,
                                promotion_timeout_seconds=promotion_timeout_seconds,
                            )
                            return await _settle_gpu_resource_promotion(
                                factory,
                                rollout,
                                repair_result,
                            )
                        demotion_result = await _demote_one_gpu_resource(
                            factory,
                            client,
                            resource_id,
                            (resource.allocatable_mb if resource is not None else None),
                            rollout,
                            control_timeout_seconds=promotion_timeout_seconds,
                        )
                        return await _settle_gpu_resource_demotion(
                            factory,
                            rollout,
                            demotion_result,
                        )
                except TimeoutError:
                    allocatable_mb = (
                        resource.allocatable_mb if resource is not None else None
                    )
                    return await fail_closed_failure_result(
                        resource_id,
                        desired_mode,
                        allocatable_mb,
                        reason="gpu_resource_repair_timeout",
                        readiness_reason="gpu_resource_repair_timeout",
                        started=started,
                        rollout=rollout,
                    )
                except Exception as exc:  # noqa: BLE001 - isolate physical resources
                    allocatable_mb = (
                        resource.allocatable_mb if resource is not None else None
                    )
                    return await fail_closed_failure_result(
                        resource_id,
                        desired_mode,
                        allocatable_mb,
                        reason=str(exc) or type(exc).__name__,
                        readiness_reason="gpu_resource_repair_failed",
                        started=started,
                        rollout=rollout,
                    )

        tasks = {}
        try:
            tasks = {
                asyncio.create_task(run_one(resource_id, desired_mode, resource)): (
                    resource_id,
                    resource.allocatable_mb if resource is not None else None,
                    desired_mode,
                    perf_counter(),
                )
                for resource_id, desired_mode, resource in resources
            }
            done, pending = await asyncio.wait(
                tasks,
                timeout=_GPU_REPAIR_BATCH_TIMEOUT_SECONDS,
            )
            results_by_resource = {tasks[task][0]: task.result() for task in done}
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            pending_tasks = tuple(pending)
            pending_results = await asyncio.gather(
                *(
                    fail_closed_failure_result(
                        tasks[task][0],
                        tasks[task][2],
                        tasks[task][1],
                        reason="gpu_arbiter_repair_batch_timeout",
                        readiness_reason="gpu_arbiter_repair_batch_timeout",
                        started=tasks[task][3],
                        rollout=rollouts_by_resource.get(tasks[task][0]),
                    )
                    for task in pending_tasks
                )
            )
            for task, result in zip(pending_tasks, pending_results, strict=True):
                results_by_resource[tasks[task][0]] = result
            results = [
                results_by_resource[resource_id]
                for resource_id, _desired_mode, _resource in resources
            ]
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    return {"skipped": False, "resources": results}


@celery_app.task(name="app.workers.ml_health.publish_ml_backend_stats")
def publish_ml_backend_stats() -> dict:
    """Push an owner-produced PerfHUD snapshot only while a client is subscribed."""

    return asyncio.run(_publish_stats_async())


async def _publish_stats_async() -> dict:
    r = redis_sync.from_url(settings.redis_url)
    try:
        raw = r.get("ml-backend-stats:subscribers")
    except Exception as e:  # noqa: BLE001
        log.debug("subscribers key read failed: %s", e)
        raw = None
    finally:
        try:
            r.close()
        except Exception:
            pass
    try:
        subscribers = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        subscribers = 0
    if subscribers <= 0:
        return {"skipped": True, "subscribers": 0}

    async with task_session() as db:
        snapshots = await backend_control_service(db).stats_snapshots()

    now = datetime.now(timezone.utc).isoformat()
    payload_snapshots = []
    for snapshot in snapshots:
        payload = snapshot.model_dump(mode="json")
        meta = payload.pop("meta", None)
        if isinstance(meta, dict):
            for key in _PERFHUD_META_KEYS:
                if key in meta:
                    payload[key] = meta[key]
        payload_snapshots.append(payload)
    r2 = redis_sync.from_url(settings.redis_url)
    try:
        r2.publish(
            "ml-backend-stats:global",
            json.dumps({"backends": payload_snapshots, "timestamp": now}),
        )
    finally:
        try:
            r2.close()
        except Exception:
            pass
    return {"published": len(payload_snapshots), "subscribers": subscribers}


async def check_all_backends(jitter_max_seconds: float = 3.0) -> dict:
    """Check all public registry IDs and isolate failures to each backend."""

    async with task_session() as db:
        backend_facts = await InferenceTopologyData(db).registry_catalog()
        backend_ids = [fact.id for fact in backend_facts]

    results: list[dict] = []
    for backend_id in backend_ids:
        if jitter_max_seconds > 0:
            await asyncio.sleep(random.uniform(0, jitter_max_seconds))
        try:
            async with task_session() as db:
                service = backend_control_service(db)
                healthy = await service.check_health(backend_id)
                await db.commit()
                fresh = await InferenceTopologyData(db).registry_facts(backend_id)
                results.append(
                    {
                        "id": str(backend_id),
                        "state": fresh.state if fresh is not None else "unknown",
                        "healthy": healthy,
                    }
                )
        except Exception as exc:  # noqa: BLE001 - one backend must not block others
            log.warning(
                "check_ml_backends_health: backend=%s failed: %s", backend_id, exc
            )
            results.append({"id": str(backend_id), "state": "error", "healthy": False})

    log.info(
        "check_ml_backends_health: checked=%d at=%s",
        len(results),
        datetime.now(timezone.utc).isoformat(),
    )
    return {"checked": len(results), "results": results}
