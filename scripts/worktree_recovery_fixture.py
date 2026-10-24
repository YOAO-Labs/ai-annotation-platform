"""Synthetic application evidence for the isolated recovery verifier.

Run only as its scoped child process. No production backup or scheduling entry.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import uuid
import zipfile

from worktree_env import WorktreeError, atomic_json
from worktree_recovery_mapping import remap_storage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))
MAPPED_COLUMNS = {
    "storage_bindings": {"bucket"},
    "dataset_asset_locations": {"bucket"},
    "dataset_source_previews": {"bucket"},
    "original_cache_entries": {"bucket", "storage_identity"},
    "prediction_input_leases": {"bucket", "storage_identity"},
    "training_storage_deletions": {"bucket"},
    "managed_original_retirements": {"bucket", "storage_identity"},
    "task_originals": {"bucket", "storage_identity"},
    "dataset_auxiliary_originals": {"bucket", "storage_identity"},
}


def protected_row(table, row):
    """Exclude only permitted physical fields, retaining nested owner evidence."""
    result = {
        key: value
        for key, value in row.items()
        if key not in MAPPED_COLUMNS.get(table, set())
    }
    if (
        table
        in {
            "training_dataset_versions",
            "training_runs",
            "training_storage_deletions",
            "training_previews",
            "sample_evaluation_runs",
            "active_learning_rounds",
            "avatar_uploads",
            "export_outputs",
            "export_media_bundles",
            "bug_attachment_uploads",
            "audit_archive_outputs",
            "comment_attachment_uploads",
            "guide_asset_uploads",
            "raster_mask_objects",
        }
        and result.get("storage_ref") is not None
    ):
        result["storage_ref"] = {
            key: value
            for key, value in result["storage_ref"].items()
            if key not in {"bucket", "storage_identity"}
        }
    return result


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()


async def database_facts(db, *, protected=False):
    """Bounded, order-independent row evidence without exporting raw cells."""
    from sqlalchemy import text

    invalidated_export_indexes = set()
    if protected:
        from worktree_recovery_export import media_cache_ids

        # The full source fingerprint still contains every cache row. Offline
        # mapping deliberately removes only typed bundle-linked indexes: their
        # immutable ZIPs retain historical signed URLs which cannot be remapped.
        invalidated_export_indexes = await media_cache_ids(db)
    tables = list(
        await db.scalars(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
            )
        )
    )
    facts = {}
    for table in tables:
        # Names come from the verified database catalog, never user SQL.
        quoted = '"' + table.replace('"', '""') + '"'
        rows = []
        result = await db.stream(
            text(f"SELECT row_to_json(record) FROM {quoted} AS record")
        )
        seen = 0
        async for row in result.scalars():
            seen += 1
            if seen > 10000:
                raise WorktreeError("演练数据库单表超过10000行预算")
            if (
                protected
                and table == "export_artifacts"
                and str(row["id"]) in invalidated_export_indexes
            ):
                continue
            if protected:
                row = protected_row(table, row)
            rows.append(digest(row))
        facts[table] = {"count": len(rows), "sha256": digest(sorted(rows))}
    return facts


async def inspect_database(db):
    from sqlalchemy import select
    from app.db.models.async_job import AsyncJob
    from app.db.models.training_dataset import TrainingDatasetVersion

    return {
        "full_database": await database_facts(db),
        "protected_database": await database_facts(db, protected=True),
        "versions": [
            {
                "id": str(row.id),
                "status": row.status,
                "snapshot_digest": row.snapshot_digest,
                "manifest_digest": row.manifest.get("content_digest"),
                "package_sha256": row.package_sha256,
            }
            for row in await db.scalars(
                select(TrainingDatasetVersion).order_by(TrainingDatasetVersion.id)
            )
        ],
        "unfinished_jobs": [
            {"id": str(row.id), "kind": row.kind, "status": row.status}
            for row in await db.scalars(
                select(AsyncJob)
                .where(AsyncJob.status.in_(["pending", "running"]))
                .order_by(AsyncJob.id)
            )
        ],
    }


TASK_ORIGINAL_CASES = ("current", "deleted", "unbound", "unfinished")


def task_original_identifier(case):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aap:recovery-task-original:{case}")


def task_original_content():
    from PIL import Image

    with io.BytesIO() as output, Image.new("RGB", (2, 2), "purple") as image:
        image.save(output, format="PNG")
        return output.getvalue()


async def seed_task_originals(db, *, actor, project, storage):
    from app.db.models.task import Task
    from app.services import managed_original_creation as creations
    from app.services import task_originals

    raw = task_original_content()
    for case in TASK_ORIGINAL_CASES:
        task = Task(
            id=task_original_identifier(case),
            project_id=project.id,
            display_id="T-REC-ORIGINAL-" + case.upper(),
            file_name=case + ".png",
            file_path="recovery/unallocated-task-original.png",
            file_type="image",
            status="uploading",
        )
        db.add(task)
        await db.flush()
        allocation = await creations.allocate_task(
            db,
            project_id=project.id,
            task_id=task.id,
            logical_path=task.file_name,
            max_bytes=len(raw),
            requested_by=actor.id,
            content_type="image/png",
        )
        task.file_path = allocation["object_key"]
        storage.client.put_object(
            Bucket=allocation["staging_bucket"],
            Key=allocation["staging_key"],
            Body=raw,
            ContentType="image/png",
        )
        if case != "unfinished":
            created = await creations.complete_direct(db, allocation)
            if case in ("current", "deleted"):
                await task_originals.publish(db, task, allocation, created)
                task.status = "pending"
                await db.flush()
        if case != "current":
            await db.delete(task)
            await db.flush()

    # A historical same-content key remains unknown, even beside verified uploads.
    unknown_key = "recovery/unknown-task-original.png"
    storage.client.put_object(
        Bucket=storage.bucket, Key=unknown_key, Body=raw, ContentType="image/png"
    )
    db.add(
        Task(
            id=task_original_identifier("legacy"),
            project_id=project.id,
            display_id="T-REC-ORIGINAL-LEGACY",
            file_name="unknown-task-original.png",
            file_path=unknown_key,
            file_type="image",
            status="pending",
        )
    )
    await db.flush()


async def verify_fixed_task_image(db, *, storage, task, actor, sha256, raw):
    from app.services.dataset_content import task_snapshot

    original_buckets = storage.bucket, storage.datasets_bucket
    # The import purpose is unrelated to either saved original location.
    storage.bucket = storage.datasets_bucket = storage.import_bucket
    try:
        snapshot = await task_snapshot(db, task.id, actor, sha256)
        try:
            assert snapshot.sha256 == sha256 and snapshot.size == len(raw)
            assert snapshot.file.read() == raw
        finally:
            snapshot.close()
    finally:
        storage.bucket, storage.datasets_bucket = original_buckets


async def verify_task_originals(db, *, storage, actor):
    from sqlalchemy import select
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.db.models.task import Task
    from app.db.models.task_original import TaskOriginal
    from app.services import (
        managed_original_creation as creations,
        managed_original_writer,
    )
    from app.services import task_originals
    from app.services.content_snapshot import ContentError

    raw = task_original_content()
    source_rows = list(
        await db.scalars(
            select(ManagedOriginalCreation).where(
                ManagedOriginalCreation.owner_kind == "task"
            )
        )
    )
    assert len(source_rows) == len(TASK_ORIGINAL_CASES)
    by_task = {row.task_id: row for row in source_rows}
    assert set(by_task) == {
        task_original_identifier(case) for case in TASK_ORIGINAL_CASES
    }
    owners = list(await db.scalars(select(TaskOriginal)))
    assert len(owners) == 2
    assert {row.task_id for row in owners} == {
        task_original_identifier(case) for case in ("current", "deleted")
    }
    for row in owners:
        owner = task_originals.resolve(
            row, endpoint_url=storage.client.meta.endpoint_url
        )
        original = by_task[row.task_id]
        assert owner.bucket == storage.bucket and original.bucket != storage.bucket
        assert row.id == row.creation_id == original.id
        assert row.project_id == original.project_id
        assert row.object_key == original.object_key
        assert row.sha256 == original.sha256 == hashlib.sha256(raw).hexdigest()
        assert row.size_bytes == original.size_bytes == len(raw)
        assert row.etag == original.etag and row.version_id == original.version_id
        assert row.created_at == original.completed_at
        # Target observations can differ from historical ETag/version receipts.
        observed = managed_original_writer.inspect_staging(
            storage.client,
            owner.bucket,
            owner.object_key,
            max_bytes=len(raw),
            timeout_seconds=60,
        )
        assert observed.sha256 == row.sha256 and observed.size_bytes == row.size_bytes
    for case in TASK_ORIGINAL_CASES:
        task_id = task_original_identifier(case)
        original = by_task[task_id]
        assert original.owner_kind == "task" and original.dataset_id is None
        assert original.status == ("allocated" if case == "unfinished" else "verified")
        assert original.bucket != storage.bucket
        assert original.staging_bucket != storage.import_bucket
        task = await db.get(Task, task_id)
        if case == "current":
            assert task is not None and task.status == "pending"
            assert (
                task.original_id == original.id
                and task.file_path == original.object_key
            )
            await verify_fixed_task_image(
                db,
                storage=storage,
                task=task,
                actor=actor,
                sha256=original.sha256,
                raw=raw,
            )
        else:
            assert task is None
        if case in ("unbound", "unfinished"):
            assert all(row.creation_id != original.id for row in owners)
        if case == "unfinished":
            assert original.receipt is None and original.verification is None
            assert original.operation_id is None and original.claimed_at is None
        else:
            assert original.receipt is not None and original.verification is not None
            assert original.settled_at is not None
        staged = managed_original_writer.inspect_staging(
            storage.client,
            storage.import_bucket,
            original.staging_key,
            max_bytes=len(raw),
            timeout_seconds=60,
        )
        assert staged.sha256 == hashlib.sha256(raw).hexdigest()
        if case == "unbound":
            unbound = managed_original_writer.inspect_staging(
                storage.client,
                storage.bucket,
                original.object_key,
                max_bytes=len(raw),
                timeout_seconds=60,
            )
            assert unbound.sha256 == original.sha256
        try:
            await creations.for_task(
                db, task_id=task_id, object_key=original.object_key
            )
        except ContentError as exc:
            assert exc.code == "managed_original_configuration_changed"
        else:
            raise AssertionError("Restoration cannot adopt a historical Task creation")
    legacy = await db.get(Task, task_original_identifier("legacy"))
    assert (
        legacy is not None
        and legacy.original_id is None
        and legacy.dataset_item_id is None
    )
    assert legacy.file_path == "recovery/unknown-task-original.png"
    assert all(row.task_id != legacy.id for row in owners)
    unknown = managed_original_writer.inspect_staging(
        storage.client,
        storage.bucket,
        legacy.file_path,
        max_bytes=len(raw),
        timeout_seconds=60,
    )
    assert unknown.sha256 == hashlib.sha256(raw).hexdigest()
    return {
        "pinned_task_originals": len(owners),
        "verified_task_original_files": len(owners),
        "deleted_task_original_owners": 1,
        "unbound_verified_task_creations": 1,
        "unfinished_task_creations": 1,
        "preserved_task_upload_staging_objects": len(source_rows),
        "unowned_legacy_task_originals": 1,
        "verified_task_original_content": 1,
    }


AUXILIARY_CASES = (
    "current_raw",
    "shared_map",
    "deleted_map",
    "unbound_raw",
    "unfinished_raw",
)


def auxiliary_identifier(case):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aap:recovery-auxiliary-original:{case}")


def auxiliary_content(kind, case=None):
    """Real LiDAR/PNG bytes plus one unbound empty raw; no execution claim."""
    if kind == "map":
        return task_original_content()
    if kind != "raw_lidar":
        raise ValueError("Unknown auxiliary fixture kind")
    if case == "unbound_raw":
        return b""
    import struct

    return struct.pack("<10f", 1, 2, 3, 0.25, 7, 4, 5, 6, 0.5, 8)


def auxiliary_contract(kind):
    if kind == "map":
        return {
            "token": "a" * 32,
            "category": "semantic_prior",
            "filename": "maps/recovery.png",
            "log_tokens": ["b" * 32],
        }
    if kind != "raw_lidar":
        raise ValueError("Unknown auxiliary fixture kind")
    return {
        "sample": {
            "token": "c" * 32,
            "timestamp": 1,
            "scene_token": "d" * 32,
            "prev": "",
            "next": "",
        },
        "sample_data": {
            "token": "e" * 32,
            "sample_token": "c" * 32,
            "calibrated_sensor_token": "f" * 32,
            "ego_pose_token": "1" * 32,
            "timestamp": 1,
            "fileformat": "pcd",
            "is_key_frame": True,
            "height": 0,
            "width": 0,
            "filename": "samples/LIDAR_TOP/recovery.pcd.bin",
            "prev": "",
            "next": "",
        },
        "calibrated_sensor": {
            "token": "f" * 32,
            "sensor_token": "2" * 32,
            "translation": [0, 0, 0],
            "rotation": [1, 0, 0, 0],
            "camera_intrinsic": [],
        },
        "sensor": {"token": "2" * 32, "channel": "LIDAR_TOP", "modality": "lidar"},
        "ego_pose": {
            "token": "1" * 32,
            "timestamp": 1,
            "translation": [0, 0, 0],
            "rotation": [1, 0, 0, 0],
        },
    }


def auxiliary_scene_metadata(owner=None):
    metadata = {
        "frame": "ego",
        "nuscenes_export": {
            "version": "v1.0-mini",
            "scene": {
                "token": "d" * 32,
                "name": "recovery-scene",
                "description": "",
                "log_token": "b" * 32,
                "nbr_samples": 1,
                "first_sample_token": "c" * 32,
                "last_sample_token": "c" * 32,
            },
            "log": {
                "token": "b" * 32,
                "logfile": "recovery.log",
                "vehicle": "fixture",
                "date_captured": "2020-01-01",
                "location": "fixture",
            },
            "map": auxiliary_contract("map"),
        },
    }
    if owner is not None:
        metadata["nuscenes_export"].update(
            map_storage_key=owner.object_key,
            map_sha256=owner.sha256,
            map_file_size=owner.size_bytes,
        )
    return metadata


async def seed_auxiliary_originals(db, *, actor, project):
    """Exercise real creation/publication; these rows qualify offline relocation only."""
    from app.db.models.dataset import Dataset, DatasetItem, Scene
    from app.db.models.task import Task
    from app.services import auxiliary_originals, managed_original_creation as creations
    from app.services.dataset_assets import register_item_asset

    dataset = Dataset(
        id=auxiliary_identifier("dataset"),
        display_id="DS-REC-AUX",
        name="Synthetic auxiliary originals",
        created_by=actor.id,
        data_type="point_cloud",
        is_temporal=True,
        metadata_={"axis_convention": "iso_8855"},
    )
    db.add(dataset)
    scenes = []
    for case in ("first", "second"):
        scene = Scene(
            id=auxiliary_identifier(case),
            dataset_id=dataset.id,
            display_id="S-REC-AUX-" + case.upper(),
            name="Auxiliary " + case,
            source_format="nuscenes",
            source_metadata=auxiliary_scene_metadata(),
            created_by=actor.id,
        )
        db.add(scene)
        scenes.append(scene)
    await db.flush()
    pcd = (
        b"VERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\n"
        b"WIDTH 2\nHEIGHT 1\nPOINTS 2\nDATA ascii\n1 2 3\n4 5 6\n"
    )
    allocation = await creations.allocate(
        db,
        dataset_id=dataset.id,
        logical_path="recovery.pcd",
        origin="directory_copy",
        max_bytes=len(pcd),
        requested_by=actor.id,
        authority={"operation": "offline-recovery-fixture"},
    )
    with io.BytesIO(pcd) as stream:
        created = await creations.create_stream(
            db, allocation, stream, content_type="application/octet-stream"
        )
    item = DatasetItem(
        id=auxiliary_identifier("item"),
        dataset_id=dataset.id,
        file_name="recovery.pcd",
        file_path=allocation["object_key"],
        file_type="point_cloud",
        file_size=len(pcd),
        content_hash=created.sha256,
        scene_id=scenes[0].id,
        frame_index=0,
        metadata_={"nuscenes_export": auxiliary_contract("raw_lidar")},
    )
    db.add(item)
    await db.flush()
    await register_item_asset(
        db, item, sha256=created.sha256, size_bytes=len(pcd), etag=created.etag
    )
    await creations.attach(db, item, created.creation_id)
    db.add(
        Task(
            id=auxiliary_identifier("task"),
            project_id=project.id,
            dataset_item_id=item.id,
            display_id="T-REC-AUX",
            file_name=item.file_name,
            file_path=item.file_path,
            file_type="point_cloud",
            status="pending",
        )
    )
    await db.flush()
    for case in AUXILIARY_CASES:
        kind = "map" if case.endswith("map") else "raw_lidar"
        contract, raw = auxiliary_contract(kind), auxiliary_content(kind, case)
        target_dataset = dataset
        if case == "deleted_map":
            target_dataset = Dataset(
                id=auxiliary_identifier("deleted-dataset"),
                display_id="DS-REC-AUX-DEL",
                name="Deleted auxiliary source",
                created_by=actor.id,
            )
            db.add(target_dataset)
            await db.flush()
        allocation = await creations.allocate_auxiliary(
            db,
            dataset_id=target_dataset.id,
            kind=kind,
            source_contract=contract,
            logical_path=contract["filename"]
            if kind == "map"
            else contract["sample_data"]["filename"],
            max_bytes=len(raw),
            requested_by=actor.id,
            authority={"operation": "offline-recovery-fixture", "recovery_case": case},
            content_type="image/png" if kind == "map" else "application/octet-stream",
        )
        if case == "unfinished_raw":
            continue
        with io.BytesIO(raw) as stream:
            created = await creations.create_stream(
                db, allocation, stream, content_type=allocation["content_type"]
            )
        if case == "unbound_raw":
            continue
        owner = await auxiliary_originals.publish(
            db, allocation, created, kind=kind, source_contract=contract
        )
        if case == "current_raw":
            item.metadata_ = {
                "nuscenes_export": {
                    **contract,
                    "source_storage_key": owner.object_key,
                    "source_sha256": owner.sha256,
                    "source_file_size": owner.size_bytes,
                }
            }
            await auxiliary_originals.bind_raw(db, item, owner)
        elif case == "shared_map":
            for scene in scenes:
                scene.source_metadata = auxiliary_scene_metadata(owner)
                await auxiliary_originals.bind_map(db, scene, owner)
        else:
            scene = Scene(
                id=auxiliary_identifier("deleted-scene"),
                dataset_id=target_dataset.id,
                display_id="S-REC-AUX-DEL",
                name="Deleted auxiliary scene",
                source_format="nuscenes",
                source_metadata=auxiliary_scene_metadata(owner),
                created_by=actor.id,
            )
            db.add(scene)
            await db.flush()
            await auxiliary_originals.bind_map(db, scene, owner)
            await db.delete(target_dataset)
        await db.flush()


async def verify_auxiliary_originals(db, *, storage):
    from sqlalchemy import select
    from app.db.models.dataset import Dataset, DatasetItem, Scene
    from app.db.models.dataset_auxiliary_original import DatasetAuxiliaryOriginal
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.services import auxiliary_originals, managed_original_creation as creations
    from app.services.content_snapshot import ContentError
    from worktree_recovery_export import _read_exact

    creations_by_case = {
        row.authority["recovery_case"]: row
        for row in await db.scalars(
            select(ManagedOriginalCreation).where(
                ManagedOriginalCreation.owner_kind == "dataset_auxiliary"
            )
        )
    }
    assert set(creations_by_case) == set(AUXILIARY_CASES)
    owners = list(await db.scalars(select(DatasetAuxiliaryOriginal)))
    assert len(owners) == 3
    by_id = {row.id: row for row in owners}
    verified_empty_objects = 0
    for case, creation in creations_by_case.items():
        assert creation.owner_kind == "dataset_auxiliary"
        assert creation.bucket != storage.datasets_bucket
        assert creation.project_id is None and creation.task_id is None
        assert creation.status == (
            "allocated" if case == "unfinished_raw" else "verified"
        )
        current = by_id.get(creation.id)
        if case in {"unbound_raw", "unfinished_raw"}:
            assert current is None
        else:
            kind = "map" if case.endswith("map") else "raw_lidar"
            locator = auxiliary_originals.resolve(
                current, endpoint_url=storage.client.meta.endpoint_url
            )
            assert locator.bucket == storage.datasets_bucket
            assert (
                current.creation_id == creation.id
                and current.dataset_id == creation.dataset_id
            )
            assert current.object_key == creation.object_key
            assert (
                current.sha256 == creation.sha256
                and current.size_bytes == creation.size_bytes
            )
            assert (
                current.etag == creation.etag
                and current.version_id == creation.version_id
            )
            assert current.created_at == creation.completed_at
            assert current.source_contract == auxiliary_contract(kind)
        if case == "unfinished_raw":
            assert creation.receipt is None and creation.verification is None
            assert creation.claimed_at is None and creation.operation_id is None
        else:
            kind = "map" if case.endswith("map") else "raw_lidar"
            raw = auxiliary_content(kind, case)
            assert creation.sha256 == hashlib.sha256(raw).hexdigest()
            assert creation.size_bytes == len(raw)
            actual = _read_exact(
                storage,
                storage.datasets_bucket,
                creation.object_key,
                creation.sha256,
                creation.size_bytes,
            )
            assert actual == raw
            verified_empty_objects += int(len(actual) == 0)
        # The immutable source attempt is not executable in the target context.
        saved = await creations._read(db, creation.id)
        try:
            await creations._validate(db, saved)
        except ContentError as exc:
            assert exc.code == "managed_original_configuration_changed"
        else:
            raise AssertionError(
                "Restoration cannot adopt an auxiliary source creation"
            )
    shared = by_id[creations_by_case["shared_map"].id]
    for case in ("first", "second"):
        scene = await db.get(Scene, auxiliary_identifier(case))
        assert scene.nuscenes_map_original_id == shared.id
        assert scene.source_metadata["nuscenes_export"]["map"] == shared.source_contract
    item = await db.get(DatasetItem, auxiliary_identifier("item"))
    assert item.nuscenes_raw_original_id == creations_by_case["current_raw"].id
    assert await db.get(Dataset, auxiliary_identifier("deleted-dataset")) is None
    assert await db.get(Scene, auxiliary_identifier("deleted-scene")) is None
    original_bucket = storage.datasets_bucket
    storage.datasets_bucket = storage.import_bucket
    try:
        sources = [
            await auxiliary_originals.resolve_task_raw(db, auxiliary_identifier("task"))
        ]
        sources.append(
            await auxiliary_originals.resolve_scene_map(
                db, auxiliary_identifier("task")
            )
        )
    finally:
        storage.datasets_bucket = original_bucket
    for source in sources:
        assert (
            source.bucket == original_bucket
            and source.owner_kind == "dataset_auxiliary"
        )
        assert _read_exact(
            storage, source.bucket, source.object_key, source.sha256, source.size_bytes
        ) == auxiliary_content(source.ownership_facts.kind)
    assert sources[1].content_id == shared.id
    assert verified_empty_objects == 1
    return {
        "pinned_auxiliary_originals": 3,
        "verified_auxiliary_objects": 4,
        "verified_empty_auxiliary_objects": verified_empty_objects,
        "shared_auxiliary_map_references": 2,
        "deleted_business_auxiliary_owners": 1,
        "unbound_verified_auxiliary_creations": 1,
        "unfinished_auxiliary_creations": 1,
        "auxiliary_recovery_qualification": "offline-retained-bytes-only",
    }


async def seed(db, external: Path):
    import pytest
    from sqlalchemy import select
    from app.core.crypto import encrypt_secret
    from app.db.base import async_session
    from app.db.models.annotation import Annotation
    from app.db.models.dataset import DatasetItem
    from app.db.models.dataset_asset import DatasetAssetLocation
    from app.db.models.dataset_source import DatasetSourceItem, DatasetSourcePreview
    from app.db.models.original_cache import OriginalCacheEntry
    from app.db.models.managed_original_retirement import ManagedOriginalRetirement
    from app.db.models.managed_original_retention_hold import (
        ManagedOriginalRetentionHold,
    )
    from app.db.models.prediction_input import PredictionInputLease
    from app.db.models.storage_connection import StorageConnection
    from app.db.models.task import Task
    from app.db.models.training_dataset import TrainingDatasetVersion
    from app.db.models.training_run import TrainingAttempt, TrainingRun
    from app.db.models.training_storage_deletion import TrainingStorageDeletion
    from app.schemas.training_dataset import TrainingDatasetSelection
    from app.services import (
        async_job as jobs,
        original_cache as cache,
        prediction_inputs,
        directory_previews,
    )
    from app.services import training_datasets as datasets, project_task_dataset
    from app.services.dataset import DatasetService
    from app.services.dataset_assets import (
        ensure_legacy_bindings,
        register_item_asset,
        LEGACY_LOCATION_ID,
    )
    from app.services.dataset_source_access import capture_dataset_write
    from app.services.storage import storage_service as storage
    from app.workers import training_dataset as worker
    from app.services import managed_original_verification as verifications
    from app.services import managed_original_creation as creations
    from app.workers import managed_original_verification as verification_worker
    from tests.factory import create_project, create_user
    from tests.test_dataset_content import create_content_world
    from tests.test_dataset_source_changes import rescan, picture, accept_all_files

    assert not await db.scalar(select(Task.id).limit(1))
    actor = await create_user(
        db, "super_admin", f"recovery-{uuid.uuid4()}@example.test", "Recovery fixture"
    )
    project = await create_project(db, owner_id=actor.id, classes=["car"])
    external.mkdir(mode=0o700)
    with pytest.MonkeyPatch.context() as patch:
        world = await create_content_world(db, actor, external, patch)
        (world[0] / "blue.png").write_bytes(picture("blue"))
        world = await rescan(db, world)
        await accept_all_files(db, world)
        binding = await ensure_legacy_bindings(db)
        green = picture("green")
        managed = DatasetItem(
            dataset_id=world[3].id,
            file_name="managed.png",
            file_path="recovery/managed.png",
            file_type="image",
            file_size=len(green),
            width=48,
            height=32,
        )
        db.add(managed)
        await db.flush()
        allocation = await creations.allocate(
            db,
            dataset_id=managed.dataset_id,
            logical_path=managed.file_name,
            origin="zip",
            max_bytes=len(green),
            requested_by=actor.id,
            content_type="image/png",
        )
        managed.file_path = allocation["object_key"]
        created = await creations.create_stream(
            db, allocation, io.BytesIO(green), content_type="image/png"
        )
        await register_item_asset(
            db,
            managed,
            sha256=hashlib.sha256(green).hexdigest(),
            size_bytes=len(green),
            binding=binding,
        )
        await creations.attach(db, managed, created.creation_id)
        authority = await capture_dataset_write(db, world[3].id, actor, None)
        await DatasetService(db).link_project(
            world[3].id, project.id, directory_authority=authority
        )
        tasks = list(
            await db.scalars(select(Task).where(Task.project_id == project.id))
        )
        assert len(tasks) == 3
        for task in tasks:
            task.status, task.reviewed_at, task.is_labeled = (
                "completed",
                datetime.now(timezone.utc),
                True,
            )
            db.add(
                Annotation(
                    task_id=task.id,
                    project_id=project.id,
                    user_id=actor.id,
                    class_name="car",
                    tool_unit_id="bbox",
                    geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                )
            )
        await db.commit()

        async def new_version(name):
            snapshot, report = await datasets.freeze_selection(
                db,
                project,
                TrainingDatasetSelection(),
                actor_id=actor.id,
                trusted_actor=True,
            )
            assert report["ready"], report["issues"]
            version = TrainingDatasetVersion(
                project_id=project.id,
                creator_id=actor.id,
                request_id=uuid.uuid4(),
                request_digest=digest(name),
                name=name,
                snapshot=snapshot,
                snapshot_digest=report["digest"],
                preparation_report=datasets.frozen_preparation_report(
                    report["digest"], report
                ),
            )
            db.add(version)
            await db.flush()
            from app.services.training_dataset_storage import allocate

            await allocate(db, version)
            await project_task_dataset.ensure_membership(db, version)
            await datasets.enqueue_dataset(db, version)
            await db.commit()
            return version

        ready = await new_version("Recovery ready freeze")
        await worker.build_dataset(
            ready.id,
            ready.async_job_id,
            ready.attempt_id,
            async_session,
            node_id="worker:content@test",
        )
        await db.refresh(ready)
        assert ready.status == "ready", ready.error_message
        partial = await new_version("Interrupted freeze")
        partial.status = "running"
        await jobs.mark_running(db, partial.async_job_id)
        await db.commit()
        reference = next(
            row for row in partial.snapshot["samples"] if row.get("asset_reference")
        )
        with tempfile.TemporaryDirectory(prefix="aap-recovery-partial-") as directory:
            await worker.materialize_sample(
                db,
                partial,
                reference,
                f"projects/{project.id}/datasets/{partial.id}",
                partial.snapshot_digest,
                Path(directory),
                "worker:content@test",
            )

        # Durable owners exercise every supported physical relocation column.
        retired = DatasetItem(
            dataset_id=world[3].id,
            file_name="retired-managed.png",
            file_path="recovery/retired-managed.png",
            file_type="image",
            file_size=len(green),
            width=48,
            height=32,
        )
        db.add(retired)
        await db.flush()
        allocation = await creations.allocate(
            db,
            dataset_id=retired.dataset_id,
            logical_path=retired.file_name,
            origin="zip",
            max_bytes=len(green),
            requested_by=actor.id,
            content_type="image/png",
        )
        retired.file_path = allocation["object_key"]
        created = await creations.create_stream(
            db, allocation, io.BytesIO(green), content_type="image/png"
        )
        await register_item_asset(
            db,
            retired,
            sha256=hashlib.sha256(green).hexdigest(),
            size_bytes=len(green),
            binding=binding,
        )
        await creations.attach(db, retired, created.creation_id)
        assert await DatasetService(db).delete_item(retired.id)
        # Related assertions must preserve independent policies and complete hold
        # history while following the same physical locator through restoration.
        peer = DatasetItem(
            dataset_id=world[3].id,
            file_name="retired-managed-peer.png",
            file_path=retired.file_path,
            file_type="image",
            file_size=len(green),
            width=48,
            height=32,
        )
        db.add(peer)
        await db.flush()
        await register_item_asset(
            db,
            peer,
            sha256=hashlib.sha256(green).hexdigest(),
            size_bytes=len(green),
            binding=binding,
        )
        assert await DatasetService(db).delete_item(peer.id)
        retired_record = await db.scalar(
            select(ManagedOriginalRetirement).where(
                ManagedOriginalRetirement.dataset_item_id == retired.id
            )
        )
        retired_record.policy_revision = 2
        retired_record.retain_until = datetime.now(timezone.utc) + timedelta(days=2)
        db.add_all(
            [
                ManagedOriginalRetentionHold(
                    id=uuid.uuid4(),
                    retirement_id=retired_record.id,
                    kind="backup",
                    reference_code="recovery-active-backup",
                    reason="Synthetic recovery preservation requirement",
                    created_by=actor.id,
                ),
                ManagedOriginalRetentionHold(
                    id=uuid.uuid4(),
                    retirement_id=retired_record.id,
                    kind="migration",
                    reference_code="recovery-released-migration",
                    reason="Synthetic historical migration requirement",
                    created_by=actor.id,
                    revision=2,
                    released_at=datetime.now(timezone.utc),
                    released_by=actor.id,
                    release_reason="Synthetic migration observation completed",
                ),
            ]
        )
        source_item = await db.scalar(
            select(DatasetSourceItem).where(DatasetSourceItem.source_id == world[5].id)
        )
        red = world[1]
        now = datetime.now(timezone.utc)
        record = OriginalCacheEntry(
            id=uuid.uuid4(),
            scope_location_id=world[4].id,
            source_id=world[5].id,
            asset_id=source_item.asset_id,
            sha256=hashlib.sha256(red).hexdigest(),
            size_bytes=len(red),
            source_revision=1,
            policy_revision=world[5].policy_revision,
            location_revision=1,
            location_id=LEGACY_LOCATION_ID,
            bucket=storage.media_cache_bucket,
            binding_generation=1,
            storage_identity=cache.storage_identity(storage),
            state="ready",
            uncertain_write=False,
            verified_at=now,
            last_accessed_at=now,
            expires_at=now + timedelta(days=1),
            writer_until=now,
            retain_until=now + timedelta(days=7),
            next_check_at=now + timedelta(days=1),
        )
        # Use the exact member bytes if the first source member is blue.
        original = world[0] / (
            "图片 空格.png"
            if source_item.path_digest == world[7].path_digest
            else "blue.png"
        )
        red = original.read_bytes()
        record.sha256, record.size_bytes = hashlib.sha256(red).hexdigest(), len(red)
        db.add(record)
        cache_key = cache.object_key(record)
        storage.client.put_object(
            Bucket=record.bucket, Key=cache_key, Body=red, ContentType="image/png"
        )
        db.add(
            DatasetAssetLocation(
                asset_id=record.asset_id,
                location_id=LEGACY_LOCATION_ID,
                bucket=record.bucket,
                object_key=cache_key,
                role="original_cache",
                reference_state="present",
                verified_at=now,
            )
        )
        preview = io.BytesIO()
        from PIL import Image

        with Image.open(io.BytesIO(red)) as image:
            image.save(preview, "WEBP", quality=80)
        preview_bytes = preview.getvalue()
        preview.close()
        preview_key = "recovery/preview.webp"
        storage.client.put_object(
            Bucket=storage.media_cache_bucket,
            Key=preview_key,
            Body=preview_bytes,
            ContentType="image/webp",
            CacheControl="private,max-age=60",
        )
        db.add(
            DatasetSourcePreview(
                source_id=source_item.source_id,
                path_digest=source_item.path_digest,
                asset_id=source_item.asset_id,
                source_sha256=record.sha256,
                spec=directory_previews.SPEC,
                policy_revision=world[5].policy_revision,
                state="ready",
                location_id=LEGACY_LOCATION_ID,
                bucket=storage.media_cache_bucket,
                object_key=preview_key,
                sha256=hashlib.sha256(preview_bytes).hexdigest(),
                size_bytes=len(preview_bytes),
            )
        )
        lease = PredictionInputLease(
            id=uuid.uuid4(),
            owner_id=uuid.uuid4(),
            owner_kind="recovery_fixture",
            task_id=tasks[0].id,
            bucket=storage.import_bucket,
            storage_identity=prediction_inputs.storage_identity(storage),
            state="cleanup_pending",
            uncertain_write=False,
            expires_at=now,
            retain_until=now + timedelta(days=7),
            next_cleanup_at=now + timedelta(days=1),
        )
        db.add(lease)
        storage.client.put_object(
            Bucket=lease.bucket,
            Key=prediction_inputs.input_prefix(lease.id) + "input.png",
            Body=red,
            ContentType="image/png",
        )
        from app.services.training_dataset_storage import prefix_for

        deletion_ref = {
            **ready.storage_ref,
            "prefix": prefix_for(project.id, uuid.uuid4()),
        }
        db.add(
            TrainingStorageDeletion(
                bucket=deletion_ref["bucket"],
                prefix=deletion_ref["prefix"],
                storage_ref=deletion_ref,
            )
        )
        storage.client.put_object(
            Bucket=storage.training_bucket,
            Key=deletion_ref["prefix"] + f"attempts/{uuid.uuid4()}/dataset.zip",
            Body=b"retained until reviewed",
        )
        from app.services import training_run_storage as run_storage

        run_config = {"task": "detect", "fixture": "isolated-recovery"}
        run = TrainingRun(
            project_id=project.id,
            creator_id=actor.id,
            dataset_id=ready.id,
            request_id=uuid.uuid4(),
            request_digest=digest("recovery-run"),
            name="Recovery stopped experiment",
            status="failed",
            config=run_config,
            config_digest=digest(run_config),
            completed_at=now,
        )
        db.add(run)
        await db.flush()
        await run_storage.allocate(db, run)
        attempt = TrainingAttempt(run_id=run.id, ordinal=1, status="failed")
        db.add(attempt)
        await db.flush()
        run.current_attempt_id = attempt.id
        weights = b"synthetic recovery weights; never deserialized"
        spec = {
            "name": "best.pt",
            "role": "weights",
            "size_bytes": len(weights),
            "sha256": hashlib.sha256(weights).hexdigest(),
            "epoch": 1,
        }
        attempt.upload_inventory = {"best.pt": spec}
        owner = run_storage.resolve(run)
        key = owner.key(f"attempts/{attempt.id}/best.pt")
        attempt.artifacts = {"best.pt": {**spec, "key": key}}
        storage.client.put_object(
            Bucket=owner.bucket,
            Key=key,
            Body=weights,
            Metadata={"sha256": spec["sha256"], "attempt-id": str(attempt.id)},
        )
        run_deletion_ref = {
            **run.storage_ref,
            "prefix": run_storage.prefix_for(project.id, uuid.uuid4()),
        }
        db.add(
            TrainingStorageDeletion(
                bucket=run_deletion_ref["bucket"],
                prefix=run_deletion_ref["prefix"],
                storage_ref=run_deletion_ref,
            )
        )
        storage.client.put_object(
            Bucket=run_deletion_ref["bucket"],
            Key=run_deletion_ref["prefix"] + f"attempts/{uuid.uuid4()}/last.pt",
            Body=b"retained run output until reviewed",
        )
        from worktree_recovery_auxiliary import seed_auxiliary

        await seed_auxiliary(
            db,
            project=project,
            actor=actor,
            dataset=ready,
            run=run,
            attempt=attempt,
            png=green,
            now=now,
            storage=storage,
        )
        from worktree_recovery_avatar import seed_avatars

        await seed_avatars(db, actor=actor, storage=storage, now=now)
        from worktree_recovery_export import seed_export_outputs

        await seed_export_outputs(db, project=project, storage=storage, now=now)
        from worktree_recovery_bug_attachments import seed_bug_attachments

        await seed_bug_attachments(db, actor=actor, storage=storage, now=now)
        from worktree_recovery_comment_attachments import seed_comment_attachments

        await seed_comment_attachments(
            db, actor=actor, project=project, storage=storage, now=now
        )
        from worktree_recovery_guide_assets import seed_guide_assets

        await seed_guide_assets(
            db, actor=actor, project=project, storage=storage, now=now
        )
        from worktree_recovery_raster_objects import seed_raster_objects

        await seed_raster_objects(
            db, actor=actor, project=project, storage=storage, now=now
        )
        await seed_task_originals(db, actor=actor, project=project, storage=storage)
        await seed_auxiliary_originals(db, actor=actor, project=project)
        from worktree_recovery_audit_archives import seed_audit_archives

        await seed_audit_archives(db, storage=storage, now=now)
        db.add(
            StorageConnection(
                name="Synthetic recovery secret",
                kind="sftp",
                config={
                    "host": "fixture.invalid",
                    "username": "fixture",
                    "base_path": "/synthetic",
                },
                secret_enc=encrypt_secret({"password": "synthetic-recovery-secret"}),
                scope="global",
                created_by=actor.id,
            )
        )
        await db.commit()
        from worktree_recovery_image_export import seed_image_exports

        image_task = next(row for row in tasks if row.dataset_item_id == managed.id)
        await seed_image_exports(
            db,
            project=project,
            actor=actor,
            storage=storage,
            raw_by_task={
                image_task.id: green,
                task_original_identifier("current"): task_original_content(),
            },
        )
        # Keep both positive and negative immutable observations, plus an
        # undispatched pending attempt. Restoring bytes must not rewrite these
        # source facts or automatically resume an old queue.
        attempt, probe_job, _ = await verifications.start(db, retired_record.id, actor)
        await db.commit()
        async with async_session() as verification_db:
            assert await verification_worker.execute(
                verification_db, probe_job.id, probe_job.celery_task_id
            )
        await db.refresh(attempt)
        assert attempt.outcome == "matched"
        changed = bytes([green[0] ^ 1]) + green[1:]
        storage.client.put_object(
            Bucket=retired_record.bucket,
            Key=retired_record.object_key,
            Body=changed,
        )
        attempt, probe_job, _ = await verifications.start(db, retired_record.id, actor)
        await db.commit()
        async with async_session() as verification_db:
            assert await verification_worker.execute(
                verification_db, probe_job.id, probe_job.celery_task_id
            )
        await db.refresh(attempt)
        assert attempt.outcome == "changed"
        assert attempt.observed_sha256 == hashlib.sha256(changed).hexdigest()
        storage.client.put_object(
            Bucket=retired_record.bucket,
            Key=retired_record.object_key,
            Body=green,
            ContentType="image/png",
        )
        attempt, _, _ = await verifications.start(db, retired_record.id, actor)
        await db.commit()
        assert attempt.status == "pending" and attempt.operation_id is None
        facts = await inspect_database(db)
        facts["external_sources"] = [
            {
                "source_id": str(world[5].id),
                "location_id": str(world[4].id),
                "root_handle": "test",
                "protection_owner": "external-original-owner",
                "backed_up": False,
            }
        ]
        return facts


async def verify_application(db, external: Path):
    from sqlalchemy import select
    from app.core.crypto import decrypt_secret
    from app.db.base import async_session
    from app.db.models.dataset import DatasetItem
    from app.db.models.dataset_source import DatasetReference, DatasetSourcePreview
    from app.db.models.original_cache import OriginalCacheEntry
    from app.db.models.managed_original_retirement import ManagedOriginalRetirement
    from app.db.models.managed_original_retention_hold import (
        ManagedOriginalRetentionHold,
    )
    from app.db.models.managed_original_verification import ManagedOriginalVerification
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.db.models.storage_connection import StorageConnection
    from app.db.models.training_dataset import TrainingDatasetVersion
    from app.db.models.user import User
    from app.db.models.task import Task
    from app.services import directory_previews, original_cache
    from app.services.dataset_assets import validate_legacy_bindings
    from app.services.content_snapshot import ContentError
    from app.services.dataset_references import reference_snapshot
    from app.services.filesystem_storage import FilesystemStorageError
    from app.services.training_dataset_analysis import read_media
    from app.services.training_dataset_bridge import content_digest, contract_of
    from app.services.storage import storage_service as storage

    assert not await validate_legacy_bindings(db)
    connection = await db.scalar(select(StorageConnection))
    assert decrypt_secret(connection.secret_enc) == {
        "password": "synthetic-recovery-secret"
    }
    for record in await db.scalars(select(OriginalCacheEntry)):
        original_cache.verify_stored(record)
    retirements = list(await db.scalars(select(ManagedOriginalRetirement)))
    assert len(retirements) == 2
    creation_rows = list(await db.scalars(select(ManagedOriginalCreation)))
    dataset_creations = [row for row in creation_rows if row.owner_kind == "dataset"]
    assert len(dataset_creations) == 3
    assert all(row.status == "verified" for row in dataset_creations)
    # Restoration maps physical locations, never historical successful writes.
    assert all(row.bucket != storage.datasets_bucket for row in dataset_creations)
    from app.services import managed_original_creation as creations

    for creation in dataset_creations:
        try:
            await creations.for_key(
                db,
                dataset_id=creation.dataset_id,
                object_key=creation.object_key,
            )
        except ContentError as exc:
            assert exc.code == "managed_original_configuration_changed"
        else:
            raise AssertionError("Source creation cannot be adopted in target context")
    actor = await db.scalar(select(User).where(User.role == "super_admin"))
    assert actor is not None
    task_originals = await verify_task_originals(db, storage=storage, actor=actor)
    auxiliary_originals = await verify_auxiliary_originals(db, storage=storage)
    managed_task = await db.scalar(
        select(Task)
        .join(DatasetItem, Task.dataset_item_id == DatasetItem.id)
        .where(DatasetItem.file_name == "managed.png")
    )
    assert managed_task is not None
    managed_creation = next(
        row for row in dataset_creations if row.object_key == managed_task.file_path
    )
    from tests.test_dataset_source_changes import picture

    await verify_fixed_task_image(
        db,
        storage=storage,
        task=managed_task,
        actor=actor,
        sha256=managed_creation.sha256,
        raw=picture("green"),
    )
    assert sum(row.creation_id is not None for row in retirements) == 1
    assert {row.creation_id for row in retirements if row.creation_id} <= {
        row.id for row in creation_rows
    }
    for record in retirements:
        response = storage.client.get_object(
            Bucket=record.bucket, Key=record.object_key
        )
        body = response["Body"]
        try:
            content = body.read()
        finally:
            body.close()
        assert len(content) == record.size_bytes
        assert hashlib.sha256(content).hexdigest() == record.sha256
    assert sum(record.retain_until is None for record in retirements) == 1
    assert sorted(record.policy_revision for record in retirements) == [1, 2]
    holds = list(await db.scalars(select(ManagedOriginalRetentionHold)))
    assert len(holds) == 2
    assert {hold.retirement_id for hold in holds} <= {
        record.id for record in retirements
    }
    assert {hold.reference_code for hold in holds} == {
        "recovery-active-backup",
        "recovery-released-migration",
    }
    assert sorted(hold.revision for hold in holds) == [1, 2]
    assert sum(hold.released_at is None for hold in holds) == 1
    for preview in await db.scalars(select(DatasetSourcePreview)):
        assert directory_previews.cached_bytes_match(preview)
    from app.services import managed_original_verification as verifications
    from app.workers import managed_original_verification as verification_worker
    from app.services.managed_original_reclamation import preview as reclamation_preview

    old_probes = list(await db.scalars(select(ManagedOriginalVerification)))
    assert len(old_probes) == 3
    assert {probe.outcome for probe in old_probes} == {"matched", "changed", None}
    old_anchor_ids = {probe.retirement_id for probe in old_probes}
    assert len(old_anchor_ids) == 1
    old_anchor_id = next(iter(old_anchor_ids))
    history = await verifications.history(db, old_anchor_id, actor)
    assert history.total == 3 and all(
        not item.context_matches for item in history.items
    )
    pending = next(probe for probe in old_probes if probe.status == "pending")
    assert pending.operation_id is None and pending.started_at is None
    # Reverify the other independent assertion explicitly in the target. The
    # restored pending source attempt stays pending, preserving recovery facts.
    target_anchor = next(record for record in retirements if record.id != old_anchor_id)
    target_probe, target_job, _ = await verifications.start(db, target_anchor.id, actor)
    await db.commit()
    async with async_session() as verification_db:
        assert await verification_worker.execute(
            verification_db, target_job.id, target_job.celery_task_id
        )
    await db.refresh(target_probe)
    assert target_probe.outcome == "matched"
    target_history = await verifications.history(db, target_anchor.id, actor)
    assert target_history.total == 1 and target_history.items[0].context_matches
    await db.refresh(pending)
    assert pending.status == "pending" and pending.started_at is None
    preview = await reclamation_preview(db, actor)
    assert preview.counts.total_assertions == preview.counts.protected_assertions == 2
    assert all(
        item.indefinite_retention
        and item.declared_backup_holds == 1
        and item.declared_migration_holds == 0
        and not item.policy_candidate
        for item in preview.items
    )
    unavailable = 0
    references = list(await db.scalars(select(DatasetReference)))
    for reference in references:
        item = await db.get(DatasetItem, reference.dataset_item_id)
        assert item is not None
        try:
            content = await reference_snapshot(db, item.id, actor)
        except FilesystemStorageError as exc:
            if exc.code != "path_unavailable":
                raise
            unavailable += 1
        else:
            content.close()
            raise WorktreeError("缺失的外部原件被错误恢复为可读")
    # A replacement at the old path must not silently become the indexed original.
    from PIL import Image

    replacement = io.BytesIO()
    Image.new("RGB", (48, 32), "green").save(replacement, format="PNG")
    restored_directory = external / "source"
    restored_directory.mkdir(mode=0o700, parents=True)
    for name in ("图片 空格.png", "blue.png"):
        (restored_directory / name).write_bytes(replacement.getvalue())
    replacements_rejected = 0
    for reference in references:
        try:
            content = await reference_snapshot(db, reference.dataset_item_id, actor)
        except (FilesystemStorageError, ContentError) as exc:
            if exc.code not in {
                "root_identity_changed",
                "directory_identity_changed",
                "source_changed",
                "content_mismatch",
            }:
                raise
            replacements_rejected += 1
        else:
            content.close()
            raise WorktreeError("同名不同内容被错误恢复为原件")
    from app.db.models.training_storage_deletion import TrainingStorageDeletion
    from app.services.training_dataset_storage import resolve_ref
    from app.db.models.training_run import TrainingAttempt, TrainingRun
    from app.services import training_run_storage as run_storage

    deletion_count = 0
    for intent in await db.scalars(select(TrainingStorageDeletion)):
        resolver = (
            run_storage.resolve_ref
            if intent.storage_ref["purpose"] == "training-run"
            else resolve_ref
        )
        owner = resolver(intent.storage_ref, expected_prefix=intent.prefix)
        assert intent.bucket == owner.bucket == storage.training_bucket
        objects = storage.client.list_objects_v2(
            Bucket=owner.bucket, Prefix=owner.prefix
        )
        assert len(objects.get("Contents", [])) == 1
        deletion_count += 1
    assert deletion_count == 2
    run_count, run_artifact_count = 0, 0
    for run in await db.scalars(select(TrainingRun)):
        assert run.status == "failed" and digest(run.config) == run.config_digest
        owner = run_storage.resolve(run)
        assert owner.bucket == storage.training_bucket
        attempt = await db.get(TrainingAttempt, run.current_attempt_id)
        assert attempt.run_id == run.id and attempt.status == "failed"
        for name, artifact in attempt.artifacts.items():
            assert {k: v for k, v in artifact.items() if k != "key"} == (
                attempt.upload_inventory[name]
            )
            run_storage.expect_attempt(owner, attempt.id, artifact["key"])
            response = storage.client.get_object(
                Bucket=owner.bucket, Key=artifact["key"]
            )
            try:
                data = response["Body"].read(1024)
            finally:
                response["Body"].close()
            assert len(data) == artifact["size_bytes"]
            assert hashlib.sha256(data).hexdigest() == artifact["sha256"]
            assert response["Metadata"] == {
                "sha256": artifact["sha256"],
                "attempt-id": str(attempt.id),
            }
            run_artifact_count += 1
        run_count += 1
    assert (run_count, run_artifact_count) == (1, 1)
    from worktree_recovery_auxiliary import verify_auxiliary

    auxiliary = await verify_auxiliary(db, storage=storage)
    from worktree_recovery_avatar import verify_avatars

    avatars = await verify_avatars(db, storage=storage)
    from worktree_recovery_export import verify_export_outputs

    exports = await verify_export_outputs(db, storage=storage)
    from worktree_recovery_image_export import verify_image_exports

    image_exports = await verify_image_exports(
        db,
        storage=storage,
        raw_by_task={
            managed_task.id: picture("green"),
            task_original_identifier("current"): task_original_content(),
        },
    )
    from worktree_recovery_bug_attachments import verify_bug_attachments

    bug_attachments = await verify_bug_attachments(db, storage=storage)
    from worktree_recovery_comment_attachments import verify_comment_attachments

    comment_attachments = await verify_comment_attachments(db, storage=storage)
    from worktree_recovery_guide_assets import verify_guide_assets

    guide_assets = await verify_guide_assets(db, storage=storage)
    from worktree_recovery_raster_objects import verify_raster_objects

    raster_objects = await verify_raster_objects(db, storage=storage)
    from worktree_recovery_audit_archives import verify_audit_archives

    audit_archives = await verify_audit_archives(db, storage=storage)
    ready_count, partial_count, media_count = 0, 0, 0
    sys.path.insert(0, str(ROOT / "apps/training-executor"))
    from training_executor.adapters import validate_dataset
    from app.services.training_dataset_storage import resolve

    for version in await db.scalars(select(TrainingDatasetVersion)):
        assert content_digest(version.snapshot) == version.snapshot_digest
        owner = resolve(version)
        assert owner.bucket == storage.training_bucket
        if version.status != "ready":
            assert (
                version.status == "running"
                and not version.package_key
                and not version.manifest
            )
            head = storage.client.head_object(
                Bucket=owner.bucket,
                Key=f"projects/{version.project_id}/datasets/{version.id}/media/{next(s for s in version.snapshot['samples'] if s.get('asset_reference'))['task_id']}",
            )
            assert head["Metadata"]["snapshot-digest"] == version.snapshot_digest
            partial_count += 1
            continue
        manifest = version.manifest
        assert (
            content_digest(
                {
                    key: value
                    for key, value in manifest.items()
                    if key != "content_digest"
                }
            )
            == manifest["content_digest"]
        )
        response = storage.client.get_object(
            Bucket=owner.bucket, Key=version.package_key
        )
        try:
            package = response["Body"].read(64 * 1024 * 1024 + 1)
        finally:
            response["Body"].close()
        assert (
            len(package) <= 64 * 1024 * 1024
            and hashlib.sha256(package).hexdigest() == version.package_sha256
        )
        with tempfile.TemporaryDirectory(prefix="aap-recovery-native-") as directory:
            from training_executor.files import extract_dataset

            archive = Path(directory) / "dataset.zip"
            archive.write_bytes(package)
            extract_dataset(archive, Path(directory) / "dataset")
            folder = Path(directory) / "dataset"
            with zipfile.ZipFile(io.BytesIO(package)) as bundle:
                assert json.loads(bundle.read("manifest.json")) == manifest
            validate_dataset(
                {
                    "data_yaml": str(folder / "data.yaml"),
                    "frozen_config": {
                        "classes": manifest["classes"],
                        "task": manifest["task"],
                        "adapter_revision": 2,
                        "data_contract": contract_of(manifest),
                    },
                }
            )
            frozen = {
                sample["task_id"]: sample for sample in version.snapshot["samples"]
            }
            for member in manifest["samples"]:
                assert member.get("source_lineage") == frozen[member["task_id"]].get(
                    "source_lineage"
                )
                media = read_media(
                    version.project_id, version, SimpleNamespace(**member)
                )
                try:
                    assert (
                        hashlib.sha256(media[0].read()).hexdigest() == member["sha256"]
                    )
                finally:
                    media[0].close()
                media_count += 1
        ready_count += 1
    assert (ready_count, partial_count, unavailable, media_count) == (1, 1, 2, 3)
    assert replacements_rejected == 2
    return {
        **auxiliary,
        **avatars,
        **exports,
        **image_exports,
        **bug_attachments,
        **comment_attachments,
        **guide_assets,
        **raster_objects,
        **task_originals,
        **auxiliary_originals,
        **audit_archives,
        "pinned_training_versions": ready_count + partial_count,
        "pinned_training_deletions": deletion_count,
        "pinned_training_runs": run_count,
        "verified_run_artifacts": run_artifact_count,
        "ready_versions": ready_count,
        "interrupted_versions": partial_count,
        "unavailable_external_originals": unavailable,
        "same_name_replacements_rejected": replacements_rejected,
        "verified_frozen_media": media_count,
        "verified_dataset_task_content": 1,
        "current_original_bucket_fallback_used": False,
        "managed_original_assertions": len(retirements),
        "preserved_original_creations": len(creation_rows),
        "source_creation_receipts_remapped": False,
        "preserved_original_verifications": len(old_probes),
        "restored_observation_context_matches": False,
        "target_original_verification": target_probe.outcome,
        "declared_retention_holds": len(holds),
        "active_declared_retention_holds": sum(
            hold.released_at is None for hold in holds
        ),
        "queue_resume": "held-for-review",
    }


async def run(options):
    if not __debug__:
        raise WorktreeError("恢复演练拒绝Python优化模式，必须保留全部校验")
    from sqlalchemy import text
    from app.config import settings
    from app.db.base import async_session, engine
    from worktree_env import load_identity, topology
    from worktree_resources import LocalResources
    from worktree_runtime import validate_inventory

    root = Path(options.root).resolve()
    if (
        os.environ.get("AAP_WORKTREE_MODE") != "test"
        or not root.parent.name.startswith("aap-recovery-")
        or root.name not in {"source", "target"}
    ):
        raise WorktreeError("只允许恢复演练本次新建的随机test资源")
    resources = topology(root, load_identity(root), "test")
    backend = LocalResources(resources, settings, code_root=ROOT)
    validate_inventory(resources, await asyncio.to_thread(backend.inspect))
    try:
        async with async_session() as db:
            if (
                await db.scalar(text("SELECT current_database()"))
                != resources["database"]
            ):
                raise WorktreeError("演练数据库与资源身份不一致")
            if options.action == "seed":
                if root.name != "source":
                    raise WorktreeError("只能向新建源写入合成样本")
                if await db.scalar(text("SELECT count(*) FROM users")):
                    raise WorktreeError("拒绝向非空演练源写入样本")
                result = await seed(db, Path(options.external))
            elif options.action == "inspect":
                result = await inspect_database(db)
            else:
                document = json.loads(Path(options.expected).read_text())
                expected = document.get("facts", document)
                if (await inspect_database(db)) != {
                    key: expected[key]
                    for key in (
                        "full_database",
                        "protected_database",
                        "versions",
                        "unfinished_jobs",
                    )
                }:
                    raise WorktreeError("恢复数据库与原始备份不一致")
                if root.name != "target":
                    raise WorktreeError("只允许变换恢复目标库")
                ledger = await remap_storage(
                    db,
                    json.loads(options.source_buckets),
                    resources["buckets"],
                    backend.s3.meta.endpoint_url,
                )
                if (
                    await database_facts(db, protected=True)
                    != expected["protected_database"]
                ):
                    raise WorktreeError("恢复映射修改了白名单以外的业务事实")
                result = await verify_application(db, Path(options.external))
                await db.commit()
                result["mapping_ledger"] = ledger
            atomic_json(Path(options.output), result)
    finally:
        await engine.dispose()
        backend.s3.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seed", "inspect", "verify"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--external", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected")
    parser.add_argument("--source-buckets")
    asyncio.run(run(parser.parse_args()))
