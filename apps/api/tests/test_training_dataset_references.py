"""Live directory/S3 freezing, source mutation and retained retry evidence."""

import copy
import hashlib
import io
import json
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services import training_datasets as service
from app.services.dataset import DatasetService
from app.services.dataset_source_access import capture_dataset_write
from app.services.storage import storage_service
from app.workers import training_dataset as worker
from tests.factory import create_project
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_source_changes import accept_all_files, picture, rescan
from tests.test_dataset_source_training_immutability import object_bytes, frozen_objects


@pytest.fixture
async def reference_training(db_session, content_world, monkeypatch):
    db, world = db_session, content_world
    (world[0] / "blue.png").write_bytes(picture("blue"))
    world = await rescan(db, world)
    await accept_all_files(db, world)
    project = await create_project(db, owner_id=world[2].id)
    project.tool_bindings = {
        **project.tool_bindings,
        "region": {"enabled": True, "classes": ["car"]},
    }
    authority = await capture_dataset_write(db, world[3].id, world[2], None)
    await DatasetService(db).link_project(
        world[3].id, project.id, directory_authority=authority
    )
    tasks = list(
        await db.scalars(
            select(Task).where(Task.project_id == project.id).order_by(Task.id)
        )
    )
    for task in tasks:
        task.status = "completed"
        task.reviewed_at = datetime.now(timezone.utc)
        task.is_labeled = True
        for tool, geometry in [
            ("bbox", {"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}),
            (
                "region",
                {"type": "polygon", "points": [[0.1, 0.1], [0.8, 0.1], [0.8, 0.8]]},
            ),
        ]:
            db.add(
                Annotation(
                    task_id=task.id,
                    project_id=project.id,
                    user_id=world[2].id,
                    class_name="car",
                    tool_unit_id=tool,
                    geometry=geometry,
                )
            )
    await db.commit()

    async def no_dispatch(*args):
        pass

    monkeypatch.setattr(service, "dispatch_dataset", no_dispatch)
    cleanup_prefix = f"projects/{project.id}/datasets/"
    try:
        yield world, project, tasks
    finally:
        await db.rollback()
        storage_service.delete_prefix(
            cleanup_prefix, bucket=storage_service.training_bucket
        )


async def create_version(db, client, auth, project, selection=None):
    url = f"/api/v1/projects/{project.id}/training-datasets"
    report = await client.post(url + ":preflight", headers=auth, json=selection or {})
    assert report.status_code == 200 and report.json()["ready"], report.text
    response = await client.post(
        url,
        headers=auth,
        json={
            **(selection or {}),
            "name": "Reference freeze",
            "request_id": str(uuid.uuid4()),
            "preflight_digest": report.json()["digest"],
        },
    )
    assert response.status_code == 202, response.text
    return await db.get(
        TrainingDatasetVersion, uuid.UUID(response.json()["id"])
    ), report.json()


async def build(db, version, node="worker:content@test", *, execution_lock=True):
    @asynccontextmanager
    async def sessions():
        yield db

    execute = worker.build_dataset if execution_lock else worker._build_dataset
    await execute(
        version.id, version.async_job_id, version.attempt_id, sessions, node_id=node
    )
    await db.refresh(version)


@pytest.mark.parametrize(
    "selection",
    [
        {},
        {"task": "segment"},
        {"task": "segment", "bridge": "ultralytics-instance-rle-1"},
    ],
)
async def test_reference_freeze_pins_labels_and_retains_originals_after_source_removal(
    db_session,
    reference_training,
    httpx_client,
    super_admin,
    selection,
    tmp_path,
    monkeypatch,
):
    db, (world, project, _) = db_session, reference_training
    project_id = project.id
    if selection.get("bridge"):
        from app.services.raster_mask_storage import (
            build_rle_reference,
            store_mask_reference_objects,
        )
        from tests.test_training_instance_rle import rle

        mask = rle(
            [
                [
                    int(4 <= x < 20 and 4 <= y < 20 and (x < 9 or y < 9))
                    for x in range(48)
                ]
                for y in range(32)
            ]
        )
        annotations = list(
            await db.scalars(
                select(Annotation)
                .where(
                    Annotation.project_id == project_id,
                    Annotation.tool_unit_id == "region",
                )
                .order_by(Annotation.task_id, Annotation.id)
            )
        )
        await db.execute(
            select(Task.id)
            .where(Task.id.in_({annotation.task_id for annotation in annotations}))
            .order_by(Task.id)
            .with_for_update(nowait=True)
        )
        reference = build_rle_reference(mask)
        for annotation in annotations:
            geometry = {"type": "raster_mask", "mask": reference}
            await store_mask_reference_objects(
                db, geometry, [(reference, mask)], task_id=annotation.task_id
            )
            annotation.geometry = geometry
        await db.commit()
    version, report = await create_version(
        db, httpx_client, headers(super_admin), project, selection
    )
    original_snapshot = copy.deepcopy(version.snapshot)
    assert report["summary"]["reference_samples"] == 2
    assert report["summary"]["original_copy_bytes"] == sum(
        p.stat().st_size for p in world[0].iterdir()
    )
    assert all(
        "source_bucket" not in s and s["asset_reference"]["schema"] == 1
        for s in version.snapshot["samples"]
    )
    annotations = list(
        await db.scalars(select(Annotation).where(Annotation.project_id == project.id))
    )
    for annotation in annotations:
        annotation.geometry = {"type": "bbox", "x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2}
        annotation.version += 1
    await db.commit()
    await build(db, version)
    assert version.status == "ready", version.error_message
    assert version.snapshot == original_snapshot
    assert version.manifest["asset_source_contract"]["schema"] == 1
    assert version.manifest["source_lineage_contract"] == {
        "schema": 1,
        "scope": "accepted-reference",
    }
    package = object_bytes(storage_service.training_bucket, version.package_key)
    assert hashlib.sha256(package).hexdigest() == version.package_sha256
    snapshot_samples = {s["task_id"]: s for s in original_snapshot["samples"]}
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        archive.extractall(tmp_path)
        assert json.loads(archive.read("manifest.json")) == version.manifest
        for sample in version.manifest["samples"]:
            assert (
                hashlib.sha256(archive.read(sample["image_path"])).hexdigest()
                == sample["asset_reference"]["sha256"]
            )
            assert (
                sample["asset_reference"]
                == snapshot_samples[sample["task_id"]]["asset_reference"]
            )
            assert (
                sample["source_lineage"]
                == snapshot_samples[sample["task_id"]]["source_lineage"]
            )
            assert sample["source_lineage"]["source_id"] == str(world[5].id)
            assert sample["source_lineage"]["accepted_revision_id"]
            assert sample["source_lineage"]["content_verified_at"]
            if selection.get("bridge"):
                label = json.loads(archive.read(sample["label_path"]))
                assert label["instances"][0]["source_geometry"] == "raster_mask"
                assert label["instances"][0]["rle"] == mask
            elif not selection:
                assert list(
                    map(float, archive.read(sample["label_path"]).decode().split())
                ) == [0, 0.25, 0.4, 0.3, 0.4]
    from pathlib import Path
    from app.services.training_dataset_bridge import contract_of

    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[2] / "training-executor")
    )
    from training_executor.adapters import validate_dataset

    validate_dataset(
        {
            "data_yaml": str(tmp_path / "data.yaml"),
            "frozen_config": {
                "classes": original_snapshot["classes"],
                "task": original_snapshot["task"],
                "adapter_revision": 2,
                "data_contract": contract_of(original_snapshot),
            },
        }
    )
    assert str(world[0]) not in json.dumps(version.manifest)
    retained = frozen_objects(f"projects/{project_id}/datasets/{version.id}/")
    for path in world[0].iterdir():
        path.unlink()
    world[5].enabled = False
    await db.commit()
    await build(db, version)  # Completed delivery is idempotent even with no source.
    assert version.status == "ready" and version.snapshot == original_snapshot
    assert frozen_objects(f"projects/{project_id}/datasets/{version.id}/") == retained
    dataset_id = original_snapshot["samples"][0]["dataset_id"]
    unlinked = await httpx_client.delete(
        f"/api/v1/datasets/{dataset_id}/link/{project_id}", headers=headers(super_admin)
    )
    assert unlinked.status_code == 200, unlinked.text
    deleted = await httpx_client.delete(
        f"/api/v1/datasets/{dataset_id}", headers=headers(super_admin)
    )
    assert deleted.status_code == 204, deleted.text
    assert frozen_objects(f"projects/{project_id}/datasets/{version.id}/") == retained
    for sample in version.manifest["samples"]:
        response = await httpx_client.get(
            f"/api/v1/projects/{project_id}/training-datasets/{version.id}/insights/samples/{sample['task_id']}/media?preview=false",
            headers=headers(super_admin),
        )
        assert response.status_code == 200, response.text
        assert hashlib.sha256(response.content).hexdigest() == sample["sha256"]


@pytest.mark.parametrize("fault", ["overwrite", "offline", "unverified_worker"])
async def test_reference_changed_before_materialization_cannot_publish(
    db_session, reference_training, httpx_client, super_admin, fault
):
    world, project, _ = reference_training
    project_id = project.id
    version, _ = await create_version(
        db_session, httpx_client, headers(super_admin), project
    )
    frozen_digest = version.snapshot_digest
    if fault == "overwrite":
        for path in world[0].iterdir():
            path.write_bytes(picture("green"))
    elif fault == "offline":
        world[5].enabled = False
        await db_session.commit()
    await build(
        db_session,
        version,
        "worker:unknown@test"
        if fault == "unverified_worker"
        else "worker:content@test",
    )
    assert version.status == "failed" and not version.package_key
    assert version.snapshot_digest == frozen_digest
    assert version.error_code != "build_failed", version.error_message
    download = await httpx_client.get(
        f"/api/v1/projects/{project_id}/training-datasets/{version.id}/download",
        headers=headers(super_admin),
    )
    assert download.status_code == 409


@pytest.mark.parametrize("stop", ["failure", "cancel"])
async def test_reference_partial_freeze_retries_same_snapshot_without_rereading_retained_source(
    db_session, reference_training, httpx_client, super_admin, monkeypatch, stop
):
    from app.db.models.dataset import DatasetItem

    db, (world, project, _) = db_session, reference_training
    project_id = project.id
    version, _ = await create_version(db, httpx_client, headers(super_admin), project)
    version_id, digest, snapshot = (
        version.id,
        version.snapshot_digest,
        copy.deepcopy(version.snapshot),
    )
    real = worker.materialize_sample
    completed = []

    async def interrupted(*args):
        if stop == "failure" and completed:
            raise RuntimeError("interrupt the second sample")
        result = await real(*args)
        completed.append(result["task_id"])
        if stop == "cancel" and len(completed) == 1:
            version.status = "cancelled"
            await db.commit()
        return result

    monkeypatch.setattr(worker, "materialize_sample", interrupted)
    await build(db, version)
    assert version.status == ("failed" if stop == "failure" else "cancelled")
    first = next(s for s in snapshot["samples"] if s["task_id"] == completed[0])
    item = await db.get(DatasetItem, uuid.UUID(first["dataset_item_id"]))
    (world[0] / item.metadata_["storage_relative_path"]).unlink()
    monkeypatch.setattr(worker, "materialize_sample", real)
    retry = await httpx_client.post(
        f"/api/v1/projects/{project_id}/training-datasets/{version.id}/retry",
        headers=headers(super_admin),
    )
    assert retry.status_code == 202 and retry.json()["id"] == str(version_id), (
        retry.text
    )
    # The retry endpoint's xact lock lives in the fixture's outer transaction.
    # Execute on that same owned session rather than acquiring it on another one.
    await build(db, version, execution_lock=False)
    assert version.status == "ready", version.error_message
    assert (
        version.id == version_id
        and version.snapshot_digest == digest
        and version.snapshot == snapshot
    )
    assert len(frozen_objects(f"projects/{project_id}/datasets/{version.id}/")) == 3


async def test_reference_preflight_cannot_report_ready_after_source_changes(
    reference_training, httpx_client, super_admin
):
    world, project, _ = reference_training
    for path in world[0].iterdir():
        path.write_bytes(picture("green"))
    report = await httpx_client.post(
        f"/api/v1/projects/{project.id}/training-datasets:preflight",
        headers=headers(super_admin),
        json={},
    )
    assert report.status_code == 200 and not report.json()["ready"]
    assert len(report.json()["issues"]) == 2


@pytest.mark.parametrize("historical", [False, True])
async def test_materialization_does_not_recapture_frozen_or_historical_lineage(
    db_session, reference_training, httpx_client, super_admin, monkeypatch, historical
):
    from app.services import training_dataset_sources
    from app.services.training_dataset_bridge import content_digest

    db, (_, project, _) = db_session, reference_training
    auth = headers(super_admin)
    version, _ = await create_version(db, httpx_client, auth, project)
    snapshot = copy.deepcopy(version.snapshot)
    if historical:
        snapshot.pop("source_lineage_contract")
        for sample in snapshot["samples"]:
            sample.pop("source_lineage")
        version.snapshot = snapshot
        version.snapshot_digest = content_digest(snapshot)
        await db.commit()

    calls = []

    async def invalid_current_evidence(*args):
        calls.append(True)
        return {"locator": "must-never-enter-a-package"}

    monkeypatch.setattr(
        training_dataset_sources, "capture_source_lineage", invalid_current_evidence
    )
    report = await httpx_client.post(
        f"/api/v1/projects/{project.id}/training-datasets:preflight",
        headers=auth,
        json={},
    )
    assert report.status_code == 200 and not report.json()["ready"]
    assert calls  # New freezes validate newly captured facts.
    calls.clear()
    await build(db, version)
    assert version.status == "ready", version.error_message
    assert not calls  # Existing freezes need content authority, not new provenance.
    assert version.snapshot == snapshot
    assert version.manifest.get("source_lineage_contract") == snapshot.get(
        "source_lineage_contract"
    )
    by_task = {sample["task_id"]: sample for sample in snapshot["samples"]}
    for member in version.manifest["samples"]:
        assert member.get("source_lineage") == by_task[member["task_id"]].get(
            "source_lineage"
        )


async def test_relocation_changes_new_lineage_but_preserves_pending_and_ready_freezes(
    db_session, reference_training, httpx_client, super_admin
):
    from app.services import dataset_source_revisions as revisions
    from tests.test_dataset_source_relocations import moved_candidate

    db, (world, project, _) = db_session, reference_training
    auth = headers(super_admin)
    path = f"/api/v1/projects/{project.id}/training-datasets"
    old, report = await create_version(db, httpx_client, auth, project)
    repeated = await httpx_client.post(path + ":preflight", headers=auth, json={})
    assert repeated.status_code == 200 and repeated.json()["digest"] == report["digest"]
    frozen_snapshot = copy.deepcopy(old.snapshot)
    old_evidence = {
        sample["task_id"]: sample["source_lineage"]
        for sample in old.snapshot["samples"]
    }
    world, run, _, target, payload = await moved_candidate(db, world)
    await revisions.accept(
        db, run.id, target["path_digest"], payload, world[2], relocate=True
    )
    await db.commit()
    stale = await httpx_client.post(
        path,
        headers=auth,
        json={
            "name": "Stale provenance",
            "request_id": str(uuid.uuid4()),
            "preflight_digest": report["digest"],
        },
    )
    assert stale.status_code == 409, stale.text
    # Materializing an existing freeze may read the current authorized locator,
    # but neither its retained provenance nor content identity can be rewritten.
    await build(db, old)
    assert old.status == "ready", old.error_message
    assert old.snapshot == frozen_snapshot
    assert all(
        sample["source_lineage"] == old_evidence[sample["task_id"]]
        for sample in old.manifest["samples"]
    )
    new, _ = await create_version(db, httpx_client, auth, project)
    assert new.snapshot_digest != old.snapshot_digest
    await build(db, new)
    assert new.status == "ready", new.error_message
    compare = await httpx_client.get(
        path + f"/{new.id}/insights/compare?baseline_id={old.id}", headers=auth
    )
    assert compare.status_code == 200, compare.text
    assert compare.json()["counts"]["source_relation_changed"] == 1
    assert compare.json()["counts"]["unchanged"] == 2
    assert compare.json()["semantic_validation_changed"] is False
    assert compare.json()["evaluation_benchmark_changed"] is False
    task_id = next(iter(old_evidence))
    world[5].enabled = False
    await db.commit()
    detail = await httpx_client.get(
        path + f"/{old.id}/insights/samples/{task_id}", headers=auth
    )
    assert detail.status_code == 200, detail.text
    response_evidence = detail.json()["source_lineage"]["evidence"]
    for key, value in old_evidence[task_id].items():
        if key in {"accepted_at", "scan_completed_at", "content_verified_at"} and value:
            assert datetime.fromisoformat(
                response_evidence[key]
            ) == datetime.fromisoformat(value)
        else:
            assert response_evidence[key] == value
    assert old.snapshot == frozen_snapshot


async def test_reference_retained_media_must_match_asset_even_if_metadata_is_rewritten(
    db_session, reference_training, httpx_client, super_admin
):
    world, project, _ = reference_training
    version, _ = await create_version(
        db_session, httpx_client, headers(super_admin), project
    )
    sample = version.snapshot["samples"][0]
    replacement = picture("green")
    storage_service.client.put_object(
        Bucket=storage_service.training_bucket,
        Key=f"projects/{project.id}/datasets/{version.id}/media/{sample['task_id']}",
        Body=replacement,
        Metadata={
            "snapshot-digest": version.snapshot_digest,
            "sha256": hashlib.sha256(replacement).hexdigest(),
        },
    )
    await build(db_session, version)
    assert version.status == "failed" and version.error_code in {
        "source_media_changed",
        "image_size_limit",
    }
    assert not version.package_key


async def test_reference_source_rewrite_during_running_build_fails(
    db_session, reference_training, httpx_client, super_admin, monkeypatch
):
    from app.services import dataset_references

    world, project, _ = reference_training
    version, _ = await create_version(
        db_session, httpx_client, headers(super_admin), project
    )
    capture = dataset_references.snapshots.capture
    started = False

    async def overwrite_then_read(*args, **kwargs):
        nonlocal started
        started = True
        assert version.status == "running"
        for path in world[0].iterdir():
            path.write_bytes(picture("green"))
        return await capture(*args, **kwargs)

    monkeypatch.setattr(dataset_references.snapshots, "capture", overwrite_then_read)
    await build(db_session, version)
    assert started and version.status == "failed" and not version.package_key


async def test_reference_dataset_shared_by_projects_keeps_labels_and_authority_separate(
    db_session, reference_training, httpx_client, super_admin, project_admin
):
    db, (world, first, _) = db_session, reference_training
    second = await create_project(db, owner_id=project_admin[0].id)
    second.tool_bindings = {"bbox": {"enabled": True, "classes": ["truck"]}}
    authority = await capture_dataset_write(db, world[3].id, world[2], None)
    await DatasetService(db).link_project(
        world[3].id, second.id, directory_authority=authority
    )
    for task in await db.scalars(select(Task).where(Task.project_id == second.id)):
        task.status, task.reviewed_at = "completed", datetime.now(timezone.utc)
        db.add(
            Annotation(
                task_id=task.id,
                project_id=second.id,
                user_id=project_admin[0].id,
                class_name="truck",
                tool_unit_id="bbox",
                geometry={"type": "bbox", "x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2},
            )
        )
    await db.commit()
    first_id, second_id = first.id, second.id
    first_version, _ = await create_version(
        db, httpx_client, headers(super_admin), first
    )
    second_version, _ = await create_version(
        db, httpx_client, headers(project_admin), second
    )
    cleanup_prefix = f"projects/{second_id}/datasets/"
    try:
        await build(db, first_version)
        await build(db, second_version)
        await db.refresh(first_version)
        assert first_version.status == second_version.status == "ready"
        assert first_version.snapshot["classes"] == ["car", "person"]
        assert second_version.snapshot["classes"] == ["truck"]
        assert {
            a["class_name"]
            for s in second_version.snapshot["samples"]
            for a in s["annotations"]
        } == {"truck"}
        denied = await httpx_client.post(
            f"/api/v1/projects/{first_id}/training-datasets:preflight",
            headers=headers(project_admin),
            json={},
        )
        assert denied.status_code in {403, 404}
    finally:
        storage_service.delete_prefix(
            cleanup_prefix, bucket=storage_service.training_bucket
        )


@pytest.mark.parametrize("revoke", ["actor", "key", "scopes"])
async def test_reference_authority_revoked_during_read_cannot_publish(
    db_session, reference_training, httpx_client, super_admin, monkeypatch, revoke
):
    from app.services.api_key_service import create_key
    from app.services import dataset_references

    db, (world, project, _) = db_session, reference_training
    key, token = await create_key(
        db, world[2], "Reference freeze", ["datasets:read", "annotations:read"]
    )
    await db.commit()
    version, _ = await create_version(
        db, httpx_client, {"Authorization": f"Bearer {token}"}, project
    )
    original = dataset_references.snapshots.capture

    async def revoke_after_io(*args, **kwargs):
        snapshot = await original(*args, **kwargs)
        if revoke == "actor":
            world[2].is_active = False
        elif revoke == "key":
            key.revoked_at = datetime.now(timezone.utc)
        else:
            key.scopes = ["datasets:read"]
        await db.flush()
        return snapshot

    monkeypatch.setattr(dataset_references.snapshots, "capture", revoke_after_io)
    await build(db, version)
    assert version.status == "failed" and not version.package_key


@pytest.mark.parametrize("overwrite", [False, True])
async def test_real_s3_reference_source_enters_training_freeze(
    db_session, super_admin, httpx_client, monkeypatch, overwrite
):
    from app.config import settings
    from cryptography.fernet import Fernet
    from app.core.crypto import encrypt_secret
    from app.db.models.dataset import Dataset
    from app.db.models.dataset_source import DatasetScanEntry
    from app.db.models.storage_connection import StorageConnection
    from app.schemas.dataset_source import DatasetSourceCreate
    from app.services import connector_guard, dataset_scans, storage_location
    from app.services.dataset_references import register_entry
    from tests.test_s3_references import scan

    db, actor = db_session, super_admin[0]
    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    # This harness owns a loopback-only disposable MinIO. Production guards
    # remain unchanged; permit just its local addresses in this fixture.
    blocked = connector_guard._is_hard_blocked
    monkeypatch.setattr(
        connector_guard,
        "_is_hard_blocked",
        lambda ip: False if str(ip) in {"127.0.0.1", "::1"} else blocked(ip),
    )
    prefix = f"reference-training-{uuid.uuid4()}/"
    project_prefix = None

    async def allowlist(*args):
        from urllib.parse import urlsplit

        endpoint = settings.minio_endpoint
        return [
            "127.0.0.1/32",
            "::1/128",
            urlsplit(endpoint if "://" in endpoint else "http://" + endpoint).hostname,
        ]

    monkeypatch.setattr(connector_guard, "get_allowlist", allowlist)
    dataset = Dataset(
        display_id=f"s3-{uuid.uuid4().hex[:12]}",
        name="Real S3 freeze",
        created_by=actor.id,
    )
    connection = StorageConnection(
        name=f"s3-training-{uuid.uuid4()}",
        kind="s3",
        scope="owner",
        created_by=actor.id,
        config={
            "endpoint": settings.minio_endpoint,
            "use_ssl": settings.minio_use_ssl,
            "bucket": storage_service.datasets_bucket,
            "base_prefix": prefix,
        },
        secret_enc=encrypt_secret(
            {
                "access_key": settings.minio_access_key,
                "secret_key": settings.minio_secret_key,
            }
        ),
    )
    db.add_all([dataset, connection])
    await db.flush()
    try:
        for name, color in [("图片 +%2F.png", "red"), ("blue.png", "blue")]:
            storage_service.client.put_object(
                Bucket=storage_service.datasets_bucket,
                Key=prefix + "collection/" + name,
                Body=picture(color),
            )
        location = await storage_location.register_connection(db, connection)
        for node in [storage_location.api_node_id(), "worker:content@test"]:
            probe = await storage_location.perform_probe(
                db, location, node, "collection/blue.png"
            )
            assert probe["ok"], probe
            await storage_location.store_probe(
                db, location, node, probe, datetime.now(timezone.utc)
            )
        source = await dataset_scans.create_source(
            db,
            DatasetSourceCreate(
                dataset_id=dataset.id,
                location_id=location.id,
                name="Real S3",
                relative_path="collection",
                node_id="content@test",
                cache_policy="none",
            ),
            actor,
        )
        run = await scan(db, (None, actor, dataset, location, source))
        for entry in await db.scalars(
            select(DatasetScanEntry).where(
                DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "file"
            )
        ):
            await register_entry(db, run.id, entry.path_digest, actor)
        project = await create_project(db, owner_id=actor.id)
        project_prefix = f"projects/{project.id}/datasets/"
        authority = await capture_dataset_write(db, dataset.id, actor, None)
        await DatasetService(db).link_project(
            dataset.id, project.id, directory_authority=authority
        )
        for task in await db.scalars(select(Task).where(Task.project_id == project.id)):
            task.status, task.reviewed_at = "completed", datetime.now(timezone.utc)
            db.add(
                Annotation(
                    task_id=task.id,
                    project_id=project.id,
                    user_id=actor.id,
                    class_name="car",
                    tool_unit_id="bbox",
                    geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                )
            )
        await db.commit()

        async def no_dispatch(*args):
            pass

        monkeypatch.setattr(service, "dispatch_dataset", no_dispatch)
        version, _ = await create_version(
            db, httpx_client, headers(super_admin), project
        )
        if overwrite:
            for name in ["图片 +%2F.png", "blue.png"]:
                storage_service.client.put_object(
                    Bucket=storage_service.datasets_bucket,
                    Key=prefix + "collection/" + name,
                    Body=picture("green"),
                )
        await build(db, version)
        assert version.status == ("failed" if overwrite else "ready"), (
            version.error_message
        )
        if not overwrite:
            for sample in version.manifest["samples"]:
                assert sample["sha256"] == sample["asset_reference"]["sha256"]
    finally:
        storage_service.delete_prefix(prefix, bucket=storage_service.datasets_bucket)
        if project_prefix:
            storage_service.delete_prefix(
                project_prefix, bucket=storage_service.training_bucket
            )


async def test_reference_retained_retry_rechecks_key_before_publication(
    db_session, reference_training, httpx_client, monkeypatch
):
    from app.services.api_key_service import create_key

    db, (world, project, _) = db_session, reference_training
    key, token = await create_key(
        db, world[2], "Retained retry", ["datasets:read", "annotations:read"]
    )
    await db.commit()
    version, _ = await create_version(
        db, httpx_client, {"Authorization": f"Bearer {token}"}, project
    )
    package = worker.build_training_package

    def interrupted(*args, **kwargs):
        raise RuntimeError("stop after every media object is retained")

    monkeypatch.setattr(worker, "build_training_package", interrupted)
    await build(db, version)
    assert version.status == "failed"
    for path in world[0].iterdir():
        path.unlink()
    monkeypatch.setattr(worker, "build_training_package", package)
    original = worker.materialize_sample

    async def revoke_after_retained(*args):
        result = await original(*args)
        key.revoked_at = datetime.now(timezone.utc)
        await db.flush()
        return result

    monkeypatch.setattr(worker, "materialize_sample", revoke_after_retained)
    await service.enqueue_dataset(db, version)
    await db.commit()
    await build(db, version)
    assert version.status == "failed" and version.error_code == "api_key_revoked"
    assert not version.package_key
