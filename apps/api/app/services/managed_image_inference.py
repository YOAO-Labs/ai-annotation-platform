"""Own one synchronous managed-image request and its verified physical inputs.

Final checks join the caller transaction. Source capture, target delivery and
cleanup use short independent transactions, with physical handles drained before
their durable operation can settle.
"""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.services import prediction_inputs, prediction_originals as originals
from app.services import storage_access
from app.services.content_snapshot import ContentError, MAX_BYTES, MAX_PIXELS
from app.services.dataset_content import content_http_error, key_authority
from app.services.image_inference_authority import authorize_image_operation
from app.services.project_write_guard import (
    lock_actor_project_authority,
    lock_actor_scope,
)
from app.services.reference_bindings import task_binding
from app.services.reference_keys import is_reference
from app.services.storage_location import api_node_id
from app.services.task_lock import TaskLockConflictError, TaskLockService
from app.services.task_scope import lock_task_scope


_SHA = re.compile(r"[0-9a-f]{64}")
_FIELDS = frozenset(
    {
        "schema",
        "task_id",
        "owner_kind",
        "content_id",
        "sha256",
        "size_bytes",
        "width",
        "height",
        "location_id",
        "location_revision",
        "maintenance_revision",
        "facts_digest",
    }
)


def managed_inference_http_error(exc):
    error = content_http_error(exc)
    messages = {
        "prediction_requires_original_provenance": "候选缺少可核验的原图来源，请刷新页面后重新运行推理。",
        "prediction_original_identity_invalid": "候选的原图来源信息已失效，请刷新页面后重新运行推理。",
        "prediction_original_receipt_invalid": "本次原图校验已失效，请刷新页面后重新运行推理。",
        "content_binding_changed": "原图来源已变化，请刷新页面后重新运行推理。",
        "prediction_input_expired": "本次推理输入已过期，请刷新页面后重新运行推理。",
    }
    if isinstance(exc, ContentError) and exc.code in messages:
        error.detail = {**error.detail, "message": messages[exc.code]}
    return error


def normalize_identity(value):
    """Validate the exact public managed provenance, without private locators."""
    try:
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise ValueError
        if type(value["schema"]) is not int or value["schema"] != 1:
            raise ValueError
        if value["owner_kind"] not in {"dataset_asset", "task_original"}:
            raise ValueError
        for name in ("task_id", "content_id", "location_id"):
            if (
                type(value[name]) is not str
                or str(uuid.UUID(value[name])) != value[name]
            ):
                raise ValueError
        for name in ("sha256", "facts_digest"):
            if type(value[name]) is not str or _SHA.fullmatch(value[name]) is None:
                raise ValueError
        for name in ("location_revision", "maintenance_revision"):
            if type(value[name]) is not int or not 1 <= value[name] <= 2**63 - 1:
                raise ValueError
        if (
            type(value["size_bytes"]) is not int
            or not 0 < value["size_bytes"] <= MAX_BYTES
        ):
            raise ValueError
        if (
            any(
                type(value[name]) is not int or not 0 < value[name] <= MAX_PIXELS
                for name in ("width", "height")
            )
            or value["width"] * value["height"] > MAX_PIXELS
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ContentError("prediction_original_identity_invalid") from None
    return deepcopy(value)


class ManagedImageOperation:
    provenance_key = "original_input"

    def __init__(self, db, task, actor, request, operation, prompt_source=None):
        if operation not in {"predict_test", "interactive_predict", "ai_mask_accept"}:
            raise ValueError("unsupported managed image operation")
        self.db, self.task_id, self.project_id = db, task.id, task.project_id
        self.actor_id, self.operation = actor.id, operation
        self.request = request
        key_id = getattr(request.state, "api_key_id", None)
        self.key_id = uuid.UUID(str(key_id)) if key_id is not None else None
        self.request_id = uuid.uuid4()
        self.prompt_source = (
            {
                name: deepcopy(prompt_source.get(name))
                for name in ("source_annotation_id", "source_version", "source_digest")
            }
            if prompt_source
            else None
        )
        self.binding = self.credentials = self._identity = None
        self.inputs = self.source = self.snapshot = self.url = None
        self._annotations_read = False
        self._ready = self._closed = self._unsettled = False
        self._acceptance_closed = False
        self._failure = None
        self._close_lock = asyncio.Lock()

    def _remember(self, failure):
        if failure is None:
            return
        if self._failure is None:
            self._failure = failure
        else:
            originals._uncertain(self._failure, failure)
            originals._uncertain(failure, self._failure)
        self._unsettled |= bool(getattr(failure, "storage_operation_unsettled", False))

    def _check_open(self):
        if self._closed or self._unsettled or not self._ready:
            raise ContentError("prediction_input_expired")
        originals.check_clock(self.credentials)
        if self.inputs is not None and self.inputs.record.expires_at <= datetime.now(
            timezone.utc
        ):
            raise ContentError("prediction_input_expired")
        if self.operation == "ai_mask_accept":
            if not self._acceptance_closed:
                raise ContentError("prediction_original_close_required")
        elif (
            self.inputs is None
            or self.inputs._closed
            or self.snapshot is None
            or self.snapshot._closed
        ):
            raise ContentError("prediction_input_expired")

    async def _prompt(self, db, *, lock=False):
        if self.prompt_source is None:
            return
        source = self.prompt_source
        try:
            identifier = uuid.UUID(str(source["source_annotation_id"]))
            if (
                type(source["source_version"]) is not int
                or source["source_version"] < 1
                or not isinstance(source["source_digest"], str)
                or _SHA.fullmatch(source["source_digest"]) is None
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ContentError("mask_prompt_source_changed") from None
        query = select(Annotation).where(Annotation.id == identifier)
        if lock:
            query = query.with_for_update(read=True, nowait=True)
        annotation = await db.scalar(query.execution_options(populate_existing=True))
        geometry = annotation.geometry if annotation is not None else None
        if not isinstance(geometry, dict):
            raise ContentError("mask_prompt_source_changed")
        if (
            annotation is None
            or annotation.task_id != self.task_id
            or not annotation.is_active
            or annotation.was_cancelled
            or annotation.is_locked
            or annotation.version != source["source_version"]
            or geometry.get("type") != "raster_mask"
            or (geometry.get("mask") or {}).get("sha256") != source["source_digest"]
        ):
            raise ContentError("mask_prompt_source_changed")

    async def _parents(self, db):
        await lock_actor_project_authority(db, self.actor_id, self.project_id)
        await lock_actor_scope(db, self.actor_id, self.project_id)

    async def _task(self, db):
        task = await originals.locked_task(db, self.task_id)
        if is_reference(task.file_path):
            raise ContentError("content_binding_changed")
        if task.batch_id is not None:
            await db.get(TaskBatch, task.batch_id, populate_existing=True)
        return task

    async def _business(self, db, task, actor):
        await authorize_image_operation(
            db,
            task,
            actor_id=self.actor_id,
            project_id=self.project_id,
            operation=self.operation,
        )
        if self._annotations_read:
            await key_authority(db, actor, self.key_id, "annotations:read")

    async def _bound(self, db, task, actor):
        await self._business(db, task, actor)
        current = {
            **task_binding(task),
            "original": await originals.managed_binding(db, task, lock=True),
        }
        if current != self.binding:
            raise ContentError("content_binding_changed")
        await self._prompt(db, lock=True)
        return task

    async def _guard(self, db):
        """Only the supplied short transaction; no inputs/session recursion."""
        await self._parents(db)
        actor = await originals.reauthorize(
            db, self.credentials, self.actor_id, self.key_id
        )
        task = await self._task(db)
        return await self._bound(db, task, actor)

    async def _fresh(self):
        async with AsyncSession(
            bind=storage_access.engine(self.db),
            expire_on_commit=False,
            autoflush=False,
        ) as db:
            return await self._guard(db)

    async def _freeze(self, *, annotations_read):
        if self.binding is not None or self._closed:
            raise ContentError("prediction_input_already_open")
        self._annotations_read = annotations_read
        await self._parents(self.db)
        actor = await self.db.get(User, self.actor_id, populate_existing=True)
        if actor is None:
            raise ContentError("prediction_authority_revoked", 403)
        self.credentials = await originals.capture_credentials(
            self.db, actor, self.request
        )
        task = await self._task(self.db)
        await self._business(self.db, task, actor)
        self.binding = originals.bounded_proof(
            {
                **task_binding(task),
                "original": await originals.managed_binding(self.db, task, lock=True),
            }
        )
        await self._prompt(self.db, lock=True)
        await self.db.commit()

    def _freeze_identity(self):
        source = self.binding["original"]
        self._identity = normalize_identity(
            {
                "schema": 1,
                "task_id": str(self.task_id),
                "owner_kind": source["owner_kind"],
                "content_id": source["content_id"],
                "sha256": source["sha256"],
                "size_bytes": source["size_bytes"],
                "width": self.snapshot.width,
                "height": self.snapshot.height,
                "location_id": source["location_id"],
                "location_revision": source["location_revision"],
                "maintenance_revision": source["maintenance_revision"],
                "facts_digest": source["facts_digest"],
            }
        )

    async def open(self, factory, *, annotations_read=False):
        if self.operation == "ai_mask_accept":
            raise ValueError("native acceptance must not upload model inputs")
        try:
            await self._freeze(annotations_read=annotations_read)
            self.inputs = await originals.settled(
                prediction_inputs.PredictionInputs.create(
                    factory,
                    owner_id=self.request_id,
                    owner_kind=self.operation,
                    task_id=self.task_id,
                    guard=self._guard,
                    source_ref=self.binding,
                ),
                late=lambda value: value.close(),
            )
            self.source = await originals.settled(
                originals.open_snapshot(
                    self.db,
                    self.task_id,
                    self.binding["original"],
                    guard=self._guard,
                    purpose=prediction_inputs.purpose(self.inputs.record),
                    node_id=api_node_id(),
                ),
                late=lambda value: value.close(),
            )
            self.snapshot = self.source.snapshot
            self._freeze_identity()
            self.url = await self.inputs.upload_snapshot(self.snapshot)
            self._ready = True
            await self.validate()
            return self
        except BaseException as failure:
            self._remember(failure)
            raise

    async def open_for_acceptance(self, *, annotations_read=False):
        if self.operation != "ai_mask_accept":
            raise ValueError("only native acceptance may omit model inputs")
        try:
            await self._freeze(annotations_read=annotations_read)
            self.source = await originals.settled(
                originals.open_snapshot(
                    self.db,
                    self.task_id,
                    self.binding["original"],
                    guard=self._guard,
                    purpose="image_accept_" + self.request_id.hex,
                    node_id=api_node_id(),
                ),
                late=lambda value: value.close(),
            )
            self.snapshot = self.source.snapshot
            self._freeze_identity()
            await originals.settled(self._fresh())
            await originals.settled(self.db.rollback())
            await originals.settled(self.source.close())
            self.source = None
            self._acceptance_closed = True
            self._ready = True
            await self.validate()
            return self
        except BaseException as failure:
            self._remember(failure)
            raise

    def identity(self):
        self._check_open()
        return normalize_identity(self._identity)

    async def validate(self):
        self._check_open()
        if self.inputs is not None:
            await self.inputs.check_live()
        return await originals.settled(self._fresh())

    async def final(self):
        """Metadata-only final fence in the caller's existing transaction."""
        self._check_open()
        if self.db.sync_session.in_nested_transaction():
            raise ContentError("prediction_original_receipt_invalid")
        await self._parents(self.db)
        actor = await originals.reauthorize(
            self.db, self.credentials, self.actor_id, self.key_id
        )
        try:
            await TaskLockService(self.db)._lock_task_scope(self.task_id, nowait=True)
        except TaskLockConflictError:
            raise ContentError("managed_inference_busy") from None
        await lock_task_scope(self.db, [self.task_id])
        task = await self.db.scalar(
            select(Task)
            .where(Task.id == self.task_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
        if task is None or is_reference(task.file_path):
            raise ContentError("content_binding_changed")
        if task.batch_id is not None:
            await self.db.get(TaskBatch, task.batch_id, populate_existing=True)
        await self._bound(self.db, task, actor)
        if self.inputs is not None:
            await prediction_inputs.check_metadata(self.db, self.inputs)
        elif self.operation != "ai_mask_accept" or not self._acceptance_closed:
            raise ContentError("prediction_original_close_required")
        self._check_open()
        return task

    async def close(self, failure=None):
        self._remember(failure)
        await originals.settled(self._close())

    async def _close(self):
        async with self._close_lock:
            if self._closed:
                if self._unsettled:
                    raise storage_access.mark_unsettled(
                        ContentError("prediction_original_close_unknown", 503)
                    )
                return
            self._closed = True
            closing = None
            try:
                if self.inputs is not None:
                    await originals.settled(self.inputs.close())
            except BaseException as failure:
                closing = failure
                self._remember(failure)
            finally:
                if self.source is not None:
                    try:
                        await originals.settled(self.source.close(self._failure))
                        if not self._unsettled:
                            self.source = None
                    except BaseException as failure:
                        if closing is None:
                            closing = failure
                        else:
                            originals._uncertain(closing, failure)
                        self._remember(failure)
            if self._unsettled:
                raise storage_access.mark_unsettled(
                    closing or ContentError("prediction_original_close_unknown", 503)
                )
            if closing is not None:
                raise closing
