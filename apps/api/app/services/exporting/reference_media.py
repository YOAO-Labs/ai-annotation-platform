"""Own the frozen selection, authority and strict media of a reference export."""

from __future__ import annotations

from copy import deepcopy
import re
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.content_snapshot import ContentError
from app.services.dataset_content import key_authority
from app.services.dataset_references import reference_context, reference_snapshot
from app.services.reference_keys import is_reference
from app.services.exporting.scope import ids_clause, lock_task_scope
from app.services.reference_bindings import (
    task_binding as _task_binding,
    reference_binding as _reference_binding,
    lock_reference_bindings,
    normalize_binding,
)


class ExportCancelled(Exception):
    """Cooperative cancellation, independent of export failure notifications."""


def task_query(project_id, batch_id=None, task_ids=None):
    query = select(Task).where(Task.project_id == project_id)
    if batch_id is not None:
        query = query.where(Task.batch_id == batch_id)
    if task_ids is not None:
        query = query.where(ids_clause(Task.id, task_ids))
    return query.order_by(Task.id).execution_options(populate_existing=True)


async def authorize_export_job(
    db, *, project_id, task_ids, job_uuid, batch_id=None, allow_reference=False
):
    """Preserve whole-project export capability and explicit-task visibility."""
    from app.services.data_management.task_filters import visible_tasks_stmt
    from app.services.project_access import ProjectCapability, resolve_project_access

    project = await db.get(Project, project_id, populate_existing=True)
    job = await db.get(AsyncJob, job_uuid, populate_existing=True)
    if project is None or job is None or job.project_id != project_id:
        raise ValueError("export scope owner or project is unavailable")
    actor = (
        await db.get(User, job.user_id, populate_existing=True)
        if job.user_id is not None
        else None
    )
    if actor is None or not actor.is_active:
        raise ValueError("export scope owner is unavailable")
    try:
        access = await resolve_project_access(db, user=actor, project=project)
    except HTTPException as exc:
        raise ValueError("export project access is no longer valid") from exc
    if ProjectCapability.EXPORT_ANNOTATIONS.value not in access.capabilities:
        raise ValueError("export capability is no longer valid")
    if task_ids is not None:
        stored = ((job.payload or {}).get("scope") or {}).get("task_ids")
        if stored is None:
            stored = (job.payload or {}).get("task_ids")
        if not task_ids or {str(v) for v in stored or []} != {str(v) for v in task_ids}:
            raise ValueError("export job task scope does not match worker arguments")
        query = (
            select(Task.id).where(Task.project_id == project_id)
            if access.is_manager
            else visible_tasks_stmt(
                project_id,
                user=actor,
                project=project,
                project_role=access.project_role,
            )
        )
        visible = set(
            (await db.scalars(query.where(ids_clause(Task.id, task_ids)))).all()
        )
        if visible != set(task_ids):
            raise ValueError("export task scope is no longer visible")
    if not allow_reference:
        from app.services.reference_keys import PREFIX

        query = task_query(project_id, batch_id, task_ids).where(
            Task.file_path.startswith(PREFIX, autoescape=True)
        )
        if await db.scalar(query.limit(1)):
            raise ContentError("reference_export_authority_missing")
    return actor


async def _capture_export_authority(
    db, project, actor, request, *, batch_id=None, task_ids=None, frozen_snapshot=None
):
    """No original I/O. Never replace this proof when republishing an existing job."""
    if project.data_type != "image":
        return None
    references = None
    if frozen_snapshot is None:
        tasks = list(
            (await db.scalars(task_query(project.id, batch_id, task_ids))).all()
        )
    else:
        # New image jobs have already measured and locked their bounded scope.
        # Reuse those facts; neither Task JSON nor source/connection credentials
        # need full ORM hydration merely to capture this separate authority.
        selected = sorted(str(v) for v in task_ids) if task_ids is not None else None
        if (
            type(frozen_snapshot) is not dict
            or type(frozen_snapshot.get("schema")) is not int
            or frozen_snapshot["schema"] != 3
            or frozen_snapshot.get("project_id") != str(project.id)
            or frozen_snapshot.get("batch_id") != (str(batch_id) if batch_id else None)
            or frozen_snapshot.get("task_ids") != selected
            or type(frozen_snapshot.get("tasks")) is not list
            or type(frozen_snapshot.get("references")) is not dict
        ):
            raise ContentError("export_scope_changed")
        tasks = []
        for facts in frozen_snapshot["tasks"]:
            tasks.append(
                SimpleNamespace(
                    id=facts["id"],
                    batch_id=facts["batch_id"],
                    dataset_item_id=facts["dataset_item_id"],
                    file_path=facts["file_path"],
                    file_name=facts["file_name"],
                    version=facts["version"],
                )
            )
        references = frozen_snapshot["references"]
        if set(references) != {
            str(task.id) for task in tasks if is_reference(task.file_path)
        }:
            raise ContentError("export_scope_changed")
    if not any(is_reference(task.file_path) for task in tasks):
        return None
    key_id = getattr(request.state, "api_key_id", None)
    proof = await key_authority(db, actor, key_id, "tasks:read")
    bindings = []
    for task in tasks:
        binding = _task_binding(task)
        if is_reference(task.file_path):
            binding["reference"] = (
                deepcopy(references[str(task.id)])
                if references is not None
                else await _reference_binding(db, task)
            )
        bindings.append(binding)
    return {
        "schema": 1,
        "actor_id": str(actor.id),
        "project_id": str(project.id),
        "batch_id": str(batch_id) if batch_id else None,
        "selected_task_ids": sorted(str(v) for v in task_ids)
        if task_ids is not None
        else None,
        "api_key_id": str(key_id) if key_id else None,
        "key_proof": proof,
        "tasks": bindings,
    }


async def capture_export_authority(db, project, actor, request, **scope):
    try:
        return await _capture_export_authority(db, project, actor, request, **scope)
    except ContentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None


async def check_cancelled(db, job_id, *, lock=False):
    query = (
        select(AsyncJob)
        .where(AsyncJob.id == job_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    job = await db.scalar(query)
    if job is None:
        raise ValueError("export async job not found")
    if job.status == "cancelled" or (job.payload or {}).get("cancel_requested"):
        raise ExportCancelled()
    if job.status not in {"pending", "running"}:
        raise ValueError("export job is no longer active")
    return job


class ReferenceExportMedia:
    def __init__(self, db, job_id, proof, *, project_id, batch_id, task_ids, node_id):
        self.db, self.job_id, self.proof = db, job_id, proof
        self.project_id, self.batch_id, self.selected_ids = (
            project_id,
            batch_id,
            task_ids,
        )
        self.node_id = node_id
        if (
            proof.get("schema") != 1
            or proof.get("project_id") != str(project_id)
            or proof.get("batch_id") != (str(batch_id) if batch_id else None)
            or proof.get("selected_task_ids")
            != (sorted(str(v) for v in task_ids) if task_ids is not None else None)
        ):
            raise ContentError("export_scope_changed")
        self.bindings = {
            uuid.UUID(row["task_id"]): normalize_binding(row) for row in proof["tasks"]
        }
        self.task_ids = sorted(self.bindings, key=str)
        self.contexts = {}
        self.paths = {}
        for task_id, binding in self.bindings.items():
            ref = binding.get("reference")
            if ref:
                # Stable IDs prevent collisions even for multiple Tasks of one Item.
                filename = binding["file_name"].replace("\\", "/").rsplit("/", 1)[-1]
                stem, separator, extension = filename.rpartition(".")
                if not separator or extension.lower() not in {
                    "png",
                    "jpg",
                    "jpeg",
                    "webp",
                }:
                    raise ContentError("reference_image_extension_invalid")
                safe_stem = re.sub(r"[^\w.-]+", "_", stem)[:120].strip(".") or "image"
                suffix = f"{safe_stem}.{extension.lower()}"
                self.paths[task_id] = (
                    f"{ref['dataset_id']}/{binding['item_id']}/{task_id}/{suffix}"
                )

    async def authorize(self):
        job = await check_cancelled(self.db, self.job_id)
        if str(job.user_id) != self.proof["actor_id"]:
            raise ContentError("export_scope_changed")
        actor = await authorize_export_job(
            self.db,
            project_id=self.project_id,
            batch_id=self.batch_id,
            task_ids=self.selected_ids,
            job_uuid=self.job_id,
            allow_reference=True,
        )
        if (
            await key_authority(self.db, actor, self.proof["api_key_id"], "tasks:read")
            != self.proof["key_proof"]
        ):
            raise ContentError("api_key_authority_revoked", 403)
        return actor

    async def _authorize_task(self, db, task, actor):
        await self.authorize()
        binding = self.bindings.get(task.id)
        if binding is None or _task_binding(task) != {
            k: v for k, v in binding.items() if k != "reference"
        }:
            raise ContentError("export_scope_changed")
        if await _reference_binding(db, task) != binding.get("reference"):
            raise ContentError("content_binding_changed")

    async def validate(self, *, final=False):
        actor = await self.authorize()
        if final:
            # Short publication transaction only; no original I/O under these locks.
            from app.services.project_write_guard import (
                lock_actor_project_authority,
                lock_actor_scope,
            )

            await lock_actor_project_authority(self.db, actor.id, self.project_id)
            await lock_actor_scope(self.db, actor.id, self.project_id)
            key_id = self.proof["api_key_id"]
            if key_id:
                await self.db.execute(
                    select(ApiKey.id)
                    .where(ApiKey.id == uuid.UUID(key_id))
                    .with_for_update(read=True, nowait=True)
                )
            await lock_task_scope(self.db, self.task_ids)
            await lock_reference_bindings(
                self.db, list(self.bindings.values()), self.node_id
            )
            await self.authorize()
        tasks = list(
            (
                await self.db.scalars(
                    task_query(self.project_id, self.batch_id, self.task_ids)
                )
            ).all()
        )
        if {task.id for task in tasks} != set(self.task_ids):
            raise ContentError("export_scope_changed")
        for task in tasks:
            binding = self.bindings[task.id]
            if _task_binding(task) != {
                k: v for k, v in binding.items() if k != "reference"
            }:
                raise ContentError("export_scope_changed")
            if "reference" in binding:
                context = await reference_context(
                    self.db,
                    task.dataset_item_id,
                    actor,
                    task_id=task.id,
                    node_id=self.node_id,
                    task_authorizer=self._authorize_task,
                )
                if task.id in self.contexts and context != self.contexts[task.id]:
                    raise ContentError("content_binding_changed")
                self.contexts[task.id] = context

    def archive_path(self, task_id):
        return f"images/{self.paths[task_id]}" if task_id in self.paths else None

    async def snapshot(self, task):
        actor = await self.authorize()
        binding = self.bindings[task.id]
        if "reference" not in binding or task.id not in self.contexts:
            raise ContentError("export_scope_changed")
        snapshot = await reference_snapshot(
            self.db,
            task.dataset_item_id,
            actor,
            task_id=task.id,
            expected_sha256=binding["reference"]["sha256"],
            api_key_id=self.proof["api_key_id"],
            node_id=self.node_id,
            task_authorizer=self._authorize_task,
        )
        try:
            if (snapshot.width, snapshot.height) != (
                binding["reference"]["width"],
                binding["reference"]["height"],
            ):
                raise ContentError("image_size_mismatch")
            await self._authorize_task(self.db, task, actor)
            return snapshot
        except BaseException:
            snapshot.close()
            raise
