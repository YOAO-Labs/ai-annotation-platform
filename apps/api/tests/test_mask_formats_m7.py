from __future__ import annotations

import io
import hashlib
import json
import os
import uuid
import zipfile
from unittest.mock import AsyncMock

import numpy as np
import pytest
from PIL import Image, ImageDraw
from pycocotools.coco import COCO
from sqlalchemy import select

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.exporting.service import ExportService
from app.services.exporting.packaging import build_export_zip
from app.services.mask_formats.catalog import registry
from app.services.mask_formats.contracts import StagedObject
from app.services.mask_formats.image_codecs import (
    binary_png_bytes,
    binary_png_to_coco,
    compress_coco_rle,
    decode_label_studio_rle,
    encode_label_studio_mask,
    indexed_png_bytes,
    indexed_png_to_coco,
    label_studio_rle_to_coco,
    mask_to_yolo_polygons,
    normalize_coco_segmentation_rle,
)
from app.services.mask_formats.image_export import (
    write_binary_png_export,
    write_indexed_png_export,
)
from app.services.raster_mask_storage import build_rle_reference
from app.utils.raster_mask_rle import decode_coco_rle, encode_coco_rle


def _official_label_studio_decode(rle: list[int]) -> np.ndarray:
    """Vendored consumer snapshot from label-studio-sdk 2.0.23.

    Source contract:
    https://github.com/HumanSignal/label-studio-sdk/tree/2.0.23/src/label_studio_sdk/converter
    The snapshot is intentionally independent of the production codec.
    """

    def access_bit(data, num):
        base = int(num // 8)
        shift = 7 - int(num % 8)
        return (data[base] & (1 << shift)) >> shift

    bits = "".join(str(access_bit(rle, index)) for index in range(len(rle) * 8))

    class InputStream:
        def __init__(self, data):
            self.data = data
            self.i = 0

        def read(self, size):
            out = self.data[self.i : self.i + size]
            self.i += size
            return int(out, 2)

    stream = InputStream(bits)
    num = stream.read(32)
    word_size = stream.read(5) + 1
    rle_sizes = [stream.read(4) + 1 for _ in range(4)]
    index = 0
    out = np.zeros(num, dtype=np.uint8)
    while index < num:
        repeated = stream.read(1)
        end = index + 1 + stream.read(rle_sizes[stream.read(2)])
        if repeated:
            out[index:end] = stream.read(word_size)
            index = end
        else:
            while index < end:
                out[index] = stream.read(word_size)
                index += 1
    return out


async def _seed_image_project(
    db, owner_id: uuid.UUID, *, width: int = 7, height: int = 5
):
    suffix = uuid.uuid4().hex[:7]
    project = Project(
        display_id=f"P-M7-{suffix}",
        name=f"Mask formats {suffix}",
        type_key="image-seg",
        type_label="图像分割",
        data_type="image",
        owner_id=owner_id,
        raster_mask_native_editing_enabled=True,
        tool_bindings={
            "region": {
                "enabled": True,
                "classes": [{"name": "car", "order": 0}],
                "attribute_schema": {"fields": []},
            }
        },
    )
    dataset = Dataset(
        display_id=f"DS-M7-{suffix}",
        name=f"Mask formats {suffix}",
        data_type="image",
        created_by=owner_id,
    )
    db.add_all([project, dataset])
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="sample.png",
        file_path="dataset/sample.png",
        file_type="image",
        width=width,
        height=height,
    )
    db.add(item)
    await db.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-M7-{suffix}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="image",
        status="pending",
    )
    db.add(task)
    await db.flush()
    return project, task, item


def _donut(width: int = 7, height: int = 5) -> dict:
    pixels = [0] * (width * height)
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            pixels[y * width + x] = 1
    pixels[(height // 2) * width + width // 2] = 0
    return encode_coco_rle(pixels, width, height)


def _three_components(width: int = 7, height: int = 5) -> dict:
    pixels = [0] * (width * height)
    for x, y in ((1, 1), (3, 2), (5, 3)):
        pixels[y * width + x] = 1
    return encode_coco_rle(pixels, width, height)


def test_coco_and_label_studio_codecs_use_real_consumer_contracts() -> None:
    rle = _donut()
    compressed = compress_coco_rle(rle)
    assert isinstance(compressed["counts"], str)
    assert (
        normalize_coco_segmentation_rle(compressed, expected_width=7, expected_height=5)
        == rle
    )

    coco = COCO()
    coco.dataset = {
        "images": [{"id": 1, "file_name": "sample.png", "width": 7, "height": 5}],
        "categories": [{"id": 1, "name": "car"}],
        "annotations": [
            {
                "id": 1,
                "image_id": 1,
                "category_id": 1,
                "segmentation": compressed,
                "bbox": [1, 1, 5, 3],
                "area": sum(decode_coco_rle(rle)),
                "iscrowd": 1,
            }
        ],
    }
    coco.createIndex()
    assert coco.annToMask(coco.anns[1]).ravel(order="C").tolist() == list(
        decode_coco_rle(rle)
    )

    encoded = encode_label_studio_mask(decode_coco_rle(rle))
    official_rgba = _official_label_studio_decode(encoded).reshape(5, 7, 4)
    assert (official_rgba[:, :, 3] > 0).astype(np.uint8).ravel().tolist() == list(
        decode_coco_rle(rle)
    )
    assert list(decode_label_studio_rle(encoded)) == official_rgba.ravel().tolist()
    assert label_studio_rle_to_coco(encoded, width=7, height=5) == rle


def test_binary_and_indexed_png_consumers_preserve_pixels_and_id_255() -> None:
    rle = _donut()
    binary = binary_png_bytes(rle)
    with Image.open(io.BytesIO(binary)) as image:
        assert image.mode == "L"
        assert set(image.getdata()) == {0, 255}
    assert binary_png_to_coco(binary, width=7, height=5) == rle

    ids = list(range(256))
    indexed = indexed_png_bytes(ids, width=16, height=16)
    with Image.open(io.BytesIO(indexed)) as image:
        assert image.mode == "P"
        assert list(image.getdata()) == ids
    decoded_255 = indexed_png_to_coco(indexed, width=16, height=16, pixel_id=255)
    assert list(decode_coco_rle(decoded_255)).count(1) == 1


@pytest.mark.asyncio
async def test_coco_annotation_adapter_round_trip_and_unknown_mapping(
    db_session,
    super_admin,
    monkeypatch,
    tmp_path,
) -> None:
    user, _token = super_admin
    project, task, _item = await _seed_image_project(db_session, user.id)
    rle = _donut()
    payload = {
        "images": [{"id": 1, "file_name": "sample.png", "width": 7, "height": 5}],
        "categories": [{"id": 4, "name": "vehicle"}],
        "annotations": [
            {
                "id": 9,
                "image_id": 1,
                "category_id": 4,
                "segmentation": compress_coco_rle(rle),
                "iscrowd": 1,
            }
        ],
    }
    path = tmp_path / "coco.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    staged = StagedObject("key", "a" * 64, str(path), path.stat().st_size)
    adapter = registry.get("coco")

    unknown = await adapter.preflight_import(
        db_session,
        project=project,
        staged=staged,
        mapping={},
        options={},
    )
    assert unknown.loss_class == "unsupported"
    assert unknown.unknown_labels == ["vehicle"]

    mapping = {"labels": {"vehicle": "car"}}
    plan = await adapter.preflight_import(
        db_session,
        project=project,
        staged=staged,
        mapping=mapping,
        options={},
    )
    assert plan.loss_class == "lossless"
    assert plan.items[0].task_id == task.id

    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    store = AsyncMock()
    monkeypatch.setattr(
        "app.services.annotations_import.store_mask_reference_objects", store
    )
    result = await adapter.execute_import_item(
        db_session,
        project=project,
        staged=staged,
        plan=plan,
        item_index=0,
        operator_user_id=user.id,
        mapping=mapping,
        options={},
    )
    assert result["status"] == "committed"
    annotation = (
        await db_session.execute(
            select(Annotation).where(Annotation.task_id == task.id)
        )
    ).scalar_one()
    assert annotation.class_name == "car"
    assert annotation.geometry["type"] == "raster_mask"
    assert annotation.geometry["mask"]["sha256"] == build_rle_reference(rle)["sha256"]
    assert store.await_count == 1

    monkeypatch.setattr(
        "app.services.exporting.service.load_coco_rle", AsyncMock(return_value=rle)
    )
    exported = json.loads(await ExportService(db_session).export_coco(project.id))
    exported_ann = exported["annotations"][0]
    assert isinstance(exported_ann["segmentation"]["counts"], str)
    consumer = COCO()
    consumer.dataset = exported
    consumer.createIndex()
    assert consumer.annToMask(exported_ann).ravel(order="C").tolist() == list(
        decode_coco_rle(rle)
    )


@pytest.mark.asyncio
async def test_png_exports_keep_overlap_and_indexed_requires_explicit_policy(
    db_session,
    super_admin,
    monkeypatch,
) -> None:
    user, _token = super_admin
    project, task, item = await _seed_image_project(db_session, user.id)
    first_rle = encode_coco_rle(
        [1 if 1 <= x <= 4 and 1 <= y <= 3 else 0 for y in range(5) for x in range(7)],
        7,
        5,
    )
    second_rle = encode_coco_rle(
        [1 if 3 <= x <= 5 and 2 <= y <= 4 else 0 for y in range(5) for x in range(7)],
        7,
        5,
    )
    annotations = []
    for index, rle in enumerate((first_rle, second_rle), start=1):
        annotation = Annotation(
            task_id=task.id,
            project_id=project.id,
            user_id=user.id,
            source="manual",
            annotation_type="raster_mask",
            tool_unit_id="region",
            class_name="car",
            geometry={"type": "raster_mask", "mask": build_rle_reference(rle)},
            z_order=index,
        )
        db_session.add(annotation)
        annotations.append(annotation)
    await db_session.flush()

    async def load(reference):
        return (
            first_rle
            if reference["sha256"] == build_rle_reference(first_rle)["sha256"]
            else second_rle
        )

    monkeypatch.setattr("app.services.mask_formats.image_export.load_coco_rle", load)

    async def chunks():
        yield [task], {task.id: annotations}, {item.id: item}

    binary_buffer = io.BytesIO()
    with zipfile.ZipFile(binary_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        await write_binary_png_export(
            archive, prefix="", project=project, chunks=chunks()
        )
    with zipfile.ZipFile(io.BytesIO(binary_buffer.getvalue())) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert len(manifest["items"][0]["instances"]) == 2
        decoded = {
            instance["source_annotation_id"]: binary_png_to_coco(
                archive.read(instance["mask_path"]), width=7, height=5
            )
            for instance in manifest["items"][0]["instances"]
        }
        assert decoded == {
            str(annotations[0].id): first_rle,
            str(annotations[1].id): second_rle,
        }

    indexed = registry.get("indexed-png")
    blocked = await indexed.preflight_export(
        db_session,
        project=project,
        scope={"project_id": str(project.id)},
        options={"indexed_overlap_policy": "error"},
    )
    assert blocked.loss_class == "unsupported"
    assert blocked.overlap_conflicts
    assert any(
        code.code == "overlap_policy_required" for code in blocked.items[0].warnings
    )

    lossy = await indexed.preflight_export(
        db_session,
        project=project,
        scope={"project_id": str(project.id)},
        options={"indexed_overlap_policy": "z_order"},
    )
    assert lossy.loss_class == "lossy"
    assert any(code.code == "overlap_resolved" for code in lossy.losses)

    indexed_buffer = io.BytesIO()
    with zipfile.ZipFile(indexed_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        await write_indexed_png_export(
            archive,
            prefix="",
            project=project,
            chunks=chunks(),
            overlap_policy="z_order",
        )
    with zipfile.ZipFile(io.BytesIO(indexed_buffer.getvalue())) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["loss_report"][0]["code"] == "overlap_resolved"
        assert manifest["loss_report"][0]["lost_pixels"] > 0


def test_yolo_polygon_consumer_and_loss_contract() -> None:
    rle = _donut()
    polygons = mask_to_yolo_polygons(rle)
    assert len(polygons) == 1
    line = "0 " + " ".join(f"{value:.8f}" for point in polygons[0] for value in point)
    values = [float(value) for value in line.split()[1:]]
    parsed = [
        (values[index] * 7, values[index + 1] * 5) for index in range(0, len(values), 2)
    ]
    consumer = Image.new("L", (7, 5), 0)
    ImageDraw.Draw(consumer).polygon(parsed, fill=1)
    assert sum(consumer.getdata()) > 0
    # YOLO cannot encode the donut hole; the consumer therefore differs from canonical pixels.
    assert list(consumer.getdata()) != list(decode_coco_rle(rle))
    assert len(mask_to_yolo_polygons(_three_components())) == 3


@pytest.mark.asyncio
async def test_image_import_adapters_preflight_external_packages(
    db_session,
    super_admin,
    tmp_path,
) -> None:
    user, _token = super_admin
    project, _task, _item = await _seed_image_project(db_session, user.id)
    rle = _donut()
    png = binary_png_bytes(rle)
    common_instance = {
        "class_id": 0,
        "class_name": "car",
        "instance_id": "instance-1",
        "source_annotation_id": "source-1",
        "source_annotation_version": 1,
    }

    label_studio_path = tmp_path / "label-studio.json"
    label_studio_path.write_text(
        json.dumps(
            [
                {
                    "data": {"image": "dataset/sample.png"},
                    "annotations": [
                        {
                            "result": [
                                {
                                    "id": "brush-1",
                                    "type": "brushlabels",
                                    "from_name": "brush",
                                    "to_name": "image",
                                    "original_width": 7,
                                    "original_height": 5,
                                    "value": {
                                        "format": "rle",
                                        "rle": encode_label_studio_mask(
                                            decode_coco_rle(rle)
                                        ),
                                        "brushlabels": ["car"],
                                    },
                                }
                            ]
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )

    binary_path = tmp_path / "binary.zip"
    with zipfile.ZipFile(binary_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("masks/instance-1.png", png)
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format_id": "binary-png",
                    "manifest_version": "1",
                    "adapter_version": "1.0.0",
                    "items": [
                        {
                            "media_path": "dataset/sample.png",
                            "width": 7,
                            "height": 5,
                            "instances": [
                                {
                                    **common_instance,
                                    "mask_path": "masks/instance-1.png",
                                    "content_sha256": hashlib.sha256(png).hexdigest(),
                                }
                            ],
                        }
                    ],
                    "loss_report": [],
                }
            ),
        )

    indexed_pixels = [255 if value else 0 for value in decode_coco_rle(rle)]
    indexed_png = indexed_png_bytes(indexed_pixels, width=7, height=5)
    indexed_path = tmp_path / "indexed.zip"
    with zipfile.ZipFile(indexed_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("masks/sample.png", indexed_png)
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format_id": "indexed-png",
                    "manifest_version": "1",
                    "adapter_version": "1.0.0",
                    "items": [
                        {
                            "media_path": "dataset/sample.png",
                            "width": 7,
                            "height": 5,
                            "mask_path": "masks/sample.png",
                            "content_sha256": hashlib.sha256(indexed_png).hexdigest(),
                            "instances": [{**common_instance, "pixel_id": 255}],
                            "loss_report": [],
                        }
                    ],
                    "loss_report": [],
                }
            ),
        )

    yolo_path = tmp_path / "yolo.zip"
    with zipfile.ZipFile(yolo_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data.yaml", "path: .\nnames: [car]\n")
        archive.writestr(
            "labels/sample.txt",
            "0 0.142857 0.200000 0.857143 0.200000 0.857143 0.800000 0.142857 0.800000\n",
        )

    cases = [
        ("label-studio-brush", label_studio_path, "lossless"),
        ("binary-png", binary_path, "lossless"),
        ("indexed-png", indexed_path, "lossless"),
        ("yolo-seg", yolo_path, "lossy"),
    ]
    for format_id, path, expected_loss in cases:
        plan = await registry.get(format_id).preflight_import(
            db_session,
            project=project,
            staged=StagedObject(
                f"key-{format_id}", "a" * 64, str(path), path.stat().st_size
            ),
            mapping={},
            options={},
        )
        assert plan.loss_class == expected_loss
        assert plan.estimated_objects == 1
        if format_id == "yolo-seg":
            assert {loss.code for loss in plan.losses} == {
                "holes_polygonized",
                "components_split",
            }


@pytest.mark.asyncio
async def test_image_mask_packaging_is_consumable(
    db_session,
    super_admin,
    monkeypatch,
) -> None:
    user, _token = super_admin
    project, task, _item = await _seed_image_project(db_session, user.id)
    rle = _donut()
    annotation = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        source="manual",
        annotation_type="raster_mask",
        tool_unit_id="region",
        class_name="car",
        geometry={"type": "raster_mask", "mask": build_rle_reference(rle)},
        z_order=1,
    )
    db_session.add(annotation)
    await db_session.flush()

    monkeypatch.setattr(
        "app.services.exporting.packaging.storage_service.generate_download_url",
        lambda *args, **kwargs: "signed-url",
    )
    monkeypatch.setattr(
        "app.services.mask_formats.image_export.load_coco_rle",
        AsyncMock(return_value=rle),
    )

    zip_path, _file_count, _size_bytes = await build_export_zip(
        db_session,
        project.id,
        batch_id=None,
        targets=[
            "label-studio-brush",
            "binary-png",
            "indexed-png",
            "yolo-seg",
        ],
        include_attributes=True,
        video_frame_mode="keyframes",
        format_options={"indexed_overlap_policy": "error"},
    )
    try:
        with zipfile.ZipFile(zip_path) as archive:
            label_studio = json.loads(
                archive.read("label-studio-brush/annotations.json")
            )
            result = label_studio[0]["annotations"][0]["result"][0]
            decoded_rgba = _official_label_studio_decode(result["value"]["rle"])
            assert decoded_rgba.reshape(5, 7, 4)[:, :, 3].astype(
                bool
            ).ravel().tolist() == list(decode_coco_rle(rle))

            binary_manifest = json.loads(archive.read("binary-png/manifest.json"))
            binary_instance = binary_manifest["items"][0]["instances"][0]
            binary_bytes = archive.read(f"binary-png/{binary_instance['mask_path']}")
            assert (
                hashlib.sha256(binary_bytes).hexdigest()
                == binary_instance["content_sha256"]
            )
            assert binary_png_to_coco(binary_bytes, width=7, height=5) == rle

            indexed_manifest = json.loads(archive.read("indexed-png/manifest.json"))
            indexed_item = indexed_manifest["items"][0]
            indexed_bytes = archive.read(f"indexed-png/{indexed_item['mask_path']}")
            assert (
                indexed_png_to_coco(
                    indexed_bytes,
                    width=7,
                    height=5,
                    pixel_id=indexed_item["instances"][0]["pixel_id"],
                )
                == rle
            )

            yolo_label = next(
                name
                for name in archive.namelist()
                if name.startswith("yolo-seg/")
                and "/labels/" in name
                and name.endswith(".txt")
            )
            values = [float(value) for value in archive.read(yolo_label).split()[1:]]
            polygon = [
                (values[index] * 7, values[index + 1] * 5)
                for index in range(0, len(values), 2)
            ]
            consumed = Image.new("L", (7, 5), 0)
            ImageDraw.Draw(consumed).polygon(polygon, fill=1)
            assert sum(consumed.getdata()) > 0
            assert list(consumed.getdata()) != list(decode_coco_rle(rle))
    finally:
        os.unlink(zip_path)


@pytest.mark.asyncio
async def test_indexed_png_preflight_rejects_256_instances(
    db_session,
    super_admin,
    monkeypatch,
) -> None:
    user, _token = super_admin
    project, task, _item = await _seed_image_project(
        db_session, user.id, width=16, height=16
    )
    rle = encode_coco_rle([1] + [0] * 255, 16, 16)
    reference = build_rle_reference(rle)
    db_session.add_all(
        [
            Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=user.id,
                source="manual",
                annotation_type="raster_mask",
                tool_unit_id="region",
                class_name="car",
                geometry={"type": "raster_mask", "mask": reference},
            )
            for _ in range(256)
        ]
    )
    await db_session.flush()
    monkeypatch.setattr(
        "app.services.mask_formats.image_export.load_coco_rle",
        AsyncMock(return_value=rle),
    )
    plan = await registry.get("indexed-png").preflight_export(
        db_session,
        project=project,
        scope={"project_id": str(project.id)},
        options={"indexed_overlap_policy": "error"},
    )
    assert plan.loss_class == "unsupported"
    assert any(code.code == "instance_id_overflow" for code in plan.items[0].warnings)
