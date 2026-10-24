"""Explicit terminal attachment destruction for guarded fixture cleanup only.

Retain the fixture and upload rows as retry anchors until the final database
transaction succeeds. No production consumer or retention policy uses this path.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import uuid

from botocore.exceptions import ClientError
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.db.models.annotation import Annotation
from app.db.models.comment_attachment_upload import CommentAttachmentUpload as Upload
from app.db.models.project import Project
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.task import Task
from app.db.models.user import User
from app.services import comment_attachment_storage as storage, storage_access
from app.services.content_snapshot import ContentError
from app.services.storage import storage_service


@dataclass(frozen=True)
class FixtureScope:
    project_ids: tuple = ()
    user_ids: tuple = ()
    annotation_ids: tuple = ()
    task_ids: tuple = ()
    dataset_ids: tuple = ()


@dataclass
class CleanupPlan:
    scope: FixtureScope
    receipts: tuple
    retired_at: dict
    revisions: dict = field(default_factory=dict)

    @property
    def annotation_ids(self):
        return tuple(
            sorted(
                set(self.scope.annotation_ids)
                | {receipt.annotation_id for receipt in self.receipts}
            )
        )


def reject(code="e2e_seed_attachment_cleanup_changed", status=409):
    raise HTTPException(
        status,
        {
            "code": code,
            "action": "结束已知操作后重试；pending或未知责任请销毁整个已验证的一次性mode",
        },
    )


def translate(exc):
    if isinstance(exc, DBAPIError):
        state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if state in {"55P03", "40P01", "40001"}:
            reject("e2e_seed_attachment_cleanup_busy")
    if isinstance(exc, ContentError):
        reject(exc.code, exc.status)
    if isinstance(exc, storage.CommentAttachmentStorageError):
        reject(exc.code, exc.status_code)
    reject("e2e_seed_attachment_cleanup_unavailable", 503)


def purposes(identifier):
    suffix = identifier.hex
    return tuple(
        prefix + suffix
        for prefix in (
            "comment_attachment_read_",
            "comment_attachment_write_",
            "comment_attachment_delete_",
            "comment_attachment_seed_delete_",
        )
    )


async def operations(db, receipt, *, excluding=None):
    statement = select(StorageOperation.id).where(
        StorageOperation.location_id == uuid.UUID(receipt.storage_ref["location_id"]),
        StorageOperation.purpose.in_(purposes(receipt.id)),
    )
    if excluding is not None:
        statement = statement.where(StorageOperation.id != excluding)
    if await db.scalar(statement.limit(1)) is not None:
        reject("e2e_seed_attachment_operation_unsettled")


async def owners(db, scope, *, lock=False):
    statement = (
        select(Upload)
        .where(Upload.project_id.in_(scope.project_ids))
        .order_by(Upload.id)
    )
    if lock:
        statement = statement.with_for_update(nowait=True)
    with db.no_autoflush:
        return list(
            await db.scalars(statement.execution_options(populate_existing=True))
        )


async def validate_context(db, row):
    # Missing historical children do not discard an immutable project snapshot.
    # An existing child in another actual project is not a fixture-owned target.
    task = await db.get(Task, row.task_id, populate_existing=True)
    annotation = await db.get(Annotation, row.annotation_id, populate_existing=True)
    if task is not None and task.project_id != row.project_id:
        reject()
    if annotation is not None and annotation.task_id != row.task_id:
        reject()


async def prepare(db, scope):
    """Validate the entire set before retirement or any physical deletion."""
    try:
        rows = await owners(db, scope, lock=True)
        if any(row.state == "pending" for row in rows):
            reject("e2e_seed_attachment_pending")
        receipts = []
        for row in rows:
            if row.state not in {"ready", "retired"}:
                reject()
            receipts.append(storage.snapshot(row))
            await validate_context(db, row)
            await operations(db, receipts[-1])
        retired_at = {}
        for row in rows:
            if row.state == "ready":
                row.state = "retired"
                row.retired_at = datetime.now(timezone.utc)
            retired_at[row.id] = row.retired_at
        await db.commit()
        return CleanupPlan(scope, tuple(receipts), retired_at)
    except HTTPException:
        raise
    except Exception as exc:
        translate(exc)


def same(row, receipt, plan):
    return (
        row is not None
        and row.state == "retired"
        and row.retired_at == plan.retired_at[receipt.id]
        and storage.same(row, receipt)
    )


def delete_and_verify(client, receipt):
    """Settle exact saved-key destruction inside the registered executor call."""
    storage.delete(client, receipt)
    owner = storage.resolve(receipt)
    try:
        client.head_object(Bucket=owner.bucket, Key=owner.object_key)
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code", "")) in {
            "404",
            "NoSuchKey",
            "NotFound",
            "NoSuchVersion",
        }:
            return
        raise
    reject("e2e_seed_attachment_object_remaining")


async def delete_objects(db, plan, resolve_scope):
    """Keep every row after I/O, including successful deletion, for retries."""
    try:
        for receipt in plan.receipts:
            revision = None

            async def acquired(permit):
                nonlocal revision
                if await resolve_scope(db) != plan.scope:
                    reject()
                with db.no_autoflush:
                    current = await db.scalar(
                        select(Upload)
                        .where(Upload.id == receipt.id)
                        .with_for_update(nowait=True)
                        .execution_options(populate_existing=True)
                    )
                if not same(current, receipt, plan):
                    reject()
                await operations(db, receipt, excluding=permit.id)
                await db.commit()
                revision = permit.revision

            await storage_access.run(
                db,
                uuid.UUID(receipt.storage_ref["location_id"]),
                "comment_attachment_seed_delete_" + receipt.id.hex,
                delete_and_verify,
                storage_service.client,
                receipt,
                on_acquired=acquired,
            )
            location = uuid.UUID(receipt.storage_ref["location_id"])
            if location in plan.revisions and plan.revisions[location] != revision:
                reject()
            plan.revisions[location] = revision
    except HTTPException:
        raise
    except Exception as exc:
        translate(exc)


async def finalize(db, plan, resolve_scope, *, before_gate=None):
    """Fence the complete scope before the caller's final database-only deletes."""
    try:
        with db.no_autoflush:
            for model, identifiers in (
                (User, plan.scope.user_ids),
                (Project, plan.scope.project_ids),
            ):
                await db.execute(
                    select(model.id)
                    .where(model.id.in_(identifiers))
                    .order_by(model.id)
                    .with_for_update(nowait=True)
                )
            if await resolve_scope(db) != plan.scope:
                reject()
            rows = await owners(db, plan.scope, lock=True)
            expected = {receipt.id: receipt for receipt in plan.receipts}
            if {row.id for row in rows} != set(expected):
                reject()
            for row in rows:
                receipt = expected[row.id]
                if not same(row, receipt, plan):
                    reject()
                await operations(db, receipt)
            if before_gate is not None:
                await before_gate()
            for location, revision in sorted(plan.revisions.items()):
                await storage_access.check(
                    db, location, expected_revision=revision, lock=True
                )
            for row in rows:
                await db.delete(row)
            await db.flush()
    except HTTPException:
        raise
    except Exception as exc:
        translate(exc)


async def assert_empty(db, plan):
    if await db.scalar(
        select(Upload.id).where(Upload.project_id.in_(plan.scope.project_ids)).limit(1)
    ):
        reject("e2e_seed_attachment_cleanup_incomplete", 500)
