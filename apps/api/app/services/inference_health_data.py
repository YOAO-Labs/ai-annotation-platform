"""Bind Native health operations to the caller's ORM transaction and unit of work."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any
from uuid import UUID

from sqlalchemy import event, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, attributes
from sqlalchemy.orm.util import identity_key

from app.db.models.gpu_backend_membership import GPUBackendMembership
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry

_STAGES = "aap_inference_health_reconciliations"
_NATIVE = "aap_inference_health_data_v1."


@dataclass
class _Reconciliation:
    pool: MLBackendServicePool
    member: MLBackendPoolMember
    params: dict[str, Any]
    pool_values: dict[str, Any]
    member_values: dict[str, Any]


@dataclass(frozen=True, slots=True)
class HealthBackendFact:
    """Health-only registry projection; stored credentials are never hydrated."""

    id: UUID
    state: str
    is_interactive: bool
    gpu_resource_id: str | None
    last_checked_at: datetime | None
    health_meta: dict[str, Any] | None
    endpoint_sha256: str


_HEALTH_BACKEND_FIELDS = {
    "id",
    "state",
    "is_interactive",
    "gpu_resource_id",
    "last_checked_at",
    "health_meta",
    "endpoint_sha256",
}
_HEALTH_ENDPOINT_NATIVE = "aap_inference_backend_endpoint_v1."


def _health_backend_fact(value) -> HealthBackendFact | None:
    if value is None:
        return None
    if type(value) is str:
        try:
            value = json.loads(value)
        except (TypeError, ValueError, UnicodeError):
            raise RuntimeError(
                "Native health backend projection is malformed"
            ) from None
    if type(value) is not dict or set(value) != _HEALTH_BACKEND_FIELDS:
        raise RuntimeError("Native health backend projection is malformed")
    identity = value["id"]
    if not isinstance(identity, UUID):
        try:
            identity = UUID(identity)
        except (TypeError, ValueError, AttributeError):
            raise RuntimeError("Native health backend identity is malformed") from None
    resource = value["gpu_resource_id"]
    if resource is not None and type(resource) is not str:
        raise RuntimeError("Native health GPU identity is malformed")
    timestamp = value["last_checked_at"]
    if timestamp is not None and not isinstance(timestamp, datetime):
        if type(timestamp) is not str:
            raise RuntimeError("Native health timestamp is malformed")
        try:
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            raise RuntimeError("Native health timestamp is malformed") from None
    metadata = value["health_meta"]
    if metadata is not None and type(metadata) is not dict:
        raise RuntimeError("Native health metadata is malformed")
    digest = value["endpoint_sha256"]
    if (
        type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise RuntimeError("Native endpoint identity is malformed")
    if type(value["state"]) is not str or type(value["is_interactive"]) is not bool:
        raise RuntimeError("Native health state is malformed")
    return HealthBackendFact(
        id=identity,
        state=value["state"],
        is_interactive=value["is_interactive"],
        gpu_resource_id=resource,
        last_checked_at=timestamp,
        health_meta=metadata,
        endpoint_sha256=digest,
    )


def _synchronise(row, applied):
    # Later caller edits still belong to the ordinary ORM unit of work.
    for name, value in applied.items():
        current = getattr(row, name)
        changed_later = current != value
        attributes.set_committed_value(row, name, value)
        if changed_later:
            setattr(row, name, current)


@event.listens_for(Session, "before_flush")
def _flush_health_reconciliations(session, flush_context, instances):
    stages = session.info.get(_STAGES, {})
    for key, stage in list(stages.items()):
        if (
            instances is not None
            and stage.pool not in instances
            and stage.member not in instances
        ):
            continue
        del stages[key]
        if (
            stage.pool not in session
            or stage.member not in session
            or stage.pool in session.deleted
            or stage.member in session.deleted
        ):
            continue
        operation = stage.params["operation"]
        # The closed literal also identifies the legacy write macro in SQL traces.
        statement = text(
            "SELECT * FROM " + _NATIVE + "apply_reconciliation("
            ":registry_id,:pool_id,'" + operation + "',:snapshot,:fingerprint,"
            ":generation,:pool_fingerprint,:member_state)"
        )
        stamps = session.connection().execute(statement, stage.params).one()
        _synchronise(stage.pool, stage.pool_values)
        _synchronise(stage.member, stage.member_values)
        for row, stamp in (
            (stage.pool, stamps.pool_updated_at),
            (stage.member, stamps.member_updated_at),
        ):
            if (
                stamp is not None
                and not attributes.get_history(row, "updated_at").has_changes()
            ):
                attributes.set_committed_value(row, "updated_at", stamp)


@event.listens_for(Session, "after_soft_rollback")
def _discard_health_reconciliations(session, previous_transaction):
    session.info.pop(_STAGES, None)


class InferenceHealthData:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _backend(self, registry_id, *, locked=False, autoflush=True):
        if autoflush and self.db.autoflush:
            await self.db.flush()
        value = await self.db.scalar(
            text(
                "SELECT "
                + _HEALTH_ENDPOINT_NATIVE
                + "health_backend_row(:registry_id,:for_update)"
            ),
            {"registry_id": registry_id, "for_update": locked},
        )
        return _health_backend_fact(value)

    async def get_backend(self, registry_id):
        return await self._backend(registry_id)

    async def read_membership(self, registry_id, gpu_resource_id, *, for_update=False):
        statement = select(
            GPUBackendMembership.membership_epoch, GPUBackendMembership.state
        ).from_statement(
            text(
                "SELECT * FROM "
                + _NATIVE
                + "membership_row(:registry_id,:resource_id,"
                + ("true" if for_update else "false")
                + ")"
            )
        )
        return (
            await self.db.execute(
                statement,
                {
                    "registry_id": registry_id,
                    "resource_id": gpu_resource_id,
                },
            )
        ).one_or_none()

    async def clock_timestamp(self):
        return await self.db.scalar(text("SELECT " + _NATIVE + "observation_clock()"))

    async def lock_current_backend(self, registry_id):
        return await self._backend(registry_id, locked=True)

    async def lock_gpu_resource(self, resource_id):
        await self.db.execute(
            text("SELECT " + _NATIVE + "lock_resource(:resource_id)"),
            {"resource_id": resource_id},
        )

    async def try_membership_promotion(self):
        return await self.db.scalar(text("SELECT " + _NATIVE + "try_promotion()"))

    async def write_health(self, registry_id, values):
        # ORM UPDATE autoflushed pending caller work at this same phase.
        if self.db.autoflush:
            await self.db.flush()
        return await self.db.scalar(
            text(
                "SELECT " + _NATIVE + "write_backend("
                ":registry_id,:state,:observed_at,:health_meta,:interactive,:has_interactive)"
            ),
            {
                "registry_id": registry_id,
                "state": values["state"],
                "observed_at": values["last_checked_at"],
                "health_meta": json.dumps(values["health_meta"]),
                "interactive": values.get("is_interactive"),
                "has_interactive": "is_interactive" in values,
            },
        )

    async def refresh_backend(self, backend):
        # Refresh only the non-secret fields changed by Native health writes.
        registry_id = backend.id
        with self.db.no_autoflush:
            fresh = await self._backend(registry_id, autoflush=False)
        if fresh is None:
            return
        cached = self.db.sync_session.identity_map.get(
            identity_key(MLBackendRegistry, ident=registry_id)
        )
        if cached is None:
            return
        state = inspect(cached)
        for name in ("state", "is_interactive", "health_meta", "last_checked_at"):
            preserve = state.attrs[name].history.has_changes()
            current = state.dict.get(name)
            attributes.set_committed_value(cached, name, getattr(fresh, name))
            if preserve:
                setattr(cached, name, current)

    async def read_member_pool(self, registry_id):
        statement = select(MLBackendPoolMember.pool_id).from_statement(
            text("SELECT " + _NATIVE + "member_pool(:registry_id) AS pool_id")
        )
        return await self.db.scalar(statement, {"registry_id": registry_id})

    def _discard_stage(self, *, pool_id=None, registry_id=None):
        stages = self.db.sync_session.info.get(_STAGES, {})
        for key, stage in list(stages.items()):
            if key == registry_id or stage.params["pool_id"] == pool_id:
                del stages[key]

    async def load_pool_member(self, registry_id):
        statement = (
            select(MLBackendPoolMember)
            .from_statement(
                text("SELECT * FROM " + _NATIVE + "member_row(:registry_id)")
            )
            .execution_options(populate_existing=True)
        )
        row = await self.db.scalar(statement, {"registry_id": registry_id})
        self._discard_stage(registry_id=registry_id)
        return row

    async def get_pool_reference(self, pool_id):
        statement = (
            select(MLBackendServicePool)
            .from_statement(
                text(
                    "SELECT * FROM aap_inference_v1.ai_mask_pool_reference_row(:pool_id)"
                )
            )
            .execution_options(populate_existing=False)
        )
        return await self.db.scalar(statement, {"pool_id": pool_id})

    async def load_pool(self, pool_id, *, registry_id):
        statement = (
            select(MLBackendServicePool)
            .from_statement(
                text("SELECT * FROM " + _NATIVE + "pool_row(:pool_id,:registry_id)")
            )
            .execution_options(populate_existing=True)
        )
        row = await self.db.scalar(
            statement, {"pool_id": pool_id, "registry_id": registry_id}
        )
        self._discard_stage(pool_id=pool_id)
        return row

    async def stage_pool_reconciliation(
        self,
        registry_id,
        pool,
        member,
        *,
        operation,
        snapshot,
        fingerprint,
        expected_generation,
        expected_pool_fingerprint,
        expected_member_state,
    ):
        if operation not in {"seed", "disable"}:
            raise ValueError("unknown health reconciliation operation")
        pool_values = {"routing_generation": expected_generation + 1}
        member_values = {}
        if operation == "seed":
            pool_values.update(
                capability_snapshot=deepcopy(snapshot),
                capability_fingerprint=fingerprint,
            )
        else:
            member_values["traffic_state"] = "disabled"
            if pool.legacy_instance_id == registry_id:
                pool_values["enabled"] = False
        stage = _Reconciliation(
            pool,
            member,
            {
                "registry_id": registry_id,
                "pool_id": pool.id,
                "operation": operation,
                "snapshot": json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
                "fingerprint": fingerprint,
                "generation": expected_generation,
                "pool_fingerprint": expected_pool_fingerprint,
                "member_state": expected_member_state,
            },
            pool_values,
            member_values,
        )
        self.db.sync_session.info.setdefault(_STAGES, {})[registry_id] = stage

    async def flush(self):
        await self.db.flush()
