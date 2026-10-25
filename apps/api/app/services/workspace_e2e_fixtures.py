"""Bind guarded test fixtures to fixed Workspace owner operations."""

from __future__ import annotations

import json
import secrets
import uuid

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aap_workspace.e2e_fixture_contract import (
    E2EProjectRecord,
    E2ETemplateRecord,
    decode_fixture_cleanup_guard,
    decode_fixture_count,
    decode_fixture_project_id,
)
from app.services.project_access import get_workspace_project


async def create_fixture_project(
    db,
    *,
    owner_id,
    name="E2E Project",
    display_id=None,
    type_key="image-det",
    type_label="图像目标检测",
    data_type="image",
    tool_bindings=None,
    scene_mode=False,
):
    if tool_bindings is None:
        from tests.factory import build_tool_bindings

        tool_bindings = build_tool_bindings(["car", "person"])
    record = E2EProjectRecord(
        id=uuid.uuid4(),
        display_id=display_id or f"P-E2E-{secrets.token_hex(3)}",
        name=name,
        type_label=type_label,
        type_key=type_key,
        data_type=data_type,
        owner_id=owner_id,
        tool_bindings=tool_bindings,
        scene_mode=scene_mode,
        ai_enabled=False,
    )
    await db.flush()
    raw = await db.scalar(
        text("SELECT aap_workspace_fixture_v1.create_project(CAST(:project AS jsonb))"),
        {"project": record.model_dump_json()},
    )
    project_id = decode_fixture_project_id(raw, record.id)
    project = await get_workspace_project(db, project_id=project_id)
    if project is None:
        raise HTTPException(status_code=503, detail="项目测试种子服务暂不可用")
    return project


async def create_fixture_templates(db, records):
    records = [E2ETemplateRecord(id=uuid.uuid4(), **record) for record in records]
    await db.flush()
    raw = await db.scalar(
        text(
            "SELECT aap_workspace_fixture_v1.create_templates(CAST(:templates AS jsonb))"
        ),
        {
            "templates": json.dumps(
                [record.model_dump(mode="json") for record in records]
            )
        },
    )
    count = decode_fixture_count(raw, expected_max=len(records))
    if count != len(records):
        raise HTTPException(status_code=503, detail="项目模板测试种子服务暂不可用")
    return records


async def guard_fixture_project_cleanup(db, project_ids):
    if not project_ids:
        return
    try:
        raw = await db.scalar(
            text(
                "SELECT aap_workspace_fixture_v1.guard_project_cleanup(CAST(:ids AS uuid[]))"
            ),
            {"ids": project_ids},
        )
    except DBAPIError as exc:
        state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if state == "55P03":
            raise HTTPException(
                status_code=409, detail={"reason": "project_busy"}
            ) from exc
        raise
    if not decode_fixture_cleanup_guard(raw):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "video_execution_not_retired",
                "message": "存在尚未退役的视频执行，请先取消并完成退役后重试",
            },
        )
