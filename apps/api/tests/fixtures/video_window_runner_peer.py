"""Run one real tracker worker in a killable process for recovery acceptance."""

from __future__ import annotations

import asyncio
import sys
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services.video_tracking.runner import run_tracker_job


async def _silent_event(_channel: str, _event: dict) -> None:
    return None


async def _run(job_id: UUID) -> int:
    engine = create_async_engine(
        settings.database_url, pool_pre_ping=True, hide_parameters=True
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            result = await run_tracker_job(
                db,
                job_id,
                publisher=_silent_event,
                shadow_session_factory=sessions,
            )
            if result is None or result.status != "pending_review":
                return 3
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    try:
        job_id = UUID(sys.argv[1])
    except ValueError:
        return 2
    return asyncio.run(_run(job_id))


if __name__ == "__main__":
    raise SystemExit(main())
