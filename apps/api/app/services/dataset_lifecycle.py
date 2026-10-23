"""Reversible dataset state with actor-bound review and independent write fences."""

from datetime import datetime, timedelta, timezone
import hashlib
import json

from fastapi import HTTPException
from jose import ExpiredSignatureError, JWTError, jwt
from sqlalchemy import Text, case, cast, func, literal, select, union
from sqlalchemy.dialects.postgresql import JSONB, aggregate_order_by
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db.enums import UserRole
from app.db.models.annotation import Annotation
from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.dataset_source import (
    DatasetRegistrationRun,
    DatasetScanRun,
    DatasetSource,
    DatasetSourceSyncSchedule,
)
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.user import User
from app.services.audit import AuditService

PREVIEW_TTL = timedelta(minutes=5)
AUDIENCE = "dataset-lifecycle-change"
ACTIVE = ("pending", "running")


def state(dataset):
    return "archived" if dataset.archived_at is not None else "active"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


async def _dataset(db, dataset_id, *, lock=False):
    stmt = (
        select(Dataset)
        .where(Dataset.id == dataset_id)
        .execution_options(populate_existing=True)
    )
    if not lock:
        dataset = await db.scalar(stmt)
    else:
        try:
            async with db.begin_nested():
                dataset = await db.scalar(
                    stmt.with_for_update(key_share=True, nowait=True)
                )
        except DBAPIError as exc:
            code = getattr(exc.orig, "sqlstate", None) or getattr(
                exc.orig, "pgcode", None
            )
            if code != "55P03":
                raise
            raise HTTPException(409, detail={"code": "dataset_busy"}) from None
    if dataset is None:
        raise HTTPException(404, "Dataset not found")
    return dataset


async def require_writable(db, dataset_id, *, expected_revision=None, lock=False):
    """Protect dataset writes without changing Task or frozen-content read policy."""
    dataset = await _dataset(db, dataset_id, lock=lock)
    if dataset.archived_at is not None:
        raise HTTPException(409, detail={"code": "dataset_archived"})
    if (
        expected_revision is not None
        and dataset.lifecycle_revision != expected_revision
    ):
        raise HTTPException(409, detail={"code": "dataset_lifecycle_changed"})
    return dataset


async def lock_actor(db, actor, api_key_id):
    from app.services.dataset_content import key_authority

    proof = await key_authority(db, actor, api_key_id, "datasets:write")
    actor = await db.scalar(
        select(User)
        .where(User.id == actor.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        actor is None
        or not actor.is_active
        or actor.role not in (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
    ):
        raise HTTPException(403, detail={"code": "access_revoked"})
    if api_key_id is not None:
        await db.scalar(
            select(ApiKey)
            .where(ApiKey.id == api_key_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    if await key_authority(db, actor, api_key_id, "datasets:write") != proof:
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    return actor, proof


def dependencies(dataset_id):
    """Count and fingerprint stored relationships in one database statement."""
    items = select(DatasetItem.id).where(DatasetItem.dataset_id == dataset_id)
    sources = select(DatasetSource.id).where(DatasetSource.dataset_id == dataset_id)
    task_ids = union(
        select(Task.id).where(Task.dataset_item_id.in_(items)),
        select(TaskDatasetItemLink.task_id).where(
            TaskDatasetItemLink.dataset_item_id.in_(items)
        ),
    )
    projects = union(
        select(ProjectDataset.project_id).where(
            ProjectDataset.dataset_id == dataset_id
        ),
        select(Task.project_id).where(Task.id.in_(task_ids)),
    )
    jobs = (
        AsyncJob.payload["dataset_id"].astext == str(dataset_id)
    ) & AsyncJob.status.in_(ACTIVE)
    members = (
        func.jsonb_array_elements(
            case(
                (
                    func.jsonb_typeof(TrainingDatasetVersion.snapshot["samples"])
                    == "array",
                    TrainingDatasetVersion.snapshot["samples"],
                ),
                else_=literal([], type_=JSONB),
            )
        )
        .table_valued("value")
        .alias("lifecycle_member")
    )
    lineage = (
        select(1)
        .select_from(members)
        .where(cast(members.c.value, JSONB)["dataset_id"].astext == str(dataset_id))
        .correlate(TrainingDatasetVersion)
        .exists()
    )

    def rows(model, *where):
        return select(func.count()).select_from(model).where(*where).scalar_subquery()

    def count(statement):
        return select(func.count()).select_from(statement.subquery()).scalar_subquery()

    def sql_digest(value):
        return func.encode(func.sha256(func.convert_to(value, "UTF8")), "hex")

    def footprint(model, fields, *where):
        values = [getattr(model, field) for field in fields.split()]
        row_digest = sql_digest(cast(func.jsonb_build_array(*values), Text))
        return (
            select(
                sql_digest(
                    func.coalesce(
                        func.string_agg(
                            row_digest, aggregate_order_by(literal(""), *values)
                        ),
                        "",
                    )
                )
            )
            .select_from(model)
            .where(*where)
            .scalar_subquery()
        )

    counts = {
        "items": count(items),
        "sources": count(sources),
        "projects": count(projects),
        "tasks": count(task_ids),
        "annotations": rows(Annotation, Annotation.task_id.in_(task_ids)),
        "active_scans": rows(
            DatasetScanRun,
            DatasetScanRun.source_id.in_(sources),
            DatasetScanRun.status.in_(ACTIVE),
        ),
        "active_registrations": rows(
            DatasetRegistrationRun,
            DatasetRegistrationRun.source_id.in_(sources),
            DatasetRegistrationRun.status.in_(ACTIVE),
        ),
        "enabled_schedules": rows(
            DatasetSourceSyncSchedule,
            DatasetSourceSyncSchedule.source_id.in_(sources),
            DatasetSourceSyncSchedule.enabled,
        ),
        "pending_jobs": rows(AsyncJob, jobs),
        "ready_lineage_versions": rows(
            TrainingDatasetVersion, lineage, TrainingDatasetVersion.status == "ready"
        ),
        "unfinished_lineage_versions": rows(
            TrainingDatasetVersion, lineage, TrainingDatasetVersion.status.in_(ACTIVE)
        ),
    }
    fingerprints = [
        footprint(DatasetItem, "id dataset_id", DatasetItem.dataset_id == dataset_id),
        footprint(
            DatasetSource,
            "id location_id config_revision policy_revision enabled unregistered_at generation",
            DatasetSource.dataset_id == dataset_id,
        ),
        footprint(
            ProjectDataset,
            "id dataset_id project_id",
            ProjectDataset.dataset_id == dataset_id,
        ),
        footprint(Task, "id dataset_item_id project_id", Task.id.in_(task_ids)),
        footprint(
            TaskDatasetItemLink,
            "id task_id dataset_item_id role",
            TaskDatasetItemLink.dataset_item_id.in_(items),
        ),
        footprint(Annotation, "id task_id", Annotation.task_id.in_(task_ids)),
        footprint(
            DatasetScanRun,
            "id source_id status generation",
            DatasetScanRun.source_id.in_(sources),
            DatasetScanRun.status.in_(ACTIVE),
        ),
        footprint(
            DatasetRegistrationRun,
            "id source_id status generation",
            DatasetRegistrationRun.source_id.in_(sources),
            DatasetRegistrationRun.status.in_(ACTIVE),
        ),
        footprint(
            DatasetSourceSyncSchedule,
            "source_id revision enabled",
            DatasetSourceSyncSchedule.source_id.in_(sources),
            DatasetSourceSyncSchedule.enabled,
        ),
        footprint(AsyncJob, "id kind status", jobs),
        footprint(
            TrainingDatasetVersion,
            "id status snapshot_digest",
            lineage,
            TrainingDatasetVersion.status.in_((*ACTIVE, "ready")),
        ),
    ]
    return select(
        func.statement_timestamp().label("measured_at"),
        *(value.label(name) for name, value in counts.items()),
        sql_digest(cast(func.jsonb_build_array(*fingerprints), Text)).label(
            "fingerprint"
        ),
    )


async def review(db, dataset_id, payload, actor, *, api_key_id=None):
    from app.services.dataset_content import key_authority

    actor, proof = await lock_actor(db, actor, api_key_id)
    dataset = await _dataset(db, dataset_id, lock=True)
    if actor.role != UserRole.SUPER_ADMIN and dataset.created_by != actor.id:
        raise HTTPException(404, "Dataset not found")
    if dataset.lifecycle_revision != payload.expected_revision:
        raise HTTPException(409, detail={"code": "dataset_lifecycle_changed"})
    if (payload.operation == "archive") != (dataset.archived_at is None):
        raise HTTPException(409, detail={"code": "dataset_lifecycle_preview_changed"})
    binding = digest(
        {
            "actor": actor.id,
            "key": api_key_id,
            "authority": proof,
            "dataset": dataset.id,
            "created_by": dataset.created_by,
            "name": dataset.name,
            "description": dataset.description,
            "metadata": dataset.metadata_,
            "data_type": dataset.data_type,
            "is_temporal": dataset.is_temporal,
            "archived_at": dataset.archived_at,
            "archived_by": dataset.archived_by,
            "revision": dataset.lifecycle_revision,
            "operation": payload.operation,
        }
    )
    snapshot = dict((await db.execute(dependencies(dataset.id))).mappings().one())
    measured_at = snapshot.pop("measured_at")
    fingerprint = snapshot.pop("fingerprint")
    counts = {key: int(value) for key, value in snapshot.items()}
    current = await db.get(User, actor.id, populate_existing=True)
    if (
        not current
        or not current.is_active
        or current.role not in (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
    ):
        raise HTTPException(403, detail={"code": "access_revoked"})
    if current.role != UserRole.SUPER_ADMIN and dataset.created_by != current.id:
        raise HTTPException(404, "Dataset not found")
    if await key_authority(db, current, api_key_id, "datasets:write") != proof:
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    return (
        dataset,
        current,
        {
            "dataset_id": dataset.id,
            "lifecycle_revision": dataset.lifecycle_revision,
            "state": state(dataset),
            "operation": payload.operation,
            "measured_at": measured_at,
            "counts": counts,
            "frozen_coverage": "recorded_dataset_membership_only",
            "binding": binding,
            "dependencies": digest({"fingerprint": fingerprint, "counts": counts}),
        },
    )


async def preview(db, dataset_id, payload, actor, *, api_key_id=None):
    _, _, reviewed = await review(db, dataset_id, payload, actor, api_key_id=api_key_id)
    binding, dependencies_digest = reviewed.pop("binding"), reviewed.pop("dependencies")
    expires_at = reviewed["measured_at"] + PREVIEW_TTL
    token = jwt.encode(
        {
            "aud": AUDIENCE,
            "exp": expires_at,
            "binding": binding,
            "dependencies": dependencies_digest,
            "measured_at": reviewed["measured_at"].isoformat(),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {**reviewed, "expires_at": expires_at, "impact_token": token}


def decode_token(token):
    try:
        data = jwt.decode(
            token, settings.secret_key, algorithms=["HS256"], audience=AUDIENCE
        )
        if not all(
            key in data for key in ("exp", "binding", "dependencies", "measured_at")
        ):
            raise ValueError()
        if data["exp"] <= datetime.now(timezone.utc).timestamp():
            raise ExpiredSignatureError()
        return data
    except ExpiredSignatureError:
        raise HTTPException(
            409, detail={"code": "dataset_lifecycle_preview_expired"}
        ) from None
    except (JWTError, ValueError, TypeError):
        raise HTTPException(
            409, detail={"code": "dataset_lifecycle_preview_invalid"}
        ) from None


async def apply(db, dataset_id, payload, actor, *, api_key_id=None, request=None):
    token = decode_token(payload.impact_token)
    dataset, actor, reviewed = await review(
        db, dataset_id, payload, actor, api_key_id=api_key_id
    )
    if any(token[key] != reviewed[key] for key in ("binding", "dependencies")):
        raise HTTPException(409, detail={"code": "dataset_lifecycle_preview_changed"})
    decode_token(payload.impact_token)
    previous_revision = dataset.lifecycle_revision
    dataset.archived_at = (
        reviewed["measured_at"] if payload.operation == "archive" else None
    )
    dataset.archived_by = actor.id if payload.operation == "archive" else None
    dataset.lifecycle_revision += 1
    await db.flush()
    await db.refresh(dataset, attribute_names=["updated_at"])
    await AuditService.log(
        db,
        actor=actor,
        action=f"dataset.{payload.operation}",
        target_type="dataset",
        target_id=str(dataset.id),
        request=request,
        status_code=200,
        detail={
            "previous_revision": previous_revision,
            "lifecycle_revision": dataset.lifecycle_revision,
            "state": state(dataset),
            "reviewed_at": token["measured_at"],
            "preview_expires_at": datetime.fromtimestamp(
                token["exp"], timezone.utc
            ).isoformat(),
            "rechecked_at": reviewed["measured_at"].isoformat(),
            "counts": reviewed["counts"],
            "frozen_coverage": reviewed["frozen_coverage"],
        },
    )
    return {
        "dataset_id": dataset.id,
        "lifecycle_revision": dataset.lifecycle_revision,
        "state": state(dataset),
        "archived_at": dataset.archived_at,
        "archived_by": dataset.archived_by,
    }
