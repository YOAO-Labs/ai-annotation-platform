"""PostgreSQL owner adapter for external format adapter plugins."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from yoao_labeling.format_adapter_contract import (
    FormatAdapterPluginRecord,
    FormatAdapterPluginSecret,
    format_adapter_public_projection_from_row,
)


class FormatAdapterStoreError(RuntimeError):
    def __init__(self, reason: str, *, status_code: int = 409) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


def _translate_db_error(exc: DBAPIError) -> FormatAdapterStoreError:
    orig = getattr(exc, "orig", exc)
    sqlstate = getattr(orig, "sqlstate", None)
    message = str(orig)
    if sqlstate == "23505":
        return FormatAdapterStoreError("format_adapter_already_exists", status_code=409)
    if sqlstate == "P0002" or "format_adapter_not_found" in message:
        return FormatAdapterStoreError("format_adapter_not_found", status_code=404)
    if sqlstate == "40001" or "format_adapter_revision_conflict" in message:
        return FormatAdapterStoreError(
            "format_adapter_revision_conflict", status_code=409
        )
    if "format_adapter_already_retired" in message:
        return FormatAdapterStoreError(
            "format_adapter_already_retired", status_code=409
        )
    if "format_adapter_status_unchanged" in message:
        return FormatAdapterStoreError(
            "format_adapter_status_unchanged", status_code=409
        )
    if sqlstate in {"23514", "23502", "22023"}:
        return FormatAdapterStoreError(message, status_code=422)
    return FormatAdapterStoreError(message, status_code=500)


class FormatAdapterStorePostgresData:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_plugins(
        self, *, include_retired: bool = False
    ) -> list[FormatAdapterPluginRecord]:
        raw = await self._db.scalar(
            text(
                "SELECT yoao_labeling_v1.list_format_adapter_plugins(:include_retired)"
            ),
            {"include_retired": include_retired},
        )
        rows = raw if isinstance(raw, list) else []
        return [format_adapter_public_projection_from_row(row) for row in rows]

    async def get_plugin(self, format_id: str) -> FormatAdapterPluginRecord | None:
        raw = await self._db.scalar(
            text("SELECT yoao_labeling_v1.get_format_adapter_plugin(:format_id)"),
            {"format_id": format_id},
        )
        if not isinstance(raw, dict):
            return None
        return format_adapter_public_projection_from_row(raw)

    async def register_plugin(
        self, payload: dict[str, Any]
    ) -> FormatAdapterPluginRecord:
        try:
            raw = await self._db.scalar(
                text(
                    "SELECT yoao_labeling_v1.register_format_adapter_plugin("
                    "CAST(:payload AS jsonb))"
                ),
                {"payload": json.dumps(payload, ensure_ascii=False)},
            )
        except DBAPIError as exc:
            raise _translate_db_error(exc) from exc
        assert isinstance(raw, dict)
        return format_adapter_public_projection_from_row(raw)

    async def transition_plugin(
        self, payload: dict[str, Any]
    ) -> FormatAdapterPluginRecord:
        try:
            raw = await self._db.scalar(
                text(
                    "SELECT yoao_labeling_v1.transition_format_adapter_plugin("
                    "CAST(:payload AS jsonb))"
                ),
                {"payload": json.dumps(payload, ensure_ascii=False)},
            )
        except DBAPIError as exc:
            raise _translate_db_error(exc) from exc
        assert isinstance(raw, dict)
        return format_adapter_public_projection_from_row(raw)

    async def open_secret(self, format_id: str) -> FormatAdapterPluginSecret | None:
        raw = await self._db.scalar(
            text(
                "SELECT yoao_labeling_v1.open_format_adapter_plugin_secret(:format_id)"
            ),
            {"format_id": format_id},
        )
        if not isinstance(raw, dict):
            return None
        return FormatAdapterPluginSecret.model_validate(raw)
