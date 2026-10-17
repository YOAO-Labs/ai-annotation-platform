"""Safety admission shared by hidden fixture routes and fixture write owners."""

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


async def assert_isolated_test_seed_database(db: AsyncSession) -> None:
    if settings.environment == "production" or not settings.e2e_seed_enabled:
        raise HTTPException(status_code=403, detail="E2E seed routes are disabled")
    database_name = await db.scalar(text("SELECT current_database()"))
    if not str(database_name or "").lower().endswith(("_e2e", "_test")):
        raise HTTPException(
            status_code=403,
            detail="E2E seed routes require an isolated _e2e or _test database",
        )
