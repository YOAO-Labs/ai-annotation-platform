"""Authenticated guide-image publication, current references and durable cleanup."""

import hashlib
import logging
import uuid

from fastapi import HTTPException
from sqlalchemy import exists, or_, select
from sqlalchemy.exc import DBAPIError

from app.db.enums import PlatformRole
from app.db.models.guide_asset_upload import GuideAssetUpload as Upload
from app.db.models.project import Project
from app.db.models.project_guide_asset import ProjectGuideAsset as Reference
from app.db.models.project_member import ProjectMember
from app.db.models.storage_maintenance import StorageOperation
from app.services import comment_attachment_authority as authority
from app.services import guide_asset_references as references
from app.services import guide_asset_storage as storage, storage_access
from app.services.content_snapshot import ContentError
from app.services.project_access import resolve_project_access
from app.services.storage import storage_service

log = logging.getLogger(__name__)
reject = references.reject


def _authority_error(exc):
    code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
    if (
        code == "api_key_authority_revoked"
        or code == "comment_attachment_authority_revoked"
    ):
        return HTTPException(403, {"code": "guide_asset_authority_revoked"})
    if isinstance(code, str) and code.startswith("comment_attachment_"):
        return HTTPException(
            exc.status_code,
            {"code": code.replace("comment_attachment_", "guide_asset_", 1)},
        )
    return exc


async def lock_authority(db, actor, request):
    try:
        return await authority.lock_authority(
            db, actor, request, allowed_roles=frozenset(PlatformRole)
        )
    except HTTPException as exc:
        raise _authority_error(exc) from None


async def reauthorize(db, proof):
    try:
        return await authority.reauthorize(db, proof)
    except HTTPException as exc:
        raise _authority_error(exc) from None


def check_clock(proof):
    try:
        authority.check_clock(proof)
    except HTTPException as exc:
        raise _authority_error(exc) from None


async def capture_authority(db, actor, request):
    _, proof = await lock_authority(db, actor, request)
    await db.commit()
    check_clock(proof)
    return proof


async def lock_projects(db, actor, ids, *, write_ids=()):
    """Lock all Projects, then memberships, before any owner or reference lock."""
    identifiers = sorted({uuid.UUID(str(value)) for value in ids})
    writes = {uuid.UUID(str(value)) for value in write_ids}
    result = {}
    try:
        with db.no_autoflush:
            for identifier in identifiers:
                project = await db.scalar(
                    select(Project)
                    .where(Project.id == identifier)
                    .with_for_update(read=identifier not in writes, nowait=True)
                    .execution_options(populate_existing=True)
                )
                if project is None:
                    reject("guide_asset_project_missing", 404)
                result[identifier] = project
            for identifier in identifiers:
                await db.scalar(
                    select(ProjectMember)
                    .where(
                        ProjectMember.project_id == identifier,
                        ProjectMember.user_id == actor.id,
                    )
                    .with_for_update(read=True, nowait=True)
                    .execution_options(populate_existing=True)
                )
            for identifier, project in result.items():
                access = await resolve_project_access(db, user=actor, project=project)
                if identifier in writes and not access.is_manager:
                    reject("guide_asset_manager_required", 403)
    except DBAPIError as exc:
        references.db_error(exc)
    return result


def http_error(exc):
    if isinstance(exc, storage.GuideAssetStorageError):
        return HTTPException(exc.status_code, {"code": exc.code})
    if isinstance(exc, ContentError):
        return HTTPException(exc.status, {"code": exc.code})
    if isinstance(exc, DBAPIError) and storage_access.unavailable(exc):
        return HTTPException(409, {"code": "guide_asset_busy"})
    return HTTPException(503, {"code": "guide_asset_storage_unavailable"})


def purpose(operation, identifier):
    return "guide_asset_" + operation + "_" + uuid.UUID(str(identifier)).hex


async def locked_owner(db, identifier, *, read=False, skip_locked=False):
    rows = await references.locked_owners(
        db, [identifier], read=read, skip_locked=skip_locked
    )
    return rows[0] if rows else None


async def capture_upload(db, actor, request, project_id):
    actor, proof = await lock_authority(db, actor, request)
    await lock_projects(db, actor, [project_id], write_ids=[project_id])
    await reauthorize(db, proof)
    await db.commit()
    check_clock(proof)
    return proof


async def upload(db, proof, project_id, data, mime_type, file_name):
    """Preserve every uncertain write as pending, with no compensating delete."""
    try:
        if type(data) is not bytes:
            reject("guide_asset_invalid_size", 400)
        if not 1 <= len(data) <= storage.MAX_BYTES:
            reject(
                "guide_asset_invalid_size",
                413 if len(data) > storage.MAX_BYTES else 400,
            )
        checksum = hashlib.sha256(data).hexdigest()
        mime_type = storage.normalize_mime_type(mime_type)
        file_name = storage.safe_file_name(file_name)
        actor = await reauthorize(db, proof)
        await lock_projects(db, actor, [project_id], write_ids=[project_id])
        row = Upload(
            id=uuid.uuid4(),
            owner_id=proof.actor_id,
            project_id=project_id,
            sha256=checksum,
            size_bytes=len(data),
            mime_type=mime_type,
            file_name=file_name,
            created_at=authority.now(),
            state="pending",
        )
        row.storage_ref = await storage.allocate(db, row)
        db.add(row)
        await db.flush()
        receipt = storage.snapshot(row)
        await reauthorize(db, proof)
        await db.commit()
        check_clock(proof)
        revision = None

        async def acquired(permit):
            nonlocal revision
            actor = await reauthorize(db, proof)
            await lock_projects(db, actor, [project_id], write_ids=[project_id])
            current = await locked_owner(db, receipt.id)
            if (
                current is None
                or current.state != "pending"
                or not storage.same(current, receipt)
            ):
                reject()
            await reauthorize(db, proof)
            await db.commit()
            check_clock(proof)
            revision = permit.revision

        def write_saved():
            check_clock(proof)
            storage.write(storage_service.client, receipt, data)

        await storage_access.run(
            db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            purpose("write", receipt.id),
            write_saved,
            on_acquired=acquired,
        )
        actor = await reauthorize(db, proof)
        projects = await lock_projects(db, actor, [project_id], write_ids=[project_id])
        current = await locked_owner(db, receipt.id)
        if (
            current is None
            or current.state != "pending"
            or not storage.same(current, receipt)
        ):
            reject()
        await storage_access.check(
            db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            expected_revision=revision,
            lock=True,
        )
        await reauthorize(db, proof)
        check_clock(proof)
        current.state = "ready"
        await db.flush()
        db.add(Reference(project_id=project_id, upload_id=receipt.id))
        await db.flush()
        project = projects[project_id]
        entry = storage.entry(receipt)
        await references.set_assets(db, project, [*(project.guide_assets or []), entry])
        await reauthorize(db, proof)
        await db.commit()
        check_clock(proof)
        return entry
    except HTTPException:
        raise
    except Exception as exc:
        raise http_error(exc) from None


def _identifier(key):
    try:
        prefix, project, middle, identifier = key.split("/")
        project_id, upload_id = uuid.UUID(project), uuid.UUID(identifier)
        if (
            prefix != "projects"
            or middle != "guide"
            or storage.key_for(project_id, upload_id) != key
        ):
            raise ValueError
        return project_id, upload_id
    except (AttributeError, ValueError, TypeError):
        reject("guide_asset_storage_unpinned")


async def _read_binding(db, actor, project_id, key, *, receipt=None):
    projects = await lock_projects(db, actor, [project_id])
    registered = any(
        isinstance(value, dict) and value.get("key") == key
        for value in projects[project_id].guide_assets or []
    )
    try:
        original_project_id, identifier = _identifier(key)
    except HTTPException:
        if registered:
            raise
        reject("guide_asset_missing", 404)
    if (
        await db.scalar(
            select(Reference.upload_id).where(
                Reference.project_id == project_id, Reference.upload_id == identifier
            )
        )
        is None
    ):
        reject(
            "guide_asset_storage_unpinned" if registered else "guide_asset_missing",
            409 if registered else 404,
        )
    row = await locked_owner(db, identifier, read=True)
    try:
        ref = await db.scalar(
            select(Reference)
            .where(
                Reference.project_id == project_id, Reference.upload_id == identifier
            )
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        references.db_error(exc)
    if ref is None:
        reject(
            "guide_asset_storage_unpinned" if registered else "guide_asset_missing",
            409 if registered else 404,
        )
    if row is None or row.project_id != original_project_id:
        reject("guide_asset_storage_unpinned")
    if row.state != "ready":
        reject("guide_asset_not_ready")
    if receipt is not None and not storage.same(row, receipt):
        reject()
    return row


async def read(db, actor, request, project_id, key):
    try:
        actor, proof = await lock_authority(db, actor, request)
        row = await _read_binding(db, actor, project_id, key)
        receipt = storage.snapshot(row)
        await reauthorize(db, proof)
        await db.commit()
        check_clock(proof)
        revision = None

        async def acquired(permit):
            nonlocal revision
            actor = await reauthorize(db, proof)
            await _read_binding(db, actor, project_id, key, receipt=receipt)
            await reauthorize(db, proof)
            await db.commit()
            check_clock(proof)
            revision = permit.revision

        def read_saved():
            check_clock(proof)
            return storage.read(storage_service.client, receipt)

        data = await storage_access.run(
            db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            purpose("read", receipt.id),
            read_saved,
            on_acquired=acquired,
        )
        actor = await reauthorize(db, proof)
        await _read_binding(db, actor, project_id, key, receipt=receipt)
        await storage_access.check(
            db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            expected_revision=revision,
            lock=True,
        )
        await reauthorize(db, proof)
        await db.commit()
        check_clock(proof)
        return receipt, data
    except HTTPException:
        raise
    except Exception as exc:
        raise http_error(exc) from None


async def unlink(db, actor, request, project_id, key):
    try:
        actor, proof = await lock_authority(db, actor, request)
        projects = await lock_projects(db, actor, [project_id], write_ids=[project_id])
        project = projects[project_id]
        entries = references.indexed_assets(project.guide_assets or [])
        if key not in entries:
            reject("guide_asset_missing", 404)
        await references.reconcile_assets(
            db,
            project,
            project.guide_assets_revision,
            [value for value in project.guide_assets if value["key"] != key],
        )
        await reauthorize(db, proof)
        await db.commit()
        check_clock(proof)
        return {"deleted": key}
    except HTTPException:
        raise
    except Exception as exc:
        raise http_error(exc) from None


async def _has_operation(db, receipt, *, excluding=None):
    statement = select(StorageOperation.id).where(
        StorageOperation.location_id == uuid.UUID(receipt.storage_ref["location_id"]),
        StorageOperation.purpose.in_(
            [purpose(operation, receipt.id) for operation in ("read", "delete")]
        ),
    )
    if excluding is not None:
        statement = statement.where(StorageOperation.id != excluding)
    return await db.scalar(statement.limit(1)) is not None


async def _cleanup_one(db, identifier):
    row = await locked_owner(db, identifier, skip_locked=True)
    if row is None or row.state == "pending":
        await db.commit()
        return "retained"
    if await references._has_reference(db, row.id):
        await db.commit()
        return "retained"
    if row.state == "ready":
        row.state, row.retired_at = "retired", authority.now()
        await db.commit()
        return "retired"
    if row.state != "retired":
        await db.commit()
        return "retained"
    row.last_attempted_at = authority.now()
    receipt = storage.snapshot(row)
    if await _has_operation(db, receipt):
        await db.commit()
        return "retained"
    await db.commit()

    async def acquired(permit):
        current = await locked_owner(db, identifier)
        if (
            current is None
            or current.state != "retired"
            or not storage.same(current, receipt)
            or await references._has_reference(db, identifier)
            or await _has_operation(db, receipt, excluding=permit.id)
        ):
            reject()
        await db.commit()

    await storage_access.run(
        db,
        uuid.UUID(receipt.storage_ref["location_id"]),
        purpose("delete", receipt.id),
        storage.delete,
        storage_service.client,
        receipt,
        on_acquired=acquired,
    )
    current = await locked_owner(db, identifier)
    if (
        current is not None
        and current.state == "retired"
        and storage.same(current, receipt)
        and not await references._has_reference(db, identifier)
        and not await _has_operation(db, receipt)
    ):
        await db.delete(current)
        await db.commit()
        return "objects"
    await db.commit()
    return "retained"


async def cleanup(db, *, identifiers=None, limit=500):
    """No implicit age policy, pending cleanup, JSON scan or reverse Project lock."""
    limit = max(1, min(int(limit), 500))
    if identifiers is None:
        referenced = exists().where(Reference.upload_id == Upload.id)
        identifiers = list(
            await db.scalars(
                select(Upload.id)
                .where(
                    or_(
                        Upload.state == "retired",
                        (Upload.state == "ready") & ~referenced,
                    )
                )
                .order_by(
                    Upload.last_attempted_at.asc().nulls_first(),
                    Upload.created_at,
                    Upload.id,
                )
                .limit(limit)
            )
        )
        await db.commit()
    else:
        identifiers = list(dict.fromkeys(identifiers))[:limit]
    result = {"objects": 0, "retired": 0, "retained": 0, "failed": 0}
    for identifier in identifiers:
        try:
            result[await _cleanup_one(db, identifier)] += 1
        except Exception:
            await db.rollback()
            result["failed"] += 1
            log.warning("Guide asset cleanup retained: %s", identifier)
    return result
