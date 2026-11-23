"""Shared raster tests: controlled object bytes with real SQL transactions.

This support does not qualify S3 behavior or renderer behavior.
"""

import hashlib
import io
from types import SimpleNamespace
import uuid

from botocore.exceptions import ClientError
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from yoao_workspace.mask_configuration_values import MaskQCConfig
from app.db.models.raster_mask_object import (
    RasterMaskObject as Object,
    RasterMaskPublication as Publication,
)
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.services import (
    raster_mask_objects as owners,
    raster_object_storage as physical,
)
from app.services import storage_access
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.raster_mask_storage import build_rle_reference, canonical_rle_bytes
from app.services.storage import storage_service
from app.utils.raster_mask_rle import encode_coco_rle
from tests._owned_s3 import patch_owned_client


class Objects:
    def __init__(self):
        self.bucket = settings.minio_bucket
        self.client = SimpleNamespace(
            meta=SimpleNamespace(
                endpoint_url=f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
            ),
            head_object=self.head,
            get_object=self.get,
            put_object=self.put,
            delete_object=self.delete,
        )
        self.objects, self.reads, self.puts, self.deleted, self.bodies = (
            {},
            [],
            [],
            [],
            [],
        )
        self.fail_put = self.fail_delete = False
        self.before_put = None
        self.body_factory = io.BytesIO

    def head(self, *, Bucket, Key):  # noqa: N803
        try:
            data = self.objects[Bucket, Key]
        except KeyError:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject") from None
        return {
            "ContentLength": len(data),
            "ETag": '"' + hashlib.sha256(data).hexdigest() + '"',
        }

    def get(self, *, Bucket, Key, IfMatch, **kwargs):  # noqa: N803
        metadata = self.head(Bucket=Bucket, Key=Key)
        assert IfMatch == metadata["ETag"]
        self.reads.append((Bucket, Key))
        body = self.body_factory(self.objects[Bucket, Key])
        self.bodies.append(body)
        return {"Body": body, **metadata}

    def put(self, *, Bucket, Key, Body, IfNoneMatch, **kwargs):  # noqa: N803
        assert IfNoneMatch == "*"
        if self.before_put is not None:
            self.before_put(Bucket, Key)
        if self.fail_put:
            raise OSError("private candidate write failure")
        if (Bucket, Key) in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[Bucket, Key] = bytes(Body)
        self.puts.append((Bucket, Key))
        return {}

    def delete(self, *, Bucket, Key, **kwargs):  # noqa: N803
        if self.fail_delete:
            raise OSError("private candidate delete failure")
        self.objects.pop((Bucket, Key), None)
        self.deleted.append((Bucket, Key))
        return {}


class Harness:
    def __init__(self, engine, blobs):
        self.factory = async_sessionmaker(engine, expire_on_commit=False)
        self.blobs, self.keys, self.identifiers = blobs, set(), set()
        self.references = set()

    def content(self):
        counts = [int(value, 16) + 1 for value in uuid.uuid4().hex]
        rle = {"encoding": "coco_rle", "size": [1, sum(counts)], "counts": counts}
        reference, data = build_rle_reference(rle), canonical_rle_bytes(rle)
        self.keys.add(reference["object_key"])
        return rle, reference, data

    async def rows(self):
        async with self.factory() as db:
            rows = list(
                (
                    await db.scalars(
                        select(Object).where(Object.logical_key.in_(self.keys))
                    )
                ).all()
            )
            self.identifiers.update(row.id for row in rows)
            return rows

    async def guards(self):
        async with self.factory() as db:
            return list(
                (
                    await db.scalars(
                        select(Publication)
                        .join(Object)
                        .where(Object.logical_key.in_(self.keys))
                    )
                ).all()
            )

    async def operations(self):
        await self.rows()
        identifiers = self.identifiers
        purposes = [
            owners.purpose(operation, identifier)
            for identifier in identifiers
            for operation in ("read", "write", "delete")
        ]
        async with self.factory() as db:
            return list(
                (
                    await db.scalars(
                        select(StorageOperation).where(
                            StorageOperation.purpose.in_(purposes)
                        )
                    )
                ).all()
            )


@pytest.fixture
async def harness(test_engine, monkeypatch):
    blobs = Objects()
    patch_owned_client(monkeypatch, blobs.client)
    result = Harness(test_engine, blobs)
    monkeypatch.setattr(physical, "storage_service", blobs)

    async def referenced_keys(db):
        return set(result.references)

    async def is_referenced(db, key):
        return key in result.references

    monkeypatch.setattr(owners, "referenced_keys", referenced_keys)
    monkeypatch.setattr(owners, "is_referenced", is_referenced)
    async with result.factory() as db:
        await ensure_legacy_bindings(db)
        await db.commit()
        await storage_access.ensure(db, LEGACY_LOCATION_ID)
    try:
        yield result
    finally:
        for body in blobs.bodies:
            io.BytesIO.close(body)
        await result.rows()
        identifiers = result.identifiers
        purposes = [
            owners.purpose(operation, identifier)
            for identifier in identifiers
            for operation in ("read", "write", "delete")
        ]
        async with result.factory() as db:
            await db.execute(
                delete(StorageOperation).where(StorageOperation.purpose.in_(purposes))
            )
            await db.execute(
                delete(Publication).where(Publication.object_id.in_(identifiers))
            )
            await db.execute(delete(Object).where(Object.id.in_(identifiers)))
            gate = await db.get(StorageMaintenanceGate, LEGACY_LOCATION_ID)
            if gate is not None and gate.state != "active":
                gate.state = "active"
                gate.revision += 1
            await db.commit()
        blobs.objects.clear()


@pytest.fixture
def consumer_mask_storage(harness, monkeypatch, test_db_url):
    """Use the owned test transport for consumers with a captured default store."""
    monkeypatch.setattr(storage_service, "client", harness.blobs.client)
    monkeypatch.setattr(settings, "database_url", test_db_url)


async def _image_project_task(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    file_path: str,
    width: int = 3,
    height: int = 2,
) -> tuple[Project, Task]:
    suffix = uuid.uuid4().hex[:6]
    project = Project(
        display_id=f"P-RMP-{suffix}",
        name=f"Raster portability {suffix}",
        type_key="image-det",
        type_label="测试",
        owner_id=owner_id,
        status="in_progress",
        raster_mask_native_editing_enabled=True,
        tool_bindings={
            "region": {
                "enabled": True,
                "classes": [{"name": "car", "order": 0}],
            }
        },
    )
    dataset = Dataset(
        display_id=f"DS-RMP-{suffix}",
        name=f"Raster portability {suffix}",
        data_type="image",
        created_by=owner_id,
    )
    db.add_all([project, dataset])
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name=file_path.rsplit("/", 1)[-1],
        file_path=file_path,
        file_type="image",
        width=width,
        height=height,
    )
    db.add(item)
    await db.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-RMP-{suffix}",
        file_name=item.file_name,
        file_path=file_path,
        file_type="image",
        status="pending",
    )
    db.add(task)
    await db.flush()
    return project, task


def _rle(rows: list[str]) -> dict:
    height = len(rows)
    width = len(rows[0])
    pixels = [1 if value == "#" else 0 for row in rows for value in row]
    return encode_coco_rle(pixels, width, height)


async def _seed_mask(
    db,
    *,
    owner_id: uuid.UUID,
    config: MaskQCConfig | None = None,
    status: str = "in_progress",
    annotator_id: uuid.UUID | None = None,
) -> tuple[Project, Task, Annotation, dict]:
    suffix = uuid.uuid4().hex[:8]
    worker_id = annotator_id or owner_id
    project = Project(
        display_id=f"P-QC-{suffix}",
        name=f"Mask QC {suffix}",
        type_label="图像-分割",
        type_key="image-seg",
        owner_id=owner_id,
        mask_qc_config=(config or MaskQCConfig()).model_dump(mode="json"),
        tool_bindings={
            "region": {
                "enabled": True,
                "classes": [{"name": "object", "order": 0}],
                "attribute_schema": {"fields": []},
            }
        },
    )
    db.add(project)
    await db.flush()
    evidence: dict = {}
    if status == "review":
        evidence = {
            "annotation_contributor_ids": [str(worker_id)],
            "review_contributor_ids": [str(worker_id)],
            "review_submitter_id": worker_id,
            "review_round_id": uuid.uuid4(),
        }
    task = Task(
        project_id=project.id,
        display_id=f"T-QC-{suffix}",
        file_name="mask.png",
        file_path="/tmp/mask.png",
        file_type="image",
        status=status,
        assignee_id=worker_id,
        **evidence,
    )
    db.add(task)
    await db.flush()
    rle = _rle(
        [
            "........",
            "........",
            "..#.....",
            "........",
            "........",
            "........",
            "........",
            "........",
        ]
    )
    reference = build_rle_reference(rle)
    annotation = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=worker_id,
        source="manual",
        annotation_type="mask",
        tool_unit_id="region",
        class_name="object",
        geometry={"type": "raster_mask", "mask": reference},
        is_active=True,
        was_cancelled=False,
        version=1,
    )
    db.add(annotation)
    await db.flush()
    return project, task, annotation, rle
