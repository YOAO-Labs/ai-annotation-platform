"""Short, read-only probes routed to one exact Celery worker."""

import asyncio
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services import storage_access
from app.services.content_snapshot import ContentError
from app.services.storage_location import get_visible, perform_probe
from app.services.dataset_source_access import require_manager
from app.services.dataset_content import key_authority
from app.services.filesystem_storage import FilesystemStorageError
from fastapi import HTTPException
from app.workers.celery_app import celery_app


@celery_app.task(
    bind=True,
    name="app.workers.storage_probe.probe_location",
    soft_time_limit=4,
    time_limit=5,
)
def probe_location(
    self,
    location_id,
    config_revision,
    actor_id,
    expected_node,
    sample_file,
    api_key_id=None,
    key_proof=None,
    maintenance_revision=1,
):
    if self.request.hostname != expected_node:
        return {"ok": False, "code": "wrong_execution_node", "capabilities": {}}
    return asyncio.run(
        _probe(
            location_id,
            config_revision,
            actor_id,
            expected_node,
            sample_file,
            api_key_id,
            key_proof,
            maintenance_revision,
        )
    )


async def _probe(
    location_id,
    config_revision,
    actor_id,
    expected_node,
    sample_file,
    api_key_id=None,
    key_proof=None,
    maintenance_revision=1,
):
    engine = create_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            actor = await require_manager(db, uuid.UUID(actor_id))
            if (
                await key_authority(db, actor, api_key_id, "datasets:write")
                != key_proof
            ):
                return {
                    "ok": False,
                    "code": "api_key_authority_revoked",
                    "capabilities": {},
                }
            location = await get_visible(db, uuid.UUID(location_id), actor)
            if location.config_revision != config_revision or not location.enabled:
                return {
                    "ok": False,
                    "code": "configuration_changed",
                    "capabilities": {},
                }
            result = await perform_probe(
                db,
                location,
                f"worker:{expected_node}",
                sample_file,
                maintenance_revision=maintenance_revision,
            )
            actor = await require_manager(db, actor.id)
            location = await get_visible(db, uuid.UUID(location_id), actor)
            if location.config_revision != config_revision or not location.enabled:
                return {
                    "ok": False,
                    "code": "configuration_changed",
                    "capabilities": {},
                }
            if (
                await key_authority(db, actor, api_key_id, "datasets:write")
                != key_proof
            ):
                return {
                    "ok": False,
                    "code": "api_key_authority_revoked",
                    "capabilities": {},
                }
            await storage_access.check(
                db, location.id, expected_revision=maintenance_revision, lock=True
            )
            return result
    except ContentError as exc:
        return {"ok": False, "code": exc.code, "capabilities": {}}
    except (FilesystemStorageError, HTTPException):
        return {"ok": False, "code": "access_revoked", "capabilities": {}}
    finally:
        await engine.dispose()
