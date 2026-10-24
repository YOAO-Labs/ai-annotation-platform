"""Own synchronous reference inference and qualify native Mask acceptance."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import uuid

import anyio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.db.models.api_key import ApiKey
from app.db.models.ai_mask_accept_decision import AiMaskAcceptDecision
from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.services.content_snapshot import ContentError
from app.services.dataset_content import content_http_error, key_authority
from app.services.dataset_references import reference_context, reference_snapshot
from app.services.image_inference_authority import authorize_image_operation
from app.services.prediction_inputs import PredictionInputs
from app.services.project_access import resolve_project_access
from app.services.project_write_guard import (
    _busy_lock_error,
    lock_actor_project_authority,
    lock_actor_scope,
)
from app.services.reference_bindings import (
    lock_reference_bindings,
    normalize_binding,
    normalize_reference,
    reference_binding,
    task_binding,
)
from app.services.reference_keys import is_reference
from app.services.storage_location import api_node_id
from app.services.task_lock import TaskLockService, TaskLockConflictError
from app.services.task_scope import lock_task_scope


_IDENTITY = (
    "dataset_id",
    "asset_id",
    "sha256",
    "size",
    "width",
    "height",
    "maintenance_revision",
)
_ISSUER = object()


class ReferenceImageOperation:
    provenance_key = "reference_input"

    def __init__(self, db, task, actor, request, operation, prompt_source=None):
        if operation not in {"predict_test", "interactive_predict", "ai_mask_accept"}:
            raise ValueError("unsupported reference image operation")
        self.db, self.task_id, self.project_id = db, task.id, task.project_id
        self.actor_id, self.operation = actor.id, operation
        self.key_id = getattr(request.state, "api_key_id", None)
        self.scopes = ["tasks:read"]
        if operation != "predict_test":
            self.scopes.append("annotations:write")
        self.inputs, self.snapshot, self.url = None, None, None
        self.prompt_source = (
            {
                key: prompt_source.get(key)
                for key in ("source_annotation_id", "source_version", "source_digest")
            }
            if prompt_source
            else None
        )

    async def _authorize(self, db, task, actor):
        await authorize_image_operation(
            db,
            task,
            actor_id=self.actor_id,
            project_id=self.project_id,
            operation=self.operation,
        )

    async def _task(self):
        task = await self.db.get(Task, self.task_id, populate_existing=True)
        if task is None or not is_reference(task.file_path):
            raise ContentError("content_binding_changed")
        if task.batch_id:
            await self.db.get(TaskBatch, task.batch_id, populate_existing=True)
        return task

    async def _keys(self, actor):
        return [
            await key_authority(self.db, actor, self.key_id, scope)
            for scope in self.scopes
        ]

    async def capture(self, *, annotations_read=False):
        if annotations_read:
            self.scopes.append("annotations:read")
        task = await self._task()
        actor = await self.db.get(User, self.actor_id)
        await self._authorize(self.db, task, actor)
        self.keys = await self._keys(actor)
        self.binding = {
            **task_binding(task),
            "reference": await reference_binding(self.db, task),
        }
        self.context = await self._context(task, actor)
        # Entry-point dependencies may have locked membership while authorizing
        # mask prompts. No such lock may span original reads or object/ML I/O.
        await self.db.commit()
        self.snapshot = await reference_snapshot(
            self.db,
            task.dataset_item_id,
            actor,
            task_id=task.id,
            expected_sha256=self.binding["reference"]["sha256"],
            api_key_id=self.key_id,
            task_authorizer=self._authorize,
        )
        ref = self.binding["reference"]
        if (self.snapshot.size, self.snapshot.width, self.snapshot.height) != (
            ref["size"],
            ref["width"],
            ref["height"],
        ):
            raise ContentError("image_size_mismatch")
        await self.validate()
        await self.db.commit()

    async def _context(self, task, actor):
        return await reference_context(
            self.db,
            task.dataset_item_id,
            actor,
            task_id=task.id,
            task_authorizer=self._authorize,
        )

    def identity(self):
        return {
            "schema": 1,
            "item_id": self.binding["item_id"],
            **{key: self.binding["reference"][key] for key in _IDENTITY},
        }

    async def validate(self):
        task = await self._task()
        actor = await self.db.get(User, self.actor_id, populate_existing=True)
        await self._authorize(self.db, task, actor)
        binding = {
            **task_binding(task),
            "reference": await reference_binding(self.db, task),
        }
        if binding != self.binding or await self._context(task, actor) != self.context:
            raise ContentError("content_binding_changed")
        if await self._keys(actor) != self.keys:
            raise ContentError("api_key_authority_revoked", 403)
        if self.inputs and self.inputs.record.expires_at <= datetime.now(timezone.utc):
            raise ContentError("prediction_input_expired")
        await self._validate_prompt_source()
        return task

    async def _validate_prompt_source(self, *, lock=False):
        if not self.prompt_source:
            return
        source = self.prompt_source
        query = select(Annotation).where(
            Annotation.id == uuid.UUID(str(source["source_annotation_id"]))
        )
        if lock:
            query = query.with_for_update(read=True, nowait=True)
        annotation = await self.db.scalar(
            query.execution_options(populate_existing=True)
        )
        if (
            annotation is None
            or annotation.task_id != self.task_id
            or not annotation.is_active
            or annotation.was_cancelled
            or annotation.is_locked
            or annotation.version != source["source_version"]
            or (annotation.geometry or {}).get("type") != "raster_mask"
            or ((annotation.geometry or {}).get("mask") or {}).get("sha256")
            != source["source_digest"]
        ):
            raise ContentError("mask_prompt_source_changed")

    async def deliver(self, factory):
        self.inputs = await PredictionInputs.create(
            factory,
            owner_id=uuid.uuid4(),
            owner_kind=self.operation,
            task_id=self.task_id,
        )
        self.url = await self.inputs.upload_snapshot(self.snapshot)
        await self.validate()
        await self.db.commit()

    async def final(self):
        await lock_actor_project_authority(self.db, self.actor_id, self.project_id)
        await lock_actor_scope(self.db, self.actor_id, self.project_id)
        if self.key_id:
            await self.db.execute(
                select(ApiKey.id)
                .where(ApiKey.id == uuid.UUID(str(self.key_id)))
                .with_for_update(read=True, nowait=True)
            )
        try:
            await TaskLockService(self.db)._lock_task_scope(self.task_id, nowait=True)
        except TaskLockConflictError:
            raise ContentError("reference_inference_busy") from None
        await lock_task_scope(self.db, [self.task_id])
        await self.db.execute(
            select(Task.id).where(Task.id == self.task_id).with_for_update(nowait=True)
        )
        await lock_reference_bindings(self.db, [self.binding], api_node_id())
        await self._validate_prompt_source(lock=True)
        return await self.validate()

    async def close(self):
        if self.inputs:
            await self.inputs.close()
        if self.snapshot:
            self.snapshot.close()


@asynccontextmanager
async def reference_image_operation(
    db,
    task,
    actor,
    request,
    operation,
    factory,
    *,
    annotations_read=False,
    prompt_source=None,
):
    if not is_reference(task.file_path):
        if task.file_type != "image":
            yield None
            return
        from app.services import prediction_originals as originals
        from app.services.managed_image_inference import ManagedImageOperation

        owner = ManagedImageOperation(
            db, task, actor, request, operation, prompt_source
        )
        failure = None
        try:
            await owner.open(factory, annotations_read=annotations_read)
            yield owner
        except BaseException as exc:
            failure = exc
            if isinstance(exc, DBAPIError) and _busy_lock_error(exc):
                raise HTTPException(
                    409, detail={"code": "managed_inference_busy"}
                ) from None
            if isinstance(exc, Exception):
                raise content_http_error(exc) from None
            raise
        finally:
            try:
                await originals.settled(db.rollback())
            finally:
                await owner.close(failure)
        return
    owner = ReferenceImageOperation(db, task, actor, request, operation, prompt_source)
    try:
        await owner.capture(annotations_read=annotations_read)
        await owner.deliver(factory)
        yield owner
    except Exception as exc:
        if isinstance(exc, DBAPIError) and _busy_lock_error(exc):
            await db.rollback()
            raise HTTPException(
                409, detail={"code": "reference_inference_busy"}
            ) from None
        raise content_http_error(exc) from None
    finally:
        # Release response locks before cleanup's independent transaction and I/O.
        with anyio.CancelScope(shield=True):
            try:
                await db.rollback()
            finally:
                await owner.close()


@dataclass(frozen=True, slots=True)
class _NativeReceipt:
    issuer: object
    session: object
    transaction: object
    actor_id: object
    task_id: object
    binding: str
    request_digest: str
    mask_digest: str
    evidence: str


async def qualify_native_acceptance(db, task, actor, request, data):
    from app.services.ai_mask_accept import (
        _validate_receipt,
        _validate_replay,
        ai_mask_accept_request_digest,
    )
    from app.services.raster_mask_storage import build_rle_reference

    if db.sync_session.in_nested_transaction():
        raise ContentError("prediction_reference_receipt_invalid")
    mask = build_rle_reference(
        data.candidate.candidate.value.rle.model_dump(mode="json")
    )
    request_digest = ai_mask_accept_request_digest(data)
    query = (
        select(AiMaskAcceptDecision)
        .where(
            AiMaskAcceptDecision.task_id == task.id,
            AiMaskAcceptDecision.idempotency_key == data.idempotency_key,
        )
        .execution_options(populate_existing=True)
    )
    existing = await db.scalar(query)
    claims = (
        None
        if existing
        else _validate_receipt(data, task_id=task.id, content_digest=mask["sha256"])
    )
    owner = ReferenceImageOperation(db, task, actor, request, "ai_mask_accept")
    try:
        await owner.capture(annotations_read=data.target.mode == "refine")
        await owner.final()
        current = await db.scalar(query.with_for_update(read=True, nowait=True))
        if current:
            project = await db.get(Project, task.project_id, populate_existing=True)
            access = await resolve_project_access(db, user=actor, project=project)
            _validate_replay(current, request_digest, actor, access=access)
            evidence = (current.response_json or {}).get("_reference_input") or {}
            origin = {
                key: evidence.get(key, 1 if key == "maintenance_revision" else None)
                for key in owner.identity()
            }
        elif existing:
            raise ContentError("prediction_reference_receipt_invalid")
        else:
            origin = claims.get("reference_input")
        if isinstance(origin, dict):
            origin = normalize_reference(origin)
        if origin != owner.identity():
            raise ContentError("prediction_requires_reference_provenance")
        return _NativeReceipt(
            _ISSUER,
            db.sync_session,
            db.sync_session.get_transaction(),
            actor.id,
            task.id,
            json.dumps(owner.binding, sort_keys=True),
            request_digest,
            mask["sha256"],
            json.dumps(
                {
                    **owner.identity(),
                    "source_id": owner.binding["reference"]["source_id"],
                    "source_revision": owner.binding["reference"]["source_revision"],
                    "accepted_revision_id": owner.binding["reference"][
                        "accepted_revision_id"
                    ],
                    "actor_id": str(actor.id),
                    "node_id": api_node_id(),
                },
                sort_keys=True,
            ),
        )
    finally:
        await owner.close()


async def validate_native_receipt(
    db, task_id, project_id, receipt, *, actor_id=None, data=None, result=None
):
    if (
        type(receipt) is not _NativeReceipt
        or receipt.issuer is not _ISSUER
        or receipt.session is not db.sync_session
        or receipt.transaction is None
        or receipt.transaction is not db.sync_session.get_transaction()
        or db.sync_session.in_nested_transaction()
        or receipt.task_id != task_id
        or (actor_id is not None and receipt.actor_id != actor_id)
    ):
        raise ContentError("prediction_reference_receipt_invalid")
    task = await db.get(Task, task_id, populate_existing=True)
    if (
        task is None
        or task.project_id != project_id
        or not is_reference(task.file_path)
    ):
        raise ContentError("prediction_reference_receipt_invalid")
    binding = {**task_binding(task), "reference": await reference_binding(db, task)}
    if binding != normalize_binding(json.loads(receipt.binding)):
        raise ContentError("content_binding_changed")
    if data is not None:
        from app.services.ai_mask_accept import ai_mask_accept_request_digest

        if ai_mask_accept_request_digest(data) != receipt.request_digest:
            raise ContentError("prediction_reference_receipt_invalid")
    if result is not None:
        if len(result) != 1:
            raise ContentError("reference_prediction_geometry_not_supported")
        geometry = result[0].get("geometry") or {}
        if (
            geometry.get("type") != "raster_mask"
            or (geometry.get("mask") or {}).get("sha256") != receipt.mask_digest
        ):
            raise ContentError("reference_prediction_geometry_not_supported")
    return json.loads(receipt.evidence)
