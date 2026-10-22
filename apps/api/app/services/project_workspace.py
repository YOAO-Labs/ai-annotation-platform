"""Bind adjacent Workspace project writes to the caller's transaction."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from aap_workspace.project_maintenance_contract import (
    decode_import_project_reference,
    decode_point_quality_config_write,
)
from app.db.models.project import Project


async def find_import_project(db: AsyncSession, display_id: str) -> uuid.UUID | None:
    """Resolve the stable nuScenes display ID without taking transaction ownership."""
    if db.autoflush:
        await db.flush()
    raw = await db.scalar(
        text("SELECT aap_workspace_v1.read_import_project(CAST(:display_id AS text))"),
        {"display_id": display_id},
    )
    return decode_import_project_reference(raw, display_id=display_id)


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
