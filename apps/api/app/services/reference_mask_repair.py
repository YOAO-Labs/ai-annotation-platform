"""Freeze repair inputs and qualify only owned, strict reference-image writes."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from sqlalchemy import select

from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.mask_repair_batch import MaskRepairBatch
from app.db.models.ml_backend_pool import MLBackendServicePool, MLBackendPoolMember
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.task import Task
from app.db.models.user import User
from app.services.content_snapshot import ContentError
from app.services.dataset_content import key_authority
from app.services.dataset_references import reference_context, reference_snapshot
from app.services.mask_repair import canonical_digest
from app.services.mask_repair_execution import authorize_repair_task
from app.services.ml_backend import MLBackendService
from app.services.ml_interaction_proxy import prepare_interactive_context
from app.services.prediction_execution import freeze_builtin_defaults
from app.services.prediction_inputs import PredictionInputs
from app.services.project_write_guard import (
    lock_actor_project_authority,
    lock_actor_scope,
)
from app.services.reference_bindings import (
    task_binding,
    reference_binding,
    lock_reference_bindings,
    normalize_binding,
)
from app.services.reference_keys import is_reference
from app.services.task_lock import TaskLockService
from app.services.task_scope import lock_task_scope

SCOPES = ("tasks:read", "annotations:read", "annotations:write")
_ISSUER = object()


def _model(capabilities, model_id):
    return next(
        (
            row
            for row in (capabilities or {}).get("models", [])
            if row.get("id") == model_id
        ),
        None,
    )


def _prompt(item):
    box = item.get("region_bbox") or {}
    return {
        "type": "interactive_box",
        "bbox": [box.get(k) for k in ("x0", "y0", "x1", "y1")],
        "output_geometry": "mask",
        **(
            {"model_id": item["action"]["model_id"]}
            if item["action"].get("model_id")
            else {}
        ),
    }


async def freeze_item(db, task, actor, item, request):
    if not is_reference(task.file_path):
        from app.services.managed_mask_repair import freeze_item as freeze_managed

        await freeze_managed(db, task, actor, item, request)
        return
    if task.file_type != "image" or item["kind"] == "rerun_tracker":
        raise ContentError("reference_requires_image_task")
    actor = await authorize_repair_task(
        db, task, actor, ai=item["kind"] == "rerun_local_sam"
    )
    key_id = getattr(getattr(request, "state", None), "api_key_id", None)
    for scope in SCOPES:
        await key_authority(db, actor, key_id, scope)
    await reference_context(
        db,
        task.dataset_item_id,
        actor,
        task_id=task.id,
        task_authorizer=lambda db, task, actor: authorize_repair_task(
            db, task, actor, ai=item["kind"] == "rerun_local_sam"
        ),
    )
    item["reference_input"] = {
        **task_binding(task),
        "reference": await reference_binding(db, task),
    }
    if item["kind"] != "rerun_local_sam":
        return
    item["reference_model"] = await freeze_model(db, task, item)


async def freeze_model(db, task, item):
    """Freeze the declared model and effective defaults for either source kind."""
    service = MLBackendService(db)
    backend = await service.get(uuid.UUID(str(item["action"]["backend_id"])))
    if backend is None or not await service.is_enabled(task.project_id, backend.id):
        raise ContentError("sam_backend_unavailable")
    capabilities = (backend.health_meta or {}).get("capabilities") or {}
    context, model_id = prepare_interactive_context(
        _prompt(item),
        capabilities,
        task_id=str(task.id),
        requested_backend_id=str(backend.id),
    )
    model = _model(capabilities, model_id)
    stage = {
        "model_variants": context.get("model_variants"),
        "params": context.get("params"),
    }
    freeze_builtin_defaults(backend, model, stage)
    context.update(
        {key: stage[key] for key in ("model_variants", "params") if stage.get(key)}
    )
    return {
        "backend_id": str(backend.id),
        "pool_id": str(pool)
        if (pool := await service.pool_id_for_registry(backend.id))
        else None,
        "model_id": model_id,
        "context": context,
        "model_digest": canonical_digest(model),
        "identity_guarantee": "declared_configuration",
    }


def unit_items(batch, section, unit):
    if section == "candidates":
        return [unit]
    return [
        (batch.plan_json or {})["items"][int(index)]
        for index in unit.get("item_indexes") or []
    ]


def pending_reference_items(batch, *, rollback=False):
    plan, result = batch.plan_json or {}, batch.result_json or {}
    items = plan.get("items") or []
    selected = []
    for shard in plan.get("shards") or []:
        completed = ((result.get("shards") or {}).get(str(shard["id"])) or {}).get(
            "status"
        ) == "completed"
        undone = (
            ((result.get("rollback") or {}).get("shards") or {}).get(str(shard["id"]))
            or {}
        ).get("status") != "completed"
        if (rollback and completed and undone) or (not rollback and not completed):
            selected.extend(
                items[int(index)] for index in shard.get("item_indexes") or []
            )
    if not rollback:
        selected.extend(
            item
            for index, item in enumerate(items)
            if item.get("kind") in {"rerun_local_sam", "rerun_tracker"}
            and ((result.get("candidates") or {}).get(str(index)) or {}).get("status")
            != "completed"
        )
    return selected


async def _capture_reference_authority(
    db, batch, actor, request, *, rollback=False, initial=False
):
    """Fresh request authority, without changing frozen content or model choices."""
    bindings = {}
    for item in pending_reference_items(batch, rollback=rollback):
        task = await db.get(
            Task, uuid.UUID(str(item["task_id"])), populate_existing=True
        )
        frozen = item.get("reference_input")
        if frozen:
            frozen = normalize_binding(frozen)
        if not frozen and (task is None or not is_reference(task.file_path)):
            continue
        if not frozen or task is None or task.file_type != "image":
            raise ContentError("repair_requires_reference_plan")
        current = {**task_binding(task), "reference": await reference_binding(db, task)}
        if {k: v for k, v in current.items() if k != "version"} != {
            k: v for k, v in frozen.items() if k != "version"
        } or (initial and current != frozen):
            raise ContentError("content_binding_changed")
        await authorize_repair_task(
            db, task, actor, ai=not rollback and item["kind"] == "rerun_local_sam"
        )
        bindings[str(task.id)] = current
    if not bindings:
        return None
    key_id = getattr(getattr(request, "state", None), "api_key_id", None)
    keys = [await key_authority(db, actor, key_id, scope) for scope in SCOPES]
    return {
        "schema": 1,
        "batch_id": str(batch.id),
        "plan_digest": batch.plan_digest,
        "actor_id": str(actor.id),
        "key_id": str(key_id) if key_id else None,
        "keys": keys,
        "rollback": rollback,
        "bindings": bindings,
    }


def unwrap_authority(value, source):
    """An explicit mixed envelope never upgrades an old managed delegation."""
    if not isinstance(value, dict):
        return None
    if type(value.get("schema")) is int and value["schema"] == 2:
        if set(value) != {"schema", "reference", "managed"}:
            raise ContentError("repair_authority_revoked", 403)
        return value[source]
    if (
        source == "reference"
        and type(value.get("schema")) is int
        and value["schema"] == 1
    ):
        return value
    return None


async def capture_job_authority(
    db, batch, actor, request, *, rollback=False, initial=False
):
    from app.services.managed_mask_repair import (
        capture_job_authority as capture_managed,
    )

    reference = await _capture_reference_authority(
        db, batch, actor, request, rollback=rollback, initial=initial
    )
    managed = await capture_managed(
        db, batch, actor, request, rollback=rollback, initial=initial
    )
    if managed is None:
        return reference
    return {"schema": 2, "reference": reference, "managed": managed}


class ReferenceRepairUnit:
    is_managed = False
    provenance_key = "reference_input"

    def __init__(self, db, factory, attempt, batch, items, node_id):
        bindings = [item.get("reference_input") for item in items]
        if not items or not bindings[0] or any(row != bindings[0] for row in bindings):
            raise ContentError("repair_requires_reference_plan")
        if not node_id or not node_id.startswith("worker:"):
            raise ContentError("worker_read_not_verified")
        self.db, self.factory, self.attempt, self.node_id = (
            db,
            factory,
            attempt,
            node_id,
        )
        self.task_id = uuid.UUID(str(items[0]["task_id"]))
        self.project_id = batch.project_id
        self.ai = not attempt.rollback and items[0]["kind"] == "rerun_local_sam"
        self.model = deepcopy(items[0].get("reference_model")) if self.ai else None
        self.unit_digest = canonical_digest(items)
        self.plan_binding = normalize_binding(deepcopy(bindings[0]))
        self.proof = None
        self.context = self.snapshot = self.inputs = self.url = self.transaction = None
        self.closed = False
        self.route = None

    async def validate(self):
        db, attempt = self.db, self.attempt
        job = await db.get(AsyncJob, attempt.job_id, populate_existing=True)
        batch = await db.get(MaskRepairBatch, attempt.batch_id, populate_existing=True)
        proof = unwrap_authority(
            (job.payload or {}).get("repair_authority") if job else None, "reference"
        )
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
            or proof.get("schema") != 1
            or proof.get("batch_id") != str(batch.id)
            or proof.get("plan_digest") != attempt.plan_digest
            or proof.get("actor_id") != str(attempt.actor_id)
            or proof.get("rollback") != attempt.rollback
        ):
            raise ContentError("repair_authority_revoked", 403)
        if self.proof is not None and proof != self.proof:
            raise ContentError("repair_authority_revoked", 403)
        self.proof = deepcopy(proof)
        task = await db.get(Task, self.task_id, populate_existing=True)
        actor = await db.get(User, attempt.actor_id, populate_existing=True)
        if (
            task is None
            or actor is None
            or task.project_id != self.project_id
            or task.file_type != "image"
        ):
            raise ContentError("content_binding_changed")
        actor = await authorize_repair_task(db, task, actor, ai=self.ai)
        binding = {**task_binding(task), "reference": await reference_binding(db, task)}
        if binding != normalize_binding(
            proof.get("bindings", {}).get(str(task.id), {})
        ) or {k: v for k, v in binding.items() if k != "version"} != {
            k: v for k, v in self.plan_binding.items() if k != "version"
        }:
            raise ContentError("content_binding_changed")
        self.binding = binding
        if [
            await key_authority(db, actor, proof["key_id"], scope) for scope in SCOPES
        ] != proof["keys"]:
            raise ContentError("api_key_authority_revoked", 403)
        context = await reference_context(
            db,
            task.dataset_item_id,
            actor,
            task_id=task.id,
            node_id=self.node_id,
            task_authorizer=self._authorize,
        )
        if self.context is not None and context != self.context:
            raise ContentError("content_binding_changed")
        self.context = context
        if self.ai:
            await self._validate_model()
        if self.inputs and self.inputs.record.expires_at <= datetime.now(timezone.utc):
            raise ContentError("prediction_input_expired")
        return task, actor

    async def _authorize(self, db, task, actor):
        # Avoid recursion through reference_context while retaining current policy.
        await authorize_repair_task(db, task, actor, ai=self.ai)

    async def _validate_model(self):
        if not self.model:
            raise ContentError("repair_model_snapshot_missing")
        service = MLBackendService(self.db)
        backend = await service.get(uuid.UUID(self.model["backend_id"]))
        if backend is None or not await service.is_enabled(self.project_id, backend.id):
            raise ContentError("sam_backend_unavailable")
        await self.db.refresh(backend)
        pool = await service.pool_id_for_registry(backend.id)
        if (str(pool) if pool else None) != self.model["pool_id"]:
            raise ContentError("repair_model_changed")
        self.validate_capabilities(
            (backend.health_meta or {}).get("capabilities") or {}
        )

    def validate_capabilities(self, capabilities):
        if (
            canonical_digest(_model(capabilities, self.model["model_id"]))
            != self.model["model_digest"]
        ):
            raise ContentError("repair_model_changed")

    async def open(self):
        task, actor = await self.validate()
        await self.db.commit()
        self.snapshot = await reference_snapshot(
            self.db,
            task.dataset_item_id,
            actor,
            task_id=task.id,
            api_key_id=self.proof["key_id"],
            node_id=self.node_id,
            expected_sha256=self.binding["reference"]["sha256"],
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
        if self.ai:
            self.inputs = await PredictionInputs.create(
                self.factory,
                owner_id=self.attempt.job_id,
                owner_kind="mask_repair",
                task_id=self.task_id,
            )
            self.url = await self.inputs.upload_snapshot(self.snapshot)
            await self.validate()
            await self.db.commit()

    async def final(self):
        await lock_actor_project_authority(
            self.db, self.attempt.actor_id, self.project_id
        )
        await lock_actor_scope(self.db, self.attempt.actor_id, self.project_id)
        if self.proof["key_id"]:
            await self.db.execute(
                select(ApiKey.id)
                .where(ApiKey.id == uuid.UUID(self.proof["key_id"]))
                .with_for_update(read=True, nowait=True)
            )
        await TaskLockService(self.db)._lock_task_scope(self.task_id, nowait=True)
        await lock_task_scope(self.db, [self.task_id])
        await self.db.execute(
            select(Task.id).where(Task.id == self.task_id).with_for_update(nowait=True)
        )
        await lock_reference_bindings(self.db, [self.binding], self.node_id)
        if self.ai:
            await self._lock_model()
        await self.validate()
        if (
            self.snapshot is None
            or self.snapshot._closed
            or self.db.sync_session.in_nested_transaction()
        ):
            raise ContentError("repair_reference_receipt_invalid")
        self.transaction = self.db.sync_session.get_transaction()

    async def _lock_model(self):
        """Keep enablement and routing identity stable through the short write."""
        pool_id = uuid.UUID(self.model["pool_id"]) if self.model["pool_id"] else None
        if (
            pool_id is None
            or self.route is None
            or self.route["pool_id"] != str(pool_id)
        ):
            raise ContentError("repair_model_changed")
        pool = await self.db.scalar(
            select(MLBackendServicePool)
            .where(MLBackendServicePool.id == pool_id)
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True)
        )
        assoc = await self.db.scalar(
            select(ProjectMLBackendPool)
            .where(
                ProjectMLBackendPool.project_id == self.project_id,
                ProjectMLBackendPool.pool_id == pool_id,
            )
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True)
        )
        if pool is None or not pool.enabled or assoc is None or not assoc.enabled:
            raise ContentError("sam_backend_unavailable")
        registry_ids = sorted(
            {uuid.UUID(self.model["backend_id"]), uuid.UUID(self.route["instance_id"])},
            key=str,
        )
        members = (
            await self.db.scalars(
                select(MLBackendPoolMember)
                .where(MLBackendPoolMember.registry_id.in_(registry_ids))
                .order_by(MLBackendPoolMember.registry_id)
                .with_for_update(read=True, nowait=True)
                .execution_options(populate_existing=True)
            )
        ).all()
        if len(members) != len(registry_ids) or any(
            row.pool_id != pool_id or row.traffic_state != "active" for row in members
        ):
            raise ContentError("repair_model_changed")
        for registry_id in registry_ids:
            backend = await self.db.scalar(
                select(MLBackendRegistry)
                .where(MLBackendRegistry.id == registry_id)
                .with_for_update(read=True, nowait=True)
                .execution_options(populate_existing=True)
            )
            if backend is None:
                raise ContentError("repair_model_changed")

    def receipt(self, kind, payload):
        if (
            self.closed
            or self.snapshot is None
            or self.snapshot._closed
            or self.transaction is None
            or self.transaction is not self.db.sync_session.get_transaction()
        ):
            raise ContentError("repair_reference_receipt_invalid")
        return _Receipt(
            _ISSUER,
            self,
            self.db.sync_session,
            self.transaction,
            kind,
            canonical_digest(payload),
        )

    def evidence(self):
        ref = self.binding["reference"]
        return {
            "schema": 1,
            "item_id": self.binding["item_id"],
            **{
                key: ref[key]
                for key in (
                    "dataset_id",
                    "asset_id",
                    "sha256",
                    "size",
                    "width",
                    "height",
                    "source_id",
                    "source_revision",
                    "accepted_revision_id",
                    "maintenance_revision",
                )
            },
            "job_id": str(self.attempt.job_id),
            "repair_id": str(self.attempt.batch_id),
            "plan_digest": self.attempt.plan_digest,
            "actor_id": str(self.attempt.actor_id),
            "node_id": self.node_id,
            "model": deepcopy(self.model),
            "route": deepcopy(self.route),
        }

    async def close(self):
        self.closed = True
        self.transaction = None
        try:
            if self.inputs:
                await self.inputs.close()
        finally:
            if self.snapshot:
                self.snapshot.close()


@dataclass(frozen=True, slots=True)
class _Receipt:
    issuer: object
    owner: object
    session: object
    transaction: object
    kind: str
    digest: str


async def validate_receipt(
    db, task_id, actor_id, receipt, kind, payload, *, project_id=None
):
    if (
        type(receipt) is not _Receipt
        or receipt.issuer is not _ISSUER
        or receipt.session is not db.sync_session
        or receipt.transaction is None
        or receipt.transaction is not db.sync_session.get_transaction()
        or db.sync_session.in_nested_transaction()
        or receipt.kind != kind
        or receipt.digest != canonical_digest(payload)
        or receipt.owner.task_id != task_id
        or receipt.owner.closed
        or receipt.owner.snapshot is None
        or receipt.owner.snapshot._closed
        or receipt.owner.transaction is not receipt.transaction
        or (actor_id is not None and receipt.owner.attempt.actor_id != actor_id)
        or (project_id is not None and receipt.owner.project_id != project_id)
    ):
        raise ContentError("repair_reference_receipt_invalid")
    await receipt.owner.validate()
    return receipt.owner.evidence()


async def qualify_repair_candidate(
    db, task, binding, prediction, metadata, *, final=False
):
    """Qualify only a persisted SAM candidate owned by its committed repair unit."""
    from app.db.models.annotation import Annotation
    from fastapi import HTTPException
    from app.services.raster_mask_storage import (
        prepare_mask_payload_for_write,
        RasterMaskContractError,
    )

    extra = (metadata.extra or {}) if metadata else {}
    info, evidence = extra.get("mask_repair"), extra.get("reference_input")
    if (
        prediction.source != "mask_repair"
        or not isinstance(info, dict)
        or not isinstance(evidence, dict)
    ):
        raise ContentError("repair_candidate_provenance_invalid")
    try:
        batch_id = uuid.UUID(str(info["batch_id"]))
    except (KeyError, ValueError, TypeError):
        raise ContentError("repair_candidate_provenance_invalid") from None
    query = select(MaskRepairBatch).where(MaskRepairBatch.id == batch_id)
    if final:
        query = query.with_for_update(read=True, nowait=True)
    batch = await db.scalar(query.execution_options(populate_existing=True))
    if (
        batch is None
        or batch.project_id != task.project_id
        or evidence.get("repair_id") != str(batch.id)
        or evidence.get("plan_digest") != batch.plan_digest
        or canonical_digest(batch.plan_json) != batch.plan_digest
    ):
        raise ContentError("repair_candidate_provenance_invalid")
    matches = [
        (index, item)
        for index, item in enumerate((batch.plan_json or {}).get("items") or [])
        if item.get("kind") == "rerun_local_sam"
        and item.get("task_id") == str(task.id)
        and item.get("issue_id") == info.get("issue_id")
        and item.get("annotation_id") == info.get("source_annotation_id")
        and item.get("source_version") == info.get("source_version")
    ]
    if len(matches) != 1:
        raise ContentError("repair_candidate_provenance_invalid")
    index, item = matches[0]
    frozen = normalize_binding(item.get("reference_input") or {})
    if (
        frozen.get("item_id") != str(task.dataset_item_id)
        or frozen.get("file_path") != task.file_path
        or frozen.get("reference") != binding["reference"]
        or evidence.get("model") != item.get("reference_model")
        or info.get("model_id") != (item.get("reference_model") or {}).get("model_id")
    ):
        raise ContentError("content_binding_changed")
    unit = ((batch.result_json or {}).get("candidates") or {}).get(str(index)) or {}
    ids = unit.get("prediction_ids") or []
    candidate_index = info.get("candidate_index")
    record = (unit.get("candidate_records") or {}).get(str(prediction.id)) or {}
    if (
        unit.get("status") != "completed"
        or unit.get("kind") != "rerun_local_sam"
        or type(candidate_index) is not int
        or not 0 <= candidate_index < len(ids)
        or ids[candidate_index] != str(prediction.id)
        or unit.get("candidate_count") != len(ids)
        or record.get("result_digest") != canonical_digest(prediction.result)
        or record.get("metadata_digest") != canonical_digest(extra)
    ):
        raise ContentError("repair_candidate_provenance_invalid")
    if final:
        # Content locks precede the source Annotation row, matching repair writes
        # and GC. Keep the native gate and exact RLE validation in this owner.
        try:
            await prepare_mask_payload_for_write(db, task, prediction.result)
        except RasterMaskContractError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from None
    query = select(Annotation).where(Annotation.id == uuid.UUID(item["annotation_id"]))
    if final:
        query = query.with_for_update(read=True, nowait=True)
    source = await db.scalar(query.execution_options(populate_existing=True))
    if (
        source is None
        or source.task_id != task.id
        or not source.is_active
        or source.was_cancelled
        or source.is_locked
        or int(source.version or 1) != item["source_version"]
        or canonical_digest(source.geometry) != item.get("source_geometry_digest")
    ):
        raise ContentError("repair_candidate_source_changed")
    accepted = await db.scalar(
        select(Annotation.id)
        .where(
            Annotation.task_id == task.id,
            Annotation.parent_prediction_id == prediction.id,
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
        )
        .limit(1)
    )
    if accepted is not None:
        raise ContentError("repair_candidate_already_accepted")
