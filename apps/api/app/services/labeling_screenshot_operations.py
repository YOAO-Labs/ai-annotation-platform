"""Run screenshot-maintenance commands on the caller's Labeling transaction."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_labeling.screenshot_operations_contract import (
    ScreenshotOperationsCommand,
    ScreenshotOperationsResult,
)
from aap_labeling.screenshot_operations_data import ScreenshotOperationsDataClient


def _sqlstate(error: BaseException) -> str | None:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        diagnostic = getattr(current, "diag", None)
        value = (
            getattr(current, "sqlstate", None)
            or getattr(current, "pgcode", None)
            or getattr(diagnostic, "sqlstate", None)
        )
        if value:
            return value
        current = (
            getattr(current, "orig", None) or current.__cause__ or current.__context__
        )
    return None


async def screenshot_operations_command(
    db: AsyncSession, request: ScreenshotOperationsCommand
) -> ScreenshotOperationsResult:
    """Execute one closed maintenance operation without committing or rolling back."""

    async def execute(payload: str):
        return await db.scalar(
            text(
                "SELECT aap_labeling_v1.screenshot_operations_command(CAST(:request AS jsonb))"
            ),
            {"request": payload},
        )

    if db.autoflush:
        await db.flush()
    try:
        return await ScreenshotOperationsDataClient(execute).command(request)
    except DBAPIError as exc:
        state = _sqlstate(exc)
        if state == "23514":
            raise HTTPException(
                status_code=409,
                detail={"reason": "screenshot_scope_changed"},
            ) from exc
        if state == "22023":
            raise HTTPException(
                status_code=400,
                detail={"reason": "invalid_screenshot_operation"},
            ) from exc
        raise


__all__ = ["screenshot_operations_command"]
