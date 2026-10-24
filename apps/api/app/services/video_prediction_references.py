"""Frozen whole-video job authority and transaction-bound result receipts.

Original and temporary-input ownership remain separate. Source files and their
permits must really settle before final business locks; final checks perform
only SQL and credential verification in the caller's transaction.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from types import SimpleNamespace
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.project import Project
from app.services import prediction_inputs, prediction_originals as authority
from app.services import storage_access, video_prediction_originals as originals
from app.services.content_snapshot import ContentError
from app.services.ml_capabilities import extract_capabilities
from app.services.prediction_execution import load_snapshot, validate_task_scope
from app.services.prediction_inputs import PredictionInputs
from app.services.project_access import resolve_project_access
from app.services.project_write_guard import (
    lock_actor_project_authority,
    lock_actor_scope,
)
from app.services.reference_bindings import task_binding
from app.services.reference_keys import is_reference

MAX_TASKS = 1000
MAX_PROOF_BYTES = 1024**2
PRIVATE_KEY = "video_prediction_authority"
_ISSUER = object()
_TASK_FIELDS = {
    "task_id",
    "batch_id",
    "item_id",
    "file_path",
    "file_name",
    "version",
    "original",
}
_PROOF_FIELDS = {
    "schema",
    "media_type",
    "job_kind",
    "actor_id",
    "project_id",
    "batch_id",
    "execution_scope",
    "predict_mode",
    "execution_unit",
    "api_key_id",
    "credentials",
    "snapshot_id",
    "snapshot_digest",
    "tasks",
    "models",
}
_MODEL_FIELDS = (
    "id",
    "task",
    "supported_inputs",
    "supported_geometric_outputs",
    "supported_prompts",
    "supported_trackers",
    "supported_variants",
    "variant_combinations",
    "default_variants",
    "params",
    "resource_profile",
)
_MODEL_BINDING_FIELDS = {
    "stage",
    "parent_stage",
    "requested_registry_id",
    "pool_id",
    "model_id",
    "declaration",
    "declaration_digest",
    "config_digest",
    "identity_guarantee",
}
_PUBLIC_ORIGINAL = (
    "schema",
    "media_type",
    "owner_kind",
    "content_id",
    "sha256",
    "size_bytes",
    "location_id",
    "location_revision",
    "maintenance_revision",
    "facts_digest",
)


def reject(code="prediction_video_scope_changed", status=409):
    raise ContentError(code, status)


def bounded(value):
    try:
        raw = authority.encoded(value).encode()
    except (ValueError, TypeError, OverflowError, UnicodeError):
        reject()
    if len(raw) > MAX_PROOF_BYTES:
        reject("prediction_video_proof_budget_exceeded", 413)
    return json.loads(raw)


def _same(left, right):
    # Python equality considers True == 1; frozen JSON contracts do not.
    return authority.encoded(left) == authority.encoded(right)


def _uuid(value):
    try:
        result = uuid.UUID(value)
        if str(result) != value:
            raise ValueError
        return result
    except (ValueError, TypeError, AttributeError):
        reject()


def _binding(value):
    if not isinstance(value, dict) or set(value) != _TASK_FIELDS:
        reject()
    _uuid(value["task_id"])
    for name in ("batch_id", "item_id"):
        if value[name] is not None:
            _uuid(value[name])
    if (
        type(value["version"]) is not int
        or value["version"] < 1
        or not isinstance(value["file_path"], str)
        or not isinstance(value["file_name"], str)
        or is_reference(value["file_path"])
    ):
        reject()
    return bounded(
        {**value, "original": originals.normalize_binding(value["original"])}
    )


def _probe(value):
    try:
        video = value["video"]
        if (
            type(value) is not dict
            or set(value) != {"probe_contract", "video"}
            or type(value["probe_contract"]) is not int
            or value["probe_contract"] != 1
            or type(video) is not dict
            or set(video)
            != {"width", "height", "fps", "frame_count", "duration_ms", "codec"}
            or any(
                type(video[key]) is not int or video[key] <= 0
                for key in ("width", "height", "frame_count")
            )
            or video["frame_count"] > 1_000_000
            or type(video["fps"]) not in (float, int)
            or not math.isfinite(video["fps"])
            or video["fps"] <= 0
            or (
                video["duration_ms"] is not None
                and (type(video["duration_ms"]) is not int or video["duration_ms"] < 0)
            )
            or not isinstance(video["codec"], str)
            or not 0 < len(video["codec"]) <= 128
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        reject("prediction_video_probe_invalid")
    return bounded(value)


def model_declaration(capabilities, stage):
    """Fields reconstructible from setup, excluding display and live health."""
    caps = extract_capabilities(capabilities)
    models = (caps or {}).get("models") or []
    model_id = stage.get("model_id")
    candidates = [
        model
        for model in models
        if (
            model.get("id") == model_id
            if model_id is not None
            else model.get("task") == stage.get("task")
        )
    ]
    if len(candidates) != 1:
        reject("prediction_video_model_unrecorded")
    return bounded(
        {
            "task_contract_revision": (caps or {}).get("task_contract_revision", 0),
            "model": {key: candidates[0].get(key) for key in _MODEL_FIELDS},
        }
    )


def _models(value):
    if not isinstance(value, list) or not value or len(value) > 1000:
        reject("prediction_video_model_unrecorded")
    identities = set()
    for row in value:
        if (
            not isinstance(row, dict)
            or set(row) != _MODEL_BINDING_FIELDS
            or type(row["stage"]) is not int
            or row["stage"] < 0
            or row["stage"] in identities
            or (
                row["parent_stage"] is not None
                and (type(row["parent_stage"]) is not int or row["parent_stage"] < 0)
            )
            or row["identity_guarantee"] != "declared_configuration"
            or row["model_id"] is not None
            and not isinstance(row["model_id"], str)
            or not isinstance(row["declaration"], dict)
            or set(row["declaration"]) != {"task_contract_revision", "model"}
            or type(row["declaration"]["task_contract_revision"]) is not int
            or row["declaration"]["task_contract_revision"] not in {0, 1}
            or not isinstance(row["declaration"]["model"], dict)
            or set(row["declaration"]["model"]) != set(_MODEL_FIELDS)
            or row["declaration_digest"] != authority.digest(row["declaration"])
            or not isinstance(row["config_digest"], str)
            or len(row["config_digest"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in row["config_digest"]
            )
        ):
            reject("prediction_video_model_unrecorded")
        _uuid(row["requested_registry_id"])
        _uuid(row["pool_id"])
        identities.add(row["stage"])
    if sum(row["parent_stage"] is None for row in value) != 1 or any(
        row["parent_stage"] is not None and row["parent_stage"] not in identities
        for row in value
    ):
        reject("prediction_video_model_unrecorded")
    return bounded(value)


async def _backend(db, identifier):
    return await db.scalar(
        select(MLBackendRegistry)
        .where(MLBackendRegistry.id == identifier)
        .with_for_update(read=True, nowait=True)
        .execution_options(populate_existing=True)
    )


async def _pool(db, project_id, registry_id, expected=None):
    member = await db.scalar(
        select(MLBackendPoolMember)
        .where(MLBackendPoolMember.registry_id == registry_id)
        .with_for_update(read=True, nowait=True)
        .execution_options(populate_existing=True)
    )
    if member is None or member.traffic_state != "active":
        reject("prediction_video_model_changed")
    pool = await db.scalar(
        select(MLBackendServicePool)
        .where(MLBackendServicePool.id == member.pool_id)
        .with_for_update(read=True, nowait=True)
        .execution_options(populate_existing=True)
    )
    association = await db.scalar(
        select(ProjectMLBackendPool)
        .where(
            ProjectMLBackendPool.project_id == project_id,
            ProjectMLBackendPool.pool_id == member.pool_id,
        )
        .with_for_update(read=True, nowait=True)
        .execution_options(populate_existing=True)
    )
    if (
        pool is None
        or not pool.enabled
        or association is None
        or not association.enabled
        or (expected is not None and str(pool.id) != expected)
    ):
        reject("prediction_video_model_changed")
    return str(pool.id)


async def _capture_models(db, project_id, payload):
    stages = payload.get("stages")
    if not isinstance(stages, list) or not stages or len(stages) > MAX_TASKS:
        reject("prediction_video_model_unrecorded")
    result, seen, size = [], set(), 2
    for stage in stages:
        if (
            not isinstance(stage, dict)
            or type(stage.get("stage")) is not int
            or stage["stage"] in seen
        ):
            reject("prediction_video_model_unrecorded")
        seen.add(stage["stage"])
        backend_id = _uuid(stage.get("ml_backend_id"))
        backend = await _backend(db, backend_id)
        if backend is None or backend.state != "connected":
            reject("prediction_video_model_changed")
        pool_id = await _pool(db, project_id, backend_id)
        declaration = model_declaration(
            (backend.health_meta or {}).get("capabilities") or {}, stage
        )
        row = {
            "stage": stage["stage"],
            "parent_stage": stage.get("parent_stage"),
            "requested_registry_id": str(backend_id),
            "pool_id": pool_id,
            "model_id": stage.get("model_id"),
            "declaration": declaration,
            "declaration_digest": authority.digest(declaration),
            "config_digest": authority.digest(stage),
            "identity_guarantee": "declared_configuration",
        }
        size += len(authority.encoded(row).encode()) + bool(result)
        if size > MAX_PROOF_BYTES:
            reject("prediction_video_proof_budget_exceeded", 413)
        result.append(row)
    return _models(result)


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
    expected_models=None,
):
    """Freeze one new request without committing its Job transaction."""
    if project.data_type != "video" or execution_unit not in {None, "video"}:
        reject("prediction_requires_whole_video_execution")
    if (
        scope not in {"bulk", "workbench"}
        or predict_mode not in {"overwrite", "append", "skip_predicted"}
        or job_kind not in {"batch_predict", "prediction_retry"}
    ):
        reject()
    if job_kind == "prediction_retry" and expected_models is None:
        reject("prediction_video_requires_new_request")
    if len(tasks) > MAX_TASKS or len({task.id for task in tasks}) != len(tasks):
        reject("prediction_video_task_budget_exceeded", 413)
    credentials = await authority.capture_credentials(db, actor, request)
    await lock_actor_project_authority(db, actor.id, project.id)
    await lock_actor_scope(db, actor.id, project.id)
    access = await resolve_project_access(db, user=actor, project=project)
    if not (
        access.has("annotation.write") if scope == "workbench" else access.is_manager
    ):
        reject("prediction_authority_revoked", 403)
    payload, snapshot_digest = await load_snapshot(db, snapshot.id, project.id)
    if (
        snapshot_digest != snapshot.digest
        or payload.get("execution_scope") != scope
        or payload.get("execution_unit") != execution_unit
    ):
        reject()
    models = await _capture_models(db, project.id, payload)
    if expected_models is not None and not _same(models, _models(expected_models)):
        reject("prediction_video_model_changed")
    proof = {
        "schema": 1,
        "media_type": "video",
        "job_kind": job_kind,
        "actor_id": str(actor.id),
        "project_id": str(project.id),
        "batch_id": str(batch_id) if batch_id else None,
        "execution_scope": scope,
        "predict_mode": predict_mode,
        "execution_unit": execution_unit,
        "api_key_id": credentials["api_key_id"],
        "credentials": credentials,
        "snapshot_id": str(snapshot.id),
        "snapshot_digest": snapshot_digest,
        "tasks": [],
        "models": models,
    }
    size = len(authority.encoded(bounded(proof)).encode())
    for current in tasks:
        task = await authority.locked_task(db, current.id)
        if (
            task.project_id != project.id
            or task.file_type != "video"
            or is_reference(task.file_path)
        ):
            reject(
                "reference_operation_not_supported"
                if is_reference(task.file_path)
                else "prediction_video_scope_changed"
            )
        if batch_id is not None and task.batch_id != batch_id:
            reject()
        await validate_task_scope(db, project, task, actor, scope)
        binding = _binding(
            {
                **task_binding(task),
                "original": await originals.managed_binding(db, task, lock=True),
            }
        )
        size += len(authority.encoded(binding).encode()) + bool(proof["tasks"])
        if size > MAX_PROOF_BYTES:
            reject("prediction_video_proof_budget_exceeded", 413)
        proof["tasks"].append(binding)
    return bounded(proof)


def public_provenance(evidence):
    """Only explicit public facts; never locators, private bindings or credentials."""
    original = evidence["original"]
    return bounded(
        {
            **{
                key: evidence[key]
                for key in (
                    "schema",
                    "media_type",
                    "task_id",
                    "batch_id",
                    "item_id",
                    "version",
                    "job_id",
                    "actor_id",
                    "node_id",
                    "execution_snapshot_id",
                    "execution_digest",
                    "input_id",
                    "input_version_id",
                )
                if key in evidence
            },
            "original": {key: original[key] for key in _PUBLIC_ORIGINAL},
            "probe": _probe(evidence["probe"]),
            "models": [
                {
                    key: row[key]
                    for key in (
                        "stage",
                        "requested_registry_id",
                        "pool_id",
                        "model_id",
                        "declaration_digest",
                        "parent_stage",
                        "config_digest",
                        "identity_guarantee",
                        "executed_registry_id",
                    )
                    if key in row
                }
                for row in evidence["models"]
            ],
        }
    )


@dataclass(frozen=True, slots=True)
class _Receipt:
    issuer: object = field(repr=False)
    session: object = field(repr=False)
    transaction: object = field(repr=False)
    task_id: uuid.UUID
    project_id: uuid.UUID
    owner: object = field(repr=False)
    binding: str = field(repr=False)
    evidence: str = field(repr=False)


async def validate_receipt(db, task_id, project_id, receipt):
    if (
        type(receipt) is not _Receipt
        or receipt.issuer is not _ISSUER
        or receipt.session is not db.sync_session
        or receipt.transaction is None
        or receipt.transaction is not db.sync_session.get_transaction()
        or db.sync_session.in_nested_transaction()
        or receipt.task_id != task_id
        or receipt.project_id != project_id
        or receipt.owner.db is not db
    ):
        reject("prediction_video_receipt_invalid")
    evidence = await receipt.owner._final_check(task_id)
    if not _same(
        receipt.owner.bindings[task_id], json.loads(receipt.binding)
    ) or not _same(evidence, json.loads(receipt.evidence)):
        reject("prediction_video_receipt_invalid")
    return public_provenance(evidence)


class VideoPredictionJob:
    media_type = "video"

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
        celery_task_id,
        execution_unit=None,
        job_kind="batch_predict",
    ):
        try:
            proof = bounded(proof)
            if not isinstance(proof, dict) or set(proof) != _PROOF_FIELDS:
                reject()
            bindings = {_uuid(row["task_id"]): _binding(row) for row in proof["tasks"]}
            if (
                type(proof["schema"]) is not int
                or proof["schema"] != 1
                or proof["media_type"] != "video"
                or job_kind not in {"batch_predict", "prediction_retry"}
                or proof["job_kind"] != job_kind
                or not actor_id
                or proof["actor_id"] != str(actor_id)
                or proof["project_id"] != str(project_id)
                or proof["batch_id"] != (str(batch_id) if batch_id else None)
                or scope not in {"bulk", "workbench"}
                or proof["execution_scope"] != scope
                or proof["predict_mode"] != predict_mode
                or predict_mode not in {"overwrite", "append", "skip_predicted"}
                or execution_unit not in {None, "video"}
                or proof["execution_unit"] != execution_unit
                or proof["snapshot_id"] != str(snapshot_id)
                or proof["snapshot_digest"] != snapshot_digest
                or task_ids is None
                or len(set(task_ids)) != len(task_ids)
                or set(bindings) != set(task_ids)
                or len(bindings) != len(proof["tasks"])
                or len(bindings) > MAX_TASKS
                or not isinstance(proof["models"], list)
                or not proof["models"]
                or not isinstance(node_id, str)
                or not node_id
                or not isinstance(celery_task_id, str)
                or not celery_task_id
            ):
                reject()
            authority.validate_credentials(
                proof["credentials"], actor_id, proof["api_key_id"]
            )
            _models(proof["models"])
        except (KeyError, TypeError, ValueError, AttributeError):
            reject()
        self.db, self.job_id, self.proof = db, job_id, proof
        self.project_id, self.actor_id, self.node_id = project_id, actor_id, node_id
        self.job_kind, self.scope, self.celery_task_id = job_kind, scope, celery_task_id
        self.bindings = bindings
        self.deliveries, self.sources, self.probes, self.input_receipts = {}, {}, {}, {}
        self.finished, self.unsettled, self.executions = set(), set(), {}

    async def authorize(self, db=None):
        db = self.db if db is None else db
        await lock_actor_project_authority(db, self.actor_id, self.project_id)
        await lock_actor_scope(db, self.actor_id, self.project_id)
        actor = await authority.reauthorize(
            db,
            self.proof["credentials"],
            self.actor_id,
            _uuid(self.proof["api_key_id"]) if self.proof["api_key_id"] else None,
        )
        project = await db.get(Project, self.project_id, populate_existing=True)
        job = await db.scalar(
            select(AsyncJob)
            .where(AsyncJob.id == self.job_id)
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True)
        )
        if (
            job is None
            or job.kind != self.job_kind
            or job.user_id != self.actor_id
            or job.project_id != self.project_id
            or job.status != "running"
            or (job.payload or {}).get("cancel_requested")
            or str(job.execution_snapshot_id) != self.proof["snapshot_id"]
            or job.celery_task_id != self.celery_task_id
            or not _same((job.payload or {}).get(PRIVATE_KEY), self.proof)
            or project is None
            or project.data_type != "video"
        ):
            reject("prediction_authority_revoked", 403)
        access = await resolve_project_access(db, user=actor, project=project)
        if not (
            access.has("annotation.write")
            if self.scope == "workbench"
            else access.is_manager
        ):
            reject("prediction_authority_revoked", 403)
        return project, actor

    async def _task(self, db, task_id):
        project, actor = await self.authorize(db)
        task = await authority.locked_task(db, task_id)
        binding = self.bindings.get(task_id)
        if (
            task.file_type != "video"
            or is_reference(task.file_path)
            or task.project_id != self.project_id
            or binding is None
            or not _same(
                task_binding(task),
                {key: value for key, value in binding.items() if key != "original"},
            )
        ):
            reject()
        await validate_task_scope(db, project, task, actor, self.scope)
        if not _same(
            await originals.managed_binding(db, task, lock=True), binding["original"]
        ):
            reject("content_binding_changed")
        payload, digest = await load_snapshot(
            db, self.proof["snapshot_id"], self.project_id
        )
        if digest != self.proof["snapshot_digest"]:
            reject()
        current_models = await _capture_models(db, self.project_id, payload)
        if not _same(current_models, self.proof["models"]):
            reject("prediction_video_model_changed")
        return task

    async def _fresh(self, task_id):
        async with AsyncSession(
            bind=storage_access.engine(self.db), expire_on_commit=False, autoflush=False
        ) as owned:
            await self._task(owned, task_id)

    async def open_inputs(self, task, factory):
        identifier = task.id
        if (
            identifier not in self.bindings
            or identifier in self.deliveries
            or identifier in self.sources
            or identifier in self.finished
            or identifier in self.unsettled
        ):
            reject("prediction_input_already_open")
        binding = self.bindings[identifier]

        async def guard(db):
            await self._task(db, identifier)

        inputs = source = None
        try:
            await self._fresh(identifier)
            inputs = await authority.settled(
                PredictionInputs.create(
                    factory,
                    owner_id=self.job_id,
                    owner_kind="video_" + self.job_kind,
                    task_id=identifier,
                    guard=guard,
                    source_ref=binding,
                ),
                late=lambda value: value.close(),
            )
            source = await authority.settled(
                originals.open_snapshot(
                    self.db,
                    identifier,
                    binding["original"],
                    guard=guard,
                    purpose="prediction_input_" + inputs.record.id.hex,
                    node_id=self.node_id,
                ),
                late=lambda value: value.aclose(),
            )
            probe = _probe(source.probe)
            url = await inputs.upload_video(source.stage, probe=probe)
            await inputs.check_live()
            await self._fresh(identifier)
            self.sources[identifier] = source
            self.deliveries[identifier] = (inputs, url)
            self.probes[identifier] = probe
            self.input_receipts[identifier] = bounded(inputs.video_receipt)
            return inputs
        except BaseException as failure:
            await self._close_pair(inputs, source, failure)
            raise

    def resolve_url(self, task):
        authority.check_clock(self.proof["credentials"])
        delivery = self.deliveries.get(task.id)
        if (
            delivery is None
            or task.id in self.unsettled
            or delivery[0]._closed
            or delivery[0]._unsettled
            or delivery[0].record.expires_at <= datetime.now(timezone.utc)
        ):
            reject("prediction_input_expired")
        return delivery[1]

    async def before_dispatch(self, task, transport, *, stage_id=None):
        self.resolve_url(task)
        models = self.proof["models"]
        if stage_id is None and len(models) == 1:
            stage_id = models[0]["stage"]
        selected = next(
            (
                row
                for row in models
                if row["stage"] == stage_id and type(stage_id) is int
            ),
            None,
        )
        if selected is None:
            reject("prediction_video_model_changed")
        actual_id = getattr(transport, "backend_id", None)
        if not isinstance(actual_id, uuid.UUID):
            reject("prediction_video_model_changed")
        await self._fresh(task.id)
        await self.deliveries[task.id][0].check_live()
        # Every short SQL transaction is closed before actual setup I/O.
        setup = await transport.setup()
        declaration = model_declaration(
            setup,
            {
                "model_id": selected["declaration"]["model"]["id"],
                "task": selected["declaration"]["model"]["task"],
            },
        )
        if not _same(declaration, selected["declaration"]):
            reject("prediction_video_model_changed")
        async with AsyncSession(
            bind=storage_access.engine(self.db), expire_on_commit=False, autoflush=False
        ) as owned:
            await self._task(owned, task.id)
            backend = await _backend(owned, actual_id)
            if (
                backend is None
                or backend.state != "connected"
                or not _same(
                    model_declaration(
                        (backend.health_meta or {}).get("capabilities") or {},
                        {"model_id": selected["declaration"]["model"]["id"]},
                    ),
                    selected["declaration"],
                )
            ):
                reject("prediction_video_model_changed")
            await _pool(owned, self.project_id, actual_id, selected["pool_id"])
        self.executions[(task.id, stage_id)] = str(actual_id)
        self.resolve_url(task)

    async def finish_source(self, task_id):
        source = self.sources.get(task_id)
        if source is None or task_id in self.unsettled:
            reject("prediction_video_source_unsettled")
        try:
            await authority.settled(source.aclose())
            if not source.closed or not source.settled or source.unsettled:
                self.unsettled.add(task_id)
                reject("prediction_video_source_unsettled")
            self.finished.add(task_id)
        except BaseException as failure:
            if (
                getattr(failure, "storage_operation_unsettled", False)
                or source.unsettled
                or not source.settled
            ):
                self.unsettled.add(task_id)
            raise

    async def lock_key(self):
        await authority.reauthorize(
            self.db,
            self.proof["credentials"],
            self.actor_id,
            _uuid(self.proof["api_key_id"]) if self.proof["api_key_id"] else None,
        )

    async def recheck_credentials(self, db=None):
        await authority.reauthorize(
            self.db if db is None else db,
            self.proof["credentials"],
            self.actor_id,
            _uuid(self.proof["api_key_id"]) if self.proof["api_key_id"] else None,
        )

    def is_managed(self, task_id):
        return task_id in self.bindings

    def failure_binding(self, task_id):
        if task_id not in self.bindings:
            reject()
        return deepcopy(self.bindings[task_id])

    def video_probe(self, task_id):
        if task_id not in self.probes or task_id in self.unsettled:
            reject("prediction_video_probe_invalid")
        return deepcopy(self.probes[task_id])

    def failure_evidence(self, task_id):
        return {
            "original_video_input": self.failure_binding(task_id),
            "video_execution": {
                "snapshot_id": self.proof["snapshot_id"],
                "snapshot_digest": self.proof["snapshot_digest"],
                "models": deepcopy(self.proof["models"]),
            },
        }

    async def qualify_native_results(self, task, anchor, writes):
        from app.services.raster_mask_storage import (
            collect_mask_geometries,
            collect_mask_references,
        )

        if any(
            (write.get("token_meta") or {}).get("mask_objects") is not None
            or collect_mask_geometries(write.get("result"))
            or collect_mask_references(write.get("result"))
            for write in writes
        ):
            reject("prediction_video_native_output_unsupported")
        return [None] * len(writes)

    async def _final_check(self, task_id):
        if (
            self.db.sync_session.in_nested_transaction()
            or self.db.sync_session.get_transaction() is None
        ):
            reject("prediction_video_receipt_invalid")
        source = self.sources.get(task_id)
        if (
            source is None
            or task_id not in self.finished
            or task_id in self.unsettled
            or source.unsettled
            or not source.closed
            or not source.settled
        ):
            reject("prediction_video_source_unsettled")
        await self._task(self.db, task_id)
        delivery = self.deliveries.get(task_id)
        self.resolve_url(SimpleNamespace(id=task_id))
        await prediction_inputs.check_metadata(self.db, delivery[0])
        saved = self.input_receipts[task_id]
        if (
            not _same(delivery[0].video_receipt, saved)
            or not _same(saved.get("probe"), self.probes[task_id])
            or saved.get("sha256") != self.bindings[task_id]["original"]["sha256"]
            or saved.get("size_bytes")
            != self.bindings[task_id]["original"]["size_bytes"]
        ):
            reject("prediction_video_input_changed")
        models = []
        for row in self.proof["models"]:
            actual = self.executions.get((task_id, row["stage"]))
            if row["parent_stage"] is None and actual is None:
                reject("prediction_video_dispatch_unrecorded")
            if actual is not None:
                backend = await _backend(self.db, _uuid(actual))
                if (
                    backend is None
                    or backend.state != "connected"
                    or not _same(
                        model_declaration(
                            (backend.health_meta or {}).get("capabilities") or {},
                            {"model_id": row["declaration"]["model"]["id"]},
                        ),
                        row["declaration"],
                    )
                ):
                    reject("prediction_video_model_changed")
                await _pool(self.db, self.project_id, backend.id, row["pool_id"])
            models.append({**row, "executed_registry_id": actual})
        return public_provenance(
            {
                **self.bindings[task_id],
                "schema": 1,
                "media_type": "video",
                "job_id": str(self.job_id),
                "actor_id": str(self.actor_id),
                "node_id": self.node_id,
                "execution_snapshot_id": self.proof["snapshot_id"],
                "execution_digest": self.proof["snapshot_digest"],
                "probe": self.probes[task_id],
                "models": models,
                "input_id": str(delivery[0].record.id),
                "input_version_id": saved["version_id"],
            }
        )

    async def final_receipt(self, task):
        evidence = await self._final_check(task.id)
        return _Receipt(
            _ISSUER,
            self.db.sync_session,
            self.db.sync_session.get_transaction(),
            task.id,
            self.project_id,
            self,
            authority.encoded(self.bindings[task.id]),
            authority.encoded(evidence),
        )

    async def _close_pair(self, inputs, source, failure=None):
        closing = None
        try:
            if inputs is not None:
                await authority.settled(inputs.close())
        except BaseException as exc:
            closing = exc
            if failure is not None:
                authority._uncertain(failure, exc)
        finally:
            if source is not None:
                try:
                    await authority.settled(source.aclose(closing or failure))
                except BaseException as exc:
                    if failure is not None:
                        authority._uncertain(failure, exc)
                    if closing is not None:
                        authority._uncertain(closing, exc)
                    else:
                        closing = exc
        if closing is not None:
            raise closing

    async def close_inputs(self, task_id):
        delivery = self.deliveries.pop(task_id, None)
        source = self.sources.pop(task_id, None)
        try:
            await self._close_pair(delivery[0] if delivery else None, source)
        finally:
            self.probes.pop(task_id, None)
            self.input_receipts.pop(task_id, None)
