"""Source responsibility declarations do not authorize I/O or prove backups."""

import json

from fastapi import HTTPException
from sqlalchemy import text

from app.services import dataset_scans as scans
from app.services import dataset_source_access as access
from app.services.dataset_source_access import key_authority


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
    protection = {
        **payload.model_dump(exclude={"expected_revision"}),
        "recorded_at": (await scans.now(db)).isoformat(),
        "recorded_by_id": str(actor.id),
    }
    changed = await db.scalar(
        text(
            "SELECT yoao_assets_v1.declare_directory_source_protection("
            "CAST(:actor_id AS uuid), CAST(:source_id AS uuid), "
            ":revision, CAST(:declaration AS jsonb))"
        ),
        {
            "actor_id": actor.id,
            "source_id": source.id,
            "revision": payload.expected_revision,
            "declaration": json.dumps(protection, separators=(",", ":")),
        },
    )
    if not changed:
        raise HTTPException(
            409, detail={"code": "external_protection_revision_changed"}
        )
    return await access.require_source(db, source_id, actor)
