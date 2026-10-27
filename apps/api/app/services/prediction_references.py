"""Qualify frozen reference inputs and issue transaction-bound prediction receipts."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.content_snapshot import ContentError
from app.services import (
    prediction_originals as originals,
    prediction_inputs,
    storage_access,
)
from app.services.dataset_content import key_authority
from app.services.dataset_references import reference_context, reference_snapshot
from app.services.prediction_execution import load_snapshot, validate_task_scope
from app.services.prediction_inputs import PredictionInputs
from app.services.project_access import resolve_project_access
from app.services.reference_bindings import (
    lock_reference_bindings,
    normalize_binding,
    reference_binding,
    task_binding,
)
from app.services.reference_keys import is_reference, PREFIX
from app.services.task_scope import ids_clause

MAX_TASKS = originals.MAX_TASKS
MAX_PROOF_BYTES = originals.MAX_PROOF_BYTES


def selection_query(project_id, *, task_ids=None, batch_id=None, scope="bulk"):
    query = select(Task).where(Task.project_id == project_id)
    if task_ids is not None:
        query = query.where(ids_clause(Task.id, task_ids))
    if batch_id is not None:
        query = query.where(Task.batch_id == batch_id)
    if scope != "workbench":
        query = query.where(Task.status == "pending")
    return query.order_by(Task.id).execution_options(populate_existing=True)


async def contains_references(db, project_id, *, skip_predicted=False, **scope):
    query = selection_query(project_id, **scope).where(
        Task.file_path.startswith(PREFIX, autoescape=True)
    )
    if skip_predicted:
        query = query.where(Task.total_predictions == 0)
    return await db.scalar(query.limit(1)) is not None


async def capture_authority(
    db,
    project,
    actor,
    request,
    tasks,
    snapshot,
    *,
    scope,
    batch_id,
    predict_mode,
    execution_unit=None,
    job_kind="batch_predict",
):
    """Server-only proof. Capture once, never replace it on pending republish."""
    if project.data_type != "image":
        raise ContentError("reference_requires_image_project")
    if execution_unit not in {None, "image"}:
        raise ContentError("reference_requires_image_execution")
    if len(tasks) > originals.MAX_TASKS:
        raise ContentError("prediction_task_budget_exceeded", 413)
    if len({task.id for task in tasks}) != len(tasks):
        raise ContentError("prediction_scope_changed")
    credentials = await originals.capture_credentials(db, actor, request)
    key_id = getattr(request.state, "api_key_id", None)
    has_references = any(is_reference(task.file_path) for task in tasks)
    key_proof = (
        await key_authority(db, actor, key_id, "tasks:read") if has_references else None
    )
    bindings = []
    for task in tasks:
        await validate_task_scope(db, project, task, actor, scope)
        binding = task_binding(task)
        if is_reference(task.file_path):
            if task.file_type != "image":
                raise ContentError("reference_requires_image_task")
            binding["reference"] = await reference_binding(db, task)
        else:
            binding["original"] = await managed_binding(db, task, lock=True)
        bindings.append(binding)
    return originals.bounded_proof(
        {
            "schema": 2,
            "job_kind": job_kind,
            "actor_id": str(actor.id),
            "project_id": str(project.id),
            "batch_id": str(batch_id) if batch_id else None,
            "execution_scope": scope,
            "predict_mode": predict_mode,
            "execution_unit": execution_unit,
            "api_key_id": str(key_id) if key_id else None,
            "key_proof": key_proof,
            "credentials": credentials,
            "snapshot_id": str(snapshot.id),
            "snapshot_digest": snapshot.digest,
            "tasks": bindings,
        }
    )


managed_binding = originals.managed_binding


_ISSUER = object()


@dataclass(frozen=True, slots=True)
class _Receipt:
    issuer: object
    session: object
    transaction: object
    task_id: uuid.UUID
    binding: str
    evidence: str


async def validate_receipt(db, task_id, project_id, receipt):
    if (
        type(receipt) is not _Receipt
        or receipt.issuer is not _ISSUER
        or receipt.session is not db.sync_session
        or receipt.transaction is None
        or receipt.transaction is not db.sync_session.get_transaction()
        or db.sync_session.in_nested_transaction()
        or receipt.task_id != task_id
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
    return json.loads(receipt.evidence)


class ReferencePredictionJob:
    def __init__(
        self,
        db,
        job_id,
        proof,
        *,
        project_id,
        actor_id,
        task_ids,
        batch_id,
        scope,
        predict_mode,
        snapshot_id,
        snapshot_digest,
        node_id,
        execution_unit=None,
        job_kind="batch_predict",
    ):
        self.db, self.job_id, self.proof, self.node_id = (
            db,
            job_id,
            deepcopy(proof),
            node_id,
        )
        self.project_id, self.actor_id, self.scope = project_id, actor_id, scope
        self.job_kind = job_kind
        try:
            originals.bounded_proof(proof)
            self.bindings = {
                uuid.UUID(row["task_id"]): normalize_binding(row)
                for row in proof["tasks"]
            }
        except (TypeError, KeyError, ValueError, AttributeError):
            raise ContentError("prediction_scope_changed") from None
        if (
            type(proof.get("schema")) is not int
            or proof.get("schema") not in {1, 2}
            or job_kind not in {"batch_predict", "prediction_retry"}
            or proof.get("job_kind", "batch_predict") != job_kind
            or not actor_id
            or proof.get("actor_id") != str(actor_id)
            or proof.get("project_id") != str(project_id)
            or proof.get("batch_id") != (str(batch_id) if batch_id else None)
            or proof.get("execution_scope") != scope
            or proof.get("predict_mode") != predict_mode
            or proof.get("execution_unit") != execution_unit
            or proof.get("snapshot_id") != str(snapshot_id)
            or proof.get("snapshot_digest") != snapshot_digest
            or task_ids is None
            or set(self.bindings) != set(task_ids)
            or len(self.bindings) != len(proof["tasks"])
            or len(self.bindings) > originals.MAX_TASKS
            or any(
                ("reference" in row) == ("original" in row)
                or (proof["schema"] == 1 and "reference" not in row)
                for row in self.bindings.values()
            )
            or not node_id
        ):
            raise ContentError("prediction_scope_changed")
        if proof["schema"] == 2:
            originals.validate_credentials(
                proof.get("credentials"), actor_id, proof.get("api_key_id")
            )
        self.contexts = {}
        self.deliveries = {}
        self.sources = {}

    async def authorize(self, db=None):
        db = db if db is not None else self.db
        job = await db.get(AsyncJob, self.job_id, populate_existing=True)
        actor = (
            await originals.reauthorize(
                db,
                self.proof["credentials"],
                self.actor_id,
                uuid.UUID(self.proof["api_key_id"])
                if self.proof["api_key_id"]
                else None,
            )
            if self.proof["schema"] == 2
            else await db.get(User, self.actor_id, populate_existing=True)
        )
        project = await db.get(Project, self.project_id, populate_existing=True)
        if (
            job is None
            or job.kind != self.job_kind
            or job.user_id != self.actor_id
            or job.project_id != self.project_id
            or job.status != "running"
            or (job.payload or {}).get("cancel_requested")
            or str(job.execution_snapshot_id) != self.proof["snapshot_id"]
            or (
                self.proof["schema"] == 2
                and (job.payload or {}).get("prediction_authority") != self.proof
            )
            or actor is None
            or not actor.is_active
            or project is None
            or project.data_type != "image"
        ):
            raise ContentError("prediction_authority_revoked", 403)
        access = await resolve_project_access(db, user=actor, project=project)
        if not (
            access.has("annotation.write")
            if self.scope == "workbench"
            else access.is_manager
        ):
            raise ContentError("prediction_authority_revoked", 403)
        if any("reference" in row for row in self.bindings.values()) and (
            await key_authority(db, actor, self.proof["api_key_id"], "tasks:read")
            != self.proof["key_proof"]
        ):
            raise ContentError("api_key_authority_revoked", 403)
        return project, actor

    async def _authorize_task(self, db, task, actor):
        project, fresh_actor = await self.authorize(db)
        if self.is_managed(task.id):
            task = await originals.locked_task(db, task.id)
        binding = self.bindings.get(task.id)
        if is_reference(task.file_path) and task.file_type != "image":
            raise ContentError("reference_requires_image_task")
        if binding is None or task_binding(task) != {
            k: v for k, v in binding.items() if k not in {"reference", "original"}
        }:
            raise ContentError("prediction_scope_changed")
        await validate_task_scope(db, project, task, fresh_actor, self.scope)
        if is_reference(task.file_path) and await reference_binding(
            db, task
        ) != binding.get("reference"):
            raise ContentError("content_binding_changed")
        if not is_reference(task.file_path) and (
            "original" not in binding
            or await managed_binding(db, task, lock=True) != binding["original"]
        ):
            raise ContentError("content_binding_changed")

    def is_managed(self, task_id):
        return "original" in self.bindings.get(task_id, {})

    def failure_binding(self, task_id):
        binding = self.bindings.get(task_id)
        if binding is None:
            raise ContentError("prediction_scope_changed")
        return deepcopy(binding)

    async def _managed_guard(self, db, task_id):
        task = await db.get(Task, task_id, populate_existing=True)
        if task is None:
            raise ContentError("prediction_scope_changed")
        await self._authorize_task(db, task, None)

    async def _fresh_managed(self, task_id):
        async with AsyncSession(
            bind=storage_access.engine(self.db),
            expire_on_commit=False,
            autoflush=False,
        ) as owned:
            await self._managed_guard(owned, task_id)

    async def validate(self, task):
        if self.is_managed(task.id):
            await self._fresh_managed(task.id)
            return
        _, actor = await self.authorize()
        await self._authorize_task(self.db, task, actor)
        if not is_reference(task.file_path):
            return
        context = await reference_context(
            self.db,
            task.dataset_item_id,
            actor,
            task_id=task.id,
            node_id=self.node_id,
            task_authorizer=self._authorize_task,
        )
        if task.id in self.contexts and self.contexts[task.id] != context:
            raise ContentError("content_binding_changed")
        self.contexts[task.id] = context

    async def open_inputs(self, task, factory):
        if task.id in self.deliveries or task.id in self.sources:
            raise ContentError("prediction_input_already_open")
        if self.is_managed(task.id):
            return await self._open_managed(task, factory)
        await self.validate(task)
        if not is_reference(task.file_path):
            raise ContentError("prediction_original_requires_new_request")
        _, actor = await self.authorize()
        ref = self.bindings[task.id]["reference"]
        snapshot = await reference_snapshot(
            self.db,
            task.dataset_item_id,
            actor,
            task_id=task.id,
            expected_sha256=ref["sha256"],
            api_key_id=self.proof["api_key_id"],
            node_id=self.node_id,
            task_authorizer=self._authorize_task,
        )
        inputs = None
        try:
            if (snapshot.width, snapshot.height, snapshot.size) != (
                ref["width"],
                ref["height"],
                ref["size"],
            ):
                raise ContentError("image_size_mismatch")
            await self.validate(task)
            inputs = await PredictionInputs.create(
                factory,
                owner_id=self.job_id,
                owner_kind=self.job_kind,
                task_id=task.id,
            )
            url = await inputs.upload_snapshot(snapshot)
            await self.validate(task)
            self.deliveries[task.id] = (inputs, url)
            return inputs
        except BaseException:
            snapshot.close()
            if inputs is not None:
                await inputs.close()
            raise

    async def _open_managed(self, task, factory):
        binding = self.bindings[task.id]

        async def guard(db):
            await self._managed_guard(db, task.id)

        inputs = source = None
        try:
            await self._fresh_managed(task.id)
            inputs = await originals.settled(
                PredictionInputs.create(
                    factory,
                    owner_id=self.job_id,
                    owner_kind=self.job_kind,
                    task_id=task.id,
                    guard=guard,
                    source_ref=binding,
                ),
                late=lambda value: value.close(),
            )
            source = await originals.settled(
                originals.open_snapshot(
                    self.db,
                    task.id,
                    binding["original"],
                    guard=guard,
                    purpose="prediction_input_" + inputs.record.id.hex,
                    node_id=self.node_id,
                ),
                late=lambda value: value.close(),
            )
            url = await inputs.upload_snapshot(source.snapshot)
            await inputs.check_live()
            await self._fresh_managed(task.id)
            self.sources[task.id] = source
            self.deliveries[task.id] = (inputs, url)
            return inputs
        except BaseException as failure:
            try:
                if inputs is not None:
                    await originals.settled(inputs.close())
            except BaseException as closing:
                originals._uncertain(failure, closing)
                raise
            finally:
                if source is not None:
                    await originals.settled(source.close(failure))
            raise

    def resolve_url(self, task):
        if self.proof["schema"] == 2:
            originals.check_clock(self.proof["credentials"])
        delivery = self.deliveries.get(task.id)
        if (
            delivery is None
            or delivery[0]._closed
            or delivery[0]._snapshot is None
            or delivery[0]._snapshot._closed
            or delivery[0].record.expires_at <= datetime.now(timezone.utc)
        ):
            raise ContentError("prediction_input_expired")
        return delivery[1]

    async def before_dispatch(self, task):
        self.resolve_url(task)
        if task.id in self.deliveries:
            await self.deliveries[task.id][0].check_live()
        await self.validate(task)

    async def close_inputs(self, task_id):
        delivery = self.deliveries.pop(task_id, None)
        source = self.sources.pop(task_id, None)
        self.contexts.pop(task_id, None)
        failure = None
        try:
            if delivery is not None:
                await originals.settled(delivery[0].close())
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if source is not None:
                await originals.settled(source.close(failure))

    async def lock_key(self):
        key_id = self.proof["api_key_id"]
        if key_id:
            await self.db.execute(
                select(ApiKey.id)
                .where(ApiKey.id == uuid.UUID(key_id))
                .with_for_update(read=True, nowait=True)
            )

    async def qualify_native_results(self, task, anchor, writes):
        if self.is_managed(task.id):
            return [None] * len(writes)
        from app.services.reference_native_predictions import qualify_results

        return await qualify_results(self, task, anchor, writes)

    async def final_receipt(self, task):
        """Caller holds job, actor/project/membership, Key and batch/Task locks."""
        if self.db.sync_session.in_nested_transaction():
            raise ContentError("prediction_reference_receipt_invalid")
        await self.lock_key()
        binding = self.bindings[task.id]
        if self.is_managed(task.id):
            await self._managed_guard(self.db, task.id)
            _, digest = await load_snapshot(
                self.db, self.proof["snapshot_id"], self.project_id
            )
            if digest != self.proof["snapshot_digest"]:
                raise ContentError("prediction_scope_changed")
            self.resolve_url(task)
            await prediction_inputs.check_metadata(self.db, self.deliveries[task.id][0])
            evidence = {
                **deepcopy(binding),
                "schema": 1,
                "job_id": str(self.job_id),
                "actor_id": str(self.actor_id),
                "node_id": self.node_id,
                "execution_snapshot_id": self.proof["snapshot_id"],
                "execution_digest": digest,
            }
            return originals.issue_receipt(self.db, task.id, binding, evidence)
        await lock_reference_bindings(self.db, [binding], self.node_id)
        await self.validate(task)
        _, digest = await load_snapshot(
            self.db, self.proof["snapshot_id"], self.project_id
        )
        if digest != self.proof["snapshot_digest"]:
            raise ContentError("prediction_scope_changed")
        if "reference" not in binding:
            return None
        self.resolve_url(task)
        ref = binding["reference"]
        evidence = {
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
        }
        evidence.update(
            schema=1,
            item_id=binding["item_id"],
            task_version=binding["version"],
            job_id=str(self.job_id),
            actor_id=str(self.actor_id),
            node_id=self.node_id,
            execution_snapshot_id=self.proof["snapshot_id"],
            execution_digest=digest,
        )
        return _Receipt(
            _ISSUER,
            self.db.sync_session,
            self.db.sync_session.get_transaction(),
            task.id,
            json.dumps(binding, sort_keys=True),
            json.dumps(evidence, sort_keys=True),
        )
