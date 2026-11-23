"""Explicit content revisions with immutable lineage and predecessor fencing."""

import uuid

from fastapi import HTTPException

from app.services.dataset_content import key_authority
from app.schemas.dataset_source import SourceMoveCandidateList, SourceRevisionList
from app.services import dataset_registration as registration
from app.services import dataset_registration_data as data
from app.services import dataset_scans as scans


class AlreadyAcceptedRevision(Exception):
    """A concurrent identical request committed while snapshot I/O was running."""


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


async def accept(
    db,
    registration_id,
    digest,
    payload,
    actor,
    *,
    api_key_id=None,
    node_id=None,
    revalidate=False,
    relocate=False,
):
    """A completed preflight grants no authority to silently replace old items."""
    from app.services.dataset_references import register_entry

    actor_id = actor.id
    proof = await key_authority(db, actor, api_key_id, "datasets:write")
    new_id = uuid.uuid4()

    mode = "relocate" if relocate else "revalidate" if revalidate else "content"

    async def guard(session):
        run = await data.read_run(session, registration_id)
        if run is None:
            raise HTTPException(404, "Registration not found")
        location, source = await scans.source_context(session, run.source_id)
        run = await data.read_run(session, registration_id)
        current_actor = await scans.require_actor(session, actor_id, source)
        await registration.request_authority(
            session, current_actor, api_key_id, proof, source
        )
        await scans.validate_write_context(
            session, location, source, snapshot=run.snapshot
        )
        if source.policy_revision != run.snapshot.get("policy_revision", 1):
            conflict("configuration_changed")
        selection = await data.check_revision(
            session, registration_id, digest, payload, mode
        )
        if selection.code:
            conflict(selection.code)
        accepted = (
            await data.revision_by_id(session, source.id, selection.accepted_id)
            if selection.accepted_id
            else None
        )
        return run, accepted

    run, accepted = await guard(db)

    if accepted:
        return result_out(accepted)
    scan_id = run.scan_run_id
    resolution = dict(
        registration_id=run.id,
        revision_id=new_id,
        before_revision_id=payload.before_revision_id,
        revalidate=revalidate or relocate,
        relocate=relocate,
        generation=payload.expected_generation,
        mode=mode,
    )

    async def before_apply(session):
        head = await data.source_head(session, run.source_id, digest)
        own = bool(head and head.current_revision_id == new_id)
        _, accepted = await guard(session)
        if accepted and not own:
            raise AlreadyAcceptedRevision

    try:
        result = await register_entry(
            db,
            scan_id,
            digest,
            actor,
            api_key_id=api_key_id,
            node_id=node_id,
            before_apply=before_apply,
            revision_context=resolution,
        )
    except AlreadyAcceptedRevision:
        await db.rollback()
        _, accepted = await guard(db)
        if accepted is None:
            conflict("source_revision_changed")
        return result_out(accepted)
    return {
        key: result[key]
        for key in (
            "revision_id",
            "previous_revision_id",
            "revision_number",
            "item_id",
            "sha256",
            "created",
        )
    }


async def history(db, source_id, digest, limit, offset):
    projection = await data.revision_history(db, source_id, digest, limit, offset)
    return SourceRevisionList.model_validate(projection).model_dump()


async def move_candidates(db, run, digest, limit, offset):
    """Same SHA is a suggestion for an explicit choice, never identity inference."""
    projection = await data.move_candidates(db, run.id, digest, limit, offset)
    if projection.get("error_code"):
        conflict(projection["error_code"])
    return SourceMoveCandidateList.model_validate(projection).model_dump()
