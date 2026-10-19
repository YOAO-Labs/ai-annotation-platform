"""Live Mask content boundary acceptance against owned test resources."""

from __future__ import annotations

import asyncio
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_assets_mask_content import codec
from aap_assets_mask_content.client import MaskContentClient
from aap_assets_mask_content.contract import (
    DeleteRequest,
    GetRequest,
    ListRequest,
    MaskContentFailure,
    PutRequest,
    RleContent,
)
from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.project import Project
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.db.models.task import Task
from app.db.models.user import User
from tests.test_asset_storage_cleanup_runtime import (
    _expect_s3_denied,
    _local_s3_client,
    _object_exists,
    _required_env,
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
    if endpoint.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("Mask runtime S3 endpoint must be loopback-only")
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
        [sys.executable, "-m", "aap_assets_mask_content_runtime"],
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


async def test_live_mask_api_and_embedded_gc_preserve_current_authority_and_roots(
    test_engine,
    tmp_path,
    httpx_client,
    db_session,
    project_admin,
    annotator,
    monkeypatch,
):
    (
        _resources,
        bucket,
        _other_bucket,
        owner_s3,
        content_credentials,
        cleanup_credentials,
    ) = _owned_resources(test_engine)
    user, token = project_admin
    _other_user, other_token = annotator
    content_process = cleanup_process = None
    content_log = cleanup_log = None
    content_client = None

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
        monkeypatch.setattr(settings, "assets_mask_content_url", content_origin)
        monkeypatch.setattr(settings, "assets_mask_content_token", content_token)
        monkeypatch.setattr(settings, "assets_mask_cleanup_url", cleanup_origin)
        monkeypatch.setattr(settings, "assets_mask_cleanup_token", cleanup_token)
        monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
        monkeypatch.setattr(settings, "raster_mask_create_enabled", True)

        project, _dataset, item, task, quota_task = await _seed_mask_world(
            db_session, user.id
        )
        quota_rows = []
        for index in range(256):
            digest = hashlib.sha256(f"{task.id}:{index}".encode()).hexdigest()
            quota_rows.append(
                RasterMaskUpload(
                    task_id=quota_task.id,
                    object_key=(
                        f"raster-masks/sha256/{digest[:2]}/{digest[2:4]}/{digest}.json"
                    ),
                )
            )
        db_session.add_all(quota_rows)
        await db_session.flush()

        content_client = MaskContentClient(content_origin, content_token)
        recent_rle = _rle(uuid4().hex)
        root_rle = _rle(uuid4().hex)
        orphan_rle = _rle(uuid4().hex)
        failed_rle = _rle(uuid4().hex)
        unauthorized_rle = _rle(uuid4().hex)
        quota_rle = _rle(uuid4().hex)
        recent_ref = content_client.put(PutRequest(content=recent_rle)).reference
        root_ref = content_client.put(PutRequest(content=root_rle)).reference
        orphan_ref = content_client.put(PutRequest(content=orphan_rle)).reference

        annotation = Annotation(
            task_id=task.id,
            project_id=project.id,
            user_id=user.id,
            source="manual",
            annotation_type="raster_mask",
            tool_unit_id="region",
            class_name="object",
            geometry={
                "type": "raster_mask",
                "mask": root_ref.model_dump(mode="json", exclude_none=True),
            },
            is_active=True,
            was_cancelled=False,
            version=1,
        )
        db_session.add(annotation)
        db_session.add(
            RasterMaskUpload(
                task_id=task.id,
                object_key=recent_ref.object_key,
                created_at=datetime.now(timezone.utc) - timedelta(days=2),
            )
        )
        await db_session.commit()

        headers = {"Authorization": f"Bearer {token}"}
        denied = await httpx_client.post(
            f"/api/v1/tasks/{task.id}/mask-content",
            json=unauthorized_rle.model_dump(mode="json"),
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert denied.status_code in {403, 404}
        assert not _object_exists(
            owner_s3, bucket, codec.build_rle_reference(unauthorized_rle).object_key
        )

        fetched = await httpx_client.get(
            f"/api/v1/annotations/{annotation.id}/mask-content",
            headers=headers,
        )
        assert fetched.status_code == 200, fetched.text
        assert fetched.json() == root_rle.model_dump(mode="json")

        with MaskContentClient(cleanup_origin, cleanup_token) as cleanup:
            initial_candidates = {
                key: _find_candidate(cleanup, key)
                for key in (
                    recent_ref.object_key,
                    root_ref.object_key,
                    orphan_ref.object_key,
                )
            }
        assert all(candidate is not None for candidate in initial_candidates.values())
        cutoff = max(
            candidate.last_modified for candidate in initial_candidates.values()
        ) + timedelta(seconds=2)
        wait_seconds = (
            cutoff + timedelta(seconds=2) - datetime.now(timezone.utc)
        ).total_seconds()
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)

        upload = await httpx_client.post(
            f"/api/v1/tasks/{task.id}/mask-content",
            json=recent_rle.model_dump(mode="json"),
            headers=headers,
        )
        assert upload.status_code == 200, upload.text
        assert upload.json()["object_key"] == recent_ref.object_key
        recent_reservation = (
            await db_session.execute(
                select(RasterMaskUpload).where(
                    RasterMaskUpload.task_id == task.id,
                    RasterMaskUpload.object_key == recent_ref.object_key,
                )
            )
        ).scalar_one()
        await db_session.refresh(recent_reservation)
        assert recent_reservation.created_at >= cutoff
        assert recent_reservation.linked_at is None
        with MaskContentClient(cleanup_origin, cleanup_token) as cleanup:
            replayed_candidate = _find_candidate(cleanup, recent_ref.object_key)
        assert replayed_candidate is not None
        assert (
            replayed_candidate.last_modified
            == initial_candidates[recent_ref.object_key].last_modified
        )
        assert replayed_candidate.last_modified < cutoff

        quota = await httpx_client.post(
            f"/api/v1/tasks/{quota_task.id}/mask-content",
            json=quota_rle.model_dump(mode="json"),
            headers=headers,
        )
        assert quota.status_code == 422
        assert quota.json()["detail"]["reason"] == "mask_quota_exceeded"
        assert not _object_exists(
            owner_s3, bucket, codec.build_rle_reference(quota_rle).object_key
        )

        _stop_runtime(content_process, content_log)
        content_process = content_log = None
        unavailable = await httpx_client.post(
            f"/api/v1/tasks/{task.id}/mask-content",
            json=failed_rle.model_dump(mode="json"),
            headers=headers,
        )
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"]["reason"] == "mask_storage_unavailable"
        await db_session.refresh(task)
        failed_key = codec.build_rle_reference(failed_rle).object_key
        assert (
            await db_session.scalar(
                select(RasterMaskUpload.id).where(
                    RasterMaskUpload.task_id == task.id,
                    RasterMaskUpload.object_key == failed_key,
                )
            )
            is None
        )
        assert not _object_exists(owner_s3, bucket, failed_key)

        # Remote deletion awaits a durable Labeling fence; exercise the current
        # embedded GC against the same owned S3 objects after the API outage.
        monkeypatch.setattr(settings, "assets_mask_content_url", "")
        monkeypatch.setattr(settings, "assets_mask_cleanup_url", "")

        db_session.add(
            RasterMaskUpload(
                task_id=task.id,
                object_key=orphan_ref.object_key,
                created_at=cutoff - timedelta(seconds=1),
            )
        )
        await db_session.flush()

        from app.application.raster_mask_retention import (
            is_raster_mask_key_referenced,
            referenced_raster_mask_keys,
        )
        from app.services.raster_mask_storage import (
            purge_unreferenced_raster_mask_objects,
        )

        rooted = await referenced_raster_mask_keys(db_session)
        assert root_ref.object_key in rooted
        target_keys = {
            recent_ref.object_key,
            root_ref.object_key,
            orphan_ref.object_key,
        }
        with MaskContentClient(cleanup_origin, cleanup_token) as cleanup:
            candidates = _all_candidates(cleanup)
        # The verifier-owned bucket may contain keys from earlier tests. Protect
        # every non-fixture candidate so this case can retire only its own orphan.
        rooted.update(item.key for item in candidates if item.key not in target_keys)
        result = await purge_unreferenced_raster_mask_objects(
            db_session,
            rooted,
            cutoff,
            is_referenced=is_raster_mask_key_referenced,
        )
        assert result["errors"] == 0
        assert result["deleted"] == 1
        assert _object_exists(owner_s3, bucket, recent_ref.object_key)
        assert _object_exists(owner_s3, bucket, root_ref.object_key)
        assert not _object_exists(owner_s3, bucket, orphan_ref.object_key)
        assert (
            await db_session.scalar(
                select(RasterMaskUpload.id).where(
                    RasterMaskUpload.task_id == task.id,
                    RasterMaskUpload.object_key == orphan_ref.object_key,
                )
            )
            is None
        )
        assert (
            await db_session.scalar(
                select(RasterMaskUpload.id).where(
                    RasterMaskUpload.task_id == task.id,
                    RasterMaskUpload.object_key == recent_ref.object_key,
                )
            )
            is not None
        )
    finally:
        if content_client is not None:
            content_client.close()
        _stop_runtime(content_process, content_log)
        _stop_runtime(cleanup_process, cleanup_log)


async def test_live_mask_gc_rechecks_a_committed_reservation_after_retiring_old_one(
    test_engine, tmp_path, monkeypatch
):
    (
        _resources,
        bucket,
        _other_bucket,
        owner_s3,
        content_credentials,
        cleanup_credentials,
    ) = _owned_resources(test_engine)
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = uuid4()
    task_id = uuid4()
    content_process = cleanup_process = None
    content_log = cleanup_log = None
    content_client = None

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
        monkeypatch.setattr(settings, "assets_mask_content_url", content_origin)
        monkeypatch.setattr(settings, "assets_mask_content_token", content_token)
        monkeypatch.setattr(settings, "assets_mask_cleanup_url", cleanup_origin)
        monkeypatch.setattr(settings, "assets_mask_cleanup_token", cleanup_token)
        content_client = MaskContentClient(content_origin, content_token)
        rle = _rle(uuid4().hex)
        monkeypatch.setattr(settings, "assets_mask_content_url", content_origin)
        monkeypatch.setattr(settings, "assets_mask_content_token", content_token)
        monkeypatch.setattr(settings, "assets_mask_cleanup_url", cleanup_origin)
        monkeypatch.setattr(settings, "assets_mask_cleanup_token", cleanup_token)
        reference = content_client.put(PutRequest(content=rle)).reference

        async with sessions.begin() as owner:
            owner.add(
                User(
                    id=actor_id,
                    email=f"mask-gc-race-{actor_id.hex}@example.test",
                    name="Mask GC race owner",
                    password_hash="test-only",
                    role="project_admin",
                    is_active=True,
                )
            )
            await owner.flush()
            project = Project(
                id=uuid4(),
                display_id=f"P-MCR-{actor_id.hex[:12]}",
                name=f"mask-gc-race-{actor_id.hex[:12]}",
                type_label="Image segmentation",
                type_key="image-seg",
                owner_id=actor_id,
                raster_mask_native_editing_enabled=True,
                tool_bindings={"region": {"enabled": True, "classes": ["object"]}},
            )
            owner.add(project)
            await owner.flush()
            task = Task(
                id=task_id,
                project_id=project.id,
                display_id=f"T-MCR-{actor_id.hex[:12]}",
                file_name="mask.png",
                file_path=f"mask-gc-race/{actor_id.hex}.png",
                file_type="image",
                status="pending",
            )
            owner.add(task)

        with MaskContentClient(cleanup_origin, cleanup_token) as cleanup:
            candidate = _find_candidate(cleanup, reference.object_key)
        assert candidate is not None
        cutoff = candidate.last_modified + timedelta(seconds=1)
        wait_seconds = (
            cutoff + timedelta(seconds=2) - datetime.now(timezone.utc)
        ).total_seconds()
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)

        async with sessions.begin() as owner:
            owner.add(
                RasterMaskUpload(
                    task_id=task_id,
                    object_key=reference.object_key,
                    created_at=cutoff - timedelta(seconds=1),
                )
            )

        from app.application.raster_mask_retention import is_raster_mask_key_referenced
        from app.services import raster_mask_storage
        from app.services.raster_mask_storage import (
            _delete_unreferenced_mask_candidate,
            reserve_raster_mask_upload,
            store_coco_rle,
        )
        from aap_assets_mask_content.storage import S3MaskContentStore

        local_cleanup = S3MaskContentStore(
            _local_s3_client(settings, *cleanup_credentials), bucket
        )
        original_lock = raster_mask_storage.lock_raster_mask_references
        race_lock_calls = 0

        async def add_committed_reservation_before_second_lock(
            db, references, **kwargs
        ):
            nonlocal race_lock_calls
            if (
                isinstance(references, dict)
                and references.get("object_key") == reference.object_key
            ):
                race_lock_calls += 1
                if race_lock_calls == 2:
                    async with sessions.begin() as racer:
                        await original_lock(
                            racer,
                            {"object_key": reference.object_key},
                            verify=False,
                        )
                        await reserve_raster_mask_upload(
                            racer,
                            task_id=task_id,
                            object_key=reference.object_key,
                            limit=256,
                        )
                        replayed = await store_coco_rle(rle.model_dump(mode="json"))
                        assert replayed["object_key"] == reference.object_key
            return await original_lock(db, references, **kwargs)

        monkeypatch.setattr(
            raster_mask_storage,
            "lock_raster_mask_references",
            add_committed_reservation_before_second_lock,
        )
        async with sessions() as gc_db:
            deleted = await _delete_unreferenced_mask_candidate(
                gc_db,
                candidate,
                local_cleanup,
                is_raster_mask_key_referenced,
                cutoff,
            )

        assert candidate.last_modified < cutoff
        assert race_lock_calls == 2
        assert deleted is False
        assert _object_exists(owner_s3, bucket, reference.object_key)
        async with sessions() as query_db:
            reservation = (
                await query_db.execute(
                    select(RasterMaskUpload).where(
                        RasterMaskUpload.task_id == task_id,
                        RasterMaskUpload.object_key == reference.object_key,
                    )
                )
            ).scalar_one()
            assert reservation.created_at >= cutoff
    finally:
        if content_client is not None:
            content_client.close()
        _stop_runtime(content_process, content_log)
        _stop_runtime(cleanup_process, cleanup_log)
