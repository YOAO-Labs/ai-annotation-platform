"""Short, read-only probes routed to one exact Celery worker."""

import asyncio
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.user import User
from app.db.enums import UserRole
from app.services.storage_location import get_visible, perform_probe
from app.workers.celery_app import celery_app


@celery_app.task(
    bind=True,
    name="app.workers.storage_probe.probe_location",
    soft_time_limit=4,
    time_limit=5,
)
def probe_location(
    self, location_id, config_revision, actor_id, expected_node, sample_file
):
    if self.request.hostname != expected_node:
        return {"ok": False, "code": "wrong_execution_node", "capabilities": {}}
    return asyncio.run(
        _probe(location_id, config_revision, actor_id, expected_node, sample_file)
    )


async def _probe(location_id, config_revision, actor_id, expected_node, sample_file):
    engine = create_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            actor = await db.get(User, uuid.UUID(actor_id))
            if (
                actor is None
                or not actor.is_active
                or actor.role != UserRole.SUPER_ADMIN
            ):
                return {"ok": False, "code": "access_revoked", "capabilities": {}}
            location = await get_visible(db, uuid.UUID(location_id), actor)
            if location.config_revision != config_revision or not location.enabled:
                return {
                    "ok": False,
                    "code": "configuration_changed",
                    "capabilities": {},
                }
            return await perform_probe(
                db, location, f"worker:{expected_node}", sample_file
            )
    finally:
        await engine.dispose()
