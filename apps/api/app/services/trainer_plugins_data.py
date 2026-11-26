"""PostgreSQL owner adapter for registered trainer plugins."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.trainer_plugin import TrainerPluginRecord


class TrainerPluginStoreError(RuntimeError):
    def __init__(self, reason: str, *, status_code: int = 409) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


def _translate_db_error(exc: DBAPIError) -> TrainerPluginStoreError:
    orig = getattr(exc, "orig", exc)
    sqlstate = getattr(orig, "sqlstate", None)
    message = str(orig)
    if sqlstate == "P0002" or "trainer_plugin_not_found" in message:
        return TrainerPluginStoreError("trainer_plugin_not_found", status_code=404)
    if sqlstate == "40001" or "trainer_plugin_revision_conflict" in message:
        return TrainerPluginStoreError(
            "trainer_plugin_revision_conflict", status_code=409
        )
    if sqlstate == "23505":
        if "uq_trainer_plugins_active_framework" in message:
            return TrainerPluginStoreError(
                "trainer_plugin_framework_conflict", status_code=409
            )
        return TrainerPluginStoreError("trainer_plugin_already_exists", status_code=409)
    if sqlstate in {"22023", "23514", "23502"}:
        for code in (
            "trainer_plugin_already_retired",
            "trainer_plugin_not_qualified",
            "trainer_plugin_invalid_transition",
            "trainer_plugin_cannot_qualify_in_status",
            "invalid_qualification_report",
            "invalid_trainer_plugin_status",
            "invalid_target_status",
        ):
            if code in message:
                return TrainerPluginStoreError(code, status_code=422)
        return TrainerPluginStoreError(message, status_code=422)
    return TrainerPluginStoreError(message, status_code=500)


class TrainerPluginsPostgresData:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _flush_if_needed(self) -> None:
        if getattr(self._db, "autoflush", False):
            await self._db.flush()

    async def list_plugins(
        self, *, include_retired: bool = False, status: str | None = None
    ) -> list[TrainerPluginRecord]:
        await self._flush_if_needed()
        try:
            async with self._db.begin_nested():
                raw = await self._db.scalar(
                    text(
                        "SELECT yoao_learning_v1.list_trainer_plugins("
                        ":include_retired, :status)"
                    ),
                    {"include_retired": include_retired, "status": status},
                )
        except DBAPIError as exc:
            raise _translate_db_error(exc) from exc
        rows = raw if isinstance(raw, list) else []
        return [TrainerPluginRecord.model_validate(row) for row in rows]

    async def get_plugin(self, plugin_id: str) -> TrainerPluginRecord | None:
        await self._flush_if_needed()
        raw = await self._db.scalar(
            text("SELECT yoao_learning_v1.get_trainer_plugin(:plugin_id)"),
            {"plugin_id": plugin_id},
        )
        if not isinstance(raw, dict):
            return None
        return TrainerPluginRecord.model_validate(raw)

    async def get_enabled_by_framework(
        self, framework_id: str
    ) -> TrainerPluginRecord | None:
        await self._flush_if_needed()
        raw = await self._db.scalar(
            text(
                "SELECT yoao_learning_v1.get_enabled_trainer_plugin_by_framework("
                ":framework_id)"
            ),
            {"framework_id": framework_id},
        )
        if not isinstance(raw, dict):
            return None
        return TrainerPluginRecord.model_validate(raw)

    async def register_plugin(self, payload: dict[str, Any]) -> TrainerPluginRecord:
        await self._flush_if_needed()
        try:
            async with self._db.begin_nested():
                raw = await self._db.scalar(
                    text(
                        "SELECT yoao_learning_v1.register_trainer_plugin("
                        "CAST(:payload AS jsonb))"
                    ),
                    {"payload": json.dumps(payload, ensure_ascii=False)},
                )
        except DBAPIError as exc:
            raise _translate_db_error(exc) from exc
        assert isinstance(raw, dict)
        return TrainerPluginRecord.model_validate(raw)

    async def qualify_plugin(self, payload: dict[str, Any]) -> TrainerPluginRecord:
        await self._flush_if_needed()
        try:
            async with self._db.begin_nested():
                raw = await self._db.scalar(
                    text(
                        "SELECT yoao_learning_v1.qualify_trainer_plugin("
                        "CAST(:payload AS jsonb))"
                    ),
                    {"payload": json.dumps(payload, ensure_ascii=False)},
                )
        except DBAPIError as exc:
            raise _translate_db_error(exc) from exc
        assert isinstance(raw, dict)
        return TrainerPluginRecord.model_validate(raw)

    async def transition_plugin(self, payload: dict[str, Any]) -> TrainerPluginRecord:
        await self._flush_if_needed()
        try:
            async with self._db.begin_nested():
                raw = await self._db.scalar(
                    text(
                        "SELECT yoao_learning_v1.transition_trainer_plugin("
                        "CAST(:payload AS jsonb))"
                    ),
                    {"payload": json.dumps(payload, ensure_ascii=False)},
                )
        except DBAPIError as exc:
            raise _translate_db_error(exc) from exc
        assert isinstance(raw, dict)
        return TrainerPluginRecord.model_validate(raw)
