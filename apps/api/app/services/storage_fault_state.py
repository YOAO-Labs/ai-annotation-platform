"""Bounded source observations; never commit or lock a caller's business rows."""

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import uuid

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models.audit_log import AuditLog
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.dataset_source import DatasetSource, DatasetSourceItem
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_fault import StorageFaultState as State
from app.db.models.storage_maintenance import StorageMaintenanceGate
from app.db.models.user import User
from app.services import storage_access, storage_location

log = logging.getLogger(__name__)
SEQUENCE = "storage_fault_observation_seq"
ACTION = "storage.fault"
RECOVERED_ACTION = "storage.fault.recovered"


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


async def common_facts(db, location, source, node_id, anchor, maintenance_revision):
    connection = (
        await db.get(StorageConnection, location.connection_id, populate_existing=True)
        if location.connection_id
        else None
    )
    return {
        "location_revision": location.config_revision,
        "source_revision": source.config_revision,
        "connection_revision": connection.revision if connection else None,
        "maintenance_revision": maintenance_revision,
        "node_id": node_id,
        "anchor": digest(anchor),
        "locator": digest([location.kind, location.config, source.config]),
    }


async def preview_facts(db, location, source):
    binding = await db.get(StorageBinding, "media-cache", populate_existing=True)
    return {
        "source_policy_revision": source.policy_revision,
        "source_policy": source.cache_policy,
        "location_policy": location.config.get("cache_policy", "preview"),
        "binding": [
            str(binding.location_id),
            binding.generation,
            digest(binding.bucket),
        ]
        if binding
        else None,
    }


@dataclass(frozen=True)
class Scope:
    key: str
    category: str
    facts: dict


@dataclass(frozen=True)
class Observation:
    sequence: int
    location_id: uuid.UUID
    source_id: uuid.UUID
    operation: str
    node_id: str
    scopes: tuple[Scope, ...]
    evidence: dict
    actor_id: str | None
    request_id: str | None


async def _bounds(connection):
    await connection.execute(text("SET LOCAL lock_timeout = '250ms'"))
    await connection.execute(text("SET LOCAL statement_timeout = '1s'"))


async def prepare(
    db,
    *,
    location=None,
    source=None,
    location_id=None,
    source_id=None,
    operation,
    node_id,
    anchor=None,
    maintenance_revision,
    path_digest=None,
    expected=None,
    asset_id=None,
    target=None,
    preview_spec=None,
    root_only=False,
    actor_id=None,
    request_id=None,
    evidence=None,
    expected_context=None,
    preview_dependencies=None,
):
    """Reserve order before source I/O, using an independent caller Engine."""
    try:
        async with asyncio.timeout(2):
            async with storage_access.engine(db).begin() as connection:
                await _bounds(connection)
                async with AsyncSession(
                    bind=connection, expire_on_commit=False, autoflush=False
                ) as independent:
                    location = location or await independent.get(
                        StorageLocation, location_id
                    )
                    source = source or await independent.get(DatasetSource, source_id)
                    if location is None or source is None:
                        return None
                    if expected_context is not None and (
                        source.config_revision != expected_context["source_revision"]
                        or location.config_revision
                        != expected_context["location_revision"]
                    ):
                        return None
                    if anchor is None:
                        anchor = await storage_location.node_anchor(
                            independent, location, node_id
                        )
                    common = await common_facts(
                        independent,
                        location,
                        source,
                        node_id,
                        anchor,
                        maintenance_revision,
                    )
                    facts = {"common": common, "path_digest": path_digest}
                    categories = {"root_unavailable": {"common": common}}
                    if not root_only:
                        categories["access_denied"] = {
                            **facts,
                            "operation": operation,
                            "target": digest(target),
                        }
                        if expected is not None:
                            categories["source_changed"] = {
                                **facts,
                                "content": digest(expected),
                                "expected_sha256": expected
                                if isinstance(expected, str)
                                else None,
                            }
                    if preview_spec is not None:
                        categories = {
                            "preview_failed": {
                                **facts,
                                "asset_id": str(asset_id),
                                "expected_sha256": expected,
                                "spec": preview_spec,
                                "dependencies": preview_dependencies
                                if preview_dependencies is not None
                                else await preview_facts(independent, location, source),
                            }
                        }
                    scopes = tuple(
                        Scope(
                            digest([str(location.id), str(source.id), category, data]),
                            category,
                            data,
                        )
                        for category, data in categories.items()
                    )
                    sequence = await connection.scalar(
                        text("SELECT nextval('storage_fault_observation_seq')")
                    )
        details = dict(evidence or {})
        details.setdefault(
            "dataset_id", str(source.dataset_id) if source.dataset_id else None
        )
        return Observation(
            sequence,
            location.id,
            source.id,
            operation,
            node_id,
            scopes,
            details,
            actor_id,
            request_id,
        )
    except Exception:
        log.warning("Storage fault observation could not be prepared")
        return None


async def _audit(connection, token, fault_id, category, code, transition):
    detail = {
        **token.evidence,
        "location_id": str(token.location_id),
        "source_id": str(token.source_id),
        "category": category,
        "code": code,
        "operation": token.operation,
        "fault_id": str(fault_id),
        "transition": transition,
    }
    if token.actor_id:
        detail["actor_id"] = token.actor_id
    await connection.execute(
        insert(AuditLog).values(
            actor_id=select(User.id).where(User.id == token.actor_id).scalar_subquery()
            if token.actor_id
            else None,
            action=RECOVERED_ACTION if transition == "recovered" else ACTION,
            target_type="storage_location",
            target_id=str(token.location_id),
            request_id=token.request_id,
            detail_json=detail,
        )
    )


async def _record(db, token, fault):
    table = State.__table__
    scopes = [
        scope for scope in token.scopes if fault is None or scope.category == fault[0]
    ]
    async with storage_access.engine(db).begin() as connection:
        await _bounds(connection)
        for scope in scopes:
            if fault is not None:
                identifier = uuid.uuid4()
                created = await connection.scalar(
                    pg_insert(table)
                    .values(
                        id=identifier,
                        scope_key=scope.key,
                        location_id=token.location_id,
                        source_id=token.source_id,
                        category=scope.category,
                        operation=token.operation,
                        node_id=token.node_id,
                        state="active",
                        first_failed_at=func.clock_timestamp(),
                        last_failed_at=func.clock_timestamp(),
                        failure_count=1,
                        last_sequence=token.sequence,
                        scope_facts=scope.facts,
                        evidence=token.evidence,
                    )
                    .on_conflict_do_nothing(index_elements=[table.c.scope_key])
                    .returning(table.c.id)
                )
                if created is not None:
                    await _audit(
                        connection,
                        token,
                        identifier,
                        scope.category,
                        fault[1],
                        "opened",
                    )
                    continue
            row = (
                (
                    await connection.execute(
                        select(table)
                        .where(table.c.scope_key == scope.key)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["last_sequence"] >= token.sequence:
                continue
            values = {
                "last_sequence": token.sequence,
                "evidence": token.evidence,
                "operation": token.operation,
            }
            if fault is not None:
                values.update(
                    state="active",
                    last_failed_at=func.clock_timestamp(),
                    failure_count=row["failure_count"] + 1,
                )
                transition = "reopened" if row["state"] == "recovered" else None
            else:
                transition = "recovered" if row["state"] == "active" else None
                if transition:
                    values.update(
                        state="recovered",
                        recovered_at=func.clock_timestamp(),
                        recovered_sequence=token.sequence,
                    )
            await connection.execute(
                update(table).where(table.c.id == row["id"]).values(**values)
            )
            if transition:
                await _audit(
                    connection,
                    token,
                    row["id"],
                    scope.category,
                    fault[1] if fault else "recovered",
                    transition,
                )


async def record(db, token, fault=None):
    if token is None or (
        fault is not None
        and not any(scope.category == fault[0] for scope in token.scopes)
    ):
        return False
    try:
        async with asyncio.timeout(2):
            await _record(db, token, fault)
        return True
    except Exception:
        log.warning("Storage fault observation could not be persisted")
        return False


async def scope_status(db, row, location, source):
    """Compare saved DB facts only; this is not a source-health probe."""
    if (
        source is None
        or not source.enabled
        or source.unregistered_at
        or source.dataset_id is None
        or not location.enabled
        or source.location_id != location.id
    ):
        return "historical"
    gate = await db.get(StorageMaintenanceGate, location.id, populate_existing=True)
    anchor = await storage_location.node_anchor(db, location, row.node_id)
    current = await common_facts(
        db, location, source, row.node_id, anchor, gate.revision if gate else 1
    )
    saved = row.scope_facts
    if current != saved.get("common"):
        return "historical"
    if row.category == "preview_failed" and saved.get(
        "dependencies"
    ) != await preview_facts(db, location, source):
        return "historical"
    if row.category in {"source_changed", "preview_failed"}:
        mapping = await db.get(
            DatasetSourceItem,
            (source.id, saved.get("path_digest")),
            populate_existing=True,
        )
        if mapping is None or not saved.get("expected_sha256"):
            return "unknown"
        if mapping.content_sha256 != saved["expected_sha256"] or (
            saved.get("asset_id") and str(mapping.asset_id) != saved["asset_id"]
        ):
            return "historical"
    return "current"


async def visible_page(db, location, actor, *, state, limit, offset):
    from app.db.enums import UserRole
    from app.services import dataset_source_access as access
    from app.db.models.async_job import AsyncJob

    query = (
        select(State, DatasetSource)
        .outerjoin(DatasetSource, DatasetSource.id == State.source_id)
        .where(State.location_id == location.id)
    )
    if actor.role != UserRole.SUPER_ADMIN:
        query = query.where(
            access.source_scope_clause(actor), DatasetSource.location_id == location.id
        )
    relation = query.subquery()
    counts = {"active": 0, "recovered": 0}
    for value, count in (
        await db.execute(
            select(relation.c.state, func.count()).group_by(relation.c.state)
        )
    ).all():
        counts[value] = count
    if state != "all":
        query = query.where(State.state == state)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await db.execute(
            query.order_by(State.last_failed_at.desc(), State.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    job_ids = {row.evidence.get("job_id") for row, _source in rows} - {None}
    job_query = select(AsyncJob.id).where(AsyncJob.id.in_(job_ids))
    if actor.role != UserRole.SUPER_ADMIN:
        job_query = job_query.where(
            AsyncJob.kind.in_(["dataset_scan", "dataset_registration"]),
            access.job_scope_clause(actor),
        )
    visible_jobs = (
        {str(identifier) for identifier in await db.scalars(job_query)}
        if job_ids
        else set()
    )
    items = []
    for row, source in rows:
        evidence = row.evidence
        item = {
            key: evidence.get(key)
            for key in (
                "dataset_id",
                "item_id",
                "asset_id",
                "task_id",
                "run_id",
                "job_id",
                "registration_id",
            )
        }
        item["dataset_id"] = source.dataset_id if source else None
        if item["job_id"] not in visible_jobs:
            item["job_id"] = None
        items.append(
            {
                **item,
                "id": row.id,
                "location_id": row.location_id,
                "source_id": row.source_id,
                "source_name": source.name if source else None,
                "node_id": row.node_id,
                "category": row.category,
                "operation": row.operation,
                "state": row.state,
                "first_failed_at": row.first_failed_at,
                "last_failed_at": row.last_failed_at,
                "recovered_at": row.recovered_at,
                "failure_count": row.failure_count,
                "scope_status": await scope_status(db, row, location, source),
                "responsible_role": "platform_admin"
                if row.category in {"root_unavailable", "access_denied"}
                else "dataset_admin",
                "responsible_user_id": None,
            }
        )
    return {"items": items, "total": total, "counts": counts, "observation_only": True}
