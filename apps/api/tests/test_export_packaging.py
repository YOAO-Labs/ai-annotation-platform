"""v0.10.27 · 导出打包纯函数单测（无 DB）。

覆盖 relative_path_from_file_path：剥 dataset 前缀拿相对路径（消除同名跨目录覆盖）。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import uuid
import zipfile
from types import SimpleNamespace

import pytest
from PIL import Image
from aap_assets_media.contract import InternalSourceRequest, MediaSource
from aap_assets_media.transport import SourceReference

from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.exporting.packaging import (
    _FETCH_FRAMES_TEMPLATE,
    _FETCH_VIDEOS_TEMPLATE,
    _build_video_export_zip,
    clean_export_targets,
    relative_path_from_file_path,
)


class _FakeMediaGateway:
    """Pure owner-reference double for the export packager's source reads."""

    def __init__(self, items):
        self.items = items

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def internal_source_reference(self, request, *, expires_in):
        assert isinstance(request, InternalSourceRequest)
        item = self.items[request.dataset_item_id]
        video_metadata = dict((item.metadata_ or {}).get("video") or {})
        source = MediaSource(
            dataset_item_id=item.id,
            dataset_id=item.dataset_id,
            source_generation=1,
            source_snapshot=hashlib.sha256(item.file_path.encode()).hexdigest(),
            timetable_revision=0,
            materialized_generation=1,
            object_identity=None,
            source_drifted=False,
            file_name=item.file_name,
            file_path=item.file_path,
            file_type=item.file_type,
            file_size=item.file_size,
            content_hash=item.content_hash,
            width=item.width,
            height=item.height,
            thumbnail_path=None,
            blurhash=None,
            video_metadata=video_metadata,
        )
        url = f"https://media.test/{source.file_path.lstrip('/')}"
        return SourceReference(
            source=source,
            original_url=url,
            video_url=url,
            poster_url=None,
            expires_in=expires_in,
        )


async def _materialize_video_chunks(chunks):
    batches = [batch async for batch in chunks]
    items = {}
    for _tasks, _annotations, batch_items in batches:
        items.update(batch_items)

    async def replay():
        for batch in batches:
            yield batch

    return items, replay()


def test_fetch_frames_script_extracts_only_selected_frames(tmp_path):
    from app.api.v1._test_seed_webcodecs import generate_fixture

    fixture = generate_fixture("h264-baseline-gop12", tmp_path)
    videos = tmp_path / "videos"
    videos.mkdir()
    (videos / "clip.mp4").write_bytes(fixture["mp4_bytes"])
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "videos": [
                    {
                        "rel_path": "clip.mp4",
                        "sequence": "clip",
                        "grid_source_frames": [0, 5, 11],
                        "sha256": hashlib.sha256(fixture["mp4_bytes"]).hexdigest(),
                        "size_bytes": len(fixture["mp4_bytes"]),
                    }
                ]
            }
        )
    )
    script = tmp_path / "fetch_frames.py"
    (tmp_path / "fetch_videos.py").write_text(_FETCH_VIDEOS_TEMPLATE)
    script.write_text(_FETCH_FRAMES_TEMPLATE)
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(path.name for path in (tmp_path / "clip" / "img1").iterdir()) == [
        "000001.jpg",
        "000002.jpg",
        "000003.jpg",
    ]


def test_strips_dataset_prefix():
    assert (
        relative_path_from_file_path("mydataset/animals/cat/001.jpg", "mydataset")
        == "animals/cat/001.jpg"
    )


def test_managed_identity_is_not_exported_as_directory():
    from app.services.dataset_storage_keys import managed_dataset_key

    key = managed_dataset_key(uuid.uuid4(), "scene-1/camera/front/001.jpg")
    assert (
        relative_path_from_file_path(key, "renamed") == "scene-1/camera/front/001.jpg"
    )


def test_same_leaf_different_dir_stay_distinct():
    a = relative_path_from_file_path("ds/animals/cat/001.jpg", "ds")
    b = relative_path_from_file_path("ds/animals/dog/001.jpg", "ds")
    assert a == "animals/cat/001.jpg"
    assert b == "animals/dog/001.jpg"
    assert a != b


def test_prefix_mismatch_returns_path_unchanged():
    # 首段非 dataset_name 时保守返回（不误删层级）。
    assert (
        relative_path_from_file_path("other/animals/cat/001.jpg", "mydataset")
        == "other/animals/cat/001.jpg"
    )


def test_leading_slash_normalized():
    assert relative_path_from_file_path("/ds/a/b.jpg", "ds") == "a/b.jpg"


def test_empty_dataset_name_returns_full_path():
    assert relative_path_from_file_path("ds/a/b.jpg", "") == "ds/a/b.jpg"


def test_flat_file_at_dataset_root():
    assert relative_path_from_file_path("ds/001.jpg", "ds") == "001.jpg"


def test_clean_export_targets_accepts_video_yolo_frames_det():
    assert clean_export_targets(["video_json", "yolo-frames-det", "video_json"]) == [
        "video_json",
        "yolo-frames-det",
    ]


def test_clean_export_targets_rejects_video_target_for_image_project():
    # v0.10.47 · 图像项目混入视频目标应在端点层就被拒，而非派发后整批失败。
    with pytest.raises(ValueError, match="image project"):
        clean_export_targets(["coco", "mot"], data_type="image")


def test_clean_export_targets_rejects_image_target_for_video_project():
    with pytest.raises(ValueError, match="video project"):
        clean_export_targets(["video_json", "coco"], data_type="video")


def test_clean_export_targets_aap_json_valid_for_both_modalities():
    # aap_json 同属图像/视频目标集，两侧都应放行。
    assert clean_export_targets(["aap_json"], data_type="image") == ["aap_json"]
    assert clean_export_targets(["aap_json"], data_type="video") == ["aap_json"]


async def test_video_yolo_frames_zip_writes_grid_labels_and_manifest(monkeypatch):
    monkeypatch.setattr(
        "app.services.exporting.packaging.storage_service.generate_download_url",
        lambda *args, **kwargs: "signed-url",
    )
    project_id = uuid.uuid4()
    item_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    task_id = uuid.uuid4()
    project = Project(
        id=project_id,
        display_id="P-1",
        name="Video Project",
        type_key="video-track",
        type_label="Video Track",
        data_type="video",
        owner_id=uuid.uuid4(),
        video_sampling={"mode": "step", "frame_step": 2},
        tool_bindings={
            "bbox": {
                "enabled": True,
                "classes": [
                    {"name": "car", "order": 0},
                    {"name": "person", "order": 1},
                ],
                "attribute_schema": {"fields": [{"key": "speed"}]},
            }
        },
    )
    item = DatasetItem(
        id=item_id,
        dataset_id=dataset_id,
        file_name="clip-a.mp4",
        file_path="videos/clip-a.mp4",
        file_type="video",
        metadata_={"video": {"fps": 10, "frame_count": 5, "width": 640, "height": 360}},
    )
    task = Task(
        id=task_id,
        project_id=project_id,
        dataset_item_id=item_id,
        display_id="T-1",
        file_name="clip-a.mp4",
        file_path="videos/clip-a.mp4",
        file_type="video",
    )
    track = Annotation(
        id=uuid.uuid4(),
        task_id=task_id,
        project_id=project_id,
        user_id=uuid.uuid4(),
        annotation_type="video_track_bbox",
        class_name="car",
        geometry={
            "type": "video_track_bbox",
            "track_id": "trk-1",
            "keyframes": [
                {"frame_index": 0, "bbox": {"x": 0.0, "y": 0.0, "w": 0.2, "h": 0.2}},
                {"frame_index": 4, "bbox": {"x": 0.6, "y": 0.6, "w": 0.2, "h": 0.2}},
            ],
            "outside": [{"from": 2, "to": 2}],
        },
        attributes={"speed": 50},
    )
    bbox = Annotation(
        id=uuid.uuid4(),
        task_id=task_id,
        project_id=project_id,
        user_id=uuid.uuid4(),
        annotation_type="video_bbox",
        class_name="person",
        geometry={
            "type": "video_bbox",
            "frame_index": 2,
            "x": 0.2,
            "y": 0.2,
            "w": 0.2,
            "h": 0.4,
        },
        attributes={"speed": 3},
    )

    # v0.12.1 · 流式签名：喂内存 chunk（单块），ZIP 落盘后断言。
    async def _chunks():
        yield [task], {task_id: [track, bbox]}, {item_id: item}

    data, file_count, size_bytes = await _run_video_zip_with_size(
        project, _chunks(), ["yolo-frames-det"]
    )
    assert size_bytes == len(data)

    assert file_count == 3
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert "data.yaml" in zf.namelist()
        assert "fetch_frames.py" in zf.namelist()
        assert zf.read("labels/clip-a/000001.txt").decode() == (
            "0 0.100000 0.100000 0.200000 0.200000"
        )
        assert zf.read("labels/clip-a/000002.txt").decode() == (
            "1 0.300000 0.400000 0.200000 0.400000"
        )
        assert zf.read("labels/clip-a/000003.txt").decode() == (
            "0 0.700000 0.700000 0.200000 0.200000"
        )
        manifest = json.loads(zf.read("manifest.json"))
        video = manifest["videos"][0]
        assert video["grid_source_frames"] == [0, 2, 4]
        assert video["frame_start_number"] == 1
        assert video["frame_output_dirs"] == ["images/clip-a"]


# ── polygon / polyline 几何进入视频导出（打包层白名单）─────────────────────
#
# 打包层此前只把 video_track_bbox / video_bbox 分组进 tracks / bboxes，其余几何被
# **静默丢弃**：polygon 标注了却一行都导不出。连带使 v0.21.20 为 points track 写的
# 外接框降级成了端到端死代码（只有纯函数单测覆盖，故 bug 潜伏至今）。
#
# 下面两个用例的几何是 test_video_yolo_frames_zip_writes_grid_labels_and_manifest
# 的 **points 版对偶**：外接框刻意取成与那里的 bbox 完全相同，故期望输出逐字节一致。


def _video_project_and_task(project_id, item_id, dataset_id, task_id):
    project = Project(
        id=project_id,
        display_id="P-2",
        name="Video Polygon Project",
        type_key="video-track",
        type_label="Video Track",
        data_type="video",
        owner_id=uuid.uuid4(),
        video_sampling={"mode": "step", "frame_step": 2},
        tool_bindings={
            "bbox": {
                "enabled": True,
                "classes": [
                    {"name": "car", "order": 0},
                    {"name": "person", "order": 1},
                ],
                "attribute_schema": {"fields": [{"key": "speed"}]},
            }
        },
    )
    item = DatasetItem(
        id=item_id,
        dataset_id=dataset_id,
        file_name="clip-a.mp4",
        file_path="videos/clip-a.mp4",
        file_type="video",
        metadata_={"video": {"fps": 10, "frame_count": 5, "width": 640, "height": 360}},
    )
    task = Task(
        id=task_id,
        project_id=project_id,
        dataset_item_id=item_id,
        display_id="T-1",
        file_name="clip-a.mp4",
        file_path="videos/clip-a.mp4",
        file_type="video",
    )
    return project, item, task


def _polygon_track(task_id, project_id):
    """外接框等价于 bbox track 版：frame0 (0,0,.2,.2) / frame4 (.6,.6,.2,.2)。"""
    return Annotation(
        id=uuid.uuid4(),
        task_id=task_id,
        project_id=project_id,
        user_id=uuid.uuid4(),
        annotation_type="video_track_polygon",
        class_name="car",
        geometry={
            "type": "video_track_polygon",
            "track_id": "trk-poly",
            "keyframes": [
                {"frame_index": 0, "points": [[0.0, 0.0], [0.2, 0.0], [0.2, 0.2]]},
                {"frame_index": 4, "points": [[0.6, 0.6], [0.8, 0.6], [0.8, 0.8]]},
            ],
            "outside": [{"from": 2, "to": 2}],
        },
        attributes={"speed": 50},
    )


def _single_frame_polygon(task_id, project_id):
    """外接框等价于单帧 bbox 版：frame2 (.2,.2,.2,.4)。"""
    return Annotation(
        id=uuid.uuid4(),
        task_id=task_id,
        project_id=project_id,
        user_id=uuid.uuid4(),
        annotation_type="video_polygon",
        class_name="person",
        geometry={
            "type": "video_polygon",
            "frame_index": 2,
            "points": [[0.2, 0.2], [0.4, 0.2], [0.4, 0.6], [0.2, 0.6]],
        },
        attributes={"speed": 3},
    )


async def _run_video_zip_with_size(project, chunks, targets):
    from app.services.exporting import packaging

    items, replay = await _materialize_video_chunks(chunks)
    fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                packaging,
                "media_gateway",
                lambda _db: _FakeMediaGateway(items),
            )
            ret_path, file_count, size_bytes = await _build_video_export_zip(
                SimpleNamespace(db=object()),
                project,
                replay,
                tmp_path=tmp_path,
                batch_id=None,
                targets=targets,
                include_attributes=True,
                video_frame_mode="keyframes",
            )
        assert ret_path == tmp_path
        with open(tmp_path, "rb") as exported:
            data = exported.read()
        return data, file_count, size_bytes
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def _run_video_zip(project, chunks, targets):
    data, file_count, _size_bytes = await _run_video_zip_with_size(
        project, chunks, targets
    )
    return data, file_count


async def test_polygon_geometries_reach_yolo_frames_det_as_bounding_boxes(monkeypatch):
    """单帧 polygon 与 polygon track 都要导出为顶点外接框，而非被丢弃 / 全 0 空框。

    单帧 polygon 尤其凶险：它没有 x/y/w/h，若不降级就会被 ``_yolo_det_line`` 的
    ``.get("x", 0)`` 导成 `1 0.000000 0.000000 0.000000 0.000000` —— 一条看似合法的
    空标注，比直接丢弃更坏。
    """
    monkeypatch.setattr(
        "app.services.exporting.packaging.storage_service.generate_download_url",
        lambda *args, **kwargs: "signed-url",
    )
    project_id, item_id, dataset_id, task_id = (uuid.uuid4() for _ in range(4))
    project, item, task = _video_project_and_task(
        project_id, item_id, dataset_id, task_id
    )
    track = _polygon_track(task_id, project_id)
    single = _single_frame_polygon(task_id, project_id)

    async def _chunks():
        yield [task], {task_id: [track, single]}, {item_id: item}

    data, file_count = await _run_video_zip(project, _chunks(), ["yolo-frames-det"])

    assert file_count == 3
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # 与 bbox 版对偶用例逐字节一致 —— 证明外接框降级正确。
        assert zf.read("labels/clip-a/000001.txt").decode() == (
            "0 0.100000 0.100000 0.200000 0.200000"
        )
        assert zf.read("labels/clip-a/000002.txt").decode() == (
            "1 0.300000 0.400000 0.200000 0.400000"
        )
        assert zf.read("labels/clip-a/000003.txt").decode() == (
            "0 0.700000 0.700000 0.200000 0.200000"
        )


async def test_polyline_single_frame_reaches_yolo_frames_det(monkeypatch):
    """开路径 polyline 同样降级为顶点外接框（不闭合不影响外接框）。"""
    monkeypatch.setattr(
        "app.services.exporting.packaging.storage_service.generate_download_url",
        lambda *args, **kwargs: "signed-url",
    )
    project_id, item_id, dataset_id, task_id = (uuid.uuid4() for _ in range(4))
    project, item, task = _video_project_and_task(
        project_id, item_id, dataset_id, task_id
    )
    polyline = Annotation(
        id=uuid.uuid4(),
        task_id=task_id,
        project_id=project_id,
        user_id=uuid.uuid4(),
        annotation_type="video_polyline",
        class_name="car",
        geometry={
            "type": "video_polyline",
            "frame_index": 0,
            "points": [[0.1, 0.1], [0.5, 0.3], [0.3, 0.5]],
        },
        attributes={},
    )

    async def _chunks():
        yield [task], {task_id: [polyline]}, {item_id: item}

    data, _ = await _run_video_zip(project, _chunks(), ["yolo-frames-det"])

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # 外接框 x∈[0.1,0.5] y∈[0.1,0.5] → cx=cy=0.3, w=h=0.4
        assert zf.read("labels/clip-a/000001.txt").decode() == (
            "0 0.300000 0.300000 0.400000 0.400000"
        )
        assert zf.read("labels/clip-a/000002.txt").decode() == ""


async def test_polygon_track_reaches_mot_gt(monkeypatch):
    """polygon track 也要进 MOT gt.txt（此前打包层丢弃 → gt.txt 空）。"""
    monkeypatch.setattr(
        "app.services.exporting.packaging.storage_service.generate_download_url",
        lambda *args, **kwargs: "signed-url",
    )
    project_id, item_id, dataset_id, task_id = (uuid.uuid4() for _ in range(4))
    project, item, task = _video_project_and_task(
        project_id, item_id, dataset_id, task_id
    )
    track = _polygon_track(task_id, project_id)

    async def _chunks():
        yield [task], {task_id: [track]}, {item_id: item}

    data, _ = await _run_video_zip(project, _chunks(), ["mot"])

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        gt_name = next(n for n in zf.namelist() if n.endswith("gt/gt.txt"))
        rows = [r for r in zf.read(gt_name).decode().splitlines() if r]
        assert rows, "polygon track 必须出现在 MOT gt.txt 中"
        # 640x360 上 frame0 外接框 (0,0,.2,.2) → 像素 (0,0,128,72)，MOT 帧号 1-based。
        assert rows[0].startswith("1,1,0.0,0.0,128.0,72.0")
        # outside 覆盖 frame 2 → 网格帧 2 无行；frame 4 → MOT 帧 3。
        assert any(r.startswith("3,1,") for r in rows)
        assert not any(r.startswith("2,1,") for r in rows)


# ── yolo-frames-seg：保留多边形顶点（不降级为外接框）───────────────────────


async def test_yolo_frames_seg_keeps_polygon_vertices(monkeypatch):
    """seg 导出保留原始顶点；同一几何在 det 里是外接框，在 seg 里是多边形。"""
    monkeypatch.setattr(
        "app.services.exporting.packaging.storage_service.generate_download_url",
        lambda *args, **kwargs: "signed-url",
    )
    project_id, item_id, dataset_id, task_id = (uuid.uuid4() for _ in range(4))
    project, item, task = _video_project_and_task(
        project_id, item_id, dataset_id, task_id
    )
    single = _single_frame_polygon(task_id, project_id)  # frame 2, 4 顶点矩形

    async def _chunks():
        yield [task], {task_id: [single]}, {item_id: item}

    data, _ = await _run_video_zip(project, _chunks(), ["yolo-frames-seg"])

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert "data.yaml" in zf.namelist()
        assert "fetch_frames.py" in zf.namelist()
        # 顶点原样展平，而非 det 版的 `1 0.3 0.4 0.2 0.4` 外接框中心宽高。
        assert zf.read("labels/clip-a/000002.txt").decode() == (
            "1 0.200000 0.200000 0.400000 0.200000 0.400000 0.600000 0.200000 0.600000"
        )
        # 其余采样帧建空 txt（YOLO 要求每张图都有 label 文件）。
        assert zf.read("labels/clip-a/000001.txt").decode() == ""
        assert zf.read("labels/clip-a/000003.txt").decode() == ""


async def test_yolo_frames_seg_expands_polygon_track_per_frame(monkeypatch):
    """polygon track 按帧展开为 seg 行；outside 帧不产出。"""
    monkeypatch.setattr(
        "app.services.exporting.packaging.storage_service.generate_download_url",
        lambda *args, **kwargs: "signed-url",
    )
    project_id, item_id, dataset_id, task_id = (uuid.uuid4() for _ in range(4))
    project, item, task = _video_project_and_task(
        project_id, item_id, dataset_id, task_id
    )
    track = _polygon_track(task_id, project_id)  # kf@0 与 kf@4，outside 覆盖 2

    async def _chunks():
        yield [task], {task_id: [track]}, {item_id: item}

    data, _ = await _run_video_zip(project, _chunks(), ["yolo-frames-seg"])

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.read("labels/clip-a/000001.txt").decode() == (
            "0 0.000000 0.000000 0.200000 0.000000 0.200000 0.200000"
        )
        assert zf.read("labels/clip-a/000002.txt").decode() == ""  # outside
        assert zf.read("labels/clip-a/000003.txt").decode() == (
            "0 0.600000 0.600000 0.800000 0.600000 0.800000 0.800000"
        )


async def test_yolo_frames_seg_skips_bbox_and_polyline(monkeypatch):
    """对齐图片侧 yolo-seg：矩形框与折线不产出 seg 行（折线非闭合区域）。"""
    monkeypatch.setattr(
        "app.services.exporting.packaging.storage_service.generate_download_url",
        lambda *args, **kwargs: "signed-url",
    )
    project_id, item_id, dataset_id, task_id = (uuid.uuid4() for _ in range(4))
    project, item, task = _video_project_and_task(
        project_id, item_id, dataset_id, task_id
    )
    bbox = Annotation(
        id=uuid.uuid4(),
        task_id=task_id,
        project_id=project_id,
        user_id=uuid.uuid4(),
        annotation_type="video_bbox",
        class_name="person",
        geometry={
            "type": "video_bbox",
            "frame_index": 0,
            "x": 0.1,
            "y": 0.1,
            "w": 0.2,
            "h": 0.2,
        },
        attributes={},
    )
    polyline = Annotation(
        id=uuid.uuid4(),
        task_id=task_id,
        project_id=project_id,
        user_id=uuid.uuid4(),
        annotation_type="video_polyline",
        class_name="car",
        geometry={
            "type": "video_polyline",
            "frame_index": 0,
            "points": [[0.1, 0.1], [0.5, 0.3], [0.3, 0.5]],
        },
        attributes={},
    )

    async def _chunks():
        yield [task], {task_id: [bbox, polyline]}, {item_id: item}

    data, _ = await _run_video_zip(project, _chunks(), ["yolo-frames-seg"])

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.read("labels/clip-a/000001.txt").decode() == ""


def test_clean_export_targets_accepts_video_yolo_frames_seg():
    assert clean_export_targets(
        ["yolo-frames-det", "yolo-frames-seg"], data_type="video"
    ) == ["yolo-frames-det", "yolo-frames-seg"]


def test_clean_export_targets_rejects_yolo_frames_seg_for_image_project():
    with pytest.raises(ValueError, match="image project"):
        clean_export_targets(["coco", "yolo-frames-seg"], data_type="image")


# ── coco-frames-seg：视频逐帧 COCO instance segmentation 单文档 ───────────────


async def test_coco_frames_seg_writes_single_coco_doc(monkeypatch):
    """单 target 时 annotations.json 落包根：像素 segmentation、空帧 image、结构契约完整。"""
    monkeypatch.setattr(
        "app.services.exporting.packaging.storage_service.generate_download_url",
        lambda *args, **kwargs: "signed-url",
    )
    project_id, item_id, dataset_id, task_id = (uuid.uuid4() for _ in range(4))
    project, item, task = _video_project_and_task(
        project_id, item_id, dataset_id, task_id
    )
    track = _polygon_track(task_id, project_id)  # car，outside 覆盖 frame2
    single = _single_frame_polygon(task_id, project_id)  # person@frame2

    async def _chunks():
        yield [task], {task_id: [track, single]}, {item_id: item}

    data, _ = await _run_video_zip(project, _chunks(), ["coco-frames-seg"])

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        assert "annotations.json" in names  # 单 target 落包根
        assert "fetch_frames.py" in names
        doc = json.loads(zf.read("annotations.json"))

    assert [im["file_name"] for im in doc["images"]] == [
        "images/clip-a/000001.jpg",
        "images/clip-a/000002.jpg",
        "images/clip-a/000003.jpg",
    ]
    assert [im["source_frame_index"] for im in doc["images"]] == [0, 2, 4]

    # 结构契约：引用完整 + 字段齐全（等价 pycocotools createIndex 可成功）。
    img_ids = {im["id"] for im in doc["images"]}
    cat_ids = {c["id"] for c in doc["categories"]}
    for a in doc["annotations"]:
        assert a["image_id"] in img_ids
        assert a["category_id"] in cat_ids
        assert a["iscrowd"] == 0
        assert len(a["bbox"]) == 4
        assert isinstance(a["segmentation"], list) and a["segmentation"]
        assert isinstance(a["segmentation"][0], list)

    # person 单帧 polygon → 像素坐标 segmentation（640×360）。
    person = next(a for a in doc["annotations"] if a["category_id"] == 1)
    assert person["segmentation"] == [
        [128.0, 72.0, 256.0, 72.0, 256.0, 216.0, 128.0, 216.0]
    ]
    assert person["bbox"] == [128.0, 72.0, 128.0, 144.0]
    # car track 展开：outside frame2 省略 → 落 image_id 0（frame0）与 2（frame4）。
    car_imgs = sorted(
        a["image_id"] for a in doc["annotations"] if a["category_id"] == 0
    )
    assert car_imgs == [0, 2]


async def test_coco_frames_seg_multi_target_subdir_and_frame_dirs(monkeypatch):
    """多 target 时落 coco-frames-seg/ 子目录，且 manifest 带 images 抽帧目录。"""
    monkeypatch.setattr(
        "app.services.exporting.packaging.storage_service.generate_download_url",
        lambda *args, **kwargs: "signed-url",
    )
    project_id, item_id, dataset_id, task_id = (uuid.uuid4() for _ in range(4))
    project, item, task = _video_project_and_task(
        project_id, item_id, dataset_id, task_id
    )
    single = _single_frame_polygon(task_id, project_id)

    async def _chunks():
        yield [task], {task_id: [single]}, {item_id: item}

    data, _ = await _run_video_zip(
        project, _chunks(), ["yolo-frames-seg", "coco-frames-seg"]
    )

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        assert "coco-frames-seg/annotations.json" in names
        assert "yolo-frames-seg/labels/clip-a/000002.txt" in names
        manifest = json.loads(zf.read("manifest.json"))

    dirs = manifest["videos"][0]["frame_output_dirs"]
    assert "coco-frames-seg/images/clip-a" in dirs
    assert "yolo-frames-seg/images/clip-a" in dirs


def test_clean_export_targets_accepts_video_coco_frames_seg():
    assert clean_export_targets(
        ["yolo-frames-seg", "coco-frames-seg"], data_type="video"
    ) == ["yolo-frames-seg", "coco-frames-seg"]


def test_clean_export_targets_rejects_coco_frames_seg_for_image_project():
    with pytest.raises(ValueError, match="image project"):
        clean_export_targets(["coco", "coco-frames-seg"], data_type="image")


async def test_davis_zip_writes_full_resolution_palette_png_and_independent_frame_spec(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.exporting.packaging.storage_service.generate_download_url",
        lambda *args, **kwargs: "signed-url",
    )
    project_id, item_id, dataset_id, task_id = (uuid.uuid4() for _ in range(4))
    project, item, task = _video_project_and_task(
        project_id, item_id, dataset_id, task_id
    )
    item.width = 3
    item.height = 2
    item.metadata_ = {"video": {"fps": 10, "frame_count": 5, "width": 3, "height": 2}}
    reference = {
        "encoding": "coco_rle_ref",
        "size": [2, 3],
        "object_key": "raster-masks/sha256/aa/bb/" + "a" * 64 + ".json",
        "sha256": "a" * 64,
        "runs": 3,
        "bytes": 50,
    }
    rle = {
        "encoding": "coco_rle",
        "size": [2, 3],
        "counts": [0, 1, 5],
    }

    async def _fake_load(_reference):
        return rle

    monkeypatch.setattr("app.services.exporting.packaging.load_coco_rle", _fake_load)
    mask = Annotation(
        id=uuid.uuid4(),
        task_id=task_id,
        project_id=project_id,
        user_id=uuid.uuid4(),
        annotation_type="video_track_mask",
        tool_unit_id="region",
        class_name="car",
        geometry={
            "type": "video_track_mask",
            "track_id": "trk-mask",
            "keyframes": [{"frame_index": 0, "mask": reference, "source": "manual"}],
            "outside": [{"from": 2, "to": 2, "source": "manual"}],
        },
        z_order=3,
    )

    async def _chunks():
        yield [task], {task_id: [mask]}, {item_id: item}

    data, _ = await _run_video_zip(project, _chunks(), ["yolo-frames-det", "davis"])
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.read("davis/ImageSets/2017/val.txt").decode() == "clip-a\n"
        names = zf.namelist()
        assert "davis/Annotations/Full-Resolution/clip-a/00000.png" in names
        assert "davis/Annotations/Full-Resolution/clip-a/00001.png" in names
        assert "davis/Annotations/Full-Resolution/clip-a/00002.png" in names
        first = Image.open(
            io.BytesIO(zf.read("davis/Annotations/Full-Resolution/clip-a/00000.png"))
        )
        middle = Image.open(
            io.BytesIO(zf.read("davis/Annotations/Full-Resolution/clip-a/00001.png"))
        )
        assert first.mode == "P"
        assert list(first.getdata()) == [1, 0, 0, 0, 0, 0]
        assert list(middle.getdata()) == [0, 0, 0, 0, 0, 0]
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["videos"][0]["frame_outputs"] == [
        {
            "dir": "yolo-frames-det/images/clip-a",
            "start_number": 1,
            "padding": 6,
            "extension": "jpg",
        },
        {
            "dir": "davis/JPEGImages/Full-Resolution/clip-a",
            "start_number": 0,
            "padding": 5,
            "extension": "jpg",
        },
    ]


async def test_davis_zip_rejects_sequence_name_collisions(monkeypatch):
    monkeypatch.setattr(
        "app.services.exporting.packaging.storage_service.generate_download_url",
        lambda *args, **kwargs: "signed-url",
    )
    project_id, item_id, dataset_id, task_id = (uuid.uuid4() for _ in range(4))
    project, item, task = _video_project_and_task(
        project_id, item_id, dataset_id, task_id
    )
    second_item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset_id,
        file_name="clip-a.mov",
        file_path="videos/clip-a.mov",
        file_type="video",
        metadata_=item.metadata_,
    )
    second_task = Task(
        id=uuid.uuid4(),
        project_id=project_id,
        dataset_item_id=second_item.id,
        display_id="T-2",
        file_name="clip-a.mov",
        file_path="videos/clip-a.mov",
        file_type="video",
    )

    async def _chunks():
        yield [task, second_task], {}, {item.id: item, second_item.id: second_item}

    with pytest.raises(ValueError, match="DAVIS sequence name collision"):
        await _run_video_zip(project, _chunks(), ["davis"])


def test_clean_export_targets_accepts_davis_only_for_video():
    assert clean_export_targets(["davis"], data_type="video") == ["davis"]
    with pytest.raises(ValueError, match="image project"):
        clean_export_targets(["davis"], data_type="image")
