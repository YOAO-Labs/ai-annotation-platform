"""Preserve RLE generations, unfinished writes and publication guards offline."""

from datetime import timedelta
import gzip
import hashlib
import uuid


CASES = (
    ("pending-empty", "pending", "identity", 1),
    ("pending-write", "pending", "identity", 2),
    ("ready-json", "ready", "identity", 3),
    ("ready-gzip", "ready", "gzip", 4),
    ("retired-generation", "retired", "identity", 5),
    ("ready-generation", "ready", "identity", 5),
    ("deleted-task", "ready", "identity", 6),
)
GUARDED_CASES = ("pending-write", "ready-json")
HISTORICAL_GZIP_MTIME = 946684800


def identifier(kind, case=""):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aap:recovery-raster-object:{kind}:{case}")


def rle_for(index):
    return {
        "encoding": "coco_rle",
        "size": [32, 48],
        "counts": [index, 1, 1535 - index],
    }


def encoded(case):
    from app.services.raster_mask_storage import (
        build_rle_gzip_reference,
        build_rle_reference,
        canonical_rle_bytes,
    )

    _, _, encoding, index = next(item for item in CASES if item[0] == case)
    rle = rle_for(index)
    canonical = canonical_rle_bytes(rle)
    if encoding == "gzip":
        return build_rle_gzip_reference(rle), gzip.compress(
            canonical, compresslevel=6, mtime=HISTORICAL_GZIP_MTIME
        )
    return build_rle_reference(rle), canonical


def legacy():
    from app.services.raster_mask_storage import (
        build_rle_reference,
        canonical_rle_bytes,
    )

    rle = rle_for(8)
    return build_rle_reference(rle), canonical_rle_bytes(rle)


async def seed_raster_objects(db, *, actor, project, storage, now):
    from sqlalchemy import select
    from app.db.models.annotation import Annotation
    from app.db.models.raster_mask_object import RasterMaskObject, RasterMaskPublication
    from app.db.models.raster_mask_upload import RasterMaskUpload
    from app.db.models.storage_maintenance import (
        StorageMaintenanceGate,
        StorageOperation,
    )
    from app.db.models.task import Task
    from app.services.dataset_assets import LEGACY_LOCATION_ID
    from app.services import raster_object_storage

    gate = await db.get(StorageMaintenanceGate, LEGACY_LOCATION_ID)
    assert gate is not None and gate.revision > 0
    owners = {}
    for case, state, _, _ in CASES:
        reference, raw = encoded(case)
        row = RasterMaskObject(
            id=identifier("owner", case),
            **raster_object_storage.content(reference, raw),
            state=state,
            created_at=now - timedelta(days=3),
            published_at=now - timedelta(days=2) if state != "pending" else None,
            last_used_at=now - timedelta(days=2) if state != "pending" else None,
            retired_at=now - timedelta(days=1) if state == "retired" else None,
            last_attempted_at=now - timedelta(hours=1),
        )
        row.storage_ref = await raster_object_storage.allocate(db, row)
        db.add(row)
        await db.flush()
        if case != "pending-empty":
            owner = raster_object_storage.resolve(row)
            storage.client.put_object(
                Bucket=owner.bucket,
                Key=owner.object_key,
                Body=raw,
                ContentType="application/gzip"
                if row.storage_encoding == "gzip"
                else "application/json",
            )
        owners[case] = row
    for case in GUARDED_CASES:
        db.add(
            RasterMaskPublication(
                id=identifier("publication", case),
                object_id=owners[case].id,
                gate_revision=gate.revision,
                created_at=now - timedelta(hours=47),
            )
        )
    db.add(
        StorageOperation(
            id=identifier("operation", "pending-write"),
            location_id=LEGACY_LOCATION_ID,
            revision=gate.revision,
            purpose="raster_object_write_" + owners["pending-write"].id.hex,
            node_id="recovery:unknown-rle-writer",
            started_at=now - timedelta(hours=47),
        )
    )

    # Frozen training snapshots already exist. Only current business rows change.
    task = await db.scalar(
        select(Task).where(Task.project_id == project.id).order_by(Task.id)
    )
    assert task is not None
    legacy_ref, legacy_raw = legacy()
    for case, reference in (
        ("current", raster_object_storage.reference(owners["ready-json"])),
        ("legacy", legacy_ref),
    ):
        db.add(
            Annotation(
                id=identifier("annotation", case),
                task_id=task.id,
                project_id=project.id,
                user_id=actor.id,
                annotation_type="raster_mask",
                tool_unit_id="region",
                class_name="car",
                geometry={"type": "raster_mask", "mask": reference},
                is_active=True,
                was_cancelled=False,
                version=1,
            )
        )
    storage.client.put_object(
        Bucket=storage.bucket,
        Key=legacy_ref["object_key"],
        Body=legacy_raw,
        ContentType="application/json",
    )

    detached = Task(
        id=identifier("task", "deleted"),
        project_id=project.id,
        display_id="T-REC-RLE-DELETED",
        file_name="deleted.png",
        file_path="recovery/deleted-rle-source.png",
        file_type="image",
        status="pending",
    )
    db.add(detached)
    await db.flush()
    db.add(
        RasterMaskUpload(
            id=identifier("upload", "deleted"),
            task_id=detached.id,
            object_key=owners["deleted-task"].logical_key,
            created_at=now - timedelta(days=2),
            linked_at=now - timedelta(days=1),
        )
    )
    detached_annotation = Annotation(
        id=identifier("annotation", "deleted"),
        task_id=detached.id,
        project_id=project.id,
        user_id=actor.id,
        annotation_type="raster_mask",
        tool_unit_id="region",
        class_name="car",
        geometry={
            "type": "raster_mask",
            "mask": raster_object_storage.reference(owners["deleted-task"]),
        },
    )
    db.add(detached_annotation)
    await db.flush()
    # Annotation task FKs do not cascade; upload reservations do.
    await db.delete(detached_annotation)
    await db.flush()
    await db.delete(detached)
    await db.flush()


async def verify_raster_objects(db, *, storage):
    from sqlalchemy import select
    from app.db.models.annotation import Annotation
    from app.db.models.raster_mask_object import RasterMaskObject, RasterMaskPublication
    from app.db.models.raster_mask_upload import RasterMaskUpload
    from app.db.models.storage_maintenance import StorageOperation
    from app.db.models.task import Task
    from app.services.dataset_assets import LEGACY_LOCATION_ID
    from app.services import raster_object_storage

    owners = {}
    files = 0
    for case, state, encoding, index in CASES:
        row = await db.get(RasterMaskObject, identifier("owner", case))
        reference, raw = encoded(case)
        assert (
            row is not None and row.state == state and row.storage_encoding == encoding
        )
        assert raster_object_storage.reference(row) == reference
        assert row.stored_sha256 == hashlib.sha256(raw).hexdigest()
        assert row.stored_size_bytes == len(raw)
        assert row.last_attempted_at - row.created_at == timedelta(hours=71)
        if state == "pending":
            assert row.published_at is None and row.last_used_at is None
        else:
            assert row.published_at - row.created_at == timedelta(days=1)
            assert row.last_used_at == row.published_at
        if state == "retired":
            assert row.retired_at - row.created_at == timedelta(days=2)
        else:
            assert row.retired_at is None
        owner = raster_object_storage.resolve(row)
        assert owner.bucket == storage.bucket
        assert owner.object_key == raster_object_storage.key_for(row.id, encoding)
        data = raster_object_storage.read(
            storage.client, row, missing_ok=case == "pending-empty"
        )
        if case == "pending-empty":
            assert data is None
        else:
            assert data == rle_for(index)
            files += 1
        owners[case] = row
    before, after = owners["retired-generation"], owners["ready-generation"]
    assert before.logical_key == after.logical_key
    assert raster_object_storage.reference(before) == raster_object_storage.reference(
        after
    )
    assert (
        before.id != after.id
        and before.storage_ref["object_key"] != after.storage_ref["object_key"]
    )
    guards = list(
        await db.scalars(
            select(RasterMaskPublication).where(
                RasterMaskPublication.id.in_(
                    [identifier("publication", case) for case in GUARDED_CASES]
                )
            )
        )
    )
    assert {row.object_id for row in guards} == {
        owners[case].id for case in GUARDED_CASES
    }
    assert all(
        row.created_at - owners["pending-write"].created_at == timedelta(hours=25)
        for row in guards
    )
    assert len({row.gate_revision for row in guards}) == 1
    operation = await db.get(StorageOperation, identifier("operation", "pending-write"))
    assert operation is not None and operation.location_id == LEGACY_LOCATION_ID
    assert operation.revision == guards[0].gate_revision
    assert operation.purpose == "raster_object_write_" + owners["pending-write"].id.hex
    assert operation.node_id == "recovery:unknown-rle-writer"
    assert operation.started_at == guards[0].created_at
    assert await db.get(Task, identifier("task", "deleted")) is None
    assert await db.get(RasterMaskUpload, identifier("upload", "deleted")) is None
    assert await db.get(Annotation, identifier("annotation", "deleted")) is None
    current = await db.get(Annotation, identifier("annotation", "current"))
    assert current is not None and current.is_active
    assert current.geometry == {
        "type": "raster_mask",
        "mask": raster_object_storage.reference(owners["ready-json"]),
    }
    historical = await db.get(Annotation, identifier("annotation", "legacy"))
    legacy_ref, legacy_raw = legacy()
    assert historical is not None and historical.geometry == {
        "type": "raster_mask",
        "mask": legacy_ref,
    }
    assert (
        await db.scalar(
            select(RasterMaskObject.id).where(
                RasterMaskObject.logical_key == legacy_ref["object_key"]
            )
        )
        is None
    )
    response = storage.client.get_object(
        Bucket=storage.bucket, Key=legacy_ref["object_key"]
    )
    try:
        assert response["Body"].read(len(legacy_raw) + 1) == legacy_raw
        assert response["Body"].read(1) == b""
    finally:
        response["Body"].close()
    return {
        "pinned_raster_objects": len(CASES),
        "verified_raster_object_files": files,
        "raster_pending_without_bytes": 1,
        "raster_publication_guards": len(guards),
        "raster_unknown_write_permits": 1,
        "raster_same_logical_generations": 2,
        "raster_deleted_task_owners": 1,
        "raster_current_annotation_refs": 1,
        "unowned_legacy_raster_objects": 1,
    }
