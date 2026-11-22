"""ADR-0050 §15 · Prometheus metrics for the routing ledger.

All gauges/counters are sourced from the Redis ledger snapshot (aggregated, not
per-process) to avoid multi-process double-counting. Labels use stable UUIDs /
controlled outcomes only — never URL, lease id, or user input (cardinality guard).
"""

from __future__ import annotations

import logging
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger(__name__)

ROUTER_SELECTIONS = Counter(
    "ml_backend_router_selections_total",
    "Route lease acquisitions by pool/instance/outcome",
    ["pool_id", "instance_id", "outcome"],
)
ROUTER_REJECTIONS = Counter(
    "ml_backend_router_rejections_total",
    "Route acquire rejections by pool/reason",
    ["pool_id", "reason"],
)
ROUTER_EJECTIONS = Counter(
    "ml_backend_router_ejections_total",
    "Passive-circuit ejections by pool/instance/reason",
    ["pool_id", "instance_id", "reason"],
)
ROUTED_REQUEST_DURATION = Histogram(
    "ml_backend_routed_request_duration_seconds",
    "Routed request duration by pool/instance/outcome",
    ["pool_id", "instance_id", "outcome"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)
ROUTER_INFLIGHT = Gauge(
    "ml_backend_router_inflight",
    "Active route leases per pool/instance (from Redis snapshot)",
    ["pool_id", "instance_id"],
)
ROUTER_MODE = Gauge(
    "ml_backend_router_mode",
    "Effective router mode (0=off, 1=observe, 2=enforce)",
    [],
)


def record_selection(pool_id: str, instance_id: str, outcome: str) -> None:
    try:
        ROUTER_SELECTIONS.labels(
            pool_id=pool_id, instance_id=instance_id, outcome=outcome
        ).inc()
    except Exception:  # noqa: BLE001 — metrics must never break dispatch
        logger.debug("failed to record selection metric", exc_info=True)


def record_rejection(pool_id: str, reason: str) -> None:
    try:
        ROUTER_REJECTIONS.labels(pool_id=pool_id, reason=reason).inc()
    except Exception:  # noqa: BLE001
        logger.debug("failed to record rejection metric", exc_info=True)


def record_ejection(pool_id: str, instance_id: str, reason: str) -> None:
    try:
        ROUTER_EJECTIONS.labels(
            pool_id=pool_id, instance_id=instance_id, reason=reason
        ).inc()
    except Exception:  # noqa: BLE001
        logger.debug("failed to record ejection metric", exc_info=True)


def record_duration(
    pool_id: str, instance_id: str, outcome: str, seconds: float
) -> None:
    try:
        ROUTED_REQUEST_DURATION.labels(
            pool_id=pool_id, instance_id=instance_id, outcome=outcome
        ).observe(seconds)
    except Exception:  # noqa: BLE001
        logger.debug("failed to record duration metric", exc_info=True)


def set_inflight(snapshots: list[dict[str, Any]]) -> None:
    """Set the inflight gauge from a Redis snapshot list.

    Each snapshot entry: {pool_id, instance_id, inflight}. Called by the periodic
    metrics publisher (or the runtime-snapshot endpoint).
    """
    try:
        ROUTER_INFLIGHT.clear()
        for s in snapshots:
            ROUTER_INFLIGHT.labels(
                pool_id=str(s["pool_id"]), instance_id=str(s["instance_id"])
            ).set(int(s.get("inflight", 0)))
    except Exception:  # noqa: BLE001
        logger.debug("failed to set inflight gauge", exc_info=True)
