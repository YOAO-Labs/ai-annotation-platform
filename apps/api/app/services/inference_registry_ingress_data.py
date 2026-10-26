"""Bind credential-free screenshot-seed ingress to the caller transaction."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, attributes, object_session

from aap_inference.registry_ingress import RegistrySeedFacts
from aap_inference_contract.topology import PoolFacts
from app.db.models.ml_backend_registry import MLBackendRegistry

_NATIVE = "aap_inference_registry_ingress_v1."
_STAGES = "aap_inference_registry_ingress_stages"
_BASELINES = "aap_inference_registry_ingress_extra_baselines"
_SEED_IDENTITY_FIELDS = (
    "id, name, url, state, is_interactive, source, health_meta, extra_params"
)

_SEED_BY_URL = text(
    "SELECT "
    + _SEED_IDENTITY_FIELDS
    + " FROM "
    + "aap_inference_topology_v1.seed_registry_by_url(:url)"
)
_SEED_CREATE = text(
    "SELECT "
    + _SEED_IDENTITY_FIELDS
    + " FROM "
    + "aap_inference_topology_v1.create_seed_registry(:registry_id,:name,:url,:marker)"
)


@dataclass
class _SeedMarkerStage:
    row: MLBackendRegistry
    marker: dict[str, Any]
    database_extra_params: Any
    previous_extra_params: Any
    staged_extra_params: Any


@dataclass
class _SeedMarkerIdentityStage:
    registry_id: UUID
    marker: dict[str, Any]


def _session_stages(session: Session) -> list[Any]:
    return session.info.setdefault(_STAGES, [])


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


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

    missing = object()
    for before, after, skip_seed in (
        (baseline, previous, True),
        (staged, current, False),
    ):
        for key in before.keys() | after.keys():
            if (skip_seed and key == "seed") or before.get(key, missing) == after.get(
                key, missing
            ):
                continue
            if key in after:
                merged[key] = deepcopy(after[key])
            else:
                merged.pop(key, None)
    return merged


def _set_stamp(row, value):
    if not attributes.get_history(row, "updated_at").has_changes():
        attributes.set_committed_value(row, "updated_at", value)


def _remember_extra(row):
    session = object_session(row)
    if session is not None and "extra_params" in row.__dict__:
        session.info.setdefault(_BASELINES, {})[row.id] = deepcopy(row.extra_params)


@event.listens_for(MLBackendRegistry, "load")
def _remember_loaded_extra(target, context):
    _remember_extra(target)


@event.listens_for(MLBackendRegistry, "after_insert")
def _remember_inserted_extra(mapper, connection, target):
    _remember_extra(target)


@event.listens_for(MLBackendRegistry, "after_update")
def _remember_updated_extra(mapper, connection, target):
    if attributes.get_history(target, "extra_params").has_changes():
        _remember_extra(target)


def _discard_ingress_stages(session: Session, row: MLBackendRegistry) -> None:
    stages = session.info.get(_STAGES)
    if not stages:
        return
    remaining = [
        stage
        for stage in stages
        if not (
            getattr(stage, "row", None) is row
            or (
                isinstance(stage, _SeedMarkerIdentityStage)
                and stage.registry_id == row.id
            )
        )
    ]
    if remaining:
        session.info[_STAGES] = remaining
    else:
        session.info.pop(_STAGES, None)


def _execute(session: Session, statement, parameters):
    return session.connection().execute(statement, parameters)


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
    session.info.setdefault(_BASELINES, {})[stage.row.id] = deepcopy(native_extra)

    attributes.set_committed_value(stage.row, "extra_params", native_extra)
    if desired_extra != native_extra:
        # Ordinary caller-owned extra-param edits remain in the ORM unit of work.
        stage.row.extra_params = desired_extra
    _set_stamp(stage.row, result["updated_at"])


def _apply_seed_marker_identity(
    session: Session, stage: _SeedMarkerIdentityStage
) -> None:
    row = next(
        (
            item
            for item in session.identity_map.values()
            if isinstance(item, MLBackendRegistry) and item.id == stage.registry_id
        ),
        None,
    )
    if (
        row is not None
        and "extra_params" in row.__dict__
        and row not in session.deleted
    ):
        current = deepcopy(row.extra_params)
        staged = {**_mapping(current), "seed": stage.marker}
        database = session.info.get(_BASELINES, {}).get(stage.registry_id, current)
        _apply_seed_marker(
            session,
            _SeedMarkerStage(
                row=row,
                marker=stage.marker,
                database_extra_params=database,
                previous_extra_params=current,
                staged_extra_params=staged,
            ),
        )
        return
    statement = text(
        "SELECT * FROM " + _NATIVE + "tag_seed_registry(:registry_id,:marker_json)"
    ).columns(extra_params=JSONB(), updated_at=DateTime(timezone=True))
    _execute(
        session,
        statement,
        {
            "registry_id": stage.registry_id,
            "marker_json": json.dumps(
                stage.marker, ensure_ascii=False, separators=(",", ":")
            ),
        },
    ).mappings().one()


@event.listens_for(Session, "before_flush")
def _flush_registry_ingress(session: Session, flush_context, instances) -> None:
    stages = session.info.get(_STAGES, [])
    for stage in list(stages):
        if isinstance(stage, _SeedMarkerIdentityStage):
            stages.remove(stage)
            _apply_seed_marker_identity(session, stage)
            continue
        if instances is not None and stage.row not in instances:
            continue
        stages.remove(stage)
        if stage.row not in session or stage.row in session.deleted:
            continue
        _apply_seed_marker(session, stage)
    if not stages:
        session.info.pop(_STAGES, None)


@event.listens_for(Session, "after_soft_rollback")
def _discard_registry_ingress_rollback(session, previous_transaction) -> None:
    session.info.pop(_STAGES, None)
    session.info.pop(_BASELINES, None)


@event.listens_for(MLBackendRegistry, "refresh")
def _discard_registry_ingress_refresh(target, context, attrs) -> None:
    session = object_session(target)
    if session is not None:
        _discard_ingress_stages(session, target)
        if attrs is None or "extra_params" in attrs:
            _remember_extra(target)


class InferenceRegistryIngressData:
    """The fixed Registry Native port bound to one API AsyncSession."""

    def __init__(self, db: AsyncSession | None) -> None:
        self.db = db

    def _db(self) -> AsyncSession:
        if self.db is None:
            raise RuntimeError("Registry ingress requires an attached AsyncSession")
        return self.db

    @staticmethod
    def _seed_facts(row: Mapping[str, Any]) -> RegistrySeedFacts:
        return RegistrySeedFacts(
            id=row["id"],
            name=row["name"],
            url=row["url"],
            state=row["state"],
            is_interactive=row["is_interactive"],
            source=row["source"],
            health_meta=row["health_meta"],
            extra_params=row["extra_params"],
        )

    async def seed_by_url(self, url: str) -> RegistrySeedFacts | None:
        db = self._db()
        if db.autoflush:
            await db.flush()
        result = await db.execute(_SEED_BY_URL, {"url": url})
        row = result.mappings().one_or_none()
        return self._seed_facts(row) if row is not None else None

    async def create_seed_registry(
        self, name: Any, url: str, marker: dict[str, Any]
    ) -> RegistrySeedFacts:
        db = self._db()
        result = await db.execute(
            _SEED_CREATE.execution_options(autoflush=False),
            {
                "registry_id": uuid4(),
                "name": name,
                "url": url,
                "marker": json.dumps(marker),
            },
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise RuntimeError("Registry ingress seed create returned no facts")
        await db.flush()
        return self._seed_facts(row)

    async def singleton_pool(self, registry_id: UUID) -> PoolFacts | None:
        result = await self._db().execute(
            text("SELECT * FROM " + _NATIVE + "singleton_pool(:registry_id)"),
            {"registry_id": registry_id},
        )
        row = result.mappings().one_or_none()
        return PoolFacts.from_record(row) if row is not None else None

    async def create_singleton_pool(
        self,
        row: RegistrySeedFacts,
        snapshot: dict[str, Any] | None,
        fingerprint: str | None,
    ) -> PoolFacts:
        # Mirrors the old pool helper's explicit flush boundary.
        await self._db().flush()
        statement = text(
            "SELECT * FROM " + _NATIVE + "create_singleton_pool("
            ":registry_id,:pool_id,:member_id,:name,:snapshot,:fingerprint)"
        ).execution_options(autoflush=False)
        result = await self._db().execute(
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
        pool = result.mappings().one_or_none()
        if pool is None:
            raise RuntimeError("Registry ingress singleton pool create returned no row")
        return PoolFacts.from_record(pool)

    async def stage_seed_marker_identity(
        self,
        row: RegistrySeedFacts,
        marker: dict[str, Any],
        previous_extra_params: Any,
    ) -> None:
        if self.db is None or _mapping(previous_extra_params).get("seed") == marker:
            return
        _session_stages(self.db.sync_session).append(
            _SeedMarkerIdentityStage(row.id, deepcopy(marker))
        )

    async def flush(self) -> None:
        if self.db is not None:
            await self.db.flush()


__all__ = ["InferenceRegistryIngressData"]
