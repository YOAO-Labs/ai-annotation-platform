"""Transaction scope for reading original content under a shared lock."""

from contextlib import asynccontextmanager

from sqlalchemy.exc import DBAPIError

from app.services import storage_access
from app.services.content_snapshot import ContentError


@asynccontextmanager
async def _reads(db, lock):
    with db.no_autoflush:
        if not lock:
            yield
            return
        connection = await db.connection()
        savepoint = await connection.begin_nested()
        try:
            yield
        except BaseException as exc:
            await savepoint.rollback()
            if isinstance(exc, DBAPIError) and (
                storage_access.unavailable(exc)
                or getattr(exc.orig, "sqlstate", None) == "40001"
                or getattr(exc.orig, "pgcode", None) == "40001"
            ):
                raise ContentError("storage_original_busy") from exc
            raise
        else:
            await savepoint.commit()
