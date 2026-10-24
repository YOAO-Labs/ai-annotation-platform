"""Canonical image ownership and transaction-bound repair qualifications."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.annotation_operation import AnnotationOperation
from app.db.models.async_job import AsyncJob
from app.db.models.mask_repair_batch import MaskRepairBatch
from app.db.models.task import Task
from app.db.models.user import User
from app.services import (
    prediction_inputs,
    prediction_originals as originals,
    storage_access,
)
from app.services.content_snapshot import ContentError
from app.services.dataset_content import content_http_error, key_authority
from app.services.managed_image_inference import normalize_identity
from app.services.mask_repair import canonical_digest
from app.services.mask_repair_execution import authorize_repair_task
from app.services.project_write_guard import (
    lock_actor_project_authority,
    lock_actor_scope,
)
from app.services.reference_bindings import task_binding
from app.services.reference_keys import is_reference
from app.services.storage_location import api_node_id
from app.services.task_lock import TaskLockService
from app.services.task_scope import lock_task_scope


_ISSUER = object()
_READ_KINDS = {"candidate_read", "annotation_read"}
_KINDS = _READ_KINDS | {"prediction", "mutation", "unit", "candidate_accept"}
_CONTEXT = {
    "batch_id",
    "repair_id",
    "plan_digest",
    "unit_digest",
    "job_id",
    "attempt_id",
    "prediction_id",
    "result_digest",
    "metadata_digest",
    "source_annotation_id",
    "source_version",
    "candidate_index",
    "annotation_id",
    "geometry_digest",
    "model",
    "route",
    "actor_id",
    "node_id",
}


def annotation_facts(row):
    return originals.bounded_proof(
        {
            column.key: getattr(row, column.key)
            for column in Annotation.__table__.columns
        }
    )


def repair_http_error(exc):
    error = content_http_error(exc)
    messages = {
        "repair_original_requires_new_plan": "原修复计划缺少可信原图证明，请重新预览并创建修复计划。",
        "repair_candidate_source_changed": "源标注已变化，请重新预览并创建修复计划。",
        "repair_model_changed": "修复模型配置已变化，请重新预览并创建修复计划。",
        "repair_authority_revoked": "本次修复授权已失效，请刷新后重试。",
    }
    if isinstance(exc, ContentError) and exc.code in messages:
        error.detail = {**error.detail, "message": messages[exc.code]}
    return error


def _without_version(binding):
    return {key: value for key, value in binding.items() if key != "version"}


def normalize_binding(binding):
    if not isinstance(binding, dict) or set(binding) != {
        "task_id",
        "batch_id",
        "item_id",
        "file_path",
        "file_name",
        "version",
        "original",
    }:
        raise ContentError("repair_original_requires_new_plan")
    value = originals.bounded_proof(binding)
    source = value["original"]
    if (
        not isinstance(source, dict)
        or type(source.get("schema")) is not int
        or source["schema"] != 1
        or source.get("owner_kind") not in {"dataset_asset", "task_original"}
        or type(value["version"]) is not int
        or value["version"] < 1
        or not isinstance(source.get("facts"), dict)
        or originals.digest(source["facts"]) != source.get("facts_digest")
    ):
        raise ContentError("repair_original_requires_new_plan")
    return value


def public_identity(binding, width, height):
    original = normalize_binding(binding)["original"]
    return normalize_identity(
        {
            "schema": 1,
            "task_id": binding["task_id"],
            **{
                key: original[key]
                for key in (
                    "owner_kind",
                    "content_id",
                    "sha256",
                    "size_bytes",
                    "location_id",
                    "location_revision",
                    "maintenance_revision",
                    "facts_digest",
                )
            },
            "width": width,
            "height": height,
        }
    )


def _public_model(model):
    return (
        {
            key: deepcopy(model[key])
            for key in (
                "backend_id",
                "pool_id",
                "model_id",
                "model_digest",
                "identity_guarantee",
            )
            if key in model
        }
        if model
        else None
    )


def public_plan(plan):
    """Project a copy; private canonical facts remain in the immutable digest."""
    result = deepcopy(plan or {})
    for item in result.get("items") or []:
        for name in (
            "original_input",
            "original_annotation",
            "original_annotations",
            "original_model",
        ):
            item.pop(name, None)
    return result


async def freeze_item(db, task, actor, item, request):
    if task.file_type != "image" or is_reference(task.file_path):
        return
    if request is None:
        raise ContentError("repair_original_requires_new_plan")
    await authorize_repair_task(db, task, actor, ai=item["kind"] == "rerun_local_sam")
    key_id = getattr(request.state, "api_key_id", None)
    await key_authority(db, actor, key_id, "annotations:write")
    current = await originals.locked_task(db, task.id)
    item["original_input"] = originals.bounded_proof(
        {
            **task_binding(current),
            "original": await originals.managed_binding(db, current, lock=True),
        }
    )
    identifiers = {uuid.UUID(item["annotation_id"])}
    identifiers.update(
        uuid.UUID(row["annotation_id"])
        for row in (item.get("payload") or {}).get("expected_versions") or []
    )
    rows = (
        await db.scalars(
            select(Annotation)
            .where(Annotation.id.in_(identifiers))
            .order_by(Annotation.id)
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True)
        )
    ).all()
    if len(rows) != len(identifiers) or any(row.task_id != task.id for row in rows):
        raise ContentError("repair_candidate_source_changed")
    facts = {str(row.id): annotation_facts(row) for row in rows}
    target = facts[item["annotation_id"]]
    versions = {
        row["annotation_id"]: row["version"]
        for row in (item.get("payload") or {}).get("expected_versions") or []
    }
    if (
        target["version"] != item.get("source_version", item.get("before_version"))
        or canonical_digest(target["geometry"])
        != item.get(
            "source_geometry_digest", canonical_digest(item.get("before_geometry"))
        )
        or not target["is_active"]
        or target["was_cancelled"]
        or target["is_locked"]
        or ("class_name" in item and target["class_name"] != item["class_name"])
        or ("tool_unit_id" in item and target["tool_unit_id"] != item["tool_unit_id"])
        or any(
            facts[identifier]["version"] != version
            for identifier, version in versions.items()
        )
    ):
        raise ContentError("repair_candidate_source_changed")
    item["original_annotations"] = facts
    item["original_annotation"] = facts[item["annotation_id"]]
    if item["kind"] == "rerun_local_sam":
        # Reuse the exact declared-model and default-freezing contract.
        from app.services.reference_mask_repair import freeze_model

        item["original_model"] = await freeze_model(db, task, item)


def _expected_annotations(batch):
    """Only committed checkpoints may advance immutable plan parent facts."""
    expected = {}
    items = (batch.plan_json or {}).get("items") or []
    for item in items:
        for identifier, facts in (item.get("original_annotations") or {}).items():
            if identifier in expected and expected[identifier] != facts:
                raise ContentError("repair_candidate_source_changed")
            expected[identifier] = deepcopy(facts)
    result = batch.result_json or {}
    for shard in (batch.plan_json or {}).get("shards") or []:
        record = (result.get("shards") or {}).get(str(shard["id"])) or {}
        if record.get("status") != "completed":
            continue
        for entry in record.get("items") or []:
            identifier = entry.get("annotation_id")
            facts = entry.get("after_facts")
            if identifier in expected:
                if not isinstance(facts, dict) or facts.get("version") != entry.get(
                    "after_version"
                ):
                    raise ContentError("repair_original_requires_new_plan")
                expected[identifier] = deepcopy(facts)
    rollback = (result.get("rollback") or {}).get("shards") or {}
    for checkpoint in rollback.values():
        if checkpoint.get("status") == "completed":
            for identifier, facts in (checkpoint.get("annotation_facts") or {}).items():
                if identifier in expected:
                    if not isinstance(facts, dict) or facts.get("version") != (
                        checkpoint.get("versions") or {}
                    ).get(identifier):
                        raise ContentError("repair_candidate_source_changed")
                    expected[identifier] = deepcopy(facts)
    return expected


async def check_annotations(db, batch, items, *, overrides=None, lock=True):
    expected = _expected_annotations(batch)
    selected = {
        identifier
        for item in items
        for identifier in (item.get("original_annotations") or {})
    }
    if not selected:
        raise ContentError("repair_original_requires_new_plan")
    expected.update(overrides or {})
    query = (
        select(Annotation)
        .where(Annotation.id.in_([uuid.UUID(value) for value in selected]))
        .order_by(Annotation.id)
    )
    if lock:
        query = query.with_for_update(read=True, nowait=True)
    rows = (await db.scalars(query.execution_options(populate_existing=True))).all()
    if len(rows) != len(selected) or any(
        annotation_facts(row) != expected[str(row.id)] for row in rows
    ):
        raise ContentError("repair_candidate_source_changed")


async def capture_job_authority(
    db, batch, actor, request, *, rollback=False, initial=False
):
    from app.services.reference_mask_repair import pending_reference_items

    bindings = {}
    pending = pending_reference_items(batch, rollback=rollback)
    selected = []
    credentials = None
    for item in pending:
        task = await db.get(Task, uuid.UUID(item["task_id"]), populate_existing=True)
        frozen = item.get("original_input")
        if not frozen and (
            task is None or task.file_type != "image" or is_reference(task.file_path)
        ):
            continue
        if (
            task is None
            or task.file_type != "image"
            or is_reference(task.file_path)
            or not frozen
        ):
            raise ContentError("repair_original_requires_new_plan")
        frozen = normalize_binding(frozen)
        if credentials is None:
            if request is None:
                raise ContentError("repair_original_requires_new_plan")
            await lock_actor_project_authority(db, actor.id, batch.project_id)
            await lock_actor_scope(db, actor.id, batch.project_id)
            credentials = await originals.capture_credentials(db, actor, request)
            await key_authority(
                db, actor, credentials["api_key_id"], "annotations:write"
            )
        task = await originals.locked_task(db, task.id)
        current = {
            **task_binding(task),
            "original": await originals.managed_binding(db, task, lock=True),
        }
        if _without_version(current) != _without_version(frozen) or (
            initial and current != frozen
        ):
            raise ContentError("content_binding_changed")
        await authorize_repair_task(
            db, task, actor, ai=not rollback and item["kind"] == "rerun_local_sam"
        )
        bindings[str(task.id)] = current
        selected.append(item)
    if not bindings:
        return None
    if request is None:
        raise ContentError("repair_original_requires_new_plan")
    await check_annotations(db, batch, selected)
    key_id = credentials["api_key_id"]
    await key_authority(db, actor, key_id, "annotations:write")
    return originals.bounded_proof(
        {
            "schema": 1,
            "batch_id": str(batch.id),
            "plan_digest": batch.plan_digest,
            "actor_id": str(actor.id),
            "key_id": key_id,
            "credentials": credentials,
            "rollback": rollback,
            "bindings": bindings,
        }
    )


class ManagedRepairSource:
    """A supplied consumer policy plus a full read closed before final locks."""

    def __init__(
        self,
        db,
        task,
        actor,
        request,
        *,
        expected,
        authorize,
        scope,
        node_id=None,
        purpose=None,
    ):
        self.db, self.task_id, self.project_id, self.actor_id = (
            db,
            task.id,
            task.project_id,
            actor.id,
        )
        self.request, self.authorize, self.scope = request, authorize, scope
        self.expected = normalize_binding(expected) if expected is not None else None
        self.read_only = self.expected is None
        if self.read_only and not scope.endswith(":read"):
            raise ContentError("repair_original_requires_new_plan")
        self.node_id = node_id or api_node_id()
        self.purpose = purpose or "mask_repair_" + uuid.uuid4().hex
        self.credentials = self.binding = None
        self.source = self.snapshot = None
        self.width = self.height = None
        self.source_closed = self.closed = self._unsettled = False
        self.transaction = None
        self.guard = self.final_guard = None
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

    async def capture(self):
        if self.credentials is not None or self.request is None:
            raise ContentError("repair_original_receipt_invalid")
        await lock_actor_project_authority(self.db, self.actor_id, self.project_id)
        await lock_actor_scope(self.db, self.actor_id, self.project_id)
        actor = await self.db.get(User, self.actor_id, populate_existing=True)
        if actor is None:
            raise ContentError("repair_authority_revoked", 403)
        self.credentials = await originals.capture_credentials(
            self.db, actor, self.request
        )
        await self._check(self.db, capture=True)
        return self

    async def _check(self, db, *, capture=False, final=False):
        if self._unsettled:
            raise storage_access.mark_unsettled(
                ContentError("prediction_original_close_unknown", 503)
            )
        await lock_actor_project_authority(db, self.actor_id, self.project_id)
        await lock_actor_scope(db, self.actor_id, self.project_id)
        key_id = (
            uuid.UUID(self.credentials["api_key_id"])
            if self.credentials["api_key_id"]
            else None
        )
        actor = await originals.reauthorize(db, self.credentials, self.actor_id, key_id)
        await key_authority(db, actor, key_id, self.scope)
        if final:
            await TaskLockService(db)._lock_task_scope(self.task_id, nowait=True)
            await lock_task_scope(db, [self.task_id])
        task = await originals.locked_task(db, self.task_id)
        if final:
            task = await db.scalar(
                select(Task)
                .where(Task.id == self.task_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        if (
            task.project_id != self.project_id
            or task.file_type != "image"
            or is_reference(task.file_path)
        ):
            raise ContentError("content_binding_changed")
        await self.authorize(db, task, actor)
        binding = originals.bounded_proof(
            {
                **task_binding(task),
                "original": await originals.managed_binding(db, task, lock=True),
            }
        )
        if self.expected is not None and _without_version(binding) != _without_version(
            self.expected
        ):
            raise ContentError("content_binding_changed")
        if capture:
            self.binding = binding
        elif binding != self.binding:
            raise ContentError("content_binding_changed")
        if self.guard is not None:
            await self.guard(db)
        return task, actor

    async def _fresh(self, db=None):
        if db is not None:
            return await self._check(db)
        async with AsyncSession(
            bind=storage_access.engine(self.db), expire_on_commit=False, autoflush=False
        ) as owned:
            return await self._check(owned)

    async def open(self):
        if self.closed or self.snapshot is not None:
            raise ContentError("repair_original_receipt_invalid")
        if self.credentials is None:
            await self.capture()
        await self.db.commit()
        try:
            self.source = await originals.settled(
                originals.open_snapshot(
                    self.db,
                    self.task_id,
                    self.binding["original"],
                    guard=self._fresh,
                    purpose=self.purpose,
                    node_id=self.node_id,
                ),
                late=lambda value: value.close(),
            )
            self.snapshot = self.source.snapshot
            self.width, self.height = self.snapshot.width, self.snapshot.height
            public_identity(self.binding, self.width, self.height)
            await originals.settled(self._fresh())
        except BaseException as failure:
            self._remember(failure)
            raise
        return self

    async def close_source(self, failure=None):
        self._remember(failure)
        if self._unsettled:
            raise storage_access.mark_unsettled(
                ContentError("prediction_original_close_unknown", 503)
            )
        if self.source_closed:
            return
        if self.source is None:
            raise ContentError("repair_original_receipt_invalid")
        try:
            await originals.settled(self.source.close(self._failure))
        except BaseException as closing:
            self._remember(closing)
            raise
        self.source_closed = True
        self.source = None

    async def open_close(self):
        try:
            await self.open()
            await originals.settled(self.db.rollback())
            await self.close_source()
        except BaseException as failure:
            self._remember(failure)
            raise
        return self

    async def final(self):
        if (
            not self.source_closed
            or self._unsettled
            or self.closed
            or self.db.sync_session.in_nested_transaction()
        ):
            raise ContentError("repair_original_receipt_invalid")
        task, _ = await self._check(self.db, final=True)
        self.transaction = self.db.sync_session.get_transaction()
        return task

    async def validate_final(self, db):
        if db is not self.db or not self.source_closed or self._unsettled:
            raise ContentError("repair_original_receipt_invalid")
        await self._check(db)
        if self.final_guard is not None:
            await self.final_guard(db)

    def evidence(self):
        if not self.source_closed or self._unsettled:
            raise ContentError("repair_original_receipt_invalid")
        return public_identity(self.binding, self.width, self.height)

    def identity(self):
        return self.evidence()

    def receipt(self, kind, payload, *, context=None):
        if (
            kind not in _KINDS
            or (
                (self.read_only or self.scope != "annotations:write")
                and kind not in _READ_KINDS
            )
            or self.closed
            or not self.source_closed
            or self._unsettled
            or self.transaction is None
            or self.transaction is not self.db.sync_session.get_transaction()
            or self.db.sync_session.in_nested_transaction()
        ):
            raise ContentError("repair_original_receipt_invalid")
        context = context or {}
        if set(context) - _CONTEXT:
            raise ContentError("repair_original_receipt_invalid")
        if context.get("model") is not None and (
            not isinstance(context["model"], dict)
            or set(context["model"])
            - {
                "backend_id",
                "pool_id",
                "model_id",
                "model_digest",
                "identity_guarantee",
            }
        ):
            raise ContentError("repair_original_receipt_invalid")
        if context.get("route") is not None and (
            not isinstance(context["route"], dict)
            or set(context["route"]) != {"instance_id", "pool_id"}
        ):
            raise ContentError("repair_original_receipt_invalid")
        return _Receipt(
            _ISSUER,
            self,
            self.db.sync_session,
            self.transaction,
            kind,
            originals.encoded(payload),
            originals.encoded(context),
        )

    async def recheck_credentials(self):
        key_id = (
            uuid.UUID(self.credentials["api_key_id"])
            if self.credentials["api_key_id"]
            else None
        )
        actor = await originals.reauthorize(
            self.db, self.credentials, self.actor_id, key_id
        )
        await key_authority(self.db, actor, key_id, self.scope)

    async def close(self, failure=None):
        self._remember(failure)
        async with self._close_lock:
            self.closed = True
            if self._unsettled:
                raise storage_access.mark_unsettled(
                    ContentError("prediction_original_close_unknown", 503)
                )
            if self.source is not None:
                await self.close_source(failure)


@dataclass(frozen=True, slots=True)
class _Receipt:
    issuer: object
    owner: object
    session: object
    transaction: object
    kind: str
    payload: str
    context: str


def receipt_payload(receipt):
    if type(receipt) is not _Receipt or receipt.issuer is not _ISSUER:
        raise ContentError("repair_original_receipt_invalid")
    return receipt.kind, json.loads(receipt.payload)


async def recheck_credentials(db, receipt):
    """Post-write credential fence; never compare the pre-write annotation."""
    receipt_payload(receipt)
    if (
        receipt.session is not db.sync_session
        or receipt.transaction is None
        or receipt.transaction is not db.sync_session.get_transaction()
        or db.sync_session.in_nested_transaction()
        or receipt.owner._unsettled
    ):
        raise ContentError("repair_original_receipt_invalid")
    await receipt.owner.recheck_credentials()


async def validate_receipt(
    db, task_id, actor_id, receipt, kind, payload, *, project_id=None
):
    receipt_payload(receipt)
    owner = receipt.owner
    if (
        receipt.session is not db.sync_session
        or receipt.transaction is None
        or receipt.transaction is not db.sync_session.get_transaction()
        or db.sync_session.in_nested_transaction()
        or receipt.kind != kind
        or receipt.payload != originals.encoded(payload)
        or owner.task_id != task_id
        or (actor_id is not None and owner.actor_id != actor_id)
        or (project_id is not None and owner.project_id != project_id)
    ):
        raise ContentError("repair_original_receipt_invalid")
    await owner.validate_final(db)
    return {**owner.evidence(), **json.loads(receipt.context)}


class ManagedRepairUnit:
    is_managed = True
    provenance_key = "original_input"

    def __init__(self, db, factory, attempt, batch, items, node_id):
        if not items or not node_id or not node_id.startswith("worker:"):
            raise ContentError("worker_read_not_verified")
        bindings = [normalize_binding(item.get("original_input")) for item in items]
        if any(binding != bindings[0] for binding in bindings):
            raise ContentError("repair_original_requires_new_plan")
        self.db, self.factory, self.attempt, self.items = (
            db,
            factory,
            attempt,
            deepcopy(items),
        )
        self.project_id, self.task_id, self.node_id = (
            batch.project_id,
            uuid.UUID(items[0]["task_id"]),
            node_id,
        )
        self.plan_binding, self.unit_digest = bindings[0], canonical_digest(items)
        self.ai = not attempt.rollback and items[0]["kind"] == "rerun_local_sam"
        self.model = deepcopy(items[0].get("original_model")) if self.ai else None
        self.proof = self.source = self.inputs = self.url = self.route = None
        self.width = self.height = None
        self._progress = {}
        self._closed = self._unsettled = False
        self._finalizing = False
        self._failure = None

    async def _authorize(self, db, task, actor):
        return await authorize_repair_task(db, task, actor, ai=self.ai)

    async def _validate(self, db, *, parents=True):
        from app.services.reference_mask_repair import unwrap_authority

        job = await db.get(AsyncJob, self.attempt.job_id, populate_existing=True)
        batch = await db.get(
            MaskRepairBatch, self.attempt.batch_id, populate_existing=True
        )
        proof = unwrap_authority(
            (job.payload or {}).get("repair_authority") if job else None, "managed"
        )
        attempt = self.attempt
        if (
            job is None
            or batch is None
            or job.status != "running"
            or job.kind != attempt.kind
            or job.project_id != self.project_id
            or batch.project_id != self.project_id
            or batch.status != ("rolling_back" if attempt.rollback else "running")
            or (job.payload or {}).get("mask_repair_batch_id") != str(batch.id)
            or job.user_id != attempt.actor_id
            or job.celery_task_id != attempt.celery_task_id
            or (job.payload or {}).get("cancel_requested")
            or (batch.rollback_async_job_id if attempt.rollback else batch.async_job_id)
            != job.id
            or batch.plan_digest != attempt.plan_digest
            or canonical_digest(batch.plan_json) != attempt.plan_digest
            or not proof
            or type(proof.get("schema")) is not int
            or proof["schema"] != 1
            or proof.get("batch_id") != str(batch.id)
            or proof.get("plan_digest") != attempt.plan_digest
            or proof.get("actor_id") != str(attempt.actor_id)
            or proof.get("rollback") != attempt.rollback
        ):
            raise ContentError("repair_authority_revoked", 403)
        if self.proof is not None and self.proof != proof:
            raise ContentError("repair_authority_revoked", 403)
        self.proof = deepcopy(proof)
        binding = normalize_binding(proof.get("bindings", {}).get(str(self.task_id)))
        if _without_version(binding) != _without_version(self.plan_binding):
            raise ContentError("content_binding_changed")
        if parents:
            await check_annotations(
                db,
                batch,
                self.items,
                overrides=self._progress,
                lock=db is not self.db or not self._finalizing,
            )
        if self.ai:
            await self._validate_model(db)
        return binding

    async def validate(self):
        if self._closed or self._unsettled:
            raise ContentError("repair_original_receipt_invalid")
        async with self.factory() as db:
            await self._validate(db)
            if self.source is not None:
                await self.source._check(db)
        if self.inputs is not None:
            await self.inputs.check_live()

    async def open(self):
        binding = await self._validate(self.db)
        credentials = self.proof.get("credentials")
        originals.validate_credentials(
            credentials, self.attempt.actor_id, self.proof.get("key_id")
        )
        task = await originals.locked_task(self.db, self.task_id)
        actor = await self.db.get(User, self.attempt.actor_id, populate_existing=True)
        self.source = ManagedRepairSource(
            self.db,
            task,
            actor,
            None,
            expected=self.plan_binding,
            authorize=self._authorize,
            scope="annotations:write",
            node_id=self.node_id,
        )
        self.source.credentials, self.source.binding = deepcopy(credentials), binding
        self.source.guard = self._validate
        self.source.final_guard = self._final_guard
        await self.db.commit()
        if self.ai:
            if not self.model:
                raise ContentError("repair_model_snapshot_missing")
            self.inputs = await originals.settled(
                prediction_inputs.PredictionInputs.create(
                    self.factory,
                    owner_id=self.attempt.job_id,
                    owner_kind="mask_repair",
                    task_id=self.task_id,
                    guard=self.source._fresh,
                    source_ref={
                        "binding": binding,
                        "batch_id": str(self.attempt.batch_id),
                        "plan_digest": self.attempt.plan_digest,
                        "unit_digest": self.unit_digest,
                    },
                ),
                late=lambda value: value.close(),
            )
            self.source.purpose = prediction_inputs.purpose(self.inputs.record)
        try:
            await self.source.open()
            self.width, self.height = self.source.width, self.source.height
            for item in self.items:
                for facts in (item.get("original_annotations") or {}).values():
                    geometry = facts.get("geometry") or {}
                    if geometry.get("type") != "raster_mask" or (
                        geometry.get("mask") or {}
                    ).get("size") != [self.height, self.width]:
                        raise ContentError("image_size_mismatch")
            if self.ai:
                self.url = await self.inputs.upload_snapshot(self.source.snapshot)
            await originals.settled(self.db.rollback())
            await self.source.close_source()
            await self.validate()
        except BaseException as failure:
            self._failure = failure
            self._unsettled |= bool(
                getattr(failure, "storage_operation_unsettled", False)
            )
            raise

    async def _validate_model(self, db):
        from app.services.ml_backend import MLBackendService

        if not self.model:
            raise ContentError("repair_model_snapshot_missing")
        service = MLBackendService(db)
        backend = await service.get(uuid.UUID(self.model["backend_id"]))
        if backend is None or not await service.is_enabled(self.project_id, backend.id):
            raise ContentError("sam_backend_unavailable")
        await db.refresh(backend)
        pool = await service.pool_id_for_registry(backend.id)
        if (str(pool) if pool else None) != self.model["pool_id"]:
            raise ContentError("repair_model_changed")
        self.validate_capabilities(
            (backend.health_meta or {}).get("capabilities") or {}
        )

    def validate_capabilities(self, capabilities):
        from app.services.reference_mask_repair import _model

        if (
            not self.model
            or canonical_digest(_model(capabilities, self.model["model_id"]))
            != self.model["model_digest"]
        ):
            raise ContentError("repair_model_changed")

    async def before_dispatch(self, transport):
        from app.services.ml_capabilities import extract_capabilities

        setup = await transport.setup()
        self.validate_capabilities(extract_capabilities(setup))
        await self.validate()
        async with self.factory() as owned:
            await self._validate_model(owned)
        await self.db.commit()

    async def _final_guard(self, db):
        await self._validate(db)
        if self.inputs is not None:
            await prediction_inputs.check_metadata(db, self.inputs)

    async def final(self):
        self._finalizing = True
        await self.source.final()
        if self.ai:
            from app.services.reference_mask_repair import ReferenceRepairUnit

            await ReferenceRepairUnit._lock_model(self)
            await self._validate_model(self.db)
        await self._final_guard(self.db)

    def receipt(self, kind, payload):
        return self.source.receipt(
            kind,
            payload,
            context={
                "batch_id": str(self.attempt.batch_id),
                "repair_id": str(self.attempt.batch_id),
                "plan_digest": self.attempt.plan_digest,
                "unit_digest": self.unit_digest,
                "job_id": str(self.attempt.job_id),
                "attempt_id": self.attempt.celery_task_id,
                "model": _public_model(self.model),
                "route": deepcopy(self.route),
                "actor_id": str(self.attempt.actor_id),
                "node_id": self.node_id,
            },
        )

    def evidence(self):
        return {
            **self.source.evidence(),
            "model": _public_model(self.model),
            "route": deepcopy(self.route),
            "repair_id": str(self.attempt.batch_id),
            "plan_digest": self.attempt.plan_digest,
            "job_id": str(self.attempt.job_id),
            "actor_id": str(self.attempt.actor_id),
            "node_id": self.node_id,
        }

    async def note_committed_mutation(self, payload, response):
        operation = await self.db.get(
            AnnotationOperation, response.operation_id, populate_existing=True
        )
        if (
            operation is None
            or operation.task_id != self.task_id
            or operation.actor_id != self.attempt.actor_id
            or operation.idempotency_key != payload.idempotency_key
            or operation.result_versions != response.result_versions
        ):
            raise ContentError("repair_original_receipt_invalid")
        for identifier, version in response.result_versions.items():
            row = await self.db.get(
                Annotation, uuid.UUID(str(identifier)), populate_existing=True
            )
            if row is None or row.version != version:
                raise ContentError("repair_candidate_source_changed")
            self._progress[str(row.id)] = annotation_facts(row)

    async def recheck_credentials(self):
        await self.source.recheck_credentials()

    async def close(self, failure=None):
        if failure is not None:
            self._failure = self._failure or failure
            self._unsettled |= bool(
                getattr(failure, "storage_operation_unsettled", False)
            )
        self._closed = True
        closing = None
        try:
            if self.inputs:
                await originals.settled(self.inputs.close())
        except BaseException as exc:
            closing = exc
            self._unsettled |= bool(getattr(exc, "storage_operation_unsettled", False))
        finally:
            if self.source:
                try:
                    await originals.settled(self.source.close(closing or self._failure))
                except BaseException as exc:
                    if closing is None:
                        closing = exc
                    else:
                        originals._uncertain(closing, exc)
                    self._unsettled |= bool(
                        getattr(exc, "storage_operation_unsettled", False)
                    )
        if self._unsettled:
            raise storage_access.mark_unsettled(
                closing or ContentError("prediction_original_close_unknown", 503)
            )
        if closing is not None:
            raise closing
