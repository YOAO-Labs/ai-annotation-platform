"""Authenticated guide-image publication, current references and durable cleanup."""

import hashlib
import json
import logging
import uuid
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.enums import PlatformRole
from app.db.models.guide_asset_upload import GuideAssetUpload
from app.services import conditional_write_accounting as accounting
from app.services import comment_attachment_authority as authority
from app.services import guide_asset_references as references
from app.services import guide_asset_storage as storage, storage_access
from app.services import storage_migration_artifact_reads as migration_reads
from app.services import storage_migration_read_publications as read_publications
from app.services.content_snapshot import ContentError
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.project_access import lock_current_project_access
from app.services.project_workspace import lock_project_row
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


async def _lock_actor_share(db, actor_id):
    """Retain the current account SHARE fence before any Project lock."""
    try:
        accounts = await IdentityAccountLockData(db).lock_accounts(
            [actor_id], mode="share", nowait=True
        )
    except DBAPIError as exc:
        references.db_error(exc)
    account = accounts.get(actor_id)
    if account is None or not account.is_active:
        reject("guide_asset_authority_revoked", 403)
    return account


async def _set_wait_timeout(db):
    previous = await db.scalar(text("SHOW lock_timeout"))
    await db.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": "1s"},
    )
    return previous


async def _restore_timeout(db, previous):
    await db.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": previous},
    )


async def lock_projects(db, actor, ids, *, write_ids=(), wait=False):
    """Fence account, Project and membership authority through owner contracts.

    SHARE/NOWAIT phases are used off the ordinary path. A single ``wait`` lock
    keeps the same account -> Project -> membership order but bounds the
    Project lock to one second, rechecking the fresh account and membership
    facts afterwards. No private Project or membership row is read directly.
    A bounded wait that expires leaves the transaction aborted for the caller's
    rollback, which also discards the local setting and keeps the lock SQLSTATE
    intact. Restoring the setting there would replace it with 25P02.
    """
    identifiers = sorted({uuid.UUID(str(value)) for value in ids})
    writes = {uuid.UUID(str(value)) for value in write_ids}
    if wait and len(identifiers) != 1:
        raise ValueError("Bounded waiting requires exactly one project")
    result = {}
    try:
        if wait:
            await _lock_actor_share(db, actor.id)
        for identifier in identifiers:
            if wait:
                previous = await _set_wait_timeout(db)
                if identifier in writes:
                    project = await lock_project_row(
                        db,
                        identifier,
                        nowait=False,
                        populate_existing=True,
                    )
                else:
                    project = await db.scalar(
                        text(
                            "SELECT yoao_workspace_v1.lock_project_reference("
                            "CAST(:project_id AS uuid))"
                        ),
                        {"project_id": identifier},
                    )
                await _restore_timeout(db, previous)
                if project is None:
                    reject("guide_asset_project_missing", 404)
            _, project, access = await lock_current_project_access(
                db, actor_id=actor.id, project_id=identifier
            )
            if identifier in writes and not access.is_manager:
                reject("guide_asset_manager_required", 403)
            result[identifier] = project
    except HTTPException as exc:
        if exc.status_code == 409:
            reject("guide_asset_busy")
        if exc.status_code == 401:
            reject("guide_asset_authority_revoked", 403)
        raise
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


async def locked_owner(db, identifier, *, skip_locked=False):
    try:
        with db.no_autoflush:
            return (
                await db.execute(
                    text(
                        "SELECT * FROM yoao_workspace_v1.lock_guide_upload(:id, :skip)"
                    ),
                    {"id": identifier, "skip": skip_locked},
                )
            ).one_or_none()
    except DBAPIError as exc:
        references.db_error(exc)


async def capture_upload(db, actor, request, project_id):
    actor, proof = await lock_authority(db, actor, request)
    await lock_projects(db, actor, [project_id], write_ids=[project_id], wait=True)
    await reauthorize(db, proof)
    await db.commit()
    check_clock(proof)
    return proof


async def upload(db, proof, project_id, data, mime_type, file_name):
    """Preserve every uncertain write as pending, with no compensating delete."""
    recorder, owned_permit, failure, result = None, None, None, None
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
        await lock_projects(db, actor, [project_id], write_ids=[project_id], wait=True)
        row = SimpleNamespace(
            id=uuid.uuid4(),
            owner_id=proof.actor_id,
            project_id=project_id,
            sha256=checksum,
            size_bytes=len(data),
            mime_type=mime_type,
            file_name=file_name,
            created_at=authority.now(),
        )
        row.storage_ref = await storage.allocate(db, row)
        await db.execute(
            text(
                "SELECT yoao_workspace_v1.stage_guide_upload("
                ":id, :owner_id, :project_id, CAST(:storage_ref AS jsonb), "
                ":sha256, :size_bytes, :mime_type, :file_name, :created_at)"
            ),
            {**vars(row), "storage_ref": json.dumps(row.storage_ref)},
        )
        receipt = storage.snapshot(row)
        await reauthorize(db, proof)
        await db.commit()
        check_clock(proof)
        recorder = accounting.recorder(
            db, receipt, producer="guide_asset_uploads", purpose="other_platform"
        )
        revision = None
        target = None

        async def acquired(permit):
            nonlocal revision, target, owned_permit
            owned_permit = permit
            actor = await reauthorize(db, proof)
            await lock_projects(
                db, actor, [project_id], write_ids=[project_id], wait=True
            )
            current = await locked_owner(db, receipt.id)
            if (
                current is None
                or current.state != "pending"
                or not storage.same(current, receipt)
            ):
                reject()
            await reauthorize(db, proof)
            target = await storage.io_target(db, receipt)
            await db.commit()
            check_clock(proof)
            revision = permit.revision

        def write_saved():
            check_clock(proof)
            storage.run_io(
                target,
                storage_service.client,
                "write",
                receipt,
                data,
                permit=owned_permit,
                accounting=recorder,
            )

        await storage_access.run(
            db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            purpose("write", receipt.id),
            write_saved,
            on_acquired=acquired,
        )
        actor = await reauthorize(db, proof)
        entry = storage.entry(receipt)
        await references.publish(
            db,
            actor_id=actor.id,
            project_id=project_id,
            receipt=receipt,
            entry=entry,
            maintenance_revision=revision,
        )
        await reauthorize(db, proof)
        await db.commit()
        check_clock(proof)
        result = entry
    except BaseException as exc:
        failure = exc
    if recorder is not None:
        failure = await accounting.finish(
            recorder,
            failure,
            settled=owned_permit is not None
            and getattr(owned_permit, "_settled", False) is True,
        )
    if isinstance(failure, HTTPException):
        raise failure
    if isinstance(failure, Exception):
        raise http_error(failure) from None
    if failure is not None:
        raise failure
    return result


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
    projects = await lock_projects(db, actor, [project_id], wait=True)
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
    try:
        row = (
            await db.execute(
                text(
                    "SELECT * FROM yoao_workspace_v1.mount_guide_upload(:project, :id)"
                ),
                {"project": project_id, "id": identifier},
            )
        ).one_or_none()
    except DBAPIError as exc:
        references.db_error(exc)
    if row is None:
        reject(
            "guide_asset_storage_unpinned" if registered else "guide_asset_missing",
            409 if registered else 404,
        )
    if row.project_id != original_project_id:
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
        owner = storage.resolve(receipt)
        read_route = await migration_reads.resolve(
            db, owner.ref, owner.object_key, receipt.sha256, receipt.size_bytes
        )
        await reauthorize(db, proof)
        await db.commit()
        check_clock(proof)
        revision = None
        target = physical_permit = None

        async def acquired(permit):
            nonlocal revision, target, physical_permit
            physical_permit = permit
            actor = await reauthorize(db, proof)
            await _read_binding(db, actor, project_id, key, receipt=receipt)
            await reauthorize(db, proof)
            if read_route is None:
                target = await storage.io_target(db, receipt)
            await db.commit()
            check_clock(proof)
            revision = permit.revision

        def read_saved():
            check_clock(proof)
            return storage.run_io(
                target, storage_service.client, "read", receipt, permit=physical_permit
            )

        if read_route is None:
            data = await storage_access.run(
                db,
                uuid.UUID(receipt.storage_ref["location_id"]),
                purpose("read", receipt.id),
                read_saved,
                on_acquired=acquired,
            )
        else:

            def read_mapped(reader):
                check_clock(proof)
                return storage.read_routed(reader, receipt)

            data = await migration_reads.run(
                db,
                read_route,
                purpose("read", receipt.id),
                read_mapped,
                on_acquired=acquired,
            )
        actor = await reauthorize(db, proof)
        await _read_binding(db, actor, project_id, key, receipt=receipt)
        if read_route is None:
            await storage_access.check(
                db,
                uuid.UUID(receipt.storage_ref["location_id"]),
                expected_revision=revision,
                lock=True,
            )
        else:
            await read_publications.recheck(db, read_route, lock=True)
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
        await references.unlink(db, actor_id=actor.id, project_id=project_id, key=key)
        await reauthorize(db, proof)
        await db.commit()
        check_clock(proof)
        return {"deleted": key}
    except HTTPException:
        raise
    except Exception as exc:
        raise http_error(exc) from None


async def _has_operation(db, receipt, *, excluding=None):
    return bool(
        await db.scalar(
            text(
                "SELECT yoao_assets_storage_v1.physical_io_is_open(:read, NULL, :excluded) "
                "OR yoao_assets_storage_v1.physical_io_is_open(:write, :location, :excluded) "
                "OR yoao_assets_storage_v1.physical_io_is_open(:delete, :location, :excluded)"
            ),
            {
                "read": purpose("read", receipt.id),
                "write": purpose("write", receipt.id),
                "delete": purpose("delete", receipt.id),
                "location": uuid.UUID(receipt.storage_ref["location_id"]),
                "excluded": excluding,
            },
        )
    )


async def _has_reference(db, identifier):
    return await db.scalar(
        text("SELECT yoao_workspace_v1.guide_upload_has_reference(:id)"),
        {"id": identifier},
    )


async def _cleanup_one(db, identifier):
    row = await locked_owner(db, identifier, skip_locked=True)
    if row is None or row.state == "pending":
        await db.commit()
        return "retained"
    if await _has_reference(db, row.id) or row.state not in {"ready", "retired"}:
        await db.commit()
        return "retained"
    updated = await db.scalar(
        text("SELECT yoao_workspace_v1.attempt_guide_retirement(:id, :now)"),
        {"id": row.id, "now": authority.now()},
    )
    if updated != "retired":
        reject()
    if row.state == "ready":
        await db.commit()
        return "retired"
    receipt = storage.snapshot(row)
    if await _has_operation(db, receipt):
        await db.commit()
        return "retained"
    await db.commit()

    target = physical_permit = None

    async def acquired(permit):
        nonlocal target, physical_permit
        physical_permit = permit
        current = await locked_owner(db, identifier)
        if (
            current is None
            or current.state != "retired"
            or not storage.same(current, receipt)
            or await _has_reference(db, identifier)
            or await _has_operation(db, receipt, excluding=permit.id)
        ):
            reject()
        target = await storage.io_target(db, receipt)
        await db.commit()

    await storage_access.run(
        db,
        uuid.UUID(receipt.storage_ref["location_id"]),
        purpose("delete", receipt.id),
        lambda: storage.run_io(
            target, storage_service.client, "delete", receipt, permit=physical_permit
        ),
        on_acquired=acquired,
    )
    current = await locked_owner(db, identifier)
    if (
        current is not None
        and current.state == "retired"
        and storage.same(current, receipt)
        and not await _has_reference(db, identifier)
        and not await _has_operation(db, receipt)
    ):
        if not await db.scalar(
            text(
                "SELECT yoao_workspace_v1.settle_guide_retirement(:id, CAST(:ref AS jsonb))"
            ),
            {"id": identifier, "ref": json.dumps(receipt.storage_ref)},
        ):
            reject()
        await db.commit()
        cached = db.sync_session.identity_map.get(
            db.sync_session.identity_key(GuideAssetUpload, identifier)
        )
        if cached is not None:
            db.sync_session.expunge(cached)
        return "objects"
    await db.commit()
    return "retained"


async def cleanup(db, *, identifiers=None, limit=500):
    """No implicit age policy, pending cleanup, JSON scan or reverse Project lock."""
    limit = max(1, min(int(limit), 500))
    if identifiers is None:
        identifiers = list(
            await db.scalars(
                text("SELECT yoao_workspace_v1.guide_retirement_candidates(:limit)"),
                {"limit": limit},
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
