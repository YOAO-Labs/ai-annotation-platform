"""Independent native lifecycle calls for managed originals."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aap_assets_source.managed_original_contract import (
    ManagedOriginalMaterial,
    ManagedOriginalOperation,
    decode_managed_original_reply,
)
from app.services import storage_access


async def call(
    db: AsyncSession,
    function: str,
    parameters: dict[str, Any],
    types: dict[str, str],
):
    engine = storage_access.engine(db)
    placeholders = ", ".join(f"CAST(:{name} AS {types[name]})" for name in parameters)
    bound = {
        name: (
            json.dumps(value, ensure_ascii=False, allow_nan=False)
            if types[name] == "jsonb"
            else value
        )
        for name, value in parameters.items()
    }
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            raw = await session.scalar(
                text(f"SELECT {function}({placeholders})"), bound
            )
            await session.commit()
    except SQLAlchemyError as exc:
        raise ValueError("managed_original_storage_unavailable") from exc
    body = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
    if not isinstance(body, dict) or body.get("schema") != "aap-managed-original/v1":
        raise ValueError("invalid managed original response")
    return body


async def ok(db, function, parameters, types):
    body = await call(db, function, parameters, types)
    if body.get("ok") is not True:
        error = body.get("error") or {}
        raise ValueError(error.get("code", "invalid_response"))
    return body.get("data")


async def begin_admitted(
    db: AsyncSession, request: dict[str, Any], *, material_type=ManagedOriginalMaterial
) -> ManagedOriginalMaterial:
    """Allocate independently; this never opens a permit or claims the creation."""
    payload = dict(request)
    payload.setdefault("kind", "role_media")
    payload.setdefault("origin", "connector")
    payload.setdefault("mode", "write")
    payload.setdefault("logical_path", payload.get("file_path"))
    payload.setdefault("file_type", "other")
    payload.setdefault("content_type", "application/octet-stream")
    for name in ("staging", "source_contract"):
        if payload.get(name) is None:
            payload.pop(name, None)
    body = await call(
        db,
        "aap_assets_v1.begin_operator_auxiliary",
        {"request": payload},
        {"request": "jsonb"},
    )
    return decode_managed_original_reply(body, material_type)


async def open_operation(db: AsyncSession, creation_id) -> ManagedOriginalOperation:
    data = await ok(
        db,
        "aap_assets_v1.open_operator_storage_operation",
        {"creation_id": creation_id},
        {"creation_id": "uuid"},
    )
    return ManagedOriginalOperation.model_validate(data)


async def close_operation(db, creation_id, operation_id, physical_settled: bool):
    await ok(
        db,
        "aap_assets_v1.close_operator_storage_operation",
        {
            "creation_id": creation_id,
            "operation_id": operation_id,
            "physical_settled": physical_settled,
        },
        {
            "creation_id": "uuid",
            "operation_id": "uuid",
            "physical_settled": "boolean",
        },
    )


async def prepared(db, creation_id, value: dict):
    await ok(
        db,
        "aap_assets_v1.record_operator_original_prepared",
        {"creation_id": creation_id, "prepared": value},
        {"creation_id": "uuid", "prepared": "jsonb"},
    )


async def writing(db, creation_id):
    await ok(
        db,
        "aap_assets_v1.mark_operator_original_writing",
        {"creation_id": creation_id},
        {"creation_id": "uuid"},
    )


async def written(db, creation_id, receipt: dict):
    await ok(
        db,
        "aap_assets_v1.record_operator_original_written",
        {"creation_id": creation_id, "receipt": receipt},
        {"creation_id": "uuid", "receipt": "jsonb"},
    )


async def verified(db, creation_id, verification: dict):
    await ok(
        db,
        "aap_assets_v1.record_operator_original_verified",
        {"creation_id": creation_id, "verification": verification},
        {"creation_id": "uuid", "verification": "jsonb"},
    )


async def fail(db, creation_id, outcome: str, error_code: str, evidence: dict):
    await ok(
        db,
        "aap_assets_v1.fail_operator_original",
        {
            "creation_id": creation_id,
            "outcome": outcome,
            "error_code": error_code,
            "evidence": evidence,
        },
        {
            "creation_id": "uuid",
            "outcome": "text",
            "error_code": "text",
            "evidence": "jsonb",
        },
    )
