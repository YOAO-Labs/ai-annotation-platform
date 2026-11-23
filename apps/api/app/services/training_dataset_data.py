"""Borrow the caller's transaction for the fixed Learning dataset ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    String,
    Uuid,
    column,
    func,
    literal,
    select,
    table,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.services.training_dataset_bridge import TrainingDatasetError
from app.db.models.training_dataset import TrainingDatasetVersion


@dataclass(frozen=True)
class NewVersion:
    project_id: UUID
    creator_id: UUID
    parent_id: UUID | None
    request_id: UUID
    request_digest: str
    name: str
    snapshot: dict
    snapshot_digest: str
    preparation_report: dict


@dataclass
class DatasetVersion:
    id: UUID
    project_id: UUID
    creator_id: UUID | None
    parent_id: UUID | None
    async_job_id: UUID | None
    attempt_id: UUID
    name: str
    status: str
    snapshot_digest: str
    snapshot: dict
    manifest: dict = field(default_factory=dict)
    preparation_report: dict | None = None
    package_key: str | None = None
    package_sha256: str | None = None
    size_bytes: int | None = None
    storage_ref: dict | None = None
    progress_pct: int = 0
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True)
class DatasetRequest:
    id: UUID
    request_digest: str


@dataclass(frozen=True)
class DatasetJobVersion:
    id: UUID
    project_id: UUID
    attempt_id: UUID
    status: str


@dataclass(frozen=True)
class DatasetMembership:
    task_dataset_id: UUID
    ordinal: int


_METADATA = (
    column("id", Uuid()),
    column("project_id", Uuid()),
    column("creator_id", Uuid()),
    column("parent_id", Uuid()),
    column("async_job_id", Uuid()),
    column("name", String),
    column("status", String),
    column("snapshot_digest", String),
    column("package_sha256", String),
    column("size_bytes", BigInteger),
    column("progress_pct", Integer),
    column("error_code", String),
    column("error_message", String),
    column("created_at", DateTime(timezone=True)),
    column("completed_at", DateTime(timezone=True)),
    column("task_dataset_id", Uuid()),
    column("ordinal", BigInteger),
    column("recognized_task", String),
    column("raw_task", String),
    column("task", String),
    column("media_type", String),
    column("format", String),
    column("source_media_type", String),
    column("class_count", Integer),
    column("annotation_scope", JSONB),
    column("summary", JSONB),
    column("contract_profile", JSONB),
)

dataset_lineage_row = table(
    "training_dataset_metadata", *_METADATA, schema="yoao_learning_v1"
)
_SUMMARY = (
    *(
        c
        for c in dataset_lineage_row.c
        if c.key not in {"raw_task", "recognized_task", "ordinal"}
    ),
    dataset_lineage_row.c.ordinal.label("version_ordinal"),
)

_VERSION_FACTS = (
    "id",
    "project_id",
    "creator_id",
    "parent_id",
    "async_job_id",
    "attempt_id",
    "name",
    "status",
    "snapshot_digest",
    "package_key",
    "package_sha256",
    "size_bytes",
    "storage_ref",
    "snapshot",
    "manifest",
    "progress_pct",
    "error_code",
    "error_message",
    "created_at",
    "completed_at",
)


def _associated_versions():
    return (
        column("id", Uuid()),
        column("project_id", Uuid()),
        column("name", String),
        column("status", String),
        column("created_at", DateTime(timezone=True)),
        column("snapshot_digest", String),
    )


def dataset_asset_versions(dataset_id: UUID, *, workbench: bool = False):
    return func.yoao_learning_v1.training_dataset_asset_versions(
        dataset_id, workbench
    ).table_valued(*_associated_versions())


def dataset_location_versions(location_id: UUID):
    return func.yoao_learning_v1.training_dataset_location_versions(
        location_id
    ).table_valued(*_associated_versions())


async def _version(db: AsyncSession, source) -> DatasetVersion | None:
    if db.autoflush:
        await db.flush()
    row = (await db.execute(select(source))).mappings().one_or_none()
    return DatasetVersion(**row) if row else None


async def read_version_facts(
    db: AsyncSession,
    version_id: UUID,
    *,
    project_id: UUID | None = None,
    lock: bool = False,
) -> DatasetVersion | None:
    if version_id is None:
        return None
    return await _version(
        db,
        func.yoao_learning_v1.training_dataset_facts(
            version_id, project_id, lock
        ).table_valued(*_VERSION_FACTS),
    )


async def read_version(
    db: AsyncSession,
    version_id: UUID,
    *,
    project_id: UUID | None = None,
    lock: bool = False,
) -> DatasetVersion | None:
    if version_id is None:
        return None
    return await _version(
        db,
        func.yoao_learning_v1.training_dataset_read(
            version_id, project_id, lock
        ).table_valued(*_VERSION_FACTS, "preparation_report"),
    )


async def hold_version(db: AsyncSession, version_id: UUID, project_id: UUID) -> bool:
    return await db.scalar(
        select(func.yoao_learning_v1.training_dataset_hold(version_id, project_id))
    )


async def read_request(
    db: AsyncSession, project_id: UUID, request_id: UUID, *, lock: bool = False
) -> DatasetRequest | None:
    source = func.yoao_learning_v1.training_dataset_request(
        project_id, request_id, lock
    ).table_valued("id", "request_digest")
    row = (await db.execute(select(source))).mappings().one_or_none()
    return DatasetRequest(**row) if row else None


async def lock_job_version(db: AsyncSession, job_id: UUID) -> DatasetJobVersion | None:
    source = func.yoao_learning_v1.training_dataset_job_lock(job_id).table_valued(
        "id", "project_id", "attempt_id", "status"
    )
    row = (await db.execute(select(source))).mappings().one_or_none()
    return DatasetJobVersion(**row) if row else None


async def insert_version(db: AsyncSession, new: NewVersion) -> DatasetVersion:
    identifier, attempt_id = uuid4(), uuid4()
    created_at = await db.scalar(
        select(
            func.yoao_learning_v1.training_dataset_create(
                identifier,
                new.project_id,
                new.creator_id,
                new.parent_id,
                new.request_id,
                new.request_digest,
                attempt_id,
                new.name,
                literal(new.snapshot, JSONB),
                new.snapshot_digest,
                literal(new.preparation_report, JSONB),
            )
        )
    )
    if created_at is None:
        raise TrainingDatasetError(
            "preflight_changed", "数据已变化，请重新预检", status_code=409
        )
    return DatasetVersion(
        id=identifier,
        project_id=new.project_id,
        creator_id=new.creator_id,
        parent_id=new.parent_id,
        async_job_id=None,
        attempt_id=attempt_id,
        name=new.name,
        status="pending",
        snapshot_digest=new.snapshot_digest,
        snapshot=new.snapshot,
        preparation_report=new.preparation_report,
        created_at=created_at,
    )


async def ensure_membership(
    db: AsyncSession, project_id: UUID, version_id: UUID
) -> DatasetMembership:
    source = func.yoao_learning_v1.training_dataset_associate(
        project_id, version_id
    ).table_valued("task_dataset_id", "ordinal", "failure")
    row = (await db.execute(select(source))).one()
    if row.failure:
        status, message = {
            "unknown_data_contract": (
                422,
                "冻结数据契约未被识别，不能分配任务版本序号",
            ),
            "task_dataset_membership_mismatch": (
                409,
                "任务版本关联与冻结数据身份不一致",
            ),
            "task_dataset_high_water_invalid": (
                409,
                "任务数据集序号高水位无效，请先核查管理元数据",
            ),
        }[row.failure]
        raise TrainingDatasetError(row.failure, message, status_code=status)
    return DatasetMembership(row.task_dataset_id, row.ordinal)


def stream_rows(project_id: UUID, stream_id: UUID | None = None):
    return func.yoao_learning_v1.training_dataset_stream(
        project_id, stream_id
    ).table_valued("id", "task")


def version_summaries():
    return select(*_SUMMARY)


async def read_summary(
    db: AsyncSession,
    project_id: UUID,
    version_id: UUID,
    *,
    include_manifest: bool = False,
) -> dict | None:
    query = version_summaries().where(
        dataset_lineage_row.c.project_id == project_id,
        dataset_lineage_row.c.id == version_id,
    )
    if include_manifest:
        query = query.add_columns(
            func.yoao_learning_v1.training_dataset_manifest(
                project_id, version_id
            ).label("manifest")
        )
    row = (await db.execute(query)).mappings().one_or_none()
    return dict(row) if row is not None else None


async def read_manifest(
    db: AsyncSession, project_id: UUID, version_id: UUID
) -> dict | None:
    return await db.scalar(
        select(func.yoao_learning_v1.training_dataset_manifest(project_id, version_id))
    )


async def read_sample_evidence(
    db: AsyncSession, project_id: UUID, version_id: UUID, task_id: UUID
) -> dict | None:
    return await db.scalar(
        select(
            func.yoao_learning_v1.training_dataset_sample_evidence(
                project_id, version_id, task_id
            )
        )
    )


def _acknowledge_version(version: DatasetVersion | TrainingDatasetVersion, **fields):
    for key, value in fields.items():
        if isinstance(version, DatasetVersion):
            setattr(version, key, value)
        else:
            set_committed_value(version, key, value)


async def set_storage(db: AsyncSession, version: DatasetVersion, ref: dict) -> None:
    changed = await db.scalar(
        select(
            func.yoao_learning_v1.training_dataset_set_storage(
                version.project_id, version.id, version.attempt_id, literal(ref, JSONB)
            )
        )
    )
    if not changed:
        raise TrainingDatasetError(
            "training_storage_identity_changed", "训练存储归属已变化", status_code=409
        )
    _acknowledge_version(version, storage_ref=ref)


async def queue_version(
    db: AsyncSession, version: DatasetVersion, job_id: UUID
) -> DatasetVersion:
    attempt_id = uuid4()
    changed = await db.scalar(
        select(
            func.yoao_learning_v1.training_dataset_queue(
                version.id, version.attempt_id, attempt_id, job_id
            )
        )
    )
    if not changed:
        raise TrainingDatasetError(
            "execution_superseded", "训练构建版本已变化", status_code=409
        )
    _acknowledge_version(
        version,
        async_job_id=job_id,
        attempt_id=attempt_id,
        status="pending",
        progress_pct=0,
        error_code=None,
        error_message=None,
        completed_at=None,
    )
    return version


async def start_version(db: AsyncSession, version: DatasetVersion) -> bool:
    return await db.scalar(
        select(
            func.yoao_learning_v1.training_dataset_start(
                version.id, version.async_job_id, version.attempt_id
            )
        )
    )


async def set_progress(
    db: AsyncSession, version: DatasetVersion, progress: int
) -> bool:
    return await db.scalar(
        select(
            func.yoao_learning_v1.training_dataset_progress(
                version.id, version.async_job_id, version.attempt_id, progress
            )
        )
    )


async def publish_version(
    db: AsyncSession,
    version: DatasetVersion,
    *,
    manifest: dict,
    package_key: str,
    package_sha256: str,
    size_bytes: int,
) -> bool:
    return await db.scalar(
        select(
            func.yoao_learning_v1.training_dataset_publish(
                version.id,
                version.async_job_id,
                version.attempt_id,
                version.snapshot_digest,
                literal(version.storage_ref, JSONB),
                literal(manifest, JSONB),
                package_key,
                package_sha256,
                size_bytes,
                datetime.now(timezone.utc),
            )
        )
    )


async def fail_version(
    db: AsyncSession,
    version: DatasetVersion | DatasetJobVersion,
    job_id: UUID,
    *,
    cancelled: bool,
    code: str | None,
    message: str | None,
) -> bool:
    return await db.scalar(
        select(
            func.yoao_learning_v1.training_dataset_fail(
                version.id,
                job_id,
                version.attempt_id,
                version.status,
                cancelled,
                code,
                message,
                datetime.now(timezone.utc),
            )
        )
    )


async def delete_version(db: AsyncSession, version: DatasetVersion) -> None:
    changed = await db.scalar(
        select(
            func.yoao_learning_v1.training_dataset_delete(
                version.project_id, version.id, version.attempt_id
            )
        )
    )
    if not changed:
        raise TrainingDatasetError(
            "execution_superseded", "训练构建版本已变化", status_code=409
        )
