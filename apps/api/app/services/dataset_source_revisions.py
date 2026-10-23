"""Explicit content revisions with immutable lineage and predecessor fencing."""

import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.db.models.dataset_source import (
    DatasetSourceRevision as Revision,
    DatasetSourceItem as Head,
    DatasetSourceChange as Change,
    DatasetRegistrationRun as Run,
    DatasetScanRun,
    DatasetScanEntry,
)
from app.services import dataset_scans as scans
from app.services import dataset_registration as registration
from app.services.dataset_content import key_authority


class AlreadyAcceptedRevision(Exception):
    """A concurrent identical request committed while snapshot I/O was running."""


def conflict(code):
    raise HTTPException(409, detail={"code": code})


async def append(db, source, scan, entry, result, asset, actor, *, resolution=None):
    """The caller holds Source and Dataset locks and owns the transaction."""
    previous = await db.scalar(
        select(Revision)
        .where(
            Revision.source_id == source.id, Revision.path_digest == entry.path_digest
        )
        .order_by(Revision.revision_number.desc())
        .limit(1)
    )
    if await db.scalar(
        select(Revision.id).where(
            Revision.source_id == source.id,
            Revision.path_digest == entry.path_digest,
            Revision.run_id == scan.id,
        )
    ):
        conflict("revision_candidate_already_accepted")
    revision = Revision(
        id=resolution["revision_id"] if resolution else uuid.uuid4(),
        source_id=source.id,
        path_digest=entry.path_digest,
        run_id=scan.id,
        revision_number=previous.revision_number + 1 if previous else 1,
        item_id_at_accept=result["item_id"],
        asset_id_at_accept=asset.id,
        dataset_item_id=result["item_id"],
        asset_id=asset.id,
        content_sha256=result["sha256"],
        ownership_mode=source.ownership_mode,
        owns_item=result.get("owns_item", result["created"]),
        previous_revision_id=(
            resolution["before_revision_id"]
            if resolution
            else previous.id
            if previous
            else None
        ),
        registration_id=resolution["registration_id"] if resolution else None,
        accepted_by_id=actor.id,
        accepted_at=await scans.now(db),
    )
    db.add(revision)
    await db.flush()
    return revision


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

    async def guard(session):
        run = await session.get(Run, registration_id, populate_existing=True)
        if run is None:
            raise HTTPException(404, "Registration not found")
        location, source = await scans.source_context(session, run.source_id)
        await session.refresh(run)
        current_actor = await scans.require_actor(session, actor_id, source)
        await registration.request_authority(
            session, current_actor, api_key_id, proof, source
        )
        await scans.validate_write_context(
            session, location, source, snapshot=run.snapshot
        )
        if source.policy_revision != run.snapshot.get("policy_revision", 1):
            conflict("configuration_changed")
        if (
            run.phase != "preflight"
            or run.status != "completed"
            or not run.comparison_complete
            or run.generation != payload.expected_generation
        ):
            conflict("revision_preflight_required")
        change = await session.get(Change, (run.id, digest), populate_existing=True)
        if relocate:
            before = await session.get(
                Revision, payload.before_revision_id, populate_existing=True
            )
            old_change = (
                await session.get(
                    Change, (run.id, before.path_digest), populate_existing=True
                )
                if before
                else None
            )
            previous_scan = (
                await session.get(DatasetScanRun, before.run_id) if before else None
            )
            candidate_scan = await session.get(DatasetScanRun, run.scan_run_id)
            if (
                change is None
                or change.kind != "added"
                or change.candidate_sha256 != payload.candidate_sha256
                or before is None
                or before.source_id != source.id
                or before.path_digest == digest
                or before.content_sha256 != payload.candidate_sha256
                or old_change is None
                or old_change.kind != "not_seen"
                or old_change.before_revision_id != payload.before_revision_id
                or old_change.before_sha256 != payload.candidate_sha256
                or previous_scan.snapshot["anchor"] != candidate_scan.snapshot["anchor"]
            ):
                conflict("revision_selection_changed")
        elif (
            change is None
            or change.kind != "changed"
            or change.reason
            != ("observation_changed" if revalidate else "content_changed")
            or (revalidate and change.before_sha256 != payload.candidate_sha256)
            or not change.before_revision_id
            or change.before_revision_id != payload.before_revision_id
            or change.candidate_sha256 != payload.candidate_sha256
        ):
            conflict("revision_selection_changed")
        candidate = await session.get(
            DatasetScanEntry, (run.scan_run_id, digest), populate_existing=True
        )
        if (
            not candidate
            or not candidate.verified_content
            or candidate.verified_content["sha256"] != payload.candidate_sha256
        ):
            conflict("revision_selection_changed")
        # Source serialization fences schedulers, scans and manual registrations.
        for model in (DatasetScanRun, Run):
            if await session.scalar(
                select(model.id)
                .where(model.source_id == source.id, model.status.in_(scans.ACTIVE))
                .limit(1)
            ):
                conflict("source_busy")
        accepted = await session.scalar(
            select(Revision)
            .where(Revision.registration_id == run.id, Revision.path_digest == digest)
            .execution_options(populate_existing=True)
        )
        if accepted:
            if (
                accepted.previous_revision_id != payload.before_revision_id
                or accepted.content_sha256 != payload.candidate_sha256
            ):
                conflict("revision_selection_changed")
            if accepted.dataset_item_id is None:
                conflict("accepted_revision_item_deleted")
            return run, change, accepted
        head = await session.get(
            Head,
            (source.id, before.path_digest if relocate else digest),
            populate_existing=True,
        )
        if head is None or head.current_revision_id != payload.before_revision_id:
            conflict("source_revision_changed")
        if relocate:
            from app.db.models.dataset_source import DatasetRegistrationEntry

            target = await session.get(
                DatasetRegistrationEntry, (run.id, digest), populate_existing=True
            )
            if (
                await session.get(Head, (source.id, digest), populate_existing=True)
                is not None
                or target is None
                or target.state != "ready"
            ):
                conflict("relocation_target_unavailable")
        return run, change, None

    run, change, accepted = await guard(db)
    if accepted:
        return result_out(accepted)
    scan_id = run.scan_run_id
    resolution = dict(
        registration_id=run.id,
        revision_id=new_id,
        before_revision_id=payload.before_revision_id,
        revalidate=revalidate or relocate,
        relocate=relocate,
    )

    async def before_apply(session):
        # At the final check our own unpublished head has already advanced.
        head = await session.get(Head, (run.source_id, digest), populate_existing=True)
        own = bool(head and head.current_revision_id == new_id)
        _, _, accepted = await guard(session)
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
        _, _, accepted = await guard(db)
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
    stmt = select(Revision).where(Revision.source_id == source_id)
    if digest:
        stmt = stmt.where(Revision.path_digest == digest)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    previous = aliased(Revision)
    rows = (
        await db.execute(
            stmt.add_columns(previous)
            .outerjoin(previous, previous.id == Revision.previous_revision_id)
            .order_by(Revision.path_digest, Revision.revision_number.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return dict(
        total=total,
        items=[
            dict(
                **result_out(r),
                acceptance_order=r.acceptance_order,
                previous_path_digest=p.path_digest if p else None,
                acceptance_kind=(
                    "initial"
                    if p is None
                    else "relocation"
                    if r.path_digest != p.path_digest
                    else "observation_revalidation"
                    if (
                        r.item_id_at_accept == p.item_id_at_accept
                        and r.asset_id_at_accept == p.asset_id_at_accept
                        and r.content_sha256 == p.content_sha256
                    )
                    else "content_revision"
                ),
                path_digest=r.path_digest,
                scan_run_id=r.run_id,
                registration_id=r.registration_id,
                accepted_by_id=r.accepted_by_id,
                accepted_at=r.accepted_at,
                item_available=r.dataset_item_id is not None,
            )
            for r, p in rows
        ],
    )


async def move_candidates(db, run, digest, limit, offset):
    """Same SHA is a suggestion for an explicit choice, never identity inference."""
    from app.db.models.dataset_source import DatasetRegistrationEntry

    old = await db.get(Change, (run.id, digest), populate_existing=True)
    if (
        not run.comparison_complete
        or run.phase != "preflight"
        or run.status != "completed"
        or old is None
        or old.kind != "not_seen"
        or old.before_revision_id is None
    ):
        conflict("revision_preflight_required")
    resolved = await db.scalar(
        select(Revision.id).where(
            Revision.registration_id == run.id,
            Revision.previous_revision_id == old.before_revision_id,
        )
    )
    head = await db.get(Head, (run.source_id, digest), populate_existing=True)
    if resolved or head is None or head.current_revision_id != old.before_revision_id:
        return dict(total=0, items=[])
    stmt = (
        select(Change)
        .join(
            DatasetRegistrationEntry,
            (DatasetRegistrationEntry.run_id == Change.run_id)
            & (DatasetRegistrationEntry.path_digest == Change.path_digest),
        )
        .where(
            Change.run_id == run.id,
            Change.kind == "added",
            Change.candidate_sha256 == old.before_sha256,
            DatasetRegistrationEntry.state == "ready",
        )
    )
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = await db.scalars(
        stmt.order_by(Change.path_digest).offset(offset).limit(limit)
    )
    return dict(
        total=total,
        items=[
            dict(
                path_digest=c.path_digest,
                relative_path=c.relative_path,
                candidate_sha256=c.candidate_sha256,
            )
            for c in rows
        ],
    )
