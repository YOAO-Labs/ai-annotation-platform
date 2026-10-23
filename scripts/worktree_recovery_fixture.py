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
}


def protected_row(table, row):
    """Exclude only permitted physical fields, retaining nested owner evidence."""
    result = {
        key: value
        for key, value in row.items()
        if key not in MAPPED_COLUMNS.get(table, set())
    }
    if (
        table in {"training_dataset_versions", "training_storage_deletions"}
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
        async for row in result.scalars():
            if len(rows) >= 10000:
                raise WorktreeError("演练数据库单表超过10000行预算")
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
                db, project, TrainingDatasetSelection(), actor_id=actor.id
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
    assert len(creation_rows) == 2
    assert all(row.status == "verified" for row in creation_rows)
    # Restoration maps physical locations, never historical successful writes.
    assert all(row.bucket != storage.datasets_bucket for row in creation_rows)
    from app.services import managed_original_creation as creations

    for creation in creation_rows:
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
    actor = await db.scalar(select(User).where(User.role == "super_admin"))
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

    deletion_count = 0
    for intent in await db.scalars(select(TrainingStorageDeletion)):
        owner = resolve_ref(intent.storage_ref, expected_prefix=intent.prefix)
        assert intent.bucket == owner.bucket == storage.training_bucket
        objects = storage.client.list_objects_v2(
            Bucket=owner.bucket, Prefix=owner.prefix
        )
        assert len(objects.get("Contents", [])) == 1
        deletion_count += 1
    assert deletion_count == 1
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
        "pinned_training_versions": ready_count + partial_count,
        "pinned_training_deletions": deletion_count,
        "ready_versions": ready_count,
        "interrupted_versions": partial_count,
        "unavailable_external_originals": unavailable,
        "same_name_replacements_rejected": replacements_rejected,
        "verified_frozen_media": media_count,
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
