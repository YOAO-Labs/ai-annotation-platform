"""Initialize the real analytics store for an isolated screenshot seed."""

from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import _REPO_ROOT_DUCKDB, settings
from app.services.duckdb_sync import sync_all


def validate_screenshot_analytics_target() -> None:
    """Refuse to write capture analytics into the shared development store."""
    database_url = make_url(settings.database_url)
    database = database_url.database or ""
    if database_url.get_backend_name() != "postgresql" or not database.endswith(
        ("_test", "_e2e")
    ):
        raise RuntimeError(
            "Screenshot analytics requires an isolated *_test or *_e2e database"
        )
    if Path(settings.duckdb_path).resolve() == Path(_REPO_ROOT_DUCKDB).resolve():
        raise RuntimeError(
            "Screenshot analytics cannot use the shared development DuckDB file; "
            "unset DUCKDB_PATH to use the isolated default or select a private path "
            "shared by the screenshot seed and API"
        )


async def prepare_screenshot_analytics(db: AsyncSession) -> dict:
    """Synchronize committed fixture data without depending on beat/cleanup workers."""
    validate_screenshot_analytics_target()
    return await sync_all(db)
