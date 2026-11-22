"""Live Mask content boundary acceptance against owned test resources."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from yoao_assets_mask_content import codec
from yoao_assets_mask_content.client import MaskContentClient
from yoao_assets_mask_content.contract import (
    DeleteRequest,
    GetRequest,
    ListRequest,
    MaskContentFailure,
    PutRequest,
    RleContent,
)
from app.config import settings
from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.project import Project
from app.db.models.raster_mask_object import RasterMaskObject
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.db.models.task import Task
from app.db.models.user import User
from app.deps import get_db
from app.services import annotations_storage, raster_mask_objects, raster_object_storage
from app.services.dataset_assets import ensure_legacy_bindings
from app.services.raster_mask_storage import (
    reserve_raster_mask_upload,
    store_coco_rle,
)
from tests.factory import create_user
from tests.test_asset_storage_cleanup_runtime import (
    _expect_s3_denied,
    _local_s3_client,
    _object_exists,
    _required_env,
    _shared_bootstrap_minio,
    _verified_resource_manifest,
    _verify_bucket_owner,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_MASK_CONTENT_ACCEPTANCE") != "1",
        reason=(
            "requires the disposable module-data verifier and owned Mask content "
            "resources"
        ),
    ),
]

ROOT = Path(__file__).resolve().parents[3]
_MASK_PREFIX = "raster-masks/sha256/"


def _owned_resources(test_engine):
    if not test_engine.url.database or not test_engine.url.database.endswith("_test"):
        pytest.fail("Mask acceptance requires the verifier-owned disposable test DB")
    resources = _verified_resource_manifest(test_engine, test_engine.url)
    bucket = resources["buckets"]["MINIO_BUCKET"]
    other_bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    owner_s3 = _local_s3_client(
        settings, settings.minio_access_key, settings.minio_secret_key
    )
    _verify_bucket_owner(owner_s3, bucket, resources["owner"])
    _verify_bucket_owner(owner_s3, other_bucket, resources["owner"])
    content_access = _required_env("AAP_MODULE_DATA_MASK_CONTENT_S3_ACCESS_KEY")
    content_secret = _required_env("AAP_MODULE_DATA_MASK_CONTENT_S3_SECRET_KEY")
    cleanup_access = _required_env("AAP_MODULE_DATA_MASK_CLEANUP_S3_ACCESS_KEY")
    cleanup_secret = _required_env("AAP_MODULE_DATA_MASK_CLEANUP_S3_SECRET_KEY")
    if (
        (content_access, content_secret)
        == (settings.minio_access_key, settings.minio_secret_key)
        or (cleanup_access, cleanup_secret)
        == (settings.minio_access_key, settings.minio_secret_key)
        or content_access == cleanup_access
        or content_secret == cleanup_secret
    ):
        pytest.fail("Mask runtimes were given an unscoped or shared S3 identity")
    return (
        resources,
        bucket,
        other_bucket,
        owner_s3,
        (content_access, content_secret),
        (cleanup_access, cleanup_secret),
    )


def _s3_endpoint() -> str:
    scheme = "https" if settings.minio_use_ssl else "http"
    endpoint = httpx.URL(f"{scheme}://{settings.minio_endpoint}")
    if _shared_bootstrap_minio(
        settings, container_id=os.environ.get("AAP_MODULE_DATA_MINIO_CONTAINER_ID")
    ) is None and endpoint.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("Mask runtime S3 endpoint must be loopback or verified bootstrap")
    return str(endpoint).rstrip("/")


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_runtime_ready(process: subprocess.Popen, origin: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("Mask content runtime exited before readiness")
        try:
            response = httpx.get(origin + "/health/ready", timeout=1, trust_env=False)
            if response.status_code == 200 and response.text == "ok":
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    pytest.fail("Mask content runtime did not become ready")


def _start_runtime(role, *, bucket: str, credentials: tuple[str, str], tmp_path):
    token = secrets.token_urlsafe(32)
    port = _free_loopback_port()
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "MASK_CONTENT_ROLE": role,
        "MASK_CONTENT_TOKEN": token,
        "MASK_CONTENT_S3_ENDPOINT_URL": _s3_endpoint(),
        "MASK_CONTENT_S3_ACCESS_KEY": credentials[0],
        "MASK_CONTENT_S3_SECRET_KEY": credentials[1],
        "MASK_CONTENT_BUCKET": bucket,
        "MASK_CONTENT_REGION": "us-east-1",
        "MASK_CONTENT_HOST": "127.0.0.1",
        "MASK_CONTENT_PORT": str(port),
    }
    assert set(environment) == {
        "PATH",
        "MASK_CONTENT_ROLE",
        "MASK_CONTENT_TOKEN",
        "MASK_CONTENT_S3_ENDPOINT_URL",
        "MASK_CONTENT_S3_ACCESS_KEY",
        "MASK_CONTENT_S3_SECRET_KEY",
        "MASK_CONTENT_BUCKET",
        "MASK_CONTENT_REGION",
        "MASK_CONTENT_HOST",
        "MASK_CONTENT_PORT",
    }
    assert len(token) >= 32
    log_stream = (tmp_path / f"mask-{role}.log").open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "yoao_assets_mask_content_runtime"],
        cwd=ROOT / "apps/assets-mask-content/src",
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log_stream,
        stderr=subprocess.STDOUT,
        close_fds=True,
        start_new_session=True,
    )
    origin = f"http://127.0.0.1:{port}"
    try:
        _wait_runtime_ready(process, origin)
    except BaseException:
        _stop_runtime(process, log_stream)
        raise
    return process, log_stream, origin, token


def _stop_runtime(process: subprocess.Popen | None, log_stream) -> None:
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
    if log_stream is not None and not log_stream.closed:
        log_stream.close()


def _rle(seed: str) -> RleContent:
    counts = [value % 16 + 1 for value in hashlib.sha256(seed.encode()).digest()]
    return RleContent(
        encoding="coco_rle",
        size=(1, 1024),
        counts=tuple([*counts, 1024 - sum(counts)]),
    )


def _find_candidate(client: MaskContentClient, key: str):
    cursor = None
    for _ in range(100):
        page = client.list(ListRequest(cursor=cursor, limit=1000))
        candidate = next((item for item in page.items if item.key == key), None)
        if candidate is not None:
            return candidate
        if page.cursor is None:
            return None
        cursor = page.cursor
    pytest.fail("Mask listing exceeded the bounded acceptance page count")


def _all_candidates(client: MaskContentClient):
    cursor = None
    candidates = []
    for _ in range(100):
        page = client.list(ListRequest(cursor=cursor, limit=1000))
        candidates.extend(page.items)
        if page.cursor is None:
            return candidates
        cursor = page.cursor
    pytest.fail("Mask listing exceeded the bounded acceptance page count")


def _expect_failure(operation, code: str) -> None:
    with pytest.raises(MaskContentFailure) as caught:
        operation()
    assert caught.value.code == code


async def test_live_mask_content_roles_and_conditional_object_operations(
    test_engine, tmp_path
):
    (
        _resources,
        bucket,
        other_bucket,
        owner_s3,
        content_credentials,
        cleanup_credentials,
    ) = _owned_resources(test_engine)
    dataset_bucket = other_bucket
    content_process = cleanup_process = None
    content_log = cleanup_log = None
    content_client = cleanup_client = None
    outside_key = f"mask-acceptance-outside/{uuid4().hex}"
    other_key = f"mask-acceptance-{uuid4().hex}"
    seeded_objects = [(bucket, outside_key), (dataset_bucket, other_key)]
    fixture_objects = []

    try:
        content_process, content_log, content_origin, content_token = _start_runtime(
            "content",
            bucket=bucket,
            credentials=content_credentials,
            tmp_path=tmp_path,
        )
        cleanup_process, cleanup_log, cleanup_origin, cleanup_token = _start_runtime(
            "cleanup",
            bucket=bucket,
            credentials=cleanup_credentials,
            tmp_path=tmp_path,
        )
        content_s3 = _local_s3_client(settings, *content_credentials)
        cleanup_s3 = _local_s3_client(settings, *cleanup_credentials)
        content_client = MaskContentClient(content_origin, content_token)
        cleanup_client = MaskContentClient(cleanup_origin, cleanup_token)

        owner_s3.put_object(Bucket=bucket, Key=outside_key, Body=b"outside-prefix")
        owner_s3.put_object(Bucket=dataset_bucket, Key=other_key, Body=b"other-bucket")

        identity_rle = _rle(uuid4().hex)
        identity_request = PutRequest(content=identity_rle)
        identity_result = content_client.put(identity_request)
        fixture_objects.append((bucket, identity_result.reference.object_key))
        assert (
            content_client.put(identity_request).reference == identity_result.reference
        )
        assert (
            content_client.get(GetRequest(reference=identity_result.reference)).content
            == identity_rle
        )

        gzip_rle = _rle(uuid4().hex)
        gzip_request = PutRequest(content=gzip_rle, storage_encoding="gzip")
        gzip_result = content_client.put(gzip_request)
        fixture_objects.append((bucket, gzip_result.reference.object_key))
        assert gzip_result.reference.storage_encoding == "gzip"
        assert content_client.put(gzip_request).reference == gzip_result.reference
        assert (
            content_client.get(GetRequest(reference=gzip_result.reference)).content
            == gzip_rle
        )
        legacy_gzip = gzip_result.reference.model_copy(
            update={"encoding": "coco_rle_gzip", "storage_encoding": None}
        )
        assert content_client.get(GetRequest(reference=legacy_gzip)).content == gzip_rle

        missing_ref = codec.build_rle_reference(_rle(uuid4().hex))
        _expect_failure(
            lambda: content_client.get(GetRequest(reference=missing_ref)),
            "object_missing",
        )

        corrupt_rle = _rle(uuid4().hex)
        corrupt_ref = codec.build_rle_reference(corrupt_rle)
        fixture_objects.append((bucket, corrupt_ref.object_key))
        owner_s3.put_object(
            Bucket=bucket, Key=corrupt_ref.object_key, Body=b"corrupt-existing-object"
        )
        _expect_failure(
            lambda: content_client.put(PutRequest(content=corrupt_rle)),
            "object_corrupt",
        )
        corrupt_body = owner_s3.get_object(Bucket=bucket, Key=corrupt_ref.object_key)[
            "Body"
        ]
        try:
            assert corrupt_body.read() == b"corrupt-existing-object"
        finally:
            corrupt_body.close()

        candidate = _find_candidate(cleanup_client, gzip_result.reference.object_key)
        assert candidate is not None
        _expect_failure(
            lambda: content_client.list(ListRequest(limit=10)), "permission_denied"
        )
        _expect_failure(
            lambda: content_client.delete(
                DeleteRequest(
                    object_key=candidate.key,
                    etag=candidate.etag,
                    last_modified=candidate.last_modified,
                )
            ),
            "permission_denied",
        )
        _expect_failure(
            lambda: cleanup_client.put(PutRequest(content=_rle(uuid4().hex))),
            "permission_denied",
        )
        assert (
            cleanup_s3.list_objects_v2(
                Bucket=bucket, Prefix=_MASK_PREFIX, MaxKeys=1
            ).get("KeyCount", 0)
            >= 1
        )

        for scoped_s3 in (content_s3, cleanup_s3):
            _expect_s3_denied(
                lambda scoped_s3=scoped_s3: scoped_s3.get_object(
                    Bucket=bucket, Key=outside_key
                )
            )
            _expect_s3_denied(
                lambda scoped_s3=scoped_s3: scoped_s3.get_object(
                    Bucket=dataset_bucket, Key=other_key
                )
            )
        _expect_s3_denied(
            lambda: content_s3.list_objects_v2(
                Bucket=bucket, Prefix=_MASK_PREFIX, MaxKeys=1
            )
        )
        _expect_s3_denied(
            lambda: content_s3.delete_object(
                Bucket=bucket, Key=identity_result.reference.object_key
            )
        )
        _expect_s3_denied(
            lambda: content_s3.put_object(
                Bucket=bucket, Key=outside_key, Body=b"forbidden"
            )
        )
        _expect_s3_denied(
            lambda: content_s3.put_object(
                Bucket=dataset_bucket, Key=other_key, Body=b"forbidden"
            )
        )
        _expect_s3_denied(
            lambda: cleanup_s3.put_object(
                Bucket=bucket,
                Key=identity_result.reference.object_key,
                Body=b"forbidden",
            )
        )
        _expect_s3_denied(
            lambda: cleanup_s3.list_objects_v2(
                Bucket=bucket, Prefix="mask-acceptance-outside/", MaxKeys=1
            )
        )
        _expect_s3_denied(lambda: cleanup_s3.list_objects_v2(Bucket=bucket, MaxKeys=1))
        _expect_s3_denied(
            lambda: cleanup_s3.delete_object(Bucket=bucket, Key=outside_key)
        )
        _expect_s3_denied(
            lambda: cleanup_s3.delete_object(Bucket=dataset_bucket, Key=other_key)
        )

        stale = DeleteRequest(
            object_key=candidate.key,
            etag=candidate.etag + "-stale",
            last_modified=candidate.last_modified,
        )
        _expect_failure(lambda: cleanup_client.delete(stale), "content_changed")
        assert _object_exists(owner_s3, bucket, candidate.key)
        removed = cleanup_client.delete(
            DeleteRequest(
                object_key=candidate.key,
                etag=candidate.etag,
                last_modified=candidate.last_modified,
            )
        )
        assert removed.deleted is True
        assert not _object_exists(owner_s3, bucket, candidate.key)
    finally:
        if content_client is not None:
            content_client.close()
        if cleanup_client is not None:
            cleanup_client.close()
        for process, stream in (
            (cleanup_process, cleanup_log),
            (content_process, content_log),
        ):
            _stop_runtime(process, stream)
        for object_bucket, key in (*seeded_objects, *fixture_objects):
            owner_s3.delete_object(Bucket=object_bucket, Key=key)


async def _seed_mask_world(db, owner_id):
    suffix = uuid4().hex[:12]
    project = Project(
        id=uuid4(),
        display_id=f"P-MCL-{suffix}",
        name=f"mask-live-{suffix}",
        type_label="Image segmentation",
        type_key="image-seg",
        owner_id=owner_id,
        raster_mask_native_editing_enabled=True,
        tool_bindings={"region": {"enabled": True, "classes": ["object"]}},
    )
    dataset = Dataset(
        id=uuid4(),
        display_id=f"D{suffix}",
        name=f"mask-live-{suffix}",
        description="",
        data_type="image",
        is_temporal=False,
        file_count=1,
        created_by=owner_id,
    )
    item_id = uuid4()
    item_path = f"mask-acceptance/{suffix}/image.png"
    item = DatasetItem(
        id=item_id,
        dataset_id=dataset.id,
        file_name="image.png",
        file_path=item_path,
        file_type="image",
        file_size=0,
        content_hash=hashlib.sha256(item_path.encode()).hexdigest(),
        width=1024,
        height=1,
        metadata_={},
    )
    db.add_all([project, dataset])
    await db.flush()
    db.add_all([item, ProjectDataset(project_id=project.id, dataset_id=dataset.id)])
    await db.flush()

    task = Task(
        id=uuid4(),
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-MCL-{suffix}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="image",
        status="pending",
    )
    quota_task = Task(
        id=uuid4(),
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-MCQ-{suffix}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="image",
        status="pending",
    )
    db.add_all([task, quota_task])
    await db.flush()
    return project, dataset, item, task, quota_task


async def test_live_mask_api_preserves_current_authority_and_roots(
    test_engine,
    httpx_client,
    app_module,
    raster_owner_registry,
    monkeypatch,
):
    (
        _resources,
        bucket,
        _other_bucket,
        owner_s3,
        _content_credentials,
        _cleanup_credentials,
    ) = _owned_resources(test_engine)
    consumer_url = make_url(settings.database_url)
    assert (consumer_url.host, consumer_url.port, consumer_url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    ), "Mask consumer must use the verifier-owned disposable database"
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    owned_versions = {}
    orphan_address = None
    project_id = dataset_id = item_id = None
    task_id = quota_task_id = None
    user_ids = []

    async with sessions() as db:

        async def override_db():
            yield db

        monkeypatch.setitem(app_module.dependency_overrides, get_db, override_db)
        try:
            await ensure_legacy_bindings(db)
            binding = await db.get(StorageBinding, "annotations")
            location = await db.get(StorageLocation, binding.location_id)
            target = await annotations_storage.target_for(db, location)
            assert binding.bucket == target.bucket == bucket
            assert target.endpoint_url == _s3_endpoint()
            suffix = uuid4().hex
            user = await create_user(
                db, "project_admin", f"mask-owner-{suffix}@test.local", "Mask owner"
            )
            outsider = await create_user(
                db, "employee", f"mask-outsider-{suffix}@test.local", "Outsider"
            )
            user_ids = [user.id, outsider.id]
            project, dataset, item, task, quota_task = await _seed_mask_world(
                db, user.id
            )
            task_id, quota_task_id = task.id, quota_task.id
            token = create_access_token(str(user.id), user.role)
            other_token = create_access_token(str(outsider.id), outsider.role)
            project_id, dataset_id, item_id = project.id, dataset.id, item.id
            await db.commit()

            recent_rle, root_rle, orphan_rle, unauthorized_rle, quota_rle = (
                _rle(uuid4().hex) for _ in range(5)
            )
            aged = datetime.now(timezone.utc) - timedelta(days=2)
            with monkeypatch.context() as clock:
                clock.setattr(raster_mask_objects, "now", lambda: aged)
                recent_ref = await store_coco_rle(
                    recent_rle.model_dump(mode="json"), db=db
                )
                root_ref = await store_coco_rle(root_rle.model_dump(mode="json"), db=db)
                orphan_ref = await store_coco_rle(
                    orphan_rle.model_dump(mode="json"), db=db
                )

            annotation = Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=user.id,
                source="manual",
                annotation_type="raster_mask",
                tool_unit_id="region",
                class_name="object",
                geometry={"type": "raster_mask", "mask": root_ref},
                is_active=True,
                was_cancelled=False,
                version=1,
            )
            db.add(annotation)
            for reference in (recent_ref, orphan_ref):
                await reserve_raster_mask_upload(
                    db, task_id=task.id, object_key=reference["object_key"], limit=256
                )
            recent_reservation = await db.scalar(
                select(RasterMaskUpload).where(
                    RasterMaskUpload.task_id == task_id,
                    RasterMaskUpload.object_key == recent_ref["object_key"],
                )
            )
            recent_reservation.created_at = aged
            db.add_all(
                RasterMaskUpload(
                    task_id=quota_task.id,
                    object_key=(
                        f"raster-masks/sha256/{digest[:2]}/{digest[2:4]}/{digest}.json"
                    ),
                )
                for digest in (
                    hashlib.sha256(f"{task.id}:{index}".encode()).hexdigest()
                    for index in range(256)
                )
            )
            await db.flush()
            assert (
                await raster_mask_objects.bind_publications(
                    db, [recent_ref, root_ref, orphan_ref]
                )
                == 3
            )
            annotation_id = annotation.id
            await db.commit()

            await db.execute(
                delete(RasterMaskUpload).where(
                    RasterMaskUpload.task_id == task.id,
                    RasterMaskUpload.object_key == orphan_ref["object_key"],
                )
            )
            await db.commit()
            rows = {
                row.logical_key: row
                for row in (
                    await db.scalars(
                        select(RasterMaskObject).where(
                            RasterMaskObject.id.in_(raster_owner_registry.objects)
                        )
                    )
                ).all()
            }
            assert set(rows) == {
                recent_ref["object_key"],
                root_ref["object_key"],
                orphan_ref["object_key"],
            }
            receipts = {}
            for key, row in rows.items():
                receipt = raster_object_storage.snapshot(row)
                location = raster_object_storage.resolve(receipt)
                assert location.bucket == bucket
                head = owner_s3.head_object(
                    Bucket=location.bucket, Key=location.object_key
                )
                owned_versions[(location.bucket, location.object_key)] = head.get(
                    "VersionId"
                )
                receipts[key] = (receipt, location, head)
            orphan_location = receipts[orphan_ref["object_key"]][1]
            orphan_address = (orphan_location.bucket, orphan_location.object_key)
            await db.rollback()

            headers = {"Authorization": f"Bearer {token}"}
            denied = await httpx_client.post(
                f"/api/v1/tasks/{task_id}/mask-content",
                json=unauthorized_rle.model_dump(mode="json"),
                headers={"Authorization": f"Bearer {other_token}"},
            )
            assert denied.status_code in {403, 404}
            unauthorized_key = codec.build_rle_reference(unauthorized_rle).object_key
            assert (
                await db.scalar(
                    select(RasterMaskObject.id).where(
                        RasterMaskObject.logical_key == unauthorized_key
                    )
                )
                is None
            )
            assert (
                await db.scalar(
                    select(RasterMaskUpload.id).where(
                        RasterMaskUpload.object_key == unauthorized_key
                    )
                )
                is None
            )

            fetched = await httpx_client.get(
                f"/api/v1/annotations/{annotation_id}/mask-content", headers=headers
            )
            assert fetched.status_code == 200, fetched.text
            assert fetched.json() == root_rle.model_dump(mode="json")
            etag = fetched.headers["etag"]

            upload = await httpx_client.post(
                f"/api/v1/tasks/{task_id}/mask-content",
                json=recent_rle.model_dump(mode="json"),
                headers=headers,
            )
            assert upload.status_code == 200, upload.text
            assert upload.json() == recent_ref
            recent_reservation = await db.scalar(
                select(RasterMaskUpload)
                .where(
                    RasterMaskUpload.task_id == task_id,
                    RasterMaskUpload.object_key == recent_ref["object_key"],
                )
                .execution_options(populate_existing=True)
            )
            assert recent_reservation is not None
            assert recent_reservation.created_at > aged + timedelta(days=1)
            assert recent_reservation.linked_at is None
            recent_owner = await db.scalar(
                select(RasterMaskObject)
                .where(RasterMaskObject.logical_key == recent_ref["object_key"])
                .execution_options(populate_existing=True)
            )
            receipt, location, original_head = receipts[recent_ref["object_key"]]
            assert raster_object_storage.same(recent_owner, receipt)
            replay_head = owner_s3.head_object(
                Bucket=location.bucket, Key=location.object_key
            )
            assert (
                replay_head.get("VersionId"),
                replay_head["ETag"],
                replay_head["LastModified"],
            ) == (
                original_head.get("VersionId"),
                original_head["ETag"],
                original_head["LastModified"],
            )

            quota = await httpx_client.post(
                f"/api/v1/tasks/{quota_task_id}/mask-content",
                json=quota_rle.model_dump(mode="json"),
                headers=headers,
            )
            assert quota.status_code == 422
            assert quota.json()["detail"]["reason"] == "mask_quota_exceeded"
            quota_key = codec.build_rle_reference(quota_rle).object_key
            assert (
                await db.scalar(
                    select(RasterMaskObject.id).where(
                        RasterMaskObject.logical_key == quota_key
                    )
                )
                is None
            )
            assert (
                await db.scalar(
                    select(RasterMaskUpload.id).where(
                        RasterMaskUpload.object_key == quota_key
                    )
                )
                is None
            )
            await db.rollback()

            async with sessions() as neighbors:
                await neighbors.scalars(
                    select(RasterMaskObject.id)
                    .where(RasterMaskObject.id.not_in(raster_owner_registry.objects))
                    .with_for_update()
                )
                await neighbors.scalars(
                    select(RasterMaskUpload.id)
                    .where(
                        RasterMaskUpload.task_id.not_in([task_id, quota_task_id]),
                        RasterMaskUpload.linked_at.is_(None),
                    )
                    .with_for_update()
                )
                foreign_expired = await neighbors.scalar(
                    select(RasterMaskUpload.id)
                    .where(
                        RasterMaskUpload.task_id.not_in([task_id, quota_task_id]),
                        RasterMaskUpload.linked_at.is_(None),
                        RasterMaskUpload.created_at
                        <= datetime.now(timezone.utc) - raster_mask_objects.GRACE,
                    )
                    .limit(1)
                )
                assert foreign_expired is None
                first = await raster_mask_objects.cleanup(db)
                second = await raster_mask_objects.cleanup(db)
                assert first["errors"] == second["errors"] == 0
                assert first["retired"] == 1 and second["deleted"] == 1
            async with sessions() as observer:
                remaining = {
                    row.logical_key: row.state
                    for row in (
                        await observer.scalars(
                            select(RasterMaskObject).where(
                                RasterMaskObject.id.in_(raster_owner_registry.objects)
                            )
                        )
                    ).all()
                }
            assert remaining == {
                root_ref["object_key"]: "ready",
                recent_ref["object_key"]: "ready",
            }
            for reference in (root_ref, recent_ref):
                _, location, _ = receipts[reference["object_key"]]
                assert _object_exists(owner_s3, location.bucket, location.object_key)
            _, orphan_location, _ = receipts[orphan_ref["object_key"]]
            assert not _object_exists(
                owner_s3, orphan_location.bucket, orphan_location.object_key
            )

            _, location, head = receipts[root_ref["object_key"]]
            current_head = owner_s3.head_object(
                Bucket=location.bucket, Key=location.object_key
            )
            assert (current_head["ETag"], current_head.get("VersionId")) == (
                head["ETag"],
                head.get("VersionId"),
            )
            delete_args = {"Bucket": location.bucket, "Key": location.object_key}
            if head.get("VersionId") is not None:
                delete_args["VersionId"] = head["VersionId"]
            owner_s3.delete_object(**delete_args)
            missing = await httpx_client.get(
                f"/api/v1/annotations/{annotation_id}/mask-content",
                headers={**headers, "If-None-Match": etag},
            )
            assert missing.status_code == 409, missing.text
            assert missing.json()["detail"]["reason"] == "raster_object_storage_missing"
            assert missing.json()["detail"]["retryable"] is True
        finally:
            await db.rollback()
            async with sessions() as observer:
                for row in (
                    await observer.scalars(
                        select(RasterMaskObject).where(
                            RasterMaskObject.id.in_(raster_owner_registry.objects)
                        )
                    )
                ).all():
                    location = raster_object_storage.resolve(row)
                    assert location.bucket == bucket
                    address = (location.bucket, location.object_key)
                    if address not in owned_versions and _object_exists(
                        owner_s3, *address
                    ):
                        head = owner_s3.head_object(Bucket=address[0], Key=address[1])
                        owned_versions[address] = head.get("VersionId")
            for (object_bucket, key), version in owned_versions.items():
                args = {"Bucket": object_bucket, "Key": key}
                markers = []
                if version is not None:
                    versions = owner_s3.list_object_versions(
                        Bucket=object_bucket, Prefix=key
                    )
                    assert not versions.get("IsTruncated")
                    actual_versions = [
                        entry["VersionId"]
                        for entry in versions.get("Versions", [])
                        if entry["Key"] == key
                    ]
                    assert set(actual_versions) <= {version}
                    markers = [
                        entry["VersionId"]
                        for entry in versions.get("DeleteMarkers", [])
                        if entry["Key"] == key
                    ]
                    assert not markers or (
                        (object_bucket, key) == orphan_address and len(markers) == 1
                    )
                    args["VersionId"] = version
                owner_s3.delete_object(**args)
                for marker in markers:
                    owner_s3.delete_object(
                        Bucket=object_bucket, Key=key, VersionId=marker
                    )
            if project_id is not None:
                await db.execute(
                    delete(Annotation).where(Annotation.project_id == project_id)
                )
                await db.execute(
                    delete(RasterMaskUpload).where(
                        RasterMaskUpload.task_id.in_([task_id, quota_task_id])
                    )
                )
                await db.execute(delete(Task).where(Task.project_id == project_id))
                await db.execute(
                    delete(ProjectDataset).where(
                        ProjectDataset.project_id == project_id
                    )
                )
                await db.execute(delete(Project).where(Project.id == project_id))
                await db.execute(delete(DatasetItem).where(DatasetItem.id == item_id))
                await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
            if user_ids:
                await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.commit()
