"""Reference export identity, portable paths and publication boundaries."""

from contextlib import asynccontextmanager, nullcontext
import hashlib
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import insert, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.db.models.annotation import Annotation
from app.db.models.api_key import ApiKey
from app.db.models.dataset import DatasetItem
from app.db.models.async_job import AsyncJob
from app.db.models.export_artifact import ExportArtifact
from app.db.models.export_media_bundle import ExportMediaBundle
from app.db.models.export_output import ExportOutput
from app.core.security import hash_password
from app.db.models.task import Task
from app.schemas.async_job import AsyncJobOut
from app.services import (
    dataset_references,
    export_media_bundles,
    export_media_storage,
    export_media_transfer,
    identity_request_authority,
    managed_original_creation,
    managed_original_reader,
)
from app.services.content_snapshot import ContentError
from app.services.exporting import image_delivery, image_jobs, output_authority
from tests.test_export_outputs import export_objects as export_objects
from app.services.exporting.packaging import (
    IMAGE_EXPORT_TARGETS,
    _FETCH_IMAGES_TEMPLATE,
    build_export_zip,
)
from app.services.exporting.reference_media import (
    ExportCancelled,
    ReferenceExportMedia,
    authorize_export_job,
    capture_export_authority,
)
from app.services.raster_mask_storage import (
    build_rle_reference,
    store_mask_reference_objects,
)
from app.services.storage import storage_service
from app.utils.raster_mask_rle import encode_coco_rle
from app.workers import export as worker
from tests.factory import create_task, create_batch, create_membership, create_project
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_references import reference_world as reference_world
from tests.test_s3_references import s3_world as s3_world
from tests.test_sftp_references import sftp_world as sftp_world
from tests.test_export_media_storage import Client as MediaClient
from tests.managed_original_client_support import Client as CreationClient
from tests.test_managed_original_reader import Provider
from tests.test_reference_original_cache import referenced_cache as referenced_cache
from tests.test_image_export_inputs import registered_dataset_image
from tests.test_training_dataset_references import (
    committed_rle_bindings as committed_rle_bindings,
)

_EXPORT_TEST_KEY = "ak_" + "export-fixture-" + "a" * 32


@pytest.fixture(autouse=True)
def _require_owned_rle_bindings(request):
    if (
        request.node.originalname
        == "test_all_async_image_formats_embed_verified_bytes_and_share_paths"
    ):
        request.getfixturevalue("committed_rle_bindings")


async def make_export(
    db, world, *, actor=None, key_id=None, batch_id=None, selected=None
):
    _, project, _, content = world
    actor = actor or content[2]
    key = await db.get(ApiKey, key_id) if key_id else None
    principal = None
    if key:
        from app.services.identity_gateway import get_identity_gateway

        principal = await get_identity_gateway().authenticate(_EXPORT_TEST_KEY, db=db)
        assert principal.actor_id == actor.id and principal.credential_id == key.id
        # Match the request admission boundary: accepted usage is committed.
        await db.commit()
    request = SimpleNamespace(
        state=SimpleNamespace(
            api_key_id=key_id,
            api_key_scopes=list(principal.scopes) if principal else None,
            identity_principal=principal,
        ),
        headers={"authorization": f"Bearer {_EXPORT_TEST_KEY}"} if key else {},
    )
    payload = {
        "output_authority": await output_authority.capture(db, project, actor, request)
    }
    proof = await capture_export_authority(
        db,
        project,
        actor,
        request,
        batch_id=batch_id,
        task_ids=selected,
    )
    if proof:
        payload["export_authority"] = proof
    if selected is not None:
        payload["task_ids"] = [str(v) for v in selected]
    job = AsyncJob(
        kind="export",
        project_id=project.id,
        user_id=actor.id,
        status="running",
        payload=payload,
        result={},
    )
    db.add(job)
    await db.flush()
    media = (
        ReferenceExportMedia(
            db,
            job.id,
            proof,
            project_id=project.id,
            batch_id=batch_id,
            task_ids=selected,
            node_id="worker:content@test",
        )
        if proof
        else None
    )
    if media:
        await media.validate()
    return job, media


async def make_image_export(db, world, *, targets, opts):
    """Bind the real frozen image sources before the Job is born."""
    _, project, _, content = world
    actor = content[2]
    request = SimpleNamespace(
        state=SimpleNamespace(
            api_key_id=None, api_key_scopes=None, identity_principal=None
        ),
        headers={},
    )
    payload = {
        "output_authority": await output_authority.capture(db, project, actor, request),
        "targets": targets,
        "batch_id": None,
        **opts,
    }
    job, receipt = await image_jobs.create_scoped_job(
        db,
        project,
        actor_id=actor.id,
        payload=payload,
        batch_id=None,
        targets=targets,
        opts=opts,
    )
    proof = await capture_export_authority(
        db,
        project,
        actor,
        request,
        frozen_snapshot=receipt.input_snapshot,
    )
    media = None
    if proof:
        job.payload = {**job.payload, "export_authority": proof}
        media = ReferenceExportMedia(
            db,
            job.id,
            proof,
            project_id=project.id,
            batch_id=None,
            task_ids=None,
            node_id="worker:content@test",
        )
        await media.validate()
    await db.commit()
    return job, media, receipt


@pytest.fixture
def borrowed_image_business_sessions(db_session, monkeypatch):
    """Keep physical/authority algorithms real inside the outer test rollback.

    These cases do not qualify independent transaction visibility or lock-free
    physical work. Those boundaries belong to the committed worker/transfer
    suites and the disposable runtime acceptance probe.
    """

    @asynccontextmanager
    async def borrowed(db):
        assert db is db_session
        yield db_session

    monkeypatch.setattr(export_media_transfer, "_session", borrowed)


@pytest.fixture
def image_transport(monkeypatch):
    """Exercise actual Creation/reader/copy algorithms against isolated providers."""
    creation, source, copy = CreationClient(), Provider(), MediaClient()
    source.meta.endpoint_url = export_media_storage._runtime_endpoint()
    monkeypatch.setattr(
        managed_original_creation, "create_client", lambda **_: creation
    )
    monkeypatch.setattr(managed_original_reader, "create_client", lambda **_: source)
    monkeypatch.setattr(export_media_storage, "create_client", lambda: copy)
    return SimpleNamespace(creation=creation, source=source, copy=copy)


async def test_all_async_image_formats_embed_verified_bytes_and_share_paths(
    db_session,
    reference_world,
    monkeypatch,
    tmp_path,
    image_transport,
    borrowed_image_business_sessions,
):
    _, project, task, content = reference_world
    duplicate = Task(
        project_id=project.id,
        display_id=f"dup-{uuid.uuid4().hex[:12]}",
        dataset_item_id=task.dataset_item_id,
        file_name=task.file_name,
        file_path=task.file_path,
        file_type="image",
    )
    db_session.add(duplicate)
    managed = await create_task(db_session, project_id=project.id)
    await db_session.commit()
    registered = await registered_dataset_image(
        db_session, project=project, dataset=content[3], data=content[1], task=managed
    )
    image_transport.source.raw = image_transport.source.actual = content[1]
    rle = encode_coco_rle([1] + [0] * (48 * 32 - 1), 48, 32)
    mask_reference = build_rle_reference(rle)
    geometry = {"type": "raster_mask", "mask": mask_reference}
    await db_session.execute(
        select(Task.id).where(Task.id == task.id).with_for_update(nowait=True)
    )
    await store_mask_reference_objects(
        db_session, geometry, [(mask_reference, rle)], task_id=task.id
    )
    db_session.add(
        Annotation(
            project_id=project.id,
            task_id=task.id,
            user_id=content[2].id,
            class_name="car",
            geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        )
    )
    db_session.add(
        Annotation(
            project_id=project.id,
            task_id=task.id,
            user_id=content[2].id,
            tool_unit_id="region",
            annotation_type="raster_mask",
            class_name="car",
            geometry=geometry,
        )
    )
    await db_session.flush()
    targets = sorted(IMAGE_EXPORT_TARGETS - {"yolo"})
    opts = {"include_attributes": True, "video_frame_mode": "keyframes"}
    job, media, receipt = await make_image_export(
        db_session, reference_world, targets=targets, opts=opts
    )
    assert set(media.task_ids) == {task.id, duplicate.id, managed.id}
    receipt = await export_media_bundles.claim(db_session, receipt.id)
    job.status = "running"
    await db_session.commit()
    monkeypatch.setattr(
        storage_service,
        "generate_download_url",
        Mock(
            side_effect=AssertionError("current-bucket original signing is forbidden")
        ),
    )
    # Owned copies are signed through the saved transport. The fake provider
    # returns a placeholder, but the packaged fetcher validates that a managed
    # receipt carries a real URL, so emulate the signed link it would receive.
    monkeypatch.setattr(
        image_transport.copy,
        "generate_presigned_url",
        lambda operation, **_params: (
            "https://saved-export.invalid/copied-object?signature=receipt"
        ),
    )

    async def guard(owned):
        await worker._reauthorize_export_final_write(owned, job.id, None)
        await export_media_bundles.check_claim(owned, receipt)

    delivery = await image_delivery.prepare(db_session, receipt, guard=guard)
    # New tasks cannot appear between passes of the frozen export.
    late_task = await create_task(db_session, project_id=project.id)
    path, _, _ = await build_export_zip(
        db_session,
        project.id,
        batch_id=None,
        task_ids=media.task_ids,
        targets=targets,
        include_attributes=True,
        video_frame_mode="keyframes",
        reference_media=media,
        image_media=delivery,
    )
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("images_manifest.json"))
            entries = {row["task_id"]: row for row in manifest["images"]}
            assert str(late_task.id) not in entries
            assert (
                entries[str(task.id)]["archive_path"]
                != entries[str(duplicate.id)]["archive_path"]
            )
            for task_id in (task.id, duplicate.id):
                entry = entries[str(task_id)]
                assert entry["storage_mode"] == "embedded"
                assert "presigned_url" not in entry
                for name in entry["archive_paths"]:
                    assert archive.read(name) == content[1]
                assert entry["sha256"] == hashlib.sha256(content[1]).hexdigest()
                for target in ("yolo-det", "yolo-obb", "yolo-seg"):
                    assert (
                        f"{target}/labels/{os.path.splitext(entry['rel_path'])[0]}.txt"
                        in archive.namelist()
                    )
                assert any(
                    image["file_name"] == entry["archive_path"]
                    for image in json.loads(archive.read("coco/annotations.json"))[
                        "images"
                    ]
                )
            assert entries[str(managed.id)]["sha256"] == registered.sha256
            assert entries[str(managed.id)]["size_bytes"] == len(content[1])
            assert (
                entries[str(managed.id)]["expires_at"] == receipt.expires_at.isoformat()
            )
            inventory = receipt.inventory["objects"]
            assert len(inventory) == 1
            copied_key = next(iter(inventory))
            assert image_transport.copy.current(copied_key)["data"] == content[1]
            assert any(
                name == "get"
                and params["Bucket"] == registered.replica.bucket
                and params["Key"] == registered.item.file_path
                for name, params in image_transport.source.calls
            )
            aap = json.loads(archive.read("native_json/annotations.json"))
            assert any(
                row["file_path"] == media.archive_path(task.id) for row in aap["tasks"]
            )
            for target in ("binary-png", "indexed-png"):
                masks = json.loads(archive.read(f"{target}/manifest.json"))
                assert masks["items"][0]["media_path"] == media.archive_path(task.id)
            brushes = json.loads(archive.read("label-studio-brush/annotations.json"))
            assert any(
                row["data"]["image"] == media.archive_path(task.id) for row in brushes
            )
            archive.extractall(tmp_path / "package")
        exported_manifest = tmp_path / "package/images_manifest.json"
        for row in manifest["images"]:
            if row.get("presigned_url"):
                row["expires_at"] = "2000-01-01T00:00:00+00:00"
        exported_manifest.write_text(json.dumps(manifest))
        result = subprocess.run(
            [sys.executable, str(tmp_path / "package/fetch_images.py")],
            capture_output=True,
            text=True,
        )
        # Both embedded entries verify even though the fake managed URL is unreachable.
        assert result.stdout.count("[ok] embedded SHA-256") == 2
        assert "Invalid manifest" not in result.stdout
        assert result.returncode == 2
    finally:
        Path(path).unlink(missing_ok=True)


async def test_whole_project_reviewer_export_keeps_broader_capability(
    db_session,
    reference_world,
    reviewer,
):
    _, project, task, _ = reference_world
    await create_membership(
        db_session, project_id=project.id, user_id=reviewer[0].id, role="reviewer"
    )
    with pytest.raises(HTTPException):
        await dataset_references.reference_context(
            db_session, task.dataset_item_id, reviewer[0], task_id=task.id
        )
    _, media = await make_export(db_session, reference_world, actor=reviewer[0])
    snapshot = await media.snapshot(task)
    snapshot.close()
    with pytest.raises(ValueError, match="visible"):
        job, _ = await make_export(
            db_session, reference_world, actor=reviewer[0], selected=[task.id]
        )


@pytest.mark.parametrize(
    "mutation", ["key_rotate", "key_scope", "source_disable", "task_version", "cancel"]
)
async def test_original_authority_and_binding_rechecked_after_snapshot(
    referenced_cache,
    mutation,
    monkeypatch,
):
    from app.db.models.dataset_source import DatasetSource

    world = referenced_cache
    async with world.factory() as db:
        task = await db.get(Task, world.task.id)
        source = await db.get(DatasetSource, world.source_id)
        source.cache_policy = "none"
        content = list(world.content)
        content[5] = source
        key = ApiKey(
            user_id=content[2].id,
            name="export",
            key_prefix=_EXPORT_TEST_KEY[:12],
            key_hash=hash_password(_EXPORT_TEST_KEY),
            scopes=["tasks:read"],
        )
        db.add(key)
        await db.commit()
        job, media = await make_export(
            db, (None, world.project, task, content), key_id=key.id
        )
        original_capture = dataset_references.snapshots.capture
        snapshots = []

        async def capture_then_change(*args, **kwargs):
            snapshot = await original_capture(*args, **kwargs)
            snapshots.append(snapshot)
            if mutation == "key_rotate":
                key.key_hash = hash_password("ak_replacement-credential")
            elif mutation == "key_scope":
                key.scopes = []
            elif mutation == "source_disable":
                source.enabled = False
            elif mutation == "task_version":
                task.version += 1
            else:
                job.status = "cancelled"
            await db.flush()
            return snapshot

        monkeypatch.setattr(
            dataset_references.snapshots, "capture", capture_then_change
        )
        with pytest.raises((ContentError, HTTPException, ExportCancelled)):
            await media.snapshot(task)
        assert snapshots and snapshots[0]._closed


@pytest.mark.parametrize(
    "change",
    ["none", "before_capture", "before_auth", "during_auth", "after_auth", "scope"],
)
async def test_output_authority_fences_real_key_authentication(
    referenced_cache, test_engine, monkeypatch, change
):
    from yoao_identity.postgres import PostgresData
    from app.services.identity_gateway import IdentityGateway

    world = referenced_cache
    token = "ak_" + uuid.uuid4().hex
    original_hash = hash_password(token)
    key = ApiKey(
        user_id=world.manager.id,
        name="export-race",
        key_prefix=token[:12],
        key_hash=original_hash,
        scopes=["tasks:read"],
    )
    async with world.factory() as setup:
        setup.add(key)
        await setup.commit()
        key_id = key.id

    # Use the real function-only Identity owner with its independent committed
    # transactions; no credential or permission result is substituted.
    gateway = IdentityGateway(
        auth_database_url=test_engine.url.render_as_string(hide_password=False),
        timeout_seconds=10,
    )
    admitted = await gateway.authenticate(token)
    request = SimpleNamespace(
        state=SimpleNamespace(
            api_key_id=key_id,
            api_key_scopes=list(admitted.scopes),
            identity_principal=admitted,
        ),
        headers={"authorization": f"Bearer {token}"},
    )
    monkeypatch.setattr(
        identity_request_authority, "get_identity_gateway", lambda: gateway
    )

    async def change_key(**values):
        async with world.factory() as owner:
            await owner.execute(
                update(ApiKey).where(ApiKey.id == key_id).values(**values)
            )
            await owner.commit()

    replacement = hash_password("ak_" + uuid.uuid4().hex)
    if change == "before_capture":
        await change_key(key_hash=replacement)
    if change == "during_auth":
        confirm = PostgresData.confirm_api_key_principal

        async def rotate_before_confirmation(data, candidate_id, expected_hash):
            await change_key(key_hash=replacement)
            return await confirm(data, candidate_id, expected_hash)

        monkeypatch.setattr(
            PostgresData, "confirm_api_key_principal", rotate_before_confirmation
        )
    if change in {"before_auth", "after_auth", "scope"}:
        authenticate = gateway.authenticate

        async def authenticate_with_change(raw, *, db=None):
            if change == "before_auth":
                await change_key(key_hash=replacement)
            principal = await authenticate(raw, db=db)
            if change == "after_auth":
                # Same plaintext still authenticates, but its verifier revision
                # changed after confirmation and cannot become the proof.
                await change_key(key_hash=hash_password(token))
            elif change == "scope":
                await change_key(scopes=[])
            return principal

        monkeypatch.setattr(gateway, "authenticate", authenticate_with_change)

    async with world.factory() as caller:
        if change != "none":
            with pytest.raises(HTTPException) as rejected:
                await output_authority.capture(
                    caller, world.project, world.manager, request
                )
            assert rejected.value.status_code == 403
        else:
            proof = await output_authority.capture(
                caller, world.project, world.manager, request
            )
            assert (
                proof["key_proof"]["hash"]
                == hashlib.sha256(original_hash.encode()).hexdigest()
            )
            await output_authority.validate(
                caller,
                SimpleNamespace(
                    user_id=world.manager.id,
                    project_id=world.project.id,
                    payload={"output_authority": proof},
                ),
            )
            # The accepted key is still fenced until the request transaction
            # commits the export proof; another owner cannot rotate it.
            async with world.factory() as writer:
                with pytest.raises(DBAPIError) as busy:
                    await writer.execute(
                        select(ApiKey.id)
                        .where(ApiKey.id == key_id)
                        .with_for_update(nowait=True)
                    )
                assert getattr(busy.value.orig, "sqlstate", None) == "55P03"


async def test_batch_outside_reference_neither_read_nor_rejected(
    db_session, reference_world
):
    _, project, _, content = reference_world
    batch = await create_batch(db_session, project_id=project.id)
    managed = await create_task(db_session, project_id=project.id)
    managed.batch_id = batch.id
    await db_session.flush()
    job = AsyncJob(
        kind="export",
        project_id=project.id,
        user_id=content[2].id,
        payload={
            "output_authority": {
                "schema": 1,
                "actor_id": str(content[2].id),
                "project_id": str(project.id),
                "api_key_id": None,
                "key_proof": None,
            }
        },
        result={},
    )
    db_session.add(job)
    await db_session.flush()
    await authorize_export_job(
        db_session,
        project_id=project.id,
        batch_id=batch.id,
        task_ids=None,
        job_uuid=job.id,
    )
    await worker._reauthorize_export_final_write(
        db_session, job.id, None, batch_id=batch.id
    )
    with pytest.raises(ContentError, match="authority_missing"):
        await authorize_export_job(
            db_session, project_id=project.id, task_ids=None, job_uuid=job.id
        )


@pytest.mark.parametrize(
    "mutation", ["source_disable", "cancel", "cancel_failure", "failure"]
)
async def test_worker_refuses_publication_but_retains_pending_after_final_rejection(
    referenced_cache,
    test_engine,
    mutation,
    monkeypatch,
    export_objects,
):
    world = referenced_cache
    project_id, source_id = world.project.id, world.source_id
    async with world.factory() as db:
        # The source itself, not an optional original cache, supplies the image.
        from app.db.models.dataset_source import DatasetSource

        source = await db.get(DatasetSource, source_id)
        source.cache_policy = "none"
        await db.commit()
        job, _, receipt = await make_image_export(
            db,
            (None, world.project, world.task, world.content),
            targets=["coco"],
            opts={},
        )
        job_id = job.id

    # The storage provider here is simulated.  Do not manufacture native
    # completion evidence for it; the live worker test exercises that owner.
    class IsolatedExecution:
        def __init__(self, *_args):
            self.token = None
            self.fixture_token = None
            self.takeover = False
            self.unsettled = False
            self.entered = False
            self._active_io = 0
            self.lost = False

        @property
        def active_io(self):
            return self._active_io

        async def acquire(self, key):
            self.key = key

        async def begin(self, job_id, _project_id, delivery_id):
            assert self.key == f"job:{job_id}" and delivery_id == "test-export"

        async def enter_io(self):
            self._active_io += 1
            self.entered = True

        async def settle_io(self):
            self._active_io -= 1
            self.entered = self._active_io != 0

    monkeypatch.setattr(worker, "_ExportExecution", IsolatedExecution)
    monkeypatch.setattr(
        worker.export_execution_context,
        "database_url",
        lambda _settings: test_engine.url,
    )
    monkeypatch.setattr(
        worker,
        "create_async_engine",
        lambda *_args, **kwargs: create_async_engine(test_engine.url, **kwargs),
    )
    notification_types = []

    async def record_notification(_db, notification):
        if notification is not None:
            notification_types.append(notification.type)

    monkeypatch.setattr(worker, "_publish_export_notification", record_notification)
    uploaded, deleted = export_objects.puts, export_objects.deleted
    original_guard = worker._reauthorize_export_final_write

    async def guard(db, job_id, ids, **kwargs):
        # The owned lifecycle checks authority in several fresh sessions before
        # and after physical work. Reject only at the original final boundary,
        # after a real ZIP PUT, so this still exercises retained responsibility.
        if not isinstance(db.bind, AsyncConnection) or not uploaded:
            return await original_guard(db, job_id, ids, **kwargs)
        if mutation == "source_disable":
            from app.db.models.dataset_source import DatasetSource

            await db.execute(
                update(DatasetSource)
                .where(DatasetSource.id == source_id)
                .values(enabled=False)
            )
        elif mutation.startswith("cancel"):
            await db.execute(
                update(AsyncJob).where(AsyncJob.id == job_id).values(status="cancelled")
            )
        else:
            raise OSError("private source/credential should not escape")
        await db.flush()
        if mutation.startswith("cancel"):
            await db.commit()
            if mutation == "cancel_failure":
                raise OSError("cancelled while upload failed")
        await original_guard(db, job_id, ids, **kwargs)

    monkeypatch.setattr(worker, "_reauthorize_export_final_write", guard)
    with (
        pytest.raises((ContentError, OSError))
        if not mutation.startswith("cancel")
        else nullcontext()
    ):
        await worker._run_export(
            project_id=str(project_id),
            batch_id=None,
            targets=["coco"],
            opts={},
            async_job_id=str(job_id),
            celery_task_id="test-export",
            worker_node_id="worker:content@test",
        )
    # Observe from a new session rather than the worker's identity map.
    async with world.factory() as observer:
        job = await observer.get(AsyncJob, job_id)
        assert len(uploaded) == 1 and not deleted
        output = await observer.scalar(
            select(ExportOutput).where(ExportOutput.job_id == job_id)
        )
        assert output is not None and output.state == "pending"
        assert output.id == receipt.zip_output_id
        assert output.media_bundle_id == receipt.id
        bundle = await observer.get(ExportMediaBundle, receipt.id)
        assert bundle is not None and bundle.state == "building"
        assert bundle.published_at is None
        assert output.expires_at == bundle.expires_at == receipt.expires_at
        assert (
            output.storage_ref["bucket"],
            output.storage_ref["object_key"],
        ) == uploaded[0]
        assert zipfile.is_zipfile(io.BytesIO(export_objects.objects[uploaded[0]]))
        assert job.status == (
            "cancelled" if mutation.startswith("cancel") else "failed"
        )
        assert not job.result.get("download_url")
        assert (
            await observer.scalar(
                select(ExportArtifact).where(ExportArtifact.project_id == project_id)
            )
            is None
        )
        assert "export.ready" not in notification_types
        if mutation == "failure":
            assert "private source" not in job.error_message


def test_fetch_script_checks_embedded_when_managed_links_expired(tmp_path):
    data = b"original"
    (tmp_path / "images").mkdir()
    (tmp_path / "images/a.png").write_bytes(data)
    (tmp_path / "images_manifest.json").write_text(
        json.dumps(
            {
                "expires_at": "2999-01-01T00:00:00+00:00",
                "images": [
                    {
                        "storage_mode": "embedded",
                        "archive_path": "images/a.png",
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size_bytes": len(data),
                    },
                    {
                        "rel_path": "managed.png",
                        "sha256": hashlib.sha256(b"managed image").hexdigest(),
                        "size_bytes": len(b"managed image"),
                        "presigned_url": "http://invalid/secret",
                        "expires_at": "2000-01-01T00:00:00+00:00",
                    },
                ],
            }
        )
    )
    script = tmp_path / "fetch_images.py"
    script.write_text(_FETCH_IMAGES_TEMPLATE)
    run = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert run.returncode == 2 and "[ok] embedded SHA-256" in run.stdout
    assert "secret" not in run.stdout
    (tmp_path / "images/a.png").write_bytes(b"modified")
    run = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert "verification failed" in run.stdout and "[ok] embedded" not in run.stdout


def test_fetch_script_resolves_same_origin_managed_urls_with_explicit_base(tmp_path):
    class Images(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/minio/owned/image.png?signature=retained"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"managed image")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Images)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        (tmp_path / "images_manifest.json").write_text(
            json.dumps(
                {
                    "expires_at": "2999-01-01T00:00:00+00:00",
                    "images": [
                        {
                            "rel_path": "managed.png",
                            "sha256": hashlib.sha256(b"managed image").hexdigest(),
                            "size_bytes": len(b"managed image"),
                            "presigned_url": "/minio/owned/image.png?signature=retained",
                        }
                    ],
                }
            )
        )
        script = tmp_path / "fetch_images.py"
        script.write_text(_FETCH_IMAGES_TEMPLATE)
        run = subprocess.run(
            [
                sys.executable,
                str(script),
                "--base-url",
                f"http://127.0.0.1:{server.server_port}",
            ],
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        assert (tmp_path / "images/managed.png").read_bytes() == b"managed image"
        assert not (tmp_path / "images/managed.png.part").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_job_dto_hides_original_key_and_paths(db_session, reference_world):
    job, _ = await make_export(db_session, reference_world)
    output = AsyncJobOut.model_validate(job).model_dump(mode="json")
    assert "export_authority" not in output["payload"]


@pytest.mark.parametrize("entrypoint", ["project", "batch", "selection"])
async def test_api_enqueues_frozen_proof_and_hides_it(
    db_session,
    reference_world,
    httpx_client,
    super_admin,
    monkeypatch,
    entrypoint,
):
    _, project, task, _ = reference_world
    monkeypatch.setattr(
        worker.run_export,
        "delay",
        Mock(return_value=SimpleNamespace(id="export-queued")),
    )
    url = f"/api/v1/projects/{project.id}"
    body = None
    if entrypoint == "project":
        url += "/export?targets=coco"
    elif entrypoint == "batch":
        batch = await create_batch(db_session, project_id=project.id)
        task.batch_id = batch.id
        await db_session.flush()
        url += f"/batches/{batch.id}/export?targets=coco"
    else:
        url += "/data-manager/tasks/export"
        body = {"task_ids": [str(task.id)], "targets": ["coco"]}
    response = await httpx_client.post(url, json=body, headers=headers(super_admin))
    assert response.status_code == 202, response.text
    job = await db_session.get(AsyncJob, uuid.UUID(response.json()["job_id"]))
    assert job.payload["export_authority"]["tasks"][0]["task_id"] == str(task.id)
    dto = await httpx_client.get(
        f"/api/v1/async-jobs/{job.id}", headers=headers(super_admin)
    )
    assert dto.status_code == 200
    assert "export_authority" not in dto.json()["payload"]
    cancelled = await httpx_client.post(
        f"/api/v1/async-jobs/{job.id}/cancel", headers=headers(super_admin)
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


async def test_disabled_source_is_api_conflict_without_dispatch(
    db_session,
    reference_world,
    httpx_client,
    super_admin,
    monkeypatch,
):
    _, project, _, content = reference_world
    content[5].enabled = False
    await db_session.flush()
    dispatch = Mock()
    monkeypatch.setattr(worker.run_export, "delay", dispatch)
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/export?targets=coco",
        headers=headers(super_admin),
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "source_unavailable"
    dispatch.assert_not_called()


async def test_synchronous_voc_keeps_reference_gate(
    reference_world,
    httpx_client,
    super_admin,
):
    _, project, _, _ = reference_world
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/export?targets=voc",
        headers=headers(super_admin),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "reference_operation_not_supported:voc"


async def test_long_filename_preserves_extension(db_session, reference_world):
    _, _, task, _ = reference_world
    task.file_name = "a" * 240 + ".PNG"
    await db_session.flush()
    _, media = await make_export(db_session, reference_world)
    filename = media.paths[task.id].rsplit("/", 1)[-1]
    assert filename.endswith(".png") and len(filename) <= 128


@pytest.mark.parametrize("failure", ["changed", "worker_missing"])
async def test_changed_content_or_unqualified_worker_cannot_export(
    db_session,
    reference_world,
    failure,
):
    _, _, task, content = reference_world
    _, media = await make_export(db_session, reference_world)
    if failure == "changed":
        (content[0] / "图片 空格.png").write_bytes(b"replacement")
    else:
        media.node_id = "worker:unqualified"
    with pytest.raises((ContentError, OSError, ValueError)):
        await media.snapshot(task)


async def test_large_frozen_selection_uses_bounded_sql_parameters(
    db_session, reference_world
):
    from app.services.exporting.reference_media import task_query
    from app.services.exporting.service import ExportService

    _, project, task, content = reference_world
    identities = [uuid.uuid4() for _ in range(33000)]
    await db_session.execute(
        insert(DatasetItem),
        [
            {
                "id": identity,
                "dataset_id": content[3].id,
                "file_name": "bulk.png",
                "file_path": f"bulk/{identity}.png",
                "file_type": "image",
            }
            for identity in identities
        ],
    )
    await db_session.execute(
        insert(Task),
        [
            {
                "id": identity,
                "project_id": project.id,
                "display_id": f"bulk-{identity.hex[:20]}",
                "file_name": "bulk.png",
                "file_path": f"bulk/{identity}.png",
                "file_type": "image",
            }
            for identity in identities
        ],
    )
    identities.append(task.id)
    assert (
        len(list(await db_session.scalars(task_query(project.id, task_ids=identities))))
        == 33001
    )
    service = ExportService(db_session, task_ids=identities)
    _, actual_tasks, _ = await service._load_data(project.id)
    assert len(actual_tasks) == 33001
    assert await service._load_predictions(project.id, identities) == []
    items = await service._load_dataset_items(
        [SimpleNamespace(dataset_item_id=identity) for identity in identities[:-1]]
        + [task]
    )
    assert task.dataset_item_id in items
    chunks = [
        chunk
        async for chunk in service.iter_export_chunks(
            project.id, None, with_annotations=False
        )
    ]
    assert sum(len(chunk[0]) for chunk in chunks) == 33001


async def test_archive_quota_failure_closes_snapshot_and_removes_zip(
    db_session,
    reference_world,
    monkeypatch,
    tmp_path,
):
    from app.config import settings
    from app.services.exporting import packaging

    _, project, _, _ = reference_world
    _, media = await make_export(db_session, reference_world)
    temporary = tmp_path / "failed.zip"
    monkeypatch.setattr(packaging, "_new_zip_tempfile", lambda: str(temporary))
    original_snapshot = media.snapshot
    snapshots = []

    async def too_small(task):
        snapshot = await original_snapshot(task)
        snapshots.append(snapshot)
        monkeypatch.setattr(settings, "mask_format_max_entry_bytes", snapshot.size - 1)
        return snapshot

    monkeypatch.setattr(media, "snapshot", too_small)
    with pytest.raises(packaging.ExportQuotaError):
        await build_export_zip(
            db_session,
            project.id,
            batch_id=None,
            task_ids=media.task_ids,
            targets=["coco"],
            include_attributes=True,
            video_frame_mode="keyframes",
            reference_media=media,
        )
    assert snapshots[0]._closed and not temporary.exists()


@pytest.mark.parametrize("backend", ["s3", "sftp"])
async def test_remote_reference_exports_use_the_same_strict_media_contract(
    db_session,
    s3_world,
    sftp_world,
    backend,
):
    from app.db.models.dataset_source import DatasetScanEntry
    from app.services.dataset import DatasetService
    from app.services.dataset_source_access import capture_dataset_write
    from tests.test_s3_references import scan as scan_s3
    from tests.test_sftp_references import scan as scan_sftp

    if backend == "s3":
        world = s3_world
        memory, actor, dataset, location, source = world
        data = memory.data
        run = await scan_s3(db_session, world)
    else:
        world = sftp_world
        root, memory, actor, dataset, location, source = world
        data = (root / "collection/ 图片 +%2F.png").read_bytes()
        run = await scan_sftp(db_session, world)
    entry = await db_session.scalar(
        select(DatasetScanEntry)
        .where(DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "file")
        .order_by(DatasetScanEntry.path_digest)
        .limit(1)
    )
    registered = await dataset_references.register_entry(
        db_session, run.id, entry.path_digest, actor
    )
    project = await create_project(db_session, owner_id=actor.id)
    authority = await capture_dataset_write(db_session, dataset.id, actor, None)
    await DatasetService(db_session).link_project(
        dataset.id, project.id, directory_authority=authority
    )
    task = await db_session.scalar(select(Task).where(Task.project_id == project.id))
    reference = (
        registered,
        project,
        task,
        (None, data, actor, dataset, location, source, run, entry),
    )
    _, media = await make_export(db_session, reference)
    path, _, _ = await build_export_zip(
        db_session,
        project.id,
        batch_id=None,
        task_ids=media.task_ids,
        targets=["coco"],
        include_attributes=True,
        video_frame_mode="keyframes",
        reference_media=media,
    )
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("images_manifest.json"))
            exported = archive.read(manifest["images"][0]["archive_path"])
            assert exported == data
            assert hashlib.sha256(exported).hexdigest() == registered["sha256"]
        await media.validate(final=True)
    finally:
        Path(path).unlink(missing_ok=True)
