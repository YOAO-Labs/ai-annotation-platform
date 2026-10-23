"""v0.14.2 D1 — ZIP 上传保留子目录回归测试。

覆盖：
1. 保留子目录：SUSTech 式布局 zip 上传后 file_path 含完整子路径。
2. zip-slip 防护：含 "../etc/passwd" 的 entry 被跳过，不入库。
3. 跨子目录同名文件仅按 content_hash 去重。
4. 点云 scene 自动推断：SUSTech 布局上传后自动建 1 个 scene 并赋 frame_index。
5. 伪多 scene zip：顶层两个非角色子目录各含 lidar → 建 2 个 scene。
"""

from __future__ import annotations

import io
import uuid
import zipfile

import pytest
from sqlalchemy import select

from app.db.models.dataset import Dataset, DatasetItem, Scene
from app.services.dataset_storage_keys import (
    dataset_relative_path,
    managed_dataset_prefix,
)
from app.services.storage import TRUSTED_NUSCENES_PREFIX, storage_service
from tests.fakes.managed_creation_storage import CreationStorage

pytestmark = pytest.mark.asyncio


# ──────────────────────────── helpers ────────────────────────────────────────


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _FakePutOnlyClient(CreationStorage):
    """Realistic conditional creation fake for ZIP and its business mappings."""


@pytest.fixture(autouse=True)
def local_creation_client(monkeypatch):
    from app.services import managed_original_creation as creations

    monkeypatch.setattr(creations, "create_client", lambda **_: storage_service.client)


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


def _noop(*args, **kwargs):
    return None


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

    fake_client = _FakePutOnlyClient()
    monkeypatch.setattr(storage_service, "client", fake_client)
    monkeypatch.setattr(storage_service, "read_image_dimensions_from_bytes", _noop)
    monkeypatch.setattr("app.workers.media.generate_thumbnail.delay", _noop)

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

    # 查 DB：file_path 应含子目录
    rows = (
        (
            await db_session.execute(
                select(DatasetItem).where(DatasetItem.dataset_id == uuid.UUID(ds_id))
            )
        )
        .scalars()
        .all()
    )
    file_paths = {dataset_relative_path(item.file_path) for item in rows}
    assert all(
        item.file_path.startswith(managed_dataset_prefix(uuid.UUID(ds["id"])))
        for item in rows
    )
    assert "lidar/000970.pcd" in file_paths
    assert "camera/front/000970.jpg" in file_paths
    assert "calib/camera/front.json" in file_paths

    # 旧拍平行为（basename only）不应存在
    assert "000970.pcd" not in file_paths
    assert "000970.jpg" not in file_paths


async def test_upload_zip_zipslip_rejected(
    httpx_client, db_session, super_admin, monkeypatch
):
    """ZIP 含 '../etc/passwd' 的 entry 被跳过，正常 entry 正常入库。"""
    user, token = super_admin

    fake_client = _FakePutOnlyClient()
    monkeypatch.setattr(storage_service, "client", fake_client)
    monkeypatch.setattr(storage_service, "read_image_dimensions_from_bytes", _noop)

    ds = await _create_dataset(
        httpx_client, token, f"pc-slip-{uuid.uuid4().hex[:6]}", "point_cloud"
    )
    ds_id = ds["id"]

    zip_bytes = _make_zip(
        {
            "../etc/passwd": b"root:x:0:0",
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
    # 恶意 entry 跳过
    assert body["skipped"] == 1, body

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
    assert dataset_relative_path(rows[0].file_path) == "lidar/a.pcd"


async def test_upload_zip_cross_dir_same_name_dedup_by_hash(
    httpx_client, db_session, super_admin, monkeypatch
):
    """跨子目录同名文件：内容不同 → 两条都入库；内容相同 → 只入 1 条（deduped==1）。"""
    user, token = super_admin

    fake_client = _FakePutOnlyClient()
    monkeypatch.setattr(storage_service, "client", fake_client)
    monkeypatch.setattr(storage_service, "read_image_dimensions_from_bytes", _noop)
    monkeypatch.setattr("app.workers.media.generate_thumbnail.delay", _noop)

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
    paths_a = {dataset_relative_path(r.file_path) for r in rows_a}
    assert "camera/front/000970.jpg" in paths_a
    assert "camera/left/000970.jpg" in paths_a

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


async def test_upload_zip_scene_auto_inferred(
    httpx_client, db_session, super_admin, monkeypatch
):
    """SUSTech 布局 zip 上传后自动建 1 个 scene，lidar items 得到 frame_index 0 和 1。"""
    user, token = super_admin

    fake_client = _FakePutOnlyClient()
    monkeypatch.setattr(storage_service, "client", fake_client)
    monkeypatch.setattr(storage_service, "read_image_dimensions_from_bytes", _noop)
    monkeypatch.setattr("app.workers.media.generate_thumbnail.delay", _noop)

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
    lidar_frame_indices = sorted(it.frame_index for it in lidar_items)
    assert lidar_frame_indices == [0, 1], lidar_frame_indices


async def test_upload_zip_multi_scene_per_subdirectory(
    httpx_client, db_session, super_admin, monkeypatch
):
    """顶层两个非角色子目录各含 lidar → 建 2 个 scene。"""
    user, token = super_admin

    fake_client = _FakePutOnlyClient()
    monkeypatch.setattr(storage_service, "client", fake_client)
    monkeypatch.setattr(storage_service, "read_image_dimensions_from_bytes", _noop)

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

    fake_client = _FakePutOnlyClient()
    monkeypatch.setattr(storage_service, "client", fake_client)
    monkeypatch.setattr(storage_service, "read_image_dimensions_from_bytes", _noop)

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


async def test_upload_zip_temporal_validation_failure_preserves_creation_responsibility(
    httpx_client, db_session, super_admin, monkeypatch
):
    """A rejected ZIP rolls back items but keeps independently verified creations."""
    user, token = super_admin

    fake_client = _FakePutOnlyClient()
    monkeypatch.setattr(storage_service, "client", fake_client)
    monkeypatch.setattr(storage_service, "read_image_dimensions_from_bytes", _noop)

    # auto 模式下根级帧序列会塌缩成单 scene,无法稳定触发"无 scene"分支;
    # 直接把 scene_inference 短路成不建 scene,真实跑后续 422 + 清理逻辑。
    from app.services import scene_inference as _scene_inference

    class _NoScene:
        notes: list[str] = []

    async def _infer_noop(*_args, **_kwargs):
        return _NoScene()

    monkeypatch.setattr(_scene_inference, "infer_and_apply", _infer_noop)

    ds = await _create_dataset(
        httpx_client,
        token,
        f"temporal-fail-{uuid.uuid4().hex[:6]}",
        "image",
        is_temporal=True,
    )
    ds_id = ds["id"]

    zip_bytes = _make_zip(
        {
            "frame_000.jpg": b"img0",
            "frame_001.jpg": b"img1",
        }
    )

    resp = await httpx_client.post(
        f"/api/v1/datasets/{ds_id}/items/upload-zip",
        headers=_bearer(token),
        files={"file": ("flat.zip", zip_bytes, "application/zip")},
    )
    assert resp.status_code == 422, resp.text

    from app.db.models.managed_original_creation import ManagedOriginalCreation

    leftover_files = [k for k in fake_client.objects if not k.endswith("/")]
    assert len(leftover_files) == 2
    creations = list(
        await db_session.scalars(
            select(ManagedOriginalCreation).where(
                ManagedOriginalCreation.dataset_id == uuid.UUID(ds_id)
            )
        )
    )
    assert len(creations) == 2
    assert {row.object_key for row in creations} == set(leftover_files)
    assert all(row.status == "verified" for row in creations)
    assert not [call for call in fake_client.calls if call[0] == "delete"]

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
