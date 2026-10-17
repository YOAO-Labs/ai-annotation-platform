from __future__ import annotations

import io
import json
import os
import uuid
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import numpy as np
import pytest
from PIL import Image
from pycocotools import mask as coco_mask
from pycocotools.coco import COCO
from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.exporting.packaging import build_export_zip
from app.services.mask_formats.catalog import registry
from app.services.mask_formats.contracts import StagedObject
from app.services.mask_formats.image_codecs import (
    compress_coco_rle,
    normalize_coco_segmentation_rle,
)
from app.services.raster_mask_storage import build_rle_reference
from app.utils.raster_mask_rle import decode_coco_rle, encode_coco_rle


async def _seed_video_project(db, owner_id: uuid.UUID):
    suffix = uuid.uuid4().hex[:7]
    project = Project(
        display_id=f"P-M8-{suffix}",
        name=f"Video mask formats {suffix}",
        type_key="video-track",
        type_label="视频追踪",
        data_type="video",
        owner_id=owner_id,
        tool_bindings={
            "region": {
                "enabled": True,
                "classes": [{"name": "car", "order": 0}],
                "attribute_schema": {"fields": []},
            }
        },
    )
    dataset = Dataset(
        display_id=f"DS-M8-{suffix}",
        name=f"Video masks {suffix}",
        data_type="video",
        created_by=owner_id,
    )
    db.add_all([project, dataset])
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="clip-a.mp4",
        file_path="dataset/clip-a.mp4",
        file_type="video",
        width=4,
        height=3,
        metadata_={
            "video": {
                "width": 4,
                "height": 3,
                "frame_count": 3,
                "fps": 10,
            }
        },
    )
    db.add(item)
    await db.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-M8-{suffix}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="video",
        status="pending",
    )
    db.add(task)
    await db.flush()
    return project, task, item


def _mask(offset: int = 0) -> dict:
    pixels = [0] * 12
    pixels[offset] = 1
    pixels[min(11, offset + 1)] = 1
    return encode_coco_rle(pixels, 4, 3)


def _staged(path, key: str) -> StagedObject:
    return StagedObject(key, "a" * 64, str(path), path.stat().st_size)


def test_video_registry_exposes_only_verified_round_trip_adapters() -> None:
    rows = {
        adapter.descriptor.format_id: adapter.descriptor
        for adapter in registry.list(media_type="video")
    }
    for format_id in ("coco-frames-seg", "davis", "youtube-vos", "mots"):
        descriptor = rows[format_id]
        assert descriptor.import_capability.supported is True
        assert descriptor.import_capability.verified is True
        assert descriptor.import_capability.enabled_for_ui is True
        assert descriptor.export_capability.verified is True
    assert rows["video_json"].import_capability.supported is False


@pytest.mark.asyncio
async def test_coco_frames_import_preserves_track_frame_outside_and_occlusion(
    db_session,
    super_admin,
    monkeypatch,
    tmp_path,
) -> None:
    user, _token = super_admin
    project, task, _item = await _seed_video_project(db_session, user.id)
    first = _mask(0)
    last = _mask(8)
    payload = {
        "images": [
            {
                "id": frame,
                "file_name": f"images/clip-a/{frame + 1:06d}.jpg",
                "width": 4,
                "height": 3,
                "source_frame_index": frame,
            }
            for frame in range(3)
        ],
        "categories": [{"id": 1, "name": "car"}],
        "annotations": [
            {
                "id": 1,
                "image_id": 0,
                "category_id": 1,
                "segmentation": compress_coco_rle(first),
                "iscrowd": 1,
                "attributes": {"__track_id": "track-car", "__occluded": False},
            },
            {
                "id": 2,
                "image_id": 2,
                "category_id": 1,
                "segmentation": compress_coco_rle(last),
                "iscrowd": 1,
                "attributes": {"__track_id": "track-car", "__occluded": True},
            },
        ],
    }
    path = tmp_path / "coco-frames.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    adapter = registry.get("coco-frames-seg")
    plan = await adapter.preflight_import(
        db_session,
        project=project,
        staged=_staged(path, "coco-frames"),
        mapping={},
        options={"frame_base": 1},
    )
    assert plan.loss_class == "lossless"
    assert plan.items[0].task_id == task.id
    assert plan.frame_mapping["clip-a"] == {"1": 0, "2": 1, "3": 2}

    store = AsyncMock()
    monkeypatch.setattr(
        "app.services.annotations_import.store_mask_reference_objects",
        store,
    )
    result = await adapter.execute_import_item(
        db_session,
        project=project,
        staged=_staged(path, "coco-frames"),
        plan=plan,
        item_index=0,
        operator_user_id=user.id,
        mapping={},
        options={"frame_base": 1},
    )
    assert result["status"] == "committed"
    annotation = (
        await db_session.execute(
            select(Annotation).where(Annotation.task_id == task.id)
        )
    ).scalar_one()
    assert annotation.geometry["type"] == "video_track_mask"
    assert annotation.geometry["track_id"] == "track-car"
    assert [row["frame_index"] for row in annotation.geometry["keyframes"]] == [0, 2]
    assert annotation.geometry["keyframes"][1]["occluded"] is True
    assert annotation.geometry["outside"] == [{"from": 1, "to": 1, "source": "manual"}]
    assert store.await_count == 1

    for annotation_row in payload["annotations"]:
        annotation_row["attributes"] = {}
    lost_path = tmp_path / "coco-no-tracks.json"
    lost_path.write_text(json.dumps(payload), encoding="utf-8")
    lost = await adapter.preflight_import(
        db_session,
        project=project,
        staged=_staged(lost_path, "coco-no-tracks"),
        mapping={},
        options={"frame_base": 1},
    )
    assert lost.loss_class == "lossy"
    assert {row.code for row in lost.losses} == {"track_identity_lost"}
    assert lost.estimated_objects == 2


@pytest.mark.asyncio
async def test_coco_frames_import_does_not_mark_unsampled_frames_outside(
    db_session,
    super_admin,
    monkeypatch,
    tmp_path,
) -> None:
    user, _token = super_admin
    project, task, _item = await _seed_video_project(db_session, user.id)
    payload = {
        "images": [
            {
                "id": frame,
                "file_name": f"images/clip-a/{frame:06d}.jpg",
                "width": 4,
                "height": 3,
                "source_frame_index": frame,
            }
            for frame in (0, 2)
        ],
        "categories": [{"id": 1, "name": "car"}],
        "annotations": [
            {
                "id": index + 1,
                "image_id": frame,
                "category_id": 1,
                "segmentation": compress_coco_rle(_mask(frame * 4)),
                "iscrowd": 1,
                "attributes": {"__track_id": "sampled-track"},
            }
            for index, frame in enumerate((0, 2))
        ],
    }
    path = tmp_path / "sampled-coco-frames.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    adapter = registry.get("coco-frames-seg")
    plan = await adapter.preflight_import(
        db_session,
        project=project,
        staged=_staged(path, "sampled-coco-frames"),
        mapping={},
        options={"frame_base": 0},
    )
    store = AsyncMock()
    monkeypatch.setattr(
        "app.services.annotations_import.store_mask_reference_objects",
        store,
    )
    await adapter.execute_import_item(
        db_session,
        project=project,
        staged=_staged(path, "sampled-coco-frames"),
        plan=plan,
        item_index=0,
        operator_user_id=user.id,
        mapping={},
        options={"frame_base": 0},
    )
    annotation = (
        await db_session.execute(
            select(Annotation).where(Annotation.task_id == task.id)
        )
    ).scalar_one()
    assert annotation.geometry["outside"] == []
    assert [row["frame_index"] for row in annotation.geometry["keyframes"]] == [0, 2]


@pytest.mark.asyncio
async def test_coco_frames_reports_non_contiguous_external_frame_mapping(
    db_session,
    super_admin,
    tmp_path,
) -> None:
    user, _token = super_admin
    project, _task, _item = await _seed_video_project(db_session, user.id)
    payload = {
        "images": [
            {
                "id": frame,
                "file_name": f"images/clip-a/{frame:06d}.jpg",
                "width": 4,
                "height": 3,
            }
            for frame in (1, 3)
        ],
        "categories": [{"id": 1, "name": "car"}],
        "annotations": [
            {
                "id": index + 1,
                "image_id": frame,
                "category_id": 1,
                "segmentation": compress_coco_rle(_mask(index * 8)),
                "iscrowd": 1,
                "attributes": {"__track_id": "non-contiguous-track"},
            }
            for index, frame in enumerate((1, 3))
        ],
    }
    path = tmp_path / "non-contiguous-coco-frames.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    plan = await registry.get("coco-frames-seg").preflight_import(
        db_session,
        project=project,
        staged=_staged(path, "non-contiguous-coco-frames"),
        mapping={},
        options={"frame_base": 1},
    )
    assert plan.loss_class == "lossless"
    assert plan.frame_mapping["clip-a"] == {"1": 0, "3": 2}


@pytest.mark.asyncio
async def test_video_mask_package_consumers_and_all_import_preflights(
    db_session,
    super_admin,
    monkeypatch,
) -> None:
    user, _token = super_admin
    project, task, _item = await _seed_video_project(db_session, user.id)
    first = _mask(0)
    last = _mask(8)
    references = {
        build_rle_reference(first)["sha256"]: first,
        build_rle_reference(last)["sha256"]: last,
    }
    annotation = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        source="manual",
        annotation_type="video_track_mask",
        tool_unit_id="region",
        class_name="car",
        geometry={
            "type": "video_track_mask",
            "track_id": "track-car",
            "keyframes": [
                {
                    "frame_index": 0,
                    "mask": build_rle_reference(first),
                    "source": "manual",
                    "occluded": False,
                },
                {
                    "frame_index": 2,
                    "mask": build_rle_reference(last),
                    "source": "manual",
                    "occluded": True,
                },
            ],
            "outside": [{"from": 1, "to": 1, "source": "manual"}],
        },
        track_id="track-car",
        z_order=1,
    )
    db_session.add(annotation)
    await db_session.flush()

    async def load(reference):
        return references[reference["sha256"]]

    monkeypatch.setattr(
        "app.services.exporting.packaging.load_coco_rle",
        load,
    )
    monkeypatch.setattr(
        "app.services.mask_formats.video_adapters.load_coco_rle",
        load,
    )
    monkeypatch.setattr(
        "app.services.exporting.packaging.storage_service.generate_download_url",
        lambda *args, **kwargs: "signed-url",
    )
    zip_path, _file_count, _size = await build_export_zip(
        db_session,
        project.id,
        batch_id=None,
        targets=["coco-frames-seg", "davis", "youtube-vos", "mots"],
        include_attributes=True,
        video_frame_mode="all_frames",
        format_options={
            "video_overlap_policy": "z_order",
            "mots_frame_base": 1,
        },
    )
    try:
        with zipfile.ZipFile(zip_path) as archive:
            coco = json.loads(archive.read("coco-frames-seg/annotations.json"))
            consumer = COCO()
            consumer.dataset = coco
            consumer.createIndex()
            coco_masks = []
            for row in coco["annotations"]:
                assert isinstance(row["segmentation"]["counts"], str)
                encoded = {
                    "size": row["segmentation"]["size"],
                    "counts": row["segmentation"]["counts"].encode("ascii"),
                }
                dense = np.asarray(coco_mask.decode(encoded)).reshape((3, 4), order="C")
                assert (
                    consumer.annToMask(row).ravel(order="C").tolist()
                    == dense.ravel(order="C").tolist()
                )
                coco_masks.append(dense.ravel(order="C").tolist())
            assert coco_masks == [
                list(decode_coco_rle(first)),
                list(decode_coco_rle(last)),
            ]

            davis_meta = json.loads(archive.read("davis/davis_manifest.json"))
            assert (
                davis_meta["sequences"]["clip-a"]["objects"]["1"]["category"] == "car"
            )
            davis_png = Image.open(
                io.BytesIO(
                    archive.read("davis/Annotations/Full-Resolution/clip-a/00000.png")
                )
            )
            assert davis_png.mode == "P"
            assert list(davis_png.getdata()).count(1) == 2

            youtube_meta = json.loads(archive.read("youtube-vos/meta.json"))
            assert youtube_meta["videos"]["clip-a"]["objects"]["1"]["frames"] == [
                "00000",
                "00001",
            ]
            youtube_png = Image.open(
                io.BytesIO(archive.read("youtube-vos/Annotations/clip-a/00001.png"))
            )
            assert list(youtube_png.getdata()).count(1) == 2

            mots_meta = json.loads(archive.read("mots/mots_manifest.json"))
            mots_path = mots_meta["sequences"]["clip-a"]["annotations_path"]
            mots_rows = archive.read(mots_path).decode().splitlines()
            assert [int(row.split()[0]) for row in mots_rows] == [1, 3]
            decoded = [
                normalize_coco_segmentation_rle(
                    {"size": [3, 4], "counts": row.split(maxsplit=5)[5]},
                    expected_width=4,
                    expected_height=3,
                )
                for row in mots_rows
            ]
            assert decoded == [first, last]

        staged = _staged(Path(zip_path), "video-package")
        plans = {}
        for format_id, options in (
            ("coco-frames-seg", {"frame_base": 1}),
            ("davis", {}),
            ("youtube-vos", {"sparse_gap_policy": "outside_gaps"}),
            ("mots", {"frame_base": 1}),
        ):
            plans[format_id] = await registry.get(format_id).preflight_import(
                db_session,
                project=project,
                staged=staged,
                mapping={},
                options=options,
            )
            assert plans[format_id].items[0].task_id == task.id
            assert plans[format_id].estimated_objects == 1
        assert plans["coco-frames-seg"].loss_class == "lossless"
        assert plans["davis"].loss_class == "lossless"
        assert plans["youtube-vos"].loss_class == "lossy"
        assert {row.code for row in plans["youtube-vos"].losses} == {
            "sparse_frames_collapsed"
        }
        assert plans["mots"].loss_class == "lossy"
    finally:
        os.unlink(zip_path)


@pytest.mark.asyncio
async def test_davis_preflight_requires_explicit_overlap_winner(
    db_session,
    super_admin,
    monkeypatch,
) -> None:
    user, _token = super_admin
    project, task, _item = await _seed_video_project(db_session, user.id)
    rle = _mask(0)
    reference = build_rle_reference(rle)
    for index in range(2):
        db_session.add(
            Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=user.id,
                source="manual",
                annotation_type="video_track_mask",
                tool_unit_id="region",
                class_name="car",
                geometry={
                    "type": "video_track_mask",
                    "track_id": f"track-{index}",
                    "keyframes": [
                        {
                            "frame_index": 0,
                            "mask": reference,
                            "source": "manual",
                            "occluded": False,
                        }
                    ],
                    "outside": [],
                },
                z_order=index,
            )
        )
    await db_session.flush()
    monkeypatch.setattr(
        "app.services.mask_formats.video_adapters.load_coco_rle",
        AsyncMock(return_value=rle),
    )
    adapter = registry.get("davis")
    video_json = await registry.get("video_json").preflight_export(
        db_session,
        project=project,
        scope={"project_id": str(project.id)},
        options={},
    )
    assert video_json.loss_class == "lossy"
    assert {row.code for row in video_json.losses} == {"nonportable_media_reference"}
    blocked = await adapter.preflight_export(
        db_session,
        project=project,
        scope={"project_id": str(project.id)},
        options={"video_overlap_policy": "error"},
    )
    assert blocked.loss_class == "unsupported"
    assert blocked.overlap_conflicts[0]["covered_pixels"] == 2
    assert {row.code for row in blocked.warnings} >= {"overlap_policy_required"}

    allowed = await adapter.preflight_export(
        db_session,
        project=project,
        scope={"project_id": str(project.id)},
        options={"video_overlap_policy": "z_order"},
    )
    assert allowed.loss_class == "lossy"
    assert {row.code for row in allowed.losses} >= {"overlap_resolved"}
