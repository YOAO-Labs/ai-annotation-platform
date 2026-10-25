"""Bind Workspace project snapshots and adjacent writes to caller transactions."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import make_transient_to_detached
from sqlalchemy.orm.attributes import set_committed_value

from aap_workspace.project_maintenance_contract import (
    decode_import_project_reference,
    decode_point_quality_config_write,
)
from aap_workspace.seed_project_contract import (
    decode_nuscenes_seed_defaults_write,
    decode_point_cloud_seed_project_created,
)
from aap_workspace_contract.project_lock import (
    ProjectLockUnavailable,
    decode_project_lock_reply,
)
from aap_workspace_contract.project_query_contract import ProjectQueryUnavailable
from aap_workspace_contract.project_query_values import materialize_project_base_row
from app.db.models.project import Project

_LOCK_PROJECT_ROW = text(
    "SELECT aap_workspace_v1.lock_project_row("
    "CAST(:project_id AS uuid), CAST(:nowait AS boolean))"
)
_LOCK_PROJECT_REFERENCES_FOR_UPDATE = text(
    "SELECT aap_workspace_v1.lock_project_references_for_update("
    "CAST(:project_ids AS uuid[]))"
)


async def merge_project_snapshot(db: AsyncSession, model, values: dict[str, object]):
    """Merge a complete Workspace row into the caller's identity map without SQL."""
    mapped_columns = {column.key for column in model.__table__.columns}
    if set(values) != mapped_columns:
        raise ValueError("Workspace snapshot does not match ORM mapped columns")
    detached = model(**values)
    make_transient_to_detached(detached)
    return await db.merge(detached, load=False)


async def lock_project_row(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    nowait: bool,
    populate_existing: bool,
) -> Project | None:
    """Lock and materialize one current Project row in the caller's transaction."""
    if db.autoflush:
        await db.flush()
    try:
        raw = await db.scalar(
            _LOCK_PROJECT_ROW,
            {"project_id": project_id, "nowait": nowait},
        )
        row_json = decode_project_lock_reply(
            raw,
            project_id=project_id,
            nowait=nowait,
        )
        values = materialize_project_base_row(row_json, target_id=project_id)
    except (ProjectLockUnavailable, ProjectQueryUnavailable):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="项目锁定服务暂不可用",
        ) from None
    if values is None:
        return None

    if populate_existing:
        return await merge_project_snapshot(db, Project, values)

    identity_key = Project.__mapper__.identity_key_from_primary_key((project_id,))
    project = db.sync_session.identity_map.get(identity_key)
    if project is None:
        return await merge_project_snapshot(db, Project, values)

    # A normal ORM SELECT refreshes expired/unloaded fields but leaves loaded and
    # locally dirty fields alone unless populate_existing was requested. The
    # complete row has already been decoded above, so hydrate only those fields
    # without issuing a second SELECT or disturbing dirty attributes.
    unloaded = inspect(project).unloaded
    for key, value in values.items():
        if key in unloaded:
            set_committed_value(project, key, value)
    return project


async def lock_project_references_for_update(
    db: AsyncSession,
    project_ids: list[uuid.UUID],
) -> list[uuid.UUID]:
    """Lock one sorted Project ID set in the caller's existing transaction."""
    if db.autoflush:
        await db.flush()
    raw = await db.scalar(
        _LOCK_PROJECT_REFERENCES_FOR_UPDATE,
        {"project_ids": project_ids},
    )
    requested_project_ids = set(project_ids)
    if not isinstance(raw, list) or any(
        not isinstance(project_id, uuid.UUID) for project_id in raw
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="项目锁定服务暂不可用",
        )
    if (
        raw != sorted(raw)
        or len(raw) != len(set(raw))
        or any(project_id not in requested_project_ids for project_id in raw)
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="项目锁定服务暂不可用",
        )
    return raw


async def find_import_project(db: AsyncSession, display_id: str) -> uuid.UUID | None:
    """Resolve the stable nuScenes display ID without taking transaction ownership."""
    if db.autoflush:
        await db.flush()
    raw = await db.scalar(
        text("SELECT aap_workspace_v1.read_import_project(CAST(:display_id AS text))"),
        {"display_id": display_id},
    )
    return decode_import_project_reference(raw, display_id=display_id)


async def read_import_project(db: AsyncSession, display_id: str) -> Project | None:
    """Retain SELECT hydration, including expired fields and loaded dirty values."""
    project_id = await find_import_project(db, display_id)
    if project_id is None:
        return None
    row_json = await db.scalar(
        text("SELECT aap_workspace_v1.read_project_row(CAST(:project_id AS uuid))"),
        {"project_id": project_id},
    )
    try:
        values = materialize_project_base_row(row_json, target_id=project_id)
    except ProjectQueryUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="项目查询服务暂不可用",
        ) from None
    if values is None:
        return None
    identity_key = Project.__mapper__.identity_key_from_primary_key((project_id,))
    project = db.sync_session.identity_map.get(identity_key)
    if project is None:
        return await merge_project_snapshot(db, Project, values)
    for key in set(inspect(project).unloaded):
        set_committed_value(project, key, values[key])
    return project


async def create_import_project(
    db: AsyncSession,
    *,
    display_id: str,
    name: str,
    owner_id: uuid.UUID,
    tool_bindings: dict | None,
) -> uuid.UUID:
    """Create the import project in the caller's transaction and return its ID."""
    project_id = uuid.uuid4()
    # The ORM creator explicitly flushed all pending work, including within
    # no_autoflush. Keep that ordering before the owner function call.
    await db.flush()
    raw = await db.scalar(
        text(
            "SELECT aap_workspace_v1.create_import_project("
            "CAST(:project_id AS uuid), CAST(:display_id AS text), "
            "CAST(:name AS text), CAST(:owner_id AS uuid), "
            "CAST(:tool_bindings AS jsonb))"
        ),
        {
            "project_id": project_id,
            "display_id": display_id,
            "name": name,
            "owner_id": owner_id,
            "tool_bindings": json.dumps(
                tool_bindings or {}, ensure_ascii=True, allow_nan=False
            ),
        },
    )
    created_project_id = decode_import_project_reference(
        raw, display_id=display_id, expected_project_id=project_id
    )
    assert created_project_id is not None
    return created_project_id


async def set_point_cloud_quality_config(
    db: AsyncSession,
    *,
    project: Project,
    config: dict[str, Any],
) -> datetime:
    """Write through Workspace while synchronizing this transaction's ORM row."""
    project_id = project.id
    # The former final ORM flush persisted this Project change together with
    # the evaluation update and any other caller-owned pending work. Flush the
    # pre-existing unit before the native write; the workflow still flushes the
    # evaluation update afterward, within the same transaction.
    await db.flush()
    raw = await db.scalar(
        text(
            "SELECT aap_workspace_v1.write_point_cloud_quality_config("
            "CAST(:project_id AS uuid), CAST(:config AS jsonb))"
        ),
        {
            "project_id": project_id,
            "config": json.dumps(config, ensure_ascii=True, allow_nan=False),
        },
    )
    updated_at = decode_point_quality_config_write(raw, project_id=project_id)
    set_committed_value(project, "point_cloud_quality_config", config)
    set_committed_value(project, "updated_at", updated_at)
    return updated_at


async def create_point_cloud_seed_project(
    db: AsyncSession,
    *,
    display_id: str,
    name: str,
    owner_id: uuid.UUID,
    tool_bindings: dict,
) -> uuid.UUID:
    """Insert the directory seed with its original non-scene defaults."""
    project_id = uuid.uuid4()
    # The original seed flushed the pending Dataset with the new Project,
    # including inside no_autoflush, before spatial setup or object writes.
    await db.flush()
    raw = await db.scalar(
        text(
            "SELECT aap_workspace_v1.create_point_cloud_seed_project("
            "CAST(:project_id AS uuid), CAST(:display_id AS text), "
            "CAST(:name AS text), CAST(:owner_id AS uuid), "
            "CAST(:tool_bindings AS jsonb))"
        ),
        {
            "project_id": project_id,
            "display_id": display_id,
            "name": name,
            "owner_id": owner_id,
            "tool_bindings": json.dumps(
                tool_bindings, ensure_ascii=True, allow_nan=False
            ),
        },
    )
    return decode_point_cloud_seed_project_created(
        raw, display_id=display_id, expected_project_id=project_id
    )


async def set_nuscenes_seed_defaults(
    db: AsyncSession,
    *,
    project: Project,
    name: str,
    tool_bindings: dict,
) -> datetime | None:
    """Reassert the four seed fields and synchronize the existing identity cache."""
    project_id = project.id
    await db.flush()
    raw = await db.scalar(
        text(
            "SELECT aap_workspace_v1.write_nuscenes_seed_defaults("
            "CAST(:project_id AS uuid), CAST(:name AS text), "
            "CAST(:tool_bindings AS jsonb))"
        ),
        {
            "project_id": project_id,
            "name": name,
            "tool_bindings": json.dumps(
                tool_bindings, ensure_ascii=True, allow_nan=False
            ),
        },
    )
    updated_at = decode_nuscenes_seed_defaults_write(raw, project_id=project_id)
    if updated_at is not None:
        for key, value in {
            "name": name,
            "scene_mode": True,
            "prefer_same_scene_continuation": True,
            "tool_bindings": tool_bindings,
            "updated_at": updated_at,
        }.items():
            set_committed_value(project, key, value)
    return updated_at
