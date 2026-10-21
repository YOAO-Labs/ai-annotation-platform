"""Bind Inference Registry ingress to the API caller's ORM transaction."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import DateTime, event, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, attributes, object_session

from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry

_NATIVE = "aap_inference_registry_ingress_v1."
_STAGES = "aap_inference_registry_ingress_stages"


@dataclass
class _ObservationStage:
    row: MLBackendRegistry
    profile: Literal["env", "seed"]
    state: str
    name: Any
    is_interactive: bool
    health_meta: Any
    last_checked_at: Any
    error_message: str | None
    usable_caps: bool
    expected_snapshot: dict[str, Any]


@dataclass
class _EnvironmentDisconnectStage:
    row: MLBackendRegistry
    expected_url: str
    expected_state: str


@dataclass
class _SeedMarkerStage:
    row: MLBackendRegistry
    marker: dict[str, Any]
    database_extra_params: Any
    previous_extra_params: Any
    staged_extra_params: Any


def _session_stages(session: Session) -> list[Any]:
    return session.info.setdefault(_STAGES, [])


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _set_applied_value(row: Any, name: str, applied: Any, staged: Any) -> None:
    current = getattr(row, name)
    changed_later = current != staged
    attributes.set_committed_value(row, name, applied)
    if changed_later:
        setattr(row, name, current)


def _merge_seed_extra(stage: _SeedMarkerStage, native_extra: Any) -> Any:
    current_extra = stage.row.extra_params
    if (
        not isinstance(current_extra, Mapping)
        and current_extra != stage.staged_extra_params
    ):
        return deepcopy(current_extra)

    merged = _mapping(native_extra)
    baseline = _mapping(stage.database_extra_params)
    previous = _mapping(stage.previous_extra_params)
    staged = _mapping(stage.staged_extra_params)
    current = _mapping(current_extra)

    for key, value in previous.items():
        if key != "seed":
            merged[key] = deepcopy(value)
    for key in baseline.keys() - previous.keys():
        if key != "seed":
            merged.pop(key, None)
    for key in staged.keys() - current.keys():
        if key != "seed":
            merged.pop(key, None)
    for key, value in current.items():
        if key != "seed":
            merged[key] = deepcopy(value)
    merged["seed"] = deepcopy(stage.marker)
    return merged


def _discard_ingress_stages(session: Session, row: MLBackendRegistry) -> None:
    stages = session.info.get(_STAGES)
    if not stages:
        return
    remaining = [stage for stage in stages if stage.row is not row]
    if remaining:
        session.info[_STAGES] = remaining
    else:
        session.info.pop(_STAGES, None)


def _execute(session: Session, statement, parameters):
    return session.connection().execute(statement, parameters)


def _apply_observation(session: Session, stage: _ObservationStage) -> None:
    if stage.profile == "env" or stage.usable_caps:
        health_meta_json = json.dumps(stage.health_meta)
    else:
        # Failed seed probes do not serialize or replace the previous snapshot.
        health_meta_json = None
    expected_snapshot_json = json.dumps(
        stage.expected_snapshot,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    statement = text(
        "SELECT " + _NATIVE + "apply_observation("
        ":registry_id,:profile,:state,:name,:interactive,:health_meta,:checked_at,"
        ":error_message,:usable_caps,:expected_snapshot)"
    ).columns(updated_at=DateTime(timezone=True))
    updated_at = _execute(
        session,
        statement,
        {
            "registry_id": stage.row.id,
            "profile": stage.profile,
            "state": stage.state,
            "name": stage.name,
            "interactive": stage.is_interactive,
            "health_meta": health_meta_json,
            "checked_at": stage.last_checked_at,
            "error_message": stage.error_message,
            "usable_caps": stage.usable_caps,
            "expected_snapshot": expected_snapshot_json,
        },
    ).scalar_one()

    row = stage.row
    _set_applied_value(row, "state", stage.state, stage.state)
    _set_applied_value(
        row, "last_checked_at", stage.last_checked_at, stage.last_checked_at
    )
    if stage.profile == "env" or stage.usable_caps:
        _set_applied_value(row, "name", stage.name, stage.name)
        _set_applied_value(
            row, "is_interactive", stage.is_interactive, stage.is_interactive
        )
        _set_applied_value(row, "health_meta", stage.health_meta, stage.health_meta)
    if stage.profile == "seed":
        _set_applied_value(
            row, "error_message", stage.error_message, stage.error_message
        )
    attributes.set_committed_value(row, "updated_at", updated_at)


def _apply_env_disconnect(session: Session, stage: _EnvironmentDisconnectStage) -> None:
    statement = text(
        "SELECT " + _NATIVE + "disconnect_env_registry(:registry_id,:expected_url)"
    ).columns(updated_at=DateTime(timezone=True))
    updated_at = _execute(
        session,
        statement,
        {"registry_id": stage.row.id, "expected_url": stage.expected_url},
    ).scalar_one()
    _set_applied_value(stage.row, "state", "disconnected", stage.expected_state)
    attributes.set_committed_value(stage.row, "updated_at", updated_at)


def _apply_seed_marker(session: Session, stage: _SeedMarkerStage) -> None:
    statement = text(
        "SELECT * FROM " + _NATIVE + "tag_seed_registry(:registry_id,:marker_json)"
    ).columns(
        extra_params=JSONB(),
        updated_at=DateTime(timezone=True),
    )
    result = (
        _execute(
            session,
            statement,
            {
                "registry_id": stage.row.id,
                "marker_json": json.dumps(
                    stage.marker,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        )
        .mappings()
        .one()
    )
    native_extra = result["extra_params"]
    desired_extra = _merge_seed_extra(stage, native_extra)

    attributes.set_committed_value(stage.row, "extra_params", native_extra)
    if desired_extra != native_extra:
        # Ordinary caller-owned extra-param edits remain in the ORM unit of work.
        stage.row.extra_params = desired_extra
    attributes.set_committed_value(stage.row, "updated_at", result["updated_at"])


@event.listens_for(Session, "before_flush")
def _flush_registry_ingress(session, flush_context, instances) -> None:
    stages = session.info.get(_STAGES, [])
    for stage in list(stages):
        if instances is not None and stage.row not in instances:
            continue
        stages.remove(stage)
        if stage.row not in session or stage.row in session.deleted:
            continue
        if isinstance(stage, _ObservationStage):
            _apply_observation(session, stage)
        elif isinstance(stage, _EnvironmentDisconnectStage):
            _apply_env_disconnect(session, stage)
        else:
            _apply_seed_marker(session, stage)
    if not stages:
        session.info.pop(_STAGES, None)


@event.listens_for(Session, "after_soft_rollback")
def _discard_registry_ingress_rollback(session, previous_transaction) -> None:
    session.info.pop(_STAGES, None)


@event.listens_for(MLBackendRegistry, "refresh")
def _discard_registry_ingress_refresh(target, context, attrs) -> None:
    session = object_session(target)
    if session is not None:
        _discard_ingress_stages(session, target)


class InferenceRegistryIngressData:
    """The fixed Registry Native port bound to one API AsyncSession."""

    def __init__(self, db: AsyncSession | None) -> None:
        self.db = db

    def _db(self) -> AsyncSession:
        if self.db is None:
            raise RuntimeError("Registry ingress requires an attached AsyncSession")
        return self.db

    async def by_url(self, url: str) -> MLBackendRegistry | None:
        statement = select(MLBackendRegistry).from_statement(
            text("SELECT * FROM " + _NATIVE + "registry_by_url(:url)")
        )
        return await self._db().scalar(statement, {"url": url})

    async def candidates(
        self, profile: Literal["env", "live"]
    ) -> list[MLBackendRegistry]:
        statement = select(MLBackendRegistry).from_statement(
            text("SELECT * FROM " + _NATIVE + "registry_candidates(:profile)")
        )
        result = await self._db().execute(statement, {"profile": profile})
        return list(result.scalars().all())

    async def create_registry(
        self,
        name: str | None,
        url: str,
        source: Literal["env", "seed"],
        *,
        is_interactive: bool = False,
        extra_params: Any = None,
    ) -> MLBackendRegistry:
        statement = (
            select(MLBackendRegistry)
            .from_statement(
                text(
                    "SELECT * FROM "
                    + _NATIVE
                    + "create_registry(:registry_id,:name,:url,:source,:interactive,:extra_params)"
                )
            )
            .execution_options(autoflush=False)
        )
        row = await self._db().scalar(
            statement,
            {
                "registry_id": uuid4(),
                "name": name,
                "url": url,
                "source": source,
                "interactive": is_interactive,
                "extra_params": json.dumps(
                    {} if extra_params is None else extra_params
                ),
            },
        )
        if row is None:
            raise RuntimeError("Registry ingress create returned no row")
        # The original ORM create flushed after adding the Registry object. The
        # Native insert runs first, then the caller's other pending rows flush.
        await self._db().flush()
        return row

    async def singleton_pool(self, registry_id: UUID) -> MLBackendServicePool | None:
        statement = select(MLBackendServicePool).from_statement(
            text("SELECT * FROM " + _NATIVE + "singleton_pool(:registry_id)")
        )
        return await self._db().scalar(statement, {"registry_id": registry_id})

    async def create_singleton_pool(
        self,
        row: MLBackendRegistry,
        snapshot: dict[str, Any] | None,
        fingerprint: str | None,
    ) -> MLBackendServicePool:
        # Mirrors the old pool helper's explicit flush boundary.
        await self._db().flush()
        statement = (
            select(MLBackendServicePool)
            .from_statement(
                text(
                    "SELECT * FROM " + _NATIVE + "create_singleton_pool("
                    ":registry_id,:pool_id,:member_id,:name,:snapshot,:fingerprint)"
                )
            )
            .execution_options(autoflush=False)
        )
        pool = await self._db().scalar(
            statement,
            {
                "registry_id": row.id,
                "pool_id": uuid4(),
                "member_id": uuid4(),
                "name": row.name,
                "snapshot": json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "fingerprint": fingerprint,
            },
        )
        if pool is None:
            raise RuntimeError("Registry ingress singleton pool create returned no row")
        return pool

    async def stage_observation(
        self,
        row: MLBackendRegistry,
        *,
        profile: Literal["env", "seed"],
        usable_caps: bool,
        expected_snapshot: dict[str, Any],
    ) -> None:
        if self.db is None:
            return
        _session_stages(self.db.sync_session).append(
            _ObservationStage(
                row=row,
                profile=profile,
                state=row.state,
                name=row.name,
                is_interactive=row.is_interactive,
                health_meta=deepcopy(row.health_meta),
                last_checked_at=row.last_checked_at,
                error_message=row.error_message,
                usable_caps=usable_caps,
                expected_snapshot=deepcopy(expected_snapshot),
            )
        )

    async def stage_env_disconnect(self, row: MLBackendRegistry) -> None:
        if self.db is None:
            return
        _session_stages(self.db.sync_session).append(
            _EnvironmentDisconnectStage(
                row=row,
                expected_url=row.url,
                expected_state=row.state,
            )
        )

    async def stage_seed_marker(
        self,
        row: MLBackendRegistry,
        marker: dict[str, Any],
        previous_extra_params: Any,
    ) -> None:
        if self.db is None:
            return
        previous = _mapping(previous_extra_params)
        if previous.get("seed") == marker:
            return
        history = attributes.get_history(row, "extra_params")
        database_extra_params = (
            deepcopy(history.deleted[0])
            if history.deleted
            else deepcopy(previous_extra_params)
        )
        _session_stages(self.db.sync_session).append(
            _SeedMarkerStage(
                row=row,
                marker=deepcopy(marker),
                database_extra_params=database_extra_params,
                previous_extra_params=deepcopy(previous_extra_params),
                staged_extra_params=deepcopy(row.extra_params),
            )
        )

    async def flush(self) -> None:
        if self.db is not None:
            await self.db.flush()

    async def sync_result(
        self, request_id: UUID, command_sha256: str
    ) -> dict[str, Any] | None:
        statement = text(
            "SELECT " + _NATIVE + "sync_result(:request_id,:command_sha256)"
        ).columns(result=JSONB())
        return await self._db().scalar(
            statement,
            {"request_id": request_id, "command_sha256": command_sha256},
        )

    async def record_sync_result(
        self,
        request_id: UUID,
        command_sha256: str,
        observed_urls: int,
    ) -> dict[str, Any]:
        statement = text(
            "SELECT "
            + _NATIVE
            + "record_sync_result(:request_id,:command_sha256,:observed_urls)"
        ).columns(result=JSONB())
        return await self._db().scalar(
            statement,
            {
                "request_id": request_id,
                "command_sha256": command_sha256,
                "observed_urls": observed_urls,
            },
        )


__all__ = ["InferenceRegistryIngressData"]
