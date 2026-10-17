"""v0.8.6 F2 · ML Backend 周期健康检查任务

每 60s 扫描所有 ML Backend，调用 `/health` 端点更新 `state` + `last_checked_at`。
单 Celery task 内串行扫描所有 backend，每个 backend 调用前 0-3s 抖动错峰，
避免同节点 backend 同时被打 health 触发 GPU CUDA 上下文 contention。

设计理由参考 `docs/plans/2026-05-07-v0.8.6-rustling-raven.md` §F2。

v0.9.11 PerfHud · 新增 publish_ml_backend_stats: 每 1s 把所有 is_active=true backend 的
/health 实时快照 publish 到 redis channel `ml-backend-stats:global`. 仅在 WS 订阅者 > 0
时执行实拉 (Redis key `ml-backend-stats:subscribers` 计数门控), 节省 GPU 探活成本.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict
import hashlib
import json
import logging
import random
import secrets
import uuid
from datetime import datetime, timezone
from time import perf_counter
from urllib.parse import urlparse

import redis as redis_sync
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import GPUArbiterMode, settings
from app.db.models.gpu_backend_membership import GPUBackendMembership
from app.db.models.ml_backend_registry import MLBackendRegistry as MLBackend
from app.services.gpu_arbitration.policy import effective_gpu_arbiter_mode
from app.services.gpu_arbitration.reconciliation import (
    observe_gpu_resource_runtime,
    repair_gpu_resource,
)
from app.services.gpu_arbitration.retirement import (
    collect_gpu_backend_tombstone,
    probe_retired_gpu_membership,
)
from app.services.gpu_arbitration.ledger import (
    GPUArbiterStore,
    GPUArbiterStoreError,
    GPUBackendDomainMember,
)
from app.services.gpu_arbitration.rollout_state import (
    GPUArbiterRolloutSnapshot,
    begin_gpu_arbiter_rollout,
    block_gpu_arbiter_rollout,
    complete_gpu_arbiter_rollout,
    read_gpu_arbiter_rollouts,
)
from app.services.gpu_arbitration.collector_database import (
    GPUCollectorDatabase,
    open_gpu_collector_database,
)
from app.services.gpu_arbitration.membership_activation import (
    GPUMembershipPromotionResult,
    promote_gpu_resource_memberships,
)
from app.services.gpu_arbitration.rollout_control import (
    GPURolloutControlResult,
    advance_gpu_resource_rollout_control,
)
from app.services.ml_backend import MLBackendService
from app.workers._db import task_session
from app.services.ml_client import MLBackendClient
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)

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
    # v0.22.3 WS4 · compute (configured_device/effective_device/effective_provider),
    # 用于 PerfHud 显示 GPU 静默退回 CPU 告警。
    "compute",
    # ADR-0049 · 保留后端真实驻留 / active / builder / borrower 信号。
    "residency",
)


def _build_stats_snapshot(
    backend: MLBackend,
    *,
    ok: bool,
    meta: dict | None,
    timestamp: str,
    physical_key: str | None = None,
    url_host: str | None = None,
    bindings: list[dict] | None = None,
) -> dict:
    snap = {
        "physical_key": physical_key or f"backend:{backend.id}",
        "url_host": url_host,
        "backend_id": str(backend.id),
        "backend_name": backend.name,
        "bindings": bindings or [_binding_for_backend(backend)],
        "state": "ok" if ok else "error",
        "timestamp": timestamp,
    }
    if meta:
        for key in _PERFHUD_META_KEYS:
            if key in meta:
                snap[key] = meta[key]
    return snap


def _endpoint_identity(url: str) -> tuple[str, str] | None:
    """Return a stable physical endpoint identity and a user-facing host label."""
    parsed = urlparse(url if "://" in url else f"//{url}")
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    scheme = parsed.scheme or "http"
    hostport = f"{host}:{parsed.port}" if parsed.port else host
    return f"{scheme}://{hostport}", hostport


def _backend_group_key(backend: MLBackend) -> tuple[str, str, str | None]:
    endpoint = _endpoint_identity(backend.url)
    if endpoint is None:
        fallback = f"backend:{backend.id}"
        return fallback, fallback, None
    endpoint_key, hostport = endpoint
    auth_key = f"{backend.auth_method}:{backend.auth_token or ''}"
    if backend.auth_method == "none" and not backend.auth_token:
        public_key = endpoint_key
    else:
        auth_fingerprint = hashlib.sha256(auth_key.encode("utf-8")).hexdigest()[:8]
        public_key = f"{endpoint_key}|auth:{auth_fingerprint}"
    return f"{endpoint_key}|auth:{auth_key}", public_key, hostport


def _binding_for_backend(
    backend: MLBackend,
    *,
    project_display_id: str | None = None,
    project_name: str | None = None,
) -> dict:
    return {
        "backend_id": str(backend.id),
        "backend_name": backend.name,
        # v0.19.0 ADR-0044 · backend 全局化, 不再属于单一项目; 项目归属由 project_ml_backend_pool
        # 关联表表达 (健康概览不再按项目拆分)。v0.23.3 ADR-0050 · 启用关联迁移到服务池层。
        "project_id": None,
        "project_display_id": project_display_id,
        "project_name": project_name,
    }


def _group_backend_rows(
    rows: list[tuple[MLBackend, str | None, str | None]],
) -> list[dict]:
    grouped: dict[str, dict] = {}
    for backend, project_display_id, project_name in rows:
        group_key, physical_key, url_host = _backend_group_key(backend)
        group = grouped.setdefault(
            group_key,
            {
                "backend": backend,
                "physical_key": physical_key,
                "url_host": url_host,
                "bindings": [],
            },
        )
        group["bindings"].append(
            _binding_for_backend(
                backend,
                project_display_id=project_display_id,
                project_name=project_name,
            )
        )
    return list(grouped.values())


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


async def _resource_memberships(
    factory: async_sessionmaker[AsyncSession], resource_id: str
) -> tuple[GPUBackendMembership, ...]:
    async with factory() as db:
        return tuple(
            (
                await db.execute(
                    select(GPUBackendMembership)
                    .where(GPUBackendMembership.gpu_resource_id == resource_id)
                    .order_by(GPUBackendMembership.backend_registry_id)
                )
            )
            .scalars()
            .all()
        )


def _worker_domain(
    memberships: tuple[GPUBackendMembership, ...],
) -> tuple[GPUBackendDomainMember, ...]:
    return tuple(
        GPUBackendDomainMember(
            backend_id=str(item.backend_registry_id),
            membership_epoch=item.membership_epoch,
            state=item.state,  # type: ignore[arg-type]
        )
        for item in memberships
    )


def _collection_document(result) -> dict:
    return {
        "backend_id": str(result.backend_id),
        "resource_id": result.resource_id,
        "membership_epoch": result.membership_epoch,
        "status": result.status,
        "reason": result.reason,
        "redis_idempotent": result.redis_idempotent,
    }


def _rollout_document(rollout: GPUArbiterRolloutSnapshot) -> dict:
    return {
        "state": rollout.state,
        "effective_mode": rollout.effective_mode.value,
        "target_mode": rollout.target_mode.value,
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


async def _settle_gpu_resource_promotion(
    factory: async_sessionmaker[AsyncSession],
    rollout: GPUArbiterRolloutSnapshot,
    repair_result: dict,
) -> dict:
    settled = rollout
    if rollout.state == "promoting":
        if rollout.transition_id is None:
            raise RuntimeError("GPU rollout promotion identity is missing")
        if repair_result.get("transition_pending") is True:
            pass
        elif repair_result["ready"] is True:
            settled = await complete_gpu_arbiter_rollout(
                factory,
                rollout.resource_id,
                rollout.transition_id,
            )
        else:
            settled = await block_gpu_arbiter_rollout(
                factory,
                rollout.resource_id,
                rollout.transition_id,
                repair_result.get("reason") or "gpu_resource_not_ready",
            )
    repair_result["effective_mode"] = settled.effective_mode.value
    repair_result["rollout"] = _rollout_document(settled)
    return repair_result


async def _settle_gpu_resource_demotion(
    factory: async_sessionmaker[AsyncSession],
    rollout: GPUArbiterRolloutSnapshot,
    result: dict,
) -> dict:
    settled = rollout
    if rollout.state != "demoting" or rollout.transition_id is None:
        raise RuntimeError("GPU rollout demotion identity is missing")
    if result.get("transition_pending") is True:
        pass
    elif result.get("demotion_complete") is True:
        settled = await complete_gpu_arbiter_rollout(
            factory,
            rollout.resource_id,
            rollout.transition_id,
        )
    else:
        settled = await block_gpu_arbiter_rollout(
            factory,
            rollout.resource_id,
            rollout.transition_id,
            result.get("reason") or "gpu_resource_demotion_unconfirmed",
        )
    result["effective_mode"] = settled.effective_mode.value
    result["rollout"] = _rollout_document(settled)
    return result


async def _latch_gpu_resource_not_ready(
    store: GPUArbiterStore,
    resource_id: str,
    allocatable_mb: int,
    *,
    reason: str,
) -> None:
    result = await store.mark_card_not_ready(
        resource_id,
        allocatable_mb,
        reason=reason,
    )
    if result.status != "not_ready":
        raise GPUArbiterStoreError(
            f"gpu readiness demotion was not confirmed: {result.status}"
        )


async def _skip_gpu_membership_promotion(
    _factory: async_sessionmaker[AsyncSession],
    _resource_id: str,
) -> tuple[GPUMembershipPromotionResult, ...]:
    """Keep a settled enforcing rollout read-only until an explicit demotion."""

    return ()


async def _repair_one_gpu_resource(
    factory: async_sessionmaker[AsyncSession],
    store: GPUArbiterStore,
    resource_id: str,
    allocatable_mb: int,
    *,
    membership_promoter: Callable[
        [async_sessionmaker[AsyncSession], str],
        Awaitable[Sequence[GPUMembershipPromotionResult]],
    ]
    | None = None,
    promotion_timeout_seconds: float = _GPU_PROMOTION_RESOURCE_TIMEOUT_SECONDS,
    rollout_transition_id: uuid.UUID | None = None,
    control_advancer: Callable[..., Awaitable[Sequence[GPURolloutControlResult]]]
    | None = None,
    collector_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict:
    started = perf_counter()
    collections: list[dict] = []
    gc_factory = collector_factory or factory

    # Complete Redis-collected/DB-pending receipts before a reset can legally
    # reintroduce the still-durable tombstone into the closed domain.
    memberships = await _resource_memberships(factory, resource_id)
    for membership in memberships:
        if membership.state != "retiring":
            continue
        finalized = await collect_gpu_backend_tombstone(
            gc_factory,
            store,
            membership.backend_registry_id,
            resource_id,
            membership.membership_epoch,
            proof=None,
        )
        if finalized.status == "collected":
            collections.append(_collection_document(finalized))

    force_proof_reset = False
    readiness_revoked = False
    promotion_failed = False

    async def revoke_ready_before_epoch_advance(target_resource_id: str) -> None:
        nonlocal readiness_revoked
        if readiness_revoked:
            return
        await _latch_gpu_resource_not_ready(
            store,
            target_resource_id,
            allocatable_mb,
            reason="membership_promotion_in_progress",
        )
        readiness_revoked = True

    try:
        async with asyncio.timeout(promotion_timeout_seconds):
            if membership_promoter is None:
                promotion_results = await promote_gpu_resource_memberships(
                    factory,
                    resource_id,
                    readiness_demoter=revoke_ready_before_epoch_advance,
                    pending_only=rollout_transition_id is not None,
                )
            else:
                promotion_results = await membership_promoter(factory, resource_id)
        promotions = [asdict(item) for item in promotion_results]
        force_proof_reset = any(item.requires_proof_reset for item in promotion_results)
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
                    if control_advancer is None:
                        control_results = await advance_gpu_resource_rollout_control(
                            factory,
                            resource_id,
                            transition_id=rollout_transition_id,
                            target_gate="enforce",
                            readiness_demoter=revoke_ready_before_epoch_advance,
                        )
                    else:
                        control_results = await control_advancer(
                            factory,
                            resource_id,
                            transition_id=rollout_transition_id,
                            target_gate="enforce",
                            readiness_demoter=revoke_ready_before_epoch_advance,
                        )
                controls = [asdict(item) for item in control_results]
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

    readiness_blocker: str | None
    if control_blocker is not None:
        readiness_blocker = control_blocker
    elif transition_pending:
        readiness_blocker = "rollout_control_awaiting_fresh_health"
    elif force_proof_reset:
        readiness_blocker = "membership_promotion_unconfirmed"
    else:
        readiness_blocker = None

    repair = await repair_gpu_resource(
        factory,
        store,
        resource_id,
        allocatable_mb,
        force_proof_reset=force_proof_reset,
        readiness_blocker=readiness_blocker,
    )

    memberships = await _resource_memberships(factory, resource_id)
    collected_now = False
    for membership in memberships:
        if membership.state != "retiring":
            continue
        probe = await probe_retired_gpu_membership(
            factory,
            membership.backend_registry_id,
            resource_id,
            membership.membership_epoch,
        )
        if probe.proof is None:
            collections.append(
                {
                    "backend_id": str(membership.backend_registry_id),
                    "resource_id": resource_id,
                    "membership_epoch": membership.membership_epoch,
                    "status": "blocked",
                    "reason": probe.reason,
                    "redis_idempotent": False,
                }
            )
            continue
        collected = await collect_gpu_backend_tombstone(
            gc_factory,
            store,
            membership.backend_registry_id,
            resource_id,
            membership.membership_epoch,
            proof=probe.proof,
        )
        collections.append(_collection_document(collected))
        collected_now = collected_now or collected.status == "collected"

    if collected_now:
        repair = await repair_gpu_resource(
            factory,
            store,
            resource_id,
            allocatable_mb,
            force_proof_reset=force_proof_reset,
            readiness_blocker=readiness_blocker,
        )

    memberships = await _resource_memberships(factory, resource_id)
    observation = await observe_gpu_resource_runtime(
        store,
        resource_id,
        _worker_domain(memberships),
    )
    result = {
        "resource_id": resource_id,
        "desired_mode": "enforce",
        "effective_mode": effective_gpu_arbiter_mode(resource_id).value,
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
        "observation": asdict(observation),
        "duration_ms": round((perf_counter() - started) * 1000),
    }
    log.info(
        "gpu_arbiter_resource_repair: %s",
        json.dumps(result, sort_keys=True, separators=(",", ":")),
    )
    return result


async def _demote_one_gpu_resource(
    factory: async_sessionmaker[AsyncSession],
    store: GPUArbiterStore,
    resource_id: str,
    allocatable_mb: int,
    rollout: GPUArbiterRolloutSnapshot,
    *,
    control_advancer: Callable[..., Awaitable[Sequence[GPURolloutControlResult]]]
    | None = None,
    control_timeout_seconds: float = _GPU_PROMOTION_RESOURCE_TIMEOUT_SECONDS,
) -> dict:
    started = perf_counter()
    assert rollout.transition_id is not None
    await _latch_gpu_resource_not_ready(
        store,
        resource_id,
        allocatable_mb,
        reason="rollout_demotion_in_progress",
    )
    snapshot = await store.snapshot(resource_id)
    runtime_quiescent = bool(
        not snapshot.leases
        and snapshot.card_queue_count == 0
        and snapshot.backend_queue_count == 0
        and not snapshot.transition_present
    )
    controls: list[dict] = []
    transition_pending = not runtime_quiescent
    demotion_complete = False
    reason = "demotion_waiting_for_runtime_quiescence"

    async def keep_not_ready(target_resource_id: str) -> None:
        await _latch_gpu_resource_not_ready(
            store,
            target_resource_id,
            allocatable_mb,
            reason="rollout_demotion_in_progress",
        )

    if runtime_quiescent:
        try:
            async with asyncio.timeout(control_timeout_seconds):
                if control_advancer is None:
                    control_results = await advance_gpu_resource_rollout_control(
                        factory,
                        resource_id,
                        transition_id=rollout.transition_id,
                        target_gate="legacy",
                        readiness_demoter=keep_not_ready,
                    )
                else:
                    control_results = await control_advancer(
                        factory,
                        resource_id,
                        transition_id=rollout.transition_id,
                        target_gate="legacy",
                        readiness_demoter=keep_not_ready,
                    )
            controls = [asdict(item) for item in control_results]
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
        "desired_mode": rollout.target_mode.value,
        "effective_mode": rollout.effective_mode.value,
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
        "ledger_revision": snapshot.ledger_revision,
        "ledger_incarnation": snapshot.ledger_incarnation,
        "committed_mb": snapshot.committed_mb,
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


async def _repair_gpu_arbiter_resources(
    *,
    membership_promoter: Callable[
        [async_sessionmaker[AsyncSession], str],
        Awaitable[Sequence[GPUMembershipPromotionResult]],
    ]
    | None = None,
) -> dict:
    """Advance desired-enforce cards and settle active demotions after health.

    Settled off/observe cards remain no-Redis; a durable demotion deliberately
    retains the old ledger until every backend confirms legacy gate.  Each
    invocation owns its async engine and Redis client so repeated Celery
    ``asyncio.run`` loops never share sockets.
    """

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
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        durable_rollouts = await read_gpu_arbiter_rollouts(factory)
    except Exception as exc:  # noqa: BLE001 - DB loss must not touch Redis
        await engine.dispose()
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
        await engine.dispose()
        return {
            "skipped": True,
            "reason": "no_active_gpu_rollouts",
            "resources": [],
        }

    collector_database: GPUCollectorDatabase | None = None
    collector_error: str | None = None
    try:
        collector_database = await open_gpu_collector_database(factory)
    except Exception as exc:  # noqa: BLE001 - never expose a credential/DSN
        collector_error = type(exc).__name__

    store = GPUArbiterStore.from_url(settings.redis_url)
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
        rollout: GPUArbiterRolloutSnapshot | None,
    ) -> dict:
        result = {
            "resource_id": resource_id,
            "desired_mode": desired_mode.value,
            "effective_mode": (
                rollout.effective_mode.value
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
        result["rollout"] = _rollout_document(rollout) if rollout is not None else None
        return result

    async def fail_closed_failure_result(
        resource_id: str,
        desired_mode: GPUArbiterMode,
        allocatable_mb: int | None,
        *,
        reason: str,
        readiness_reason: str,
        started: float,
        rollout: GPUArbiterRolloutSnapshot | None,
    ) -> dict:
        try:
            async with asyncio.timeout(fail_closed_timeout_seconds):
                if allocatable_mb is None:
                    allocatable_mb = (await store.snapshot(resource_id)).allocatable_mb
                await _latch_gpu_resource_not_ready(
                    store,
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
                    settled_rollout = await block_gpu_arbiter_rollout(
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

    rollouts_by_resource: dict[str, GPUArbiterRolloutSnapshot] = {}

    async def run_one(resource_id: str, desired_mode: GPUArbiterMode, resource) -> dict:
        async with semaphore:
            started = perf_counter()
            rollout: GPUArbiterRolloutSnapshot | None = None
            try:
                async with asyncio.timeout(per_resource_timeout_seconds):
                    current = durable_by_resource.get(resource_id)
                    target_mode = desired_mode
                    if (
                        desired_mode is GPUArbiterMode.ENFORCE
                        and current is not None
                        and current.effective_mode is GPUArbiterMode.ENFORCE
                        and current.state in {"demoting", "blocked"}
                        and current.target_mode is not GPUArbiterMode.ENFORCE
                    ):
                        # Finish an already-started rollback before a later cycle
                        # may promote again; this avoids mixed-gate compensation.
                        target_mode = current.target_mode
                    rollout = await begin_gpu_arbiter_rollout(
                        factory,
                        resource_id,
                        target_mode,
                    )
                    rollouts_by_resource[resource_id] = rollout
                    if target_mode is GPUArbiterMode.ENFORCE:
                        if resource is None:
                            raise RuntimeError(
                                "enforce resource configuration is missing"
                            )
                        if collector_database is None:
                            raise RuntimeError(
                                "gpu_collector_isolation_unavailable"
                                + (f":{collector_error}" if collector_error else "")
                            )
                        repair_result = await _repair_one_gpu_resource(
                            factory,
                            store,
                            resource_id,
                            resource.allocatable_mb,
                            membership_promoter=(
                                _skip_gpu_membership_promotion
                                if rollout.state == "enforcing"
                                else membership_promoter
                            ),
                            promotion_timeout_seconds=promotion_timeout_seconds,
                            rollout_transition_id=rollout.transition_id,
                            collector_factory=collector_database.session_factory,
                        )
                        return await _settle_gpu_resource_promotion(
                            factory,
                            rollout,
                            repair_result,
                        )
                    allocatable_mb = (
                        resource.allocatable_mb
                        if resource is not None
                        else (await store.snapshot(resource_id)).allocatable_mb
                    )
                    demotion_result = await _demote_one_gpu_resource(
                        factory,
                        store,
                        resource_id,
                        allocatable_mb,
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
        try:
            await store.aclose()
        except Exception as exc:  # noqa: BLE001 - task result remains useful
            log.warning("gpu_arbiter_store_close_failed: %s", exc)
        if collector_database is not None:
            await collector_database.engine.dispose()
        await engine.dispose()
    return {"skipped": False, "resources": results}


@celery_app.task(name="app.workers.ml_health.publish_ml_backend_stats")
def publish_ml_backend_stats() -> dict:
    """v0.9.11 PerfHud · 1s 实时快照推送到 WS. 0 订阅者时短路 skip."""
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

    # Celery prefork + 全局 asyncpg engine 共享会触发 "another operation in progress",
    # 用 per-task engine + NullPool 模式 (与 application.run_preannotation 一致). 1s 高频但单次 < 50ms.
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with SessionLocal() as db:
            # 全局注册表; state == 'disconnected' 跳过 (一直 down 的 backend 不打)
            backends = (
                (
                    await db.execute(
                        select(MLBackend)
                        .where(MLBackend.state != "disconnected")
                        .order_by(MLBackend.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            rows = [(b, None, None) for b in backends]
    finally:
        await engine.dispose()

    snapshots: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    for group in _group_backend_rows(list(rows)):
        backend = group["backend"]
        try:
            client = MLBackendClient(backend)
            ok, meta = await client.health_meta()
            snapshots.append(
                _build_stats_snapshot(
                    backend,
                    ok=ok,
                    meta=meta,
                    timestamp=now,
                    physical_key=group["physical_key"],
                    url_host=group["url_host"],
                    bindings=group["bindings"],
                )
            )
        except Exception as exc:  # noqa: BLE001 — 单 backend 失败不影响其他
            log.debug("publish_ml_backend_stats: backend=%s err=%s", backend.id, exc)
            snapshots.append(
                {
                    "physical_key": group["physical_key"],
                    "url_host": group["url_host"],
                    "backend_id": str(backend.id),
                    "backend_name": backend.name,
                    "bindings": group["bindings"],
                    "state": "error",
                    "timestamp": now,
                }
            )

    r2 = redis_sync.from_url(settings.redis_url)
    try:
        # 单帧 publish 整个 list, 前端按 physical_key 路由到对应 PerfHud panel.
        r2.publish(
            "ml-backend-stats:global",
            json.dumps({"backends": snapshots, "timestamp": now}),
        )
    finally:
        try:
            r2.close()
        except Exception:
            pass
    return {"published": len(snapshots), "subscribers": subscribers}


async def check_all_backends(jitter_max_seconds: float = 3.0) -> dict:
    """串行扫描所有 ML Backend；每个 backend 检查前抖动 0~jitter_max 秒错峰。

    返回 ``{"checked": N, "results": [{"id":..., "state":..., "healthy":bool}, ...]}``。
    """
    async with task_session() as db:
        rows = (await db.execute(select(MLBackend.id))).scalars().all()
        backend_ids = list(rows)

    results: list[dict] = []
    for backend_id in backend_ids:
        if jitter_max_seconds > 0:
            await asyncio.sleep(random.uniform(0, jitter_max_seconds))
        try:
            async with task_session() as db:
                svc = MLBackendService(db)
                healthy = await svc.check_health(backend_id)
                await db.commit()
                # 重新读取以拿到 fresh state
                fresh = await svc.get(backend_id)
                results.append(
                    {
                        "id": str(backend_id),
                        "state": fresh.state if fresh else "unknown",
                        "healthy": healthy,
                    }
                )
        except Exception as exc:  # noqa: BLE001 — 单个 backend 失败不影响其他
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
