"""Frozen native bitmap packaging and analysis; no database or live storage."""

from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace
import uuid
import zipfile

import pytest
from PIL import Image

from app.schemas.training_dataset import TrainingDatasetSelection
from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    build_training_package,
    instance_rle_payload,
    instance_rle_text,
    instance_rle_semantic,
)
from app.services.training_dataset_analysis import derive, read_instance_mask
from app.services.training_dataset_insight_adapters import assert_parent_compatible
from tests.training_original_fixtures import original_receipt, staged_image
from training_contract.dataset import (
    INSTANCE_RLE_CONTRACT,
    SEGMENTATION_CONTRACT,
    instance_rle_evidence,
)


def rle(pixels):
    height, width = len(pixels), len(pixels[0])
    counts, previous, count = [], 0, 0
    for x in range(width):
        for y in range(height):
            value = pixels[y][x]
            if value == previous:
                count += 1
            else:
                counts.append(count)
                previous, count = value, 1
    counts.append(count)
    return {"encoding": "coco_rle", "size": [height, width], "counts": counts}


def annotation(identifier, mask):
    from app.services.raster_mask_storage import build_rle_reference

    return {
        "id": identifier,
        "version": 7,
        "class_name": "shape",
        "tool_unit_id": "region",
        "geometry": {"type": "raster_mask", "mask": build_rle_reference(mask)},
    }


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    hole = rle([[1, 1, 1, 0], [1, 0, 1, 0], [1, 1, 1, 1]])
    disconnected = rle([[1, 0, 0, 1], [0, 0, 0, 0], [1, 0, 0, 1]])
    masks = [hole, disconnected]
    objects = {}
    sources, media = [], []
    for number in range(3):
        task_id = str(uuid.uuid4())
        annotations = (
            [
                annotation(str(uuid.UUID(int=number * 2 + index + 1)), mask)
                for index, mask in enumerate(masks)
            ]
            if number < 2
            else []
        )
        for item, mask in zip(annotations, masks, strict=False):
            objects[item["geometry"]["mask"]["object_key"]] = mask
        image = tmp_path / f"{task_id}.png"
        Image.new("RGB", (4, 3), (20 + number, 40, 100)).save(image)
        sources.append(
            {
                "task_id": task_id,
                "annotations": annotations,
                "eligibility": {"negative": not annotations},
            }
        )
        media.append(
            {
                "task_id": task_id,
                "path": str(image),
                "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                "width": 4,
                "height": 3,
                "size_bytes": image.stat().st_size,
                "extension": ".png",
            }
        )
    monkeypatch.setattr(
        "app.services.raster_mask_storage.load_coco_rle_sync",
        lambda ref: objects[ref["object_key"]],
    )
    snapshot = {
        **INSTANCE_RLE_CONTRACT,
        "classes": ["shape"],
        "samples": sources,
        "annotation_scope": {"tool_unit_ids": ["region"]},
        "source": {"media_type": "image"},
        "seed": 42,
        "validation_fraction": 0.2,
    }
    return snapshot, media, masks


def test_explicit_bridge_preserves_old_request_bytes():
    old = TrainingDatasetSelection(task="segment").canonical_payload()
    assert "bridge" not in old
    new = TrainingDatasetSelection(
        task="segment", bridge="ultralytics-instance-rle-1"
    ).canonical_payload()
    assert new["bridge"] == INSTANCE_RLE_CONTRACT["bridge"]
    with pytest.raises(ValueError, match="桥接"):
        TrainingDatasetSelection(bridge="ultralytics-instance-rle-1")


def test_holes_disconnected_overlap_exact_bytes_source_receipts_and_negative(
    frozen, tmp_path
):
    snapshot, media, masks = frozen
    package = tmp_path / "native.zip"
    manifest = build_training_package(snapshot, media, package)
    assert manifest["schema"] == 4
    assert manifest["summary"]["objects"] == 4
    assert manifest["summary"]["negatives"] == 1
    with zipfile.ZipFile(package) as archive:
        for sample in manifest["samples"]:
            payload = json.loads(archive.read(sample["label_path"]))
            assert "raster-masks/" not in json.dumps(payload)
            assert [
                instance["rle"] for instance in payload["instances"]
            ] == masks or not payload["instances"]
            assert sample["semantic_label_sha256"] == instance_rle_semantic(
                sample["instances"]
            )
            for instance in payload["instances"]:
                assert instance["annotation_version"] == 7
                assert all(
                    instance[key] == value
                    for key, value in instance_rle_evidence(instance["rle"]).items()
                )
    analysis = derive(snapshot, manifest, None, "snapshot")
    assert analysis["overview"]["evidence"]["unknown_label_members"] == 0
    assert analysis["overview"]["task_statistics"]["geometry"] == "instance-mask"
    assert analysis["overview"]["task_statistics"]["mask_area_pixels_sum"] == 26


def test_source_version_noise_does_not_change_training_semantics(frozen, tmp_path):
    snapshot, media, _ = frozen
    first = build_training_package(snapshot, media, tmp_path / "first.zip")
    changed = deepcopy(snapshot)
    for sample in changed["samples"]:
        for item in sample["annotations"]:
            item["version"] += 1
    second = build_training_package(
        changed, media, tmp_path / "second.zip", parent_manifest=first
    )
    assert first["semantic_partitions"] == second["semantic_partitions"]
    assert first["content_digest"] != second["content_digest"]
    assert first["split_history"] == second["split_history"]


def test_cross_bridge_parent_is_rejected_before_any_repartition(frozen, tmp_path):
    snapshot, media, _ = frozen
    with pytest.raises(TrainingDatasetError, match="桥接"):
        build_training_package(
            snapshot,
            media,
            tmp_path / "rejected.zip",
            parent_manifest=SEGMENTATION_CONTRACT,
        )
    with pytest.raises(TrainingDatasetError) as exc:
        assert_parent_compatible(
            SEGMENTATION_CONTRACT, SEGMENTATION_CONTRACT, INSTANCE_RLE_CONTRACT
        )
    assert exc.value.code == "data_contract_mismatch"


def test_frozen_mask_reader_uses_independent_training_object_and_digest(
    frozen, monkeypatch
):
    snapshot, media, masks = frozen
    payload = instance_rle_payload(snapshot["samples"][0], ["shape"], 4, 3)
    encoded = instance_rle_text(payload).encode()
    from app.config import settings
    from app.services.dataset_assets import LEGACY_LOCATION_ID
    from app.services.managed_original_retirement import managed_storage_identity
    from app.services.training_dataset_storage import prefix_for

    project = uuid.uuid4()
    version = SimpleNamespace(id=uuid.uuid4(), project_id=project)
    endpoint = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    version.storage_ref = {
        "schema": 1,
        "purpose": "training-dataset",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": "retained-mask-bucket",
        "prefix": prefix_for(project, version.id),
        "storage_identity": managed_storage_identity(endpoint, "retained-mask-bucket"),
        "origin": "new",
        "evidence": None,
    }
    source = snapshot["samples"][0]
    sample = SimpleNamespace(
        included=True,
        label_sha256=hashlib.sha256(encoded).hexdigest(),
        task_id=source["task_id"],
    )
    observed = {}

    def get_object(**kwargs):
        observed.update(kwargs)
        return {"Body": io.BytesIO(encoded)}

    monkeypatch.setattr(
        "app.services.training_dataset_analysis.storage.client.get_object", get_object
    )
    result = read_instance_mask(
        project, version, sample, payload["instances"][0]["annotation_id"]
    )
    assert result["rle"] == masks[0]
    assert observed["Key"].endswith(f"/masks/{sample.task_id}.json")
    assert observed["Bucket"] == "retained-mask-bucket"
    sample.label_sha256 = "0" * 64
    with pytest.raises(TrainingDatasetError, match="校验"):
        read_instance_mask(
            project, version, sample, payload["instances"][0]["annotation_id"]
        )


def test_gc_roots_keep_retriable_snapshots_and_fixed_trials():
    from app.workers.cleanup import (
        _MASK_REFERENCE_QUERIES,
        _MASK_REFERENCE_EXISTS_QUERIES,
    )

    for queries in (_MASK_REFERENCE_QUERIES, _MASK_REFERENCE_EXISTS_QUERIES):
        text = "\n".join(queries)
        assert "training_dataset_versions" in text
        assert "status <> 'ready'" in text
        assert "trained_model_deployments" in text
        assert "jsonb_path_query(trial," in text


def test_verified_original_dimensions_use_full_image_and_orientation():
    from app.services import training_dataset_originals

    data = io.BytesIO()
    with Image.new("RGB", (4, 3)) as image:
        exif = image.getexif()
        exif[274] = 6
        image.save(data, format="JPEG", exif=exif)
    raw = data.getvalue()
    stage = staged_image(raw)
    try:
        result = training_dataset_originals.inspect_image(stage, {})
        assert (result["width"], result["height"]) == (3, 4)
        assert result["sha256"] == hashlib.sha256(raw).hexdigest()
        assert result["size_bytes"] == len(raw)
        assert not stage.file.closed
    finally:
        stage.close()


def test_locator_only_capture_requires_registered_original(monkeypatch):
    from app.services import training_datasets

    def unexpected_io(**kwargs):
        pytest.fail("Locator-only inputs must be rejected before storage I/O")

    monkeypatch.setattr(
        training_datasets.storage_service.client, "head_object", unexpected_io
    )
    monkeypatch.setattr(
        training_datasets.storage_service.client, "get_object", unexpected_io
    )
    with pytest.raises(TrainingDatasetError) as error:
        training_datasets.capture_source_media(
            {"raster_contract": True, "source_bucket": "private", "source_key": "image"}
        )
    assert error.value.code == "original_input_unrecorded"


def test_worker_materializes_independent_bitmap_object_without_source_storage_path(
    frozen, tmp_path, monkeypatch
):
    from app.workers import training_dataset as worker
    from botocore.exceptions import ClientError

    snapshot, media, masks = frozen
    source, image = snapshot["samples"][0], media[0]
    from pathlib import Path

    raw = Path(image["path"]).read_bytes()
    uploaded, metadata, bodies = {}, {}, []

    def head(*, Bucket, Key, **kwargs):
        if (Bucket, Key) not in uploaded:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        body = uploaded[Bucket, Key]
        return {
            "ContentLength": len(body),
            "ETag": '"' + hashlib.md5(body).hexdigest() + '"',
            "Metadata": metadata.get((Bucket, Key), {}),
        }

    def get(*, Bucket, Key, IfMatch=None, **kwargs):
        expected = head(Bucket=Bucket, Key=Key)
        assert IfMatch == expected["ETag"]
        body = io.BytesIO(uploaded[Bucket, Key])
        bodies.append(body)
        return {**expected, "Body": body}

    def put(path, bucket, key, **kwargs):
        uploaded[bucket, key] = Path(path).read_bytes()
        metadata[bucket, key] = kwargs.get("ExtraArgs", {}).get("Metadata", {})

    monkeypatch.setattr(worker.storage_service.client, "head_object", head)
    monkeypatch.setattr(worker.storage_service.client, "get_object", get)
    monkeypatch.setattr(worker.storage_service.client, "upload_file", put)
    monkeypatch.setattr(
        worker.storage_service,
        "upload_file",
        lambda path, key, **kwargs: put(path, kwargs["bucket"], key),
    )
    sample = {
        **source,
        "source_key": "mutable-key",
        "original": original_receipt(raw),
        "media": {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)},
        "expected_width": 4,
        "expected_height": 3,
        "raster_contract": True,
        "classes": ["shape"],
    }
    stage = staged_image(raw)
    try:
        result = worker.materialize_image(
            sample,
            "projects/frozen/datasets/version",
            "snapshot",
            tmp_path,
            bucket="retained-training",
            verified_original=stage,
        )
        assert not stage.file.closed
    finally:
        stage.close()
    assert result["width"] == 4 and result["height"] == 3
    key = f"projects/frozen/datasets/version/masks/{source['task_id']}.json"
    mask = uploaded["retained-training", key]
    assert [item["rle"] for item in json.loads(mask)["instances"]] == masks
    assert b"private-bucket" not in mask and b"mutable-key" not in mask
    assert {bucket for bucket, _key in uploaded} == {"retained-training"}
    assert all(body.closed for body in bodies)
