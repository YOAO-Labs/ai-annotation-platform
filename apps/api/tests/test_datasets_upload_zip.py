"""v0.14.2 D1 — ZIP 上传保留子目录回归测试。

覆盖：
1. 保留子目录：SUSTech 式布局 zip 上传后 file_path 含完整子路径。
2. zip-slip 防护：含 "../etc/passwd" 的 entry 被跳过，不入库。
3. 跨子目录同名文件仅按 content_hash 去重。
4. 点云 scene 自动推断：SUSTech 布局上传后自动建 1 个 scene 并赋 frame_index。
5. 伪多 scene zip：顶层两个非角色子目录各含 lidar → 建 2 个 scene。
"""

from __future__ import annotations

import hashlib
import io
import uuid
import zipfile
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import select
from botocore.exceptions import ClientError

from app.config import settings
from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.services.storage import TRUSTED_NUSCENES_PREFIX, storage_service

pytestmark = pytest.mark.asyncio


# ──────────────────────────── helpers ────────────────────────────────────────


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _FakeDatasetSourceStorage:
    """Small S3 fake for immutable ZIP publications and legacy observations."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.parts: dict[str, list[bytes]] = {}
        self.deleted: list[str] = []
        self._next_upload_id = 0

    @staticmethod
    def _etag(body: bytes) -> str:
        return f'"{hashlib.md5(body).hexdigest()}"'

    def put_object(self, *, Bucket, Key, Body, ContentType=None, IfNoneMatch=None):
        if hasattr(Body, "read"):
            Body = Body.read()
        body = Body if isinstance(Body, bytes) else bytes(Body)
        if Key in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[Key] = body

    def head_object(self, *, Bucket, Key, IfMatch=None):
        body = self.objects.get(Key)
        if body is None:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        etag = self._etag(body)
        if IfMatch is not None and IfMatch != etag:
            raise ClientError({"Error": {"Code": "412"}}, "HeadObject")
        return {"ContentLength": len(body), "ETag": etag}

    def get_object(self, *, Bucket, Key, Range=None, IfMatch=None):
        metadata = self.head_object(Bucket=Bucket, Key=Key, IfMatch=IfMatch)
        body = self.objects[Key]
        if Range:
            start, end = Range.removeprefix("bytes=").split("-", 1)
            body = body[int(start) : int(end) + 1]
        return {**metadata, "Body": io.BytesIO(body)}

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None, MaxKeys=1000):
        keys = sorted(
            key
            for key in self.objects
            if key.startswith(Prefix) and not key.endswith("/")
        )
        if ContinuationToken:
            keys = [key for key in keys if key > ContinuationToken]
        page = keys[:MaxKeys]
        return {
            "Contents": [
                {
                    "Key": key,
                    "Size": len(self.objects[key]),
                    "ETag": self._etag(self.objects[key]),
                }
                for key in page
            ],
            "IsTruncated": len(keys) > MaxKeys,
            **({"NextContinuationToken": page[-1]} if len(keys) > MaxKeys else {}),
        }

    def get_paginator(self, operation_name):
        assert operation_name == "list_objects_v2"
        client = self

        class Paginator:
            def paginate(self, **parameters):
                yield client.list_objects_v2(**parameters)

        return Paginator()

    def create_multipart_upload(self, *, Bucket, Key, ContentType, IfNoneMatch="*"):
        self._next_upload_id += 1
        upload_id = str(self._next_upload_id)
        self.parts[upload_id] = []
        return {"UploadId": upload_id}

    def upload_part(self, *, Bucket, Key, PartNumber, UploadId, Body):
        self.parts[UploadId].append(bytes(Body))
        return {"ETag": f'"part-{PartNumber}"'}

    def complete_multipart_upload(
        self, *, Bucket, Key, UploadId, MultipartUpload, IfNoneMatch="*"
    ):
        if Key in self.objects:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed"}}, "CompleteMultipartUpload"
            )
        self.objects[Key] = b"".join(self.parts.pop(UploadId))
        return {"ETag": self._etag(self.objects[Key])}

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        self.parts.pop(UploadId, None)

    def delete_object(self, *, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    def head_bucket(self, *, Bucket):
        # create_dataset → ensure_bucket 会先 head_bucket 探测;假装 bucket 已存在。
        return {}


def _make_zip(entries: dict[str, bytes]) -> bytes:
    """在内存中构造 ZIP 包，entries = {zip内路径: 内容}。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for path, data in entries.items():
            zf.writestr(path, data)
    return buf.getvalue()


async def _create_dataset(
    httpx_client,
    token: str,
    name: str,
    data_type: str = "image",
    is_temporal: bool = False,
) -> dict:
    """通过 API 创建 dataset，返回响应 JSON。"""
    resp = await httpx_client.post(
        "/api/v1/datasets",
        headers=_bearer(token),
        json={"name": name, "data_type": data_type, "is_temporal": is_temporal},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _assert_zip_storage_paths(
    ds_id: str, rows: list[DatasetItem], expected: set[str]
) -> None:
    logical_paths = {
        (item.metadata_ or {}).get("source_ingest", {}).get("relpath"): item
        for item in rows
    }
    assert set(logical_paths) == expected
    for relpath, item in logical_paths.items():
        key_parts = item.file_path.split("/", 4)
        assert key_parts[:3] == ["dataset-sources", ds_id, str(item.id)]
        assert uuid.UUID(key_parts[3])
        assert key_parts[4] == relpath


def _use_local_source_runtime(monkeypatch) -> None:
    monkeypatch.setattr(settings, "assets_source_url", "")
    monkeypatch.setattr(settings, "assets_source_token", "")


# ──────────────────────────── test cases ─────────────────────────────────────


async def test_delete_item_rejects_mismatched_dataset(
    httpx_client, db_session, super_admin
):
    _, token = super_admin
    ds_a = await _create_dataset(
        httpx_client, token, f"delete-a-{uuid.uuid4().hex[:6]}"
    )
    ds_b = await _create_dataset(
        httpx_client, token, f"delete-b-{uuid.uuid4().hex[:6]}"
    )
    item = DatasetItem(
        dataset_id=uuid.UUID(ds_b["id"]),
        file_name="keep.jpg",
        file_path="keep.jpg",
        file_type="image",
    )
    db_session.add(item)
    await db_session.flush()

    response = await httpx_client.delete(
        f"/api/v1/datasets/{ds_a['id']}/items/{item.id}",
        headers=_bearer(token),
    )

    assert response.status_code == 404
    assert await db_session.get(DatasetItem, item.id) is item


async def test_reserved_nuscenes_storage_namespace_cannot_be_uploaded(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    create_response = await httpx_client.post(
        "/api/v1/datasets",
        headers=_bearer(token),
        json={"name": TRUSTED_NUSCENES_PREFIX, "data_type": "image"},
    )
    assert create_response.status_code == 422

    legacy = Dataset(
        display_id=f"DS-RSV-{uuid.uuid4().hex[:6]}",
        name=f"{TRUSTED_NUSCENES_PREFIX}-victim",
        data_type="image",
        created_by=user.id,
    )
    db_session.add(legacy)
    await db_session.flush()
    upload_response = await httpx_client.post(
        f"/api/v1/datasets/{legacy.id}/items/upload-init",
        headers=_bearer(token),
        json={"file_name": "target.bin", "content_type": "application/octet-stream"},
    )
    assert upload_response.status_code == 422


async def test_upload_zip_preserves_subdirectories(
    httpx_client, db_session, super_admin, monkeypatch
):
    """上传含子目录的 zip → DatasetItem.file_path 保留完整子路径，不拍平。"""
    user, token = super_admin
    _use_local_source_runtime(monkeypatch)

    fake_client = _FakeDatasetSourceStorage()
    monkeypatch.setattr(storage_service, "client", fake_client)

    ds = await _create_dataset(
        httpx_client, token, f"pc-d1-{uuid.uuid4().hex[:6]}", "point_cloud"
    )
    ds_id = ds["id"]

    zip_bytes = _make_zip(
        {
            "lidar/000970.pcd": b"pcd-data",
            "camera/front/000970.jpg": b"jpg-data",
            "calib/camera/front.json": b'{"extrinsic": []}',
        }
    )

    resp = await httpx_client.post(
        f"/api/v1/datasets/{ds_id}/items/upload-zip",
        headers=_bearer(token),
        files={"file": ("scene.zip", zip_bytes, "application/zip")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["added"] == 3, body

    # Logical paths preserve the ZIP subtree under immutable publications.
    rows = (
        (
            await db_session.execute(
                select(DatasetItem).where(DatasetItem.dataset_id == uuid.UUID(ds_id))
            )
        )
        .scalars()
        .all()
    )
    _assert_zip_storage_paths(
        ds_id,
        rows,
        {
            "lidar/000970.pcd",
            "camera/front/000970.jpg",
            "calib/camera/front.json",
        },
    )


async def test_upload_zip_zipslip_rejected(
    httpx_client, db_session, super_admin, monkeypatch
):
    """ZIP 含 '../etc/passwd' 的 entry 被跳过，正常 entry 正常入库。"""
    user, token = super_admin
    _use_local_source_runtime(monkeypatch)

    fake_client = _FakeDatasetSourceStorage()
    monkeypatch.setattr(storage_service, "client", fake_client)

    ds = await _create_dataset(
        httpx_client, token, f"pc-slip-{uuid.uuid4().hex[:6]}", "point_cloud"
    )
    ds_id = ds["id"]

    zip_bytes = _make_zip(
        {
            "../etc/passwd": b"root:x:0:0",
            ".DS_Store": b"metadata",
            "lidar/a.pcd": b"legit-pcd",
        }
    )

    resp = await httpx_client.post(
        f"/api/v1/datasets/{ds_id}/items/upload-zip",
        headers=_bearer(token),
        files={"file": ("x.zip", zip_bytes, "application/zip")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 正常文件入库
    assert body["added"] == 1, body
    # Unsafe entries are excluded; safe hidden entries still count in total_in_zip.
    assert body["skipped"] == 2, body
    assert body["total_in_zip"] == 2, body

    rows = (
        (
            await db_session.execute(
                select(DatasetItem).where(DatasetItem.dataset_id == uuid.UUID(ds_id))
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    _assert_zip_storage_paths(ds_id, rows, {"lidar/a.pcd"})


async def test_upload_zip_cross_dir_same_name_dedup_by_hash(
    httpx_client, db_session, super_admin, monkeypatch
):
    """跨子目录同名文件：内容不同 → 两条都入库；内容相同 → 只入 1 条（deduped==1）。"""
    user, token = super_admin
    _use_local_source_runtime(monkeypatch)

    fake_client = _FakeDatasetSourceStorage()
    monkeypatch.setattr(storage_service, "client", fake_client)

    # ── 情况 A：内容不同，两条都入库 ──
    ds_a = await _create_dataset(
        httpx_client, token, f"dedup-a-{uuid.uuid4().hex[:6]}", "image"
    )
    ds_a_id = ds_a["id"]

    zip_bytes_a = _make_zip(
        {
            "camera/front/000970.jpg": b"content-front",
            "camera/left/000970.jpg": b"content-left",
        }
    )
    resp_a = await httpx_client.post(
        f"/api/v1/datasets/{ds_a_id}/items/upload-zip",
        headers=_bearer(token),
        files={"file": ("a.zip", zip_bytes_a, "application/zip")},
    )
    assert resp_a.status_code == 200, resp_a.text
    body_a = resp_a.json()
    assert body_a["added"] == 2, body_a
    assert body_a["deduped"] == 0, body_a

    rows_a = (
        (
            await db_session.execute(
                select(DatasetItem).where(DatasetItem.dataset_id == uuid.UUID(ds_a_id))
            )
        )
        .scalars()
        .all()
    )
    _assert_zip_storage_paths(
        ds_a_id,
        rows_a,
        {"camera/front/000970.jpg", "camera/left/000970.jpg"},
    )

    # ── 情况 B：内容完全相同，只入 1 条 ──
    ds_b = await _create_dataset(
        httpx_client, token, f"dedup-b-{uuid.uuid4().hex[:6]}", "image"
    )
    ds_b_id = ds_b["id"]

    same_content = b"same-bytes"
    zip_bytes_b = _make_zip(
        {
            "camera/front/000970.jpg": same_content,
            "camera/left/000970.jpg": same_content,
        }
    )
    resp_b = await httpx_client.post(
        f"/api/v1/datasets/{ds_b_id}/items/upload-zip",
        headers=_bearer(token),
        files={"file": ("b.zip", zip_bytes_b, "application/zip")},
    )
    assert resp_b.status_code == 200, resp_b.text
    body_b = resp_b.json()
    assert body_b["added"] == 1, body_b
    assert body_b["deduped"] == 1, body_b


async def test_upload_zip_rollback_retains_old_and_new_immutable_source_bytes(
    httpx_client, db_session, super_admin, monkeypatch
):
    _, token = super_admin
    _use_local_source_runtime(monkeypatch)
    fake_client = _FakeDatasetSourceStorage()
    monkeypatch.setattr(storage_service, "client", fake_client)
    ds = await _create_dataset(
        httpx_client, token, f"immutable-{uuid.uuid4().hex[:6]}", "other"
    )
    ds_id = ds["id"]

    first_bytes = b"old source bytes"
    first_response = await httpx_client.post(
        f"/api/v1/datasets/{ds_id}/items/upload-zip",
        headers={**_bearer(token), "Idempotency-Key": str(uuid.uuid4())},
        files={
            "file": (
                "first.zip",
                _make_zip({"nested/asset.bin": first_bytes}),
                "application/zip",
            )
        },
    )
    assert first_response.status_code == 200, first_response.text
    first_item = (
        await db_session.execute(
            select(DatasetItem).where(DatasetItem.dataset_id == uuid.UUID(ds_id))
        )
    ).scalar_one()
    old_key = first_item.file_path
    assert fake_client.objects[old_key] == first_bytes

    monkeypatch.setattr(
        db_session,
        "commit",
        AsyncMock(side_effect=SQLAlchemyError("forced outer commit failure")),
    )
    replacement_bytes = b"new source bytes"
    failed_response = await httpx_client.post(
        f"/api/v1/datasets/{ds_id}/items/upload-zip",
        headers={**_bearer(token), "Idempotency-Key": str(uuid.uuid4())},
        files={
            "file": (
                "replacement.zip",
                _make_zip({"nested/asset.bin": replacement_bytes}),
                "application/zip",
            )
        },
    )

    assert failed_response.status_code == 503, failed_response.text
    publication_keys = [
        key
        for key in fake_client.objects
        if key.startswith(f"dataset-sources/{ds_id}/")
        and key.endswith("/nested/asset.bin")
    ]
    assert len(publication_keys) == 2, publication_keys
    assert fake_client.objects[old_key] == first_bytes
    assert replacement_bytes in [fake_client.objects[key] for key in publication_keys]
    assert fake_client.deleted == []
    rows = (
        (
            await db_session.execute(
                select(DatasetItem).where(DatasetItem.dataset_id == uuid.UUID(ds_id))
            )
        )
        .scalars()
        .all()
    )
    assert [item.id for item in rows] == [first_item.id]


async def test_upload_zip_scene_auto_inferred(
    httpx_client, db_session, super_admin, monkeypatch
):
    """SUSTech 布局 zip 上传后自动建 1 个 scene，lidar items 得到 frame_index 0 和 1。"""
    user, token = super_admin
    _use_local_source_runtime(monkeypatch)

    fake_client = _FakeDatasetSourceStorage()
    monkeypatch.setattr(storage_service, "client", fake_client)

    ds = await _create_dataset(
        httpx_client, token, f"scene-auto-{uuid.uuid4().hex[:6]}", "point_cloud"
    )
    ds_id = ds["id"]

    zip_bytes = _make_zip(
        {
            "lidar/000970.pcd": b"pcd0",
            "lidar/000971.pcd": b"pcd1",
            "camera/front/000970.jpg": b"jpg0",
            "camera/front/000971.jpg": b"jpg1",
            "calib/camera/front.json": b'{"extrinsic": []}',
        }
    )

    resp = await httpx_client.post(
        f"/api/v1/datasets/{ds_id}/items/upload-zip",
        headers=_bearer(token),
        files={"file": ("pc.zip", zip_bytes, "application/zip")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["added"] == 5, body

    # scene_inference_notes 非空（说明 scene 推断已运行）
    assert body["scene_inference_notes"], body

    # 查 DB：1 个 scene
    scenes = (
        (
            await db_session.execute(
                select(Scene).where(Scene.dataset_id == uuid.UUID(ds_id))
            )
        )
        .scalars()
        .all()
    )
    assert len(scenes) == 1, f"expected 1 scene, got {len(scenes)}"

    # lidar items frame_index 为 0 和 1。
    # 注意:.pcd 经 mimetypes 猜不出类型,upload_zip 落 file_type="other";
    # 但 scene_inference 的 group_frames 按后缀 .pcd 识别 lidar 并赋 frame_index,
    # 故这里按路径 /lidar/ 过滤,而非 file_type。
    lidar_items = (
        (
            await db_session.execute(
                select(DatasetItem).where(
                    DatasetItem.dataset_id == uuid.UUID(ds_id),
                    DatasetItem.file_path.like("%/lidar/%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(lidar_items) == 2
    assert {item.file_type for item in lidar_items} == {"other"}
    lidar_frame_indices = sorted(it.frame_index for it in lidar_items)
    assert lidar_frame_indices == [0, 1], lidar_frame_indices


async def test_upload_zip_multi_scene_per_subdirectory(
    httpx_client, db_session, super_admin, monkeypatch
):
    """顶层两个非角色子目录各含 lidar → 建 2 个 scene。"""
    user, token = super_admin
    _use_local_source_runtime(monkeypatch)

    fake_client = _FakeDatasetSourceStorage()
    monkeypatch.setattr(storage_service, "client", fake_client)

    ds = await _create_dataset(
        httpx_client, token, f"multi-scene-{uuid.uuid4().hex[:6]}", "point_cloud"
    )
    ds_id = ds["id"]

    zip_bytes = _make_zip(
        {
            "scene_a/lidar/000.pcd": b"pcd-a",
            "scene_b/lidar/000.pcd": b"pcd-b",
        }
    )

    resp = await httpx_client.post(
        f"/api/v1/datasets/{ds_id}/items/upload-zip",
        headers=_bearer(token),
        files={"file": ("multi.zip", zip_bytes, "application/zip")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["added"] == 2, body

    scenes = (
        (
            await db_session.execute(
                select(Scene).where(Scene.dataset_id == uuid.UUID(ds_id))
            )
        )
        .scalars()
        .all()
    )
    assert len(scenes) == 2, (
        f"expected 2 scenes, got {len(scenes)}: {[s.name for s in scenes]}"
    )


async def test_upload_zip_notes_reserved_role_top_level_mix(
    httpx_client, db_session, super_admin, monkeypatch
):
    """顶层混用角色名与 scene 名时,响应 notes 指出保留目录名冲突。"""
    user, token = super_admin
    _use_local_source_runtime(monkeypatch)

    fake_client = _FakeDatasetSourceStorage()
    monkeypatch.setattr(storage_service, "client", fake_client)

    ds = await _create_dataset(
        httpx_client, token, f"mixed-top-{uuid.uuid4().hex[:6]}", "point_cloud"
    )
    ds_id = ds["id"]

    zip_bytes = _make_zip(
        {
            "lidar/000.pcd": b"pcd-a",
            "scene_b/lidar/000.pcd": b"pcd-b",
        }
    )

    resp = await httpx_client.post(
        f"/api/v1/datasets/{ds_id}/items/upload-zip",
        headers=_bearer(token),
        files={"file": ("mixed.zip", zip_bytes, "application/zip")},
    )
    assert resp.status_code == 200, resp.text
    notes = resp.json()["scene_inference_notes"]
    assert any("保留角色目录" in note and "import-formats.md" in note for note in notes)


async def test_upload_zip_temporal_validation_rejects_empty_archive(
    httpx_client, db_session, super_admin, monkeypatch
):
    """Temporal ZIPs without importable files fail the owner-side scene guard."""
    user, token = super_admin
    _use_local_source_runtime(monkeypatch)

    fake_client = _FakeDatasetSourceStorage()
    monkeypatch.setattr(storage_service, "client", fake_client)

    ds = await _create_dataset(
        httpx_client,
        token,
        f"temporal-fail-{uuid.uuid4().hex[:6]}",
        "image",
        is_temporal=True,
    )
    ds_id = ds["id"]

    zip_bytes = _make_zip({})

    resp = await httpx_client.post(
        f"/api/v1/datasets/{ds_id}/items/upload-zip",
        headers=_bearer(token),
        files={"file": ("flat.zip", zip_bytes, "application/zip")},
    )
    assert resp.status_code == 422, resp.text
    assert (
        resp.json()["detail"] == "声明为时序数据集但未识别出任何 scene,请检查目录结构"
    )

    assert [key for key in fake_client.objects if not key.endswith("/")] == []

    # 事务 rollback,DatasetItem 不应落库
    rows = (
        (
            await db_session.execute(
                select(DatasetItem).where(DatasetItem.dataset_id == uuid.UUID(ds_id))
            )
        )
        .scalars()
        .all()
    )
    assert rows == []
