"""Guide reference publication through the native Workspace owner.

Read helpers stay local for authenticated byte reads and durable cleanup. Every
publication write of ``projects.guide_assets``, its revision and
``project_guide_assets`` travels through
``aap_workspace_configuration_v1.guide_assets_step`` in the caller's
transaction. Project deletion is the separate Workspace retirement path that
removes a project's references and retires its last-reference uploads.
"""

from copy import deepcopy
import json

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError

from app.db.models.guide_asset_upload import GuideAssetUpload as Upload
from app.db.models.project_guide_asset import ProjectGuideAsset as Reference
from app.services import comment_attachment_authority as authority
from app.services import guide_asset_storage as storage
from app.services.workspace_configuration import (
    ConfigurationExecution,
    execute_configuration,
    execute_guide_action,
)


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


def grouped_existing_assets(assets):
    """Historical JSON can contain duplicate keys or dictionaries without keys.

    Preserve those source facts without treating any metadata as a reference.
    New explicit lists still pass the stricter indexed_assets validator.
    """
    if not isinstance(assets, list):
        reject("guide_asset_receipt_mismatch")
    _json(assets)
    result = {}
    for value in assets:
        if isinstance(value, dict) and isinstance(value.get("key"), str):
            result.setdefault(value["key"], []).append(value)
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


def forget_cached_references(db, project_ids):
    """Native owner writes bypass this assembly's ORM identity map.

    A guide command inserts and removes association rows in SQL, so detach the
    cached rows of the affected projects. A later ``get``/query then observes
    the owner's committed state instead of a removed or replaced instance.
    """
    affected = {str(value) for value in project_ids}
    session = db.sync_session
    for (model, identity, _token), row in list(session.identity_map.items()):
        if model is Reference and str(identity[0]) in affected:
            session.expunge(row)


async def publish(
    db,
    *,
    actor_id,
    project_id,
    receipt,
    entry,
    maintenance_revision,
) -> ConfigurationExecution:
    """Publish one verified pending upload through the owner command."""
    execution = await execute_configuration(
        db,
        operation="guide_asset_publish",
        actor_id=actor_id,
        target_id=project_id,
        payload={
            "upload_id": str(receipt.id),
            "sha256": receipt.sha256,
            "size_bytes": receipt.size_bytes,
            "maintenance_revision": maintenance_revision,
            "storage_ref": receipt.storage_ref,
            "entry": entry,
        },
    )
    forget_cached_references(db, [project_id])
    return execution


async def unlink(db, *, actor_id, project_id, key):
    """Remove one displayed key and retire only its last-reference owners."""
    execution = await execute_configuration(
        db,
        operation="guide_asset_unlink",
        actor_id=actor_id,
        target_id=project_id,
        payload={"key": key},
    )
    forget_cached_references(db, [project_id])
    return execution


def _raise_command_failure(error):
    from aap_workspace.configuration import ConfigurationCommandFailure

    if isinstance(error, ConfigurationCommandFailure):
        failure = error.error
        raise HTTPException(
            failure.status_code, failure.detail, failure.headers
        ) from None
    raise error


async def apply(db, *, actor_id, project, expected_revision, assets):
    """Apply an explicit, exact list against the current Project revision."""
    try:
        execution = await execute_guide_action(
            db,
            operation="update_project",
            actor_id=actor_id,
            target_id=project.id,
            payload={},
            action="guide_assets_apply",
            args={
                "expected_revision": expected_revision,
                "guide_assets_ascii": _assets_ascii(assets),
            },
        )
    except Exception as error:  # noqa: BLE001 - re-raised by the helper
        _raise_command_failure(error)
    forget_cached_references(db, [project.id])
    return execution


async def copy(db, *, actor_id, source, target):
    """Copy the current source list and references into an empty target."""
    source_refs = await _references(db, source.id)
    snapshot = {
        "owner_id": str(source.owner_id),
        "annotation_guide": source.annotation_guide,
        "guide_assets": deepcopy(source.guide_assets or []),
        "guide_assets_revision": source.guide_assets_revision,
        "upload_ids": [str(ref.upload_id) for ref in source_refs],
    }
    try:
        await execute_guide_action(
            db,
            operation="create_project",
            actor_id=actor_id,
            target_id=None,
            payload={"source_project_id": str(source.id)},
            action="guide_assets_copy",
            args={
                "source_project_id": str(source.id),
                "target_project_id": str(target.id),
                "source_snapshot": snapshot,
            },
        )
    except Exception as error:  # noqa: BLE001 - re-raised by the helper
        _raise_command_failure(error)
    forget_cached_references(db, [target.id])
    return snapshot["guide_assets"]


def _assets_ascii(assets):
    try:
        return json.dumps(
            assets,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError):
        reject("guide_asset_receipt_mismatch")


async def _retire_unreferenced(db, owners):
    for row in owners:
        if row.state == "ready" and not await _has_reference(db, row.id):
            row.state, row.retired_at = "retired", authority.now()


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
