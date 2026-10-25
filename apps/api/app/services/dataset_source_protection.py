"""Source responsibility declarations do not authorize I/O or prove backups."""

from fastapi import HTTPException

from app.services import dataset_scans as scans
from app.services import dataset_source_access as access
from app.services.dataset_content import key_authority


def declaration(source):
    if source.external_protection is None:
        return None
    return {
        **source.external_protection,
        "revision": source.external_protection_revision,
    }


async def update(db, source_id, payload, actor, *, api_key_id=None):
    await access.require_source(db, source_id, actor)
    proof = await key_authority(db, actor, api_key_id, "datasets:write")
    _, source = await scans.source_context(db, source_id)
    actor = await scans.require_actor(db, actor.id, source)
    if await key_authority(db, actor, api_key_id, "datasets:write") != proof:
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    if source.external_protection_revision != payload.expected_revision:
        raise HTTPException(
            409, detail={"code": "external_protection_revision_changed"}
        )
    source.external_protection = {
        **payload.model_dump(exclude={"expected_revision"}),
        "recorded_at": (await scans.now(db)).isoformat(),
        "recorded_by_id": str(actor.id),
    }
    source.external_protection_revision += 1
    await db.flush()
    return source
