"""Explicit content revisions with immutable lineage and predecessor fencing."""

import uuid

from fastapi import HTTPException

from app.schemas.dataset_source import SourceMoveCandidateList, SourceRevisionList
from app.services import dataset_registration_data as data


def conflict(code):
    raise HTTPException(409, detail={"code": code})


async def append(db, source, scan, entry, result, asset, actor, *, resolution=None):
    """The caller holds Source and Dataset locks and owns the transaction."""
    resolution = resolution or {}
    revision_id = resolution.get("revision_id") or uuid.uuid4()
    code = await data.append_revision(
        db, revision_id, source, scan, entry, result, asset, actor, resolution
    )
    if code:
        if code == "access_revoked":
            raise HTTPException(403, detail={"code": code})
        conflict(code)
    return await data.revision_by_id(db, source.id, revision_id)


def result_out(revision, created=False):
    return dict(
        revision_id=revision.id,
        previous_revision_id=revision.previous_revision_id,
        revision_number=revision.revision_number,
        item_id=revision.item_id_at_accept,
        sha256=revision.content_sha256,
        created=created,
    )


async def history(db, source_id, digest, limit, offset):
    projection = await data.revision_history(db, source_id, digest, limit, offset)
    return SourceRevisionList.model_validate(projection).model_dump()


async def move_candidates(db, run, digest, limit, offset):
    """Same SHA is a suggestion for an explicit choice, never identity inference."""
    projection = await data.move_candidates(db, run.id, digest, limit, offset)
    if projection.get("error_code"):
        conflict(projection["error_code"])
    return SourceMoveCandidateList.model_validate(projection).model_dump()
