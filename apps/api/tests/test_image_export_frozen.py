"""Pure image format regressions: frozen rows, canonical dimensions and receipts."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import uuid
import zipfile

import pytest

from app.services.content_snapshot import ContentError
from app.services.exporting.image_frozen import FrozenImage
from app.services.exporting import packaging, service


def forbidden(*args, **kwargs):
    pytest.fail(
        "image serialization must not read live business rows or sign raw originals"
    )


@pytest.fixture(autouse=True)
def owned_zip_path(monkeypatch, tmp_path):
    def allocate():
        path = tmp_path / (uuid.uuid4().hex + ".zip")
        path.touch()
        return str(path)

    monkeypatch.setattr(packaging, "_new_zip_tempfile", allocate)
    yield
    import shutil

    shutil.rmtree(tmp_path)


def facts(*, reference=False, primary=False):
    project, task, item, dataset, scene, track, batch = [uuid.uuid4() for _ in range(7)]
    stamp = "2026-01-01T00:00:00+00:00"
    annotation = {
        "id": str(uuid.uuid4()),
        "project_id": str(project),
        "task_id": str(task),
        "class_name": "car",
        "geometry": {"type": "bbox", "x": 0.25, "y": 0.0, "w": 0.5, "h": 1.0},
        "tool_unit_id": "bbox",
        "source": "manual",
        "temporal_role": "sample",
        "attributes": {"color": "red", "removed": "secret", "_audit": "retained"},
        "created_at": stamp,
        "version": 4,
        "scene_track_id": str(track),
        "track_id": "trk_frozen",
    }
    return {
        "schema": 3,
        "project_id": str(project),
        "batch_id": str(batch),
        "task_ids": None,
        "targets": ["coco", "aap_json", "yolo-det"],
        "options": {},
        "project": {
            "id": str(project),
            "name": "frozen",
            "display_id": "P0001",
            "data_type": "image",
            "type_key": "image",
            "rendering_config": {"zoom": 2},
            "annotation_guide": "original guide",
            "tool_bindings": {
                "bbox": {
                    "enabled": True,
                    "classes": [{"name": "car"}],
                    "attribute_schema": {"fields": [{"key": "color", "type": "text"}]},
                },
                "region": {"enabled": True, "classes": [{"name": "car"}]},
            },
        },
        "tasks": [
            {
                "id": str(task),
                "project_id": str(project),
                "batch_id": str(batch),
                "dataset_item_id": None if primary else str(item),
                "file_name": "same.png",
                "file_path": f"__aap_reference__/{item}/" + "a" * 64
                if reference
                else "private/current-key.png",
                "display_id": "T0001",
                "file_type": "image",
                "version": 7,
            }
        ],
        "batch": {"display_id": "B0001"},
        "links": [],
        "items": [
            {
                "id": str(item),
                "dataset_id": str(dataset),
                "width": 4,
                "height": 3,
                "content_hash": "legacy-md5",
                "metadata_": {},
                "file_path": "private/current-key.png",
            }
        ],
        "datasets": [
            {
                "id": str(dataset),
                "name": "frozen dataset",
                "metadata_": {"axis_convention": "iso"},
            }
        ],
        "annotations": [
            annotation,
            {**deepcopy(annotation), "id": str(uuid.uuid4()), "class_name": "removed"},
        ],
        "predictions": [],
        "scenes": [{"id": str(scene), "name": "frozen scene"}],
        "tracks": [
            {
                "id": str(track),
                "scene_id": str(scene),
                "track_id": "trk_frozen",
                "class_name": "car",
                "presence_mode": "explicit",
                "attributes": {"track_color": "blue"},
                "attributes_meta": {},
            }
        ],
        "intervals": [
            {
                "id": str(uuid.uuid4()),
                "scene_track_id": str(track),
                "start_frame": 2,
                "end_frame": 4,
                "source": "manual",
            }
        ],
        "references": {str(task): {"size": 8}} if reference else {},
        "media": []
        if reference
        else [
            {
                "task_id": str(task),
                "role": "image",
                "link_id": None,
                "source_key": "a" * 64,
                "source": {
                    "item_id": str(item),
                    "media_type": "image",
                    "sha256": hashlib.sha256(b"managed").hexdigest(),
                    "size_bytes": 7,
                },
            }
        ],
    }


class Context(FrozenImage):
    def __init__(self, snapshot):
        super().__init__(snapshot)
        self.paths = {task.id: f"tasks/{task.id}/same.png" for task in self.tasks}
        self.media_paths = {key: "images/" + value for key, value in self.paths.items()}
        multi = len(snapshot["targets"]) > 1
        self.archive_paths = {
            key: [
                "images/" + value,
                *[
                    f"{target}/images/{value}"
                    for target in snapshot["targets"]
                    if multi and target in packaging.YOLO_TARGETS
                ],
            ]
            for key, value in self.paths.items()
        }
        self.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    def service(self, db, **kwargs):
        return super().service(db, media_paths=self.media_paths)

    def entry(self, task_id):
        return {
            "presigned_url": "/minio/saved-copy?signature=receipt",
            "expires_at": self.expires_at.isoformat(),
            "sha256": hashlib.sha256(b"managed").hexdigest(),
            "size_bytes": 7,
        }


async def test_frozen_aap_class_attributes_scene_axis_and_batch_without_live_reads():
    data = facts(primary=True)
    frozen = Context(data)
    before = deepcopy(data)
    data["project"]["name"] = "changed"
    data["annotations"][0]["geometry"]["x"] = 0.9
    data["tracks"][0]["attributes"]["track_color"] = "changed"
    svc = frozen.service(
        SimpleNamespace(execute=forbidden, get=forbidden, expunge_all=forbidden)
    )
    result = json.loads(
        await svc.export_aap_json(
            frozen.project_id, batch_id=frozen.batch_id, axis_frame="source"
        )
    )
    assert result["project"]["name"] == "frozen"
    assert result["project"]["annotation_guide"] == "original guide"
    assert result["exported_from"]["batch_display_id"] == "B0001"
    ann = result["tasks"][0]["annotations"]
    assert len(ann) == 1 and ann[0]["geometry"]["x"] == 0.25
    assert ann[0]["attributes"] == {"color": "red"}
    assert result["scene_tracks"][0]["scene_name"] == "frozen scene"
    assert result["scene_tracks"][0]["attributes"] == {"track_color": "blue"}
    assert result["scene_tracks"][0]["intervals"][0]["start_frame"] == 2
    assert frozen.tasks[0].dataset_item_id is None and frozen.tasks[0].version == 7
    assert frozen.axis_convention(frozen.tasks[0].id) == "iso"
    assert before["annotations"][0]["attributes"]["removed"] == "secret"
    with pytest.raises(ContentError, match="image_export_inputs_changed"):
        await svc._load_data(uuid.uuid4(), frozen.batch_id)


async def test_aap_predictions_preserve_the_existing_unfiltered_class_semantics():
    data = facts()
    data["predictions"] = [
        {
            "id": str(uuid.uuid4()),
            "task_id": data["tasks"][0]["id"],
            "project_id": data["project_id"],
            "created_at": "2026-01-01T00:00:00+00:00",
            "source": "ml_backend",
            "model_version": "frozen-model",
            "score": 0.8,
            "tool_unit_id": "bbox",
            "result": [
                {
                    "type": "rectanglelabels",
                    "value": {
                        "x": 10,
                        "y": 20,
                        "width": 30,
                        "height": 40,
                        "rectanglelabels": ["not-in-current-classes"],
                    },
                }
            ],
        }
    ]
    frozen = Context(data)
    data["predictions"][0]["result"][0]["value"]["rectanglelabels"] = ["changed"]
    output = json.loads(
        await frozen.service(None).export_aap_json(
            frozen.project_id, batch_id=frozen.batch_id
        )
    )
    prediction = output["tasks"][0]["predictions"][0]
    assert prediction["class_name"] == "not-in-current-classes"
    assert (
        prediction["model_version"] == "frozen-model"
        and prediction["source"] == "ml_backend"
    )


@pytest.mark.parametrize(
    "targets",
    [
        ["coco"],
        ["aap_json"],
        ["yolo"],
        ["yolo-det"],
        ["yolo-obb"],
        ["yolo-seg"],
        ["binary-png"],
        ["indexed-png"],
        ["label-studio-brush"],
        [
            "coco",
            "aap_json",
            "yolo",
            "yolo-det",
            "yolo-obb",
            "yolo-seg",
            "binary-png",
            "indexed-png",
            "label-studio-brush",
        ],
    ],
)
async def test_owned_image_formats_use_saved_receipt_and_uniform_paths(
    monkeypatch, targets
):
    data = facts(primary=True)
    data["targets"] = targets
    mask = deepcopy(data["annotations"][0])
    mask.update(
        id=str(uuid.uuid4()),
        tool_unit_id="region",
        geometry={
            "type": "raster_mask",
            "mask": {
                "sha256": "b" * 64,
                "object_key": "raster-masks/sha256/" + "b" * 64,
                "runs": 2,
            },
        },
    )
    data["annotations"].append(mask)

    async def load_mask(reference):
        return {"encoding": "coco_rle", "size": [3, 4], "counts": [0, 12]}

    from app.services.mask_formats import image_export

    monkeypatch.setattr(service, "load_coco_rle", load_mask)
    monkeypatch.setattr(image_export, "load_coco_rle", load_mask)
    ctx = Context(data)
    monkeypatch.setattr(packaging.storage_service, "generate_download_url", forbidden)
    path, count, size = await packaging.build_export_zip(
        SimpleNamespace(execute=forbidden, get=forbidden),
        ctx.project_id,
        batch_id=ctx.batch_id,
        targets=targets,
        include_attributes=True,
        video_frame_mode="keyframes",
        image_media=ctx,
    )
    try:
        assert count >= 0 and size > 0
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            assert len(names) == len(set(names))
            manifest = json.loads(archive.read("images_manifest.json"))
            entry = manifest["images"][0]
            task = ctx.tasks[0]
            assert entry["presigned_url"] == ctx.entry(task.id)["presigned_url"]
            assert entry["sha256"] == ctx.entry(task.id)["sha256"]
            assert entry["size_bytes"] == 7
            assert (
                entry["expires_at"]
                == manifest["expires_at"]
                == ctx.expires_at.isoformat()
            )
            assert entry["archive_paths"] == ctx.archive_paths[task.id]
            assert (
                "private/current-key"
                not in archive.read("images_manifest.json").decode()
            )
            for target in targets:
                prefix = target + "/" if len(targets) > 1 else ""
                if target in packaging.YOLO_TARGETS:
                    stem = ctx.paths[task.id].rsplit(".", 1)[0]
                    assert prefix + "labels/" + stem + ".txt" in names
                    assert (
                        prefix + "images/" + ctx.paths[task.id]
                        in entry["archive_paths"]
                    )
                if target == "coco":
                    image = json.loads(archive.read(prefix + "annotations.json"))[
                        "images"
                    ][0]
                    assert image["file_name"] == ctx.media_paths[task.id]
                    assert (image["width"], image["height"]) == (4, 3)
                if target == "aap_json":
                    assert (
                        json.loads(archive.read(prefix + "annotations.json"))["tasks"][
                            0
                        ]["file_path"]
                        == ctx.media_paths[task.id]
                    )
                if target in {"binary-png", "indexed-png"}:
                    row = json.loads(archive.read(prefix + "manifest.json"))["items"][0]
                    assert row["media_path"] == ctx.media_paths[task.id]
                    assert (row["width"], row["height"]) == (4, 3)
    finally:
        Path(path).unlink(missing_ok=True)


async def test_reference_still_requires_snapshot_and_closes_it(monkeypatch):
    data = facts(reference=True)
    data["targets"] = ["coco", "yolo-det"]
    ctx = Context(data)
    task = ctx.tasks[0]
    body = b"original"
    stream = io.BytesIO(body)
    calls = []

    async def snapshot(current):
        assert current.version == 7 and current is task
        calls.append(current.id)
        return SimpleNamespace(
            file=stream,
            size=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            close=stream.close,
        )

    reference = SimpleNamespace(
        paths=ctx.paths,
        bindings={
            task.id: {"reference": {"size": len(body), "asset_id": "owned-reference"}}
        },
        snapshot=snapshot,
    )
    monkeypatch.setattr(packaging.storage_service, "generate_download_url", forbidden)
    with pytest.raises(ValueError, match="reference_export_authority_missing"):
        await packaging.build_export_zip(
            None,
            ctx.project_id,
            batch_id=ctx.batch_id,
            targets=data["targets"],
            include_attributes=False,
            video_frame_mode="keyframes",
            image_media=ctx,
        )
    path, _, _ = await packaging.build_export_zip(
        None,
        ctx.project_id,
        batch_id=ctx.batch_id,
        targets=data["targets"],
        include_attributes=False,
        video_frame_mode="keyframes",
        image_media=ctx,
        reference_media=reference,
    )
    try:
        assert calls == [task.id] and stream.closed
        with zipfile.ZipFile(path) as archive:
            entry = json.loads(archive.read("images_manifest.json"))["images"][0]
            assert entry["storage_mode"] == "embedded" and "presigned_url" not in entry
            assert all(archive.read(name) == body for name in entry["archive_paths"])
    finally:
        Path(path).unlink(missing_ok=True)


async def test_empty_frozen_scope_is_valid(monkeypatch):
    data = facts()
    data.update(tasks=[], annotations=[], media=[], items=[], datasets=[])
    ctx = Context(data)
    monkeypatch.setattr(packaging.storage_service, "generate_download_url", forbidden)
    path, count, _ = await packaging.build_export_zip(
        None,
        ctx.project_id,
        batch_id=ctx.batch_id,
        targets=data["targets"],
        include_attributes=False,
        video_frame_mode="keyframes",
        image_media=ctx,
    )
    try:
        assert count == 0
        with zipfile.ZipFile(path) as archive:
            assert json.loads(archive.read("images_manifest.json"))["images"] == []
            assert json.loads(archive.read("coco/annotations.json"))["images"] == []
    finally:
        Path(path).unlink(missing_ok=True)


async def test_portable_masks_check_loaded_runs_json_and_deduplicate(monkeypatch):
    calls = []

    async def load(ref):
        calls.append(ref["sha256"])
        return {"encoding": "coco_rle", "size": [1, 2], "counts": [0, 2]}

    monkeypatch.setattr(service, "load_coco_rle", load)
    monkeypatch.setattr(service, "MAX_PORTABLE_MASK_RUNS", 3)

    def geometry(digest, runs=2):
        return {
            "mask": {
                "sha256": digest,
                "object_key": "raster-masks/sha256/" + digest,
                "runs": runs,
            }
        }

    retained = service._PortableMaskObjects()
    await service._collect_portable_mask_objects(geometry("a"), retained)
    await service._collect_portable_mask_objects(geometry("a"), retained)
    assert calls == ["a"]
    with pytest.raises(ValueError, match="expansion budget"):
        await service._collect_portable_mask_objects(geometry("b"), retained)
    assert calls == ["a"] and set(retained) == {"a"}
    with pytest.raises(ValueError, match="expansion budget"):
        await service._collect_portable_mask_objects(geometry("b", 1), retained)
    assert set(retained) == {"a"}
    monkeypatch.setattr(service, "MAX_PORTABLE_MASK_JSON_BYTES", 10)
    with pytest.raises(ValueError, match="JSON budget"):
        await service._collect_portable_mask_objects(
            geometry("c"), service._PortableMaskObjects()
        )
