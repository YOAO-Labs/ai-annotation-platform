"""Project references, exact visible metadata and irreversible retirement.

Callers hold every relevant Project lock before entering these helpers. Owners
are locked by UUID before reference rows; no helper commits or performs I/O.
"""

from copy import deepcopy
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError

from app.db.models.guide_asset_upload import GuideAssetUpload as Upload
from app.db.models.project_guide_asset import ProjectGuideAsset as Reference
from app.services import comment_attachment_authority as authority
from app.services import guide_asset_storage as storage, storage_access
from app.services.content_snapshot import ContentError


def reject(code="guide_asset_changed", status=409):
    raise HTTPException(status, {"code": code})


def db_error(exc):
    state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    if state in {"55P03", "40P01", "40001"}:
        reject("guide_asset_busy")
    if state in {"23503", "23505", "23514"}:
        reject()
    reject("guide_asset_unavailable", 503)


async def locked_owners(db, identifiers, *, read=False, skip_locked=False):
    try:
        with db.no_autoflush:
            return list(
                await db.scalars(
                    select(Upload)
                    .where(Upload.id.in_(identifiers))
                    .order_by(Upload.id)
                    .with_for_update(
                        read=read, nowait=not skip_locked, skip_locked=skip_locked
                    )
                    .execution_options(populate_existing=True)
                )
            )
    except DBAPIError as exc:
        db_error(exc)


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        reject("guide_asset_receipt_mismatch")


def receipt(row):
    try:
        return storage.snapshot(row)
    except storage.GuideAssetStorageError as exc:
        reject(exc.code, exc.status_code)


def entry(row):
    try:
        return storage.entry(row)
    except storage.GuideAssetStorageError as exc:
        reject(exc.code, exc.status_code)


def indexed_assets(assets):
    if not isinstance(assets, list):
        reject("guide_asset_receipt_mismatch")
    result = {}
    for entry in assets:
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            reject("guide_asset_receipt_mismatch")
        key = entry["key"]
        if key in result:
            reject("guide_asset_duplicate_key")
        _json(entry)
        result[key] = entry
    return result


async def _references(db, project_id, *, lock=False):
    statement = select(Reference).where(Reference.project_id == project_id)
    if lock:
        statement = statement.with_for_update(nowait=True)
    try:
        with db.no_autoflush:
            return list(
                await db.scalars(
                    statement.order_by(Reference.upload_id).execution_options(
                        populate_existing=True
                    )
                )
            )
    except DBAPIError as exc:
        db_error(exc)


async def _locked_current(db, project):
    located = await _references(db, project.id)
    identifiers = {row.upload_id for row in located}
    owners = await locked_owners(db, identifiers)
    refs = await _references(db, project.id, lock=True)
    if {row.upload_id for row in refs} != identifiers or len(owners) != len(refs):
        reject()
    return {row.id: row for row in owners}, refs


async def _has_reference(db, identifier):
    # The owner lock serializes all new references. Never reverse-lock Project.
    return (
        await db.scalar(
            select(Reference.upload_id)
            .where(Reference.upload_id == identifier)
            .limit(1)
        )
        is not None
    )


async def _retire_unreferenced(db, owners):
    for row in owners:
        if row.state == "ready" and not await _has_reference(db, row.id):
            row.state, row.retired_at = "retired", authority.now()


async def set_assets(db, project, assets):
    if _json(project.guide_assets or []) != _json(assets):
        project.guide_assets = deepcopy(assets)
        await db.flush()
        # The database trigger owns revision increments for every writer.
        await db.refresh(project, attribute_names=["guide_assets_revision"])


async def reconcile_assets(db, project, expected_revision, assets):
    """Accept only an exact subset/reorder under the caller's Project UPDATE."""
    if type(expected_revision) is not int or (
        expected_revision != project.guide_assets_revision
    ):
        reject("guide_asset_revision_conflict")
    existing = indexed_assets(project.guide_assets or [])
    supplied = indexed_assets(assets)
    for key, value in supplied.items():
        if key not in existing or _json(value) != _json(existing[key]):
            reject("guide_asset_receipt_mismatch")
    owners, refs = await _locked_current(db, project)
    for ref in refs:
        row = owners[ref.upload_id]
        key = storage.key_for(row.project_id, row.id)
        if key in supplied:
            if row.state != "ready" or _json(supplied[key]) != _json(entry(row)):
                reject("guide_asset_receipt_mismatch")
            receipt(row)
        else:
            await db.delete(ref)
    await db.flush()
    await _retire_unreferenced(db, owners.values())
    await set_assets(db, project, assets)


async def copy_references(db, source, target):
    """Copy current source references after caller locks/authorizes both projects."""
    if (
        source.id == target.id
        or target.guide_assets
        or await _references(db, target.id)
    ):
        reject()
    assets = deepcopy(source.guide_assets or [])
    entries = indexed_assets(assets)
    owners, refs = await _locked_current(db, source)
    receipts = []
    for ref in refs:
        row = owners[ref.upload_id]
        key = storage.key_for(row.project_id, row.id)
        if row.state != "ready" or _json(entries.get(key)) != _json(entry(row)):
            reject("guide_asset_receipt_mismatch")
        receipts.append(receipt(row))
    # Owner locks precede every new Ref; no metadata creates an owner or a Ref.
    for ref in refs:
        db.add(Reference(project_id=target.id, upload_id=ref.upload_id))
    await db.flush()
    for location in sorted(
        {uuid.UUID(row.storage_ref["location_id"]) for row in receipts}
    ):
        try:
            await storage_access.check(db, location, lock=True)
        except ContentError as exc:
            reject(exc.code, exc.status)
    await set_assets(db, target, assets)
    return assets


async def retire_for_projects(db, project_ids):
    """Unlink caller-locked projects and retain independent last-reference owners."""
    with db.no_autoflush:
        identifiers = set(
            await db.scalars(
                select(Reference.upload_id).where(Reference.project_id.in_(project_ids))
            )
        )
    owners = await locked_owners(db, identifiers)
    try:
        await db.execute(delete(Reference).where(Reference.project_id.in_(project_ids)))
    except DBAPIError as exc:
        db_error(exc)
    await db.flush()
    await _retire_unreferenced(db, owners)
    await db.flush()
    return [row.id for row in owners if row.state == "retired"]
