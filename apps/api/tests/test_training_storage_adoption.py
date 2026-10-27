"""Legacy adoption proves retained content without current-source reads or writes."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
import uuid
import zipfile

from PIL import Image
import pytest

from app.services import training_storage_adoption as adoption
from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    build_training_package,
    content_digest,
    instance_rle_payload,
    instance_rle_text,
)
from tests.test_training_instance_rle import frozen as frozen


class Store:
    def __init__(self, bucket):
        self.bucket, self.objects, self.metadata = bucket, {}, {}
        self.after_read = None
        self.close_failure = False
        self.reads = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return SimpleNamespace(paginate=self.pages)

    def pages(self, *, Bucket, Prefix):
        assert Bucket == self.bucket
        yield {
            "Contents": [
                {"Key": key, "ETag": self.etag(data), "Size": len(data)}
                for key, data in sorted(self.objects.items())
                if key.startswith(Prefix)
            ]
        }

    @staticmethod
    def etag(data):
        return '"' + hashlib.sha256(data).hexdigest() + '"'

    def head_object(self, *, Bucket, Key):
        assert Bucket == self.bucket
        return {
            "ContentLength": len(self.objects[Key]),
            "ETag": self.etag(self.objects[Key]),
            "Metadata": self.metadata.get(Key, {}),
            "LastModified": datetime(2026, 10, 24, tzinfo=timezone.utc),
        }

    def get_object(self, *, Bucket, Key, IfMatch, **kwargs):
        head = self.head_object(Bucket=Bucket, Key=Key)
        assert IfMatch == head["ETag"]
        self.reads.append(Key)
        parent = self

        class Body(io.BytesIO):
            def close(self):
                super().close()
                if parent.after_read:
                    parent.after_read(Key)
                if parent.close_failure:
                    raise OSError("uncertain close")

        return {**head, "Body": Body(self.objects[Key])}


@pytest.fixture(params=[False, True], ids=["detection", "rle"])
def retained(request, frozen, tmp_path, monkeypatch):
    snapshot, media, _ = frozen
    snapshot = deepcopy(snapshot)
    if not request.param:
        snapshot = {**snapshot, "schema": 1}
        for key in ["task", "media_type", "format", "geometry", "bridge"]:
            snapshot.pop(key, None)
        for sample in snapshot["samples"]:
            sample["annotations"] = [
                {
                    "id": str(uuid.uuid4()),
                    "version": 1,
                    "class_name": "shape",
                    "tool_unit_id": "bbox",
                    "geometry": {
                        "type": "bbox",
                        "x": 0.1,
                        "y": 0.1,
                        "w": 0.4,
                        "h": 0.5,
                    },
                }
            ]
    for sample, image in zip(snapshot["samples"], media, strict=True):
        sample["media"] = {"size_bytes": image["size_bytes"]}
        sample["asset_reference"] = {"sha256": image["sha256"]}
        sample["expected_width"], sample["expected_height"] = (
            image["width"],
            image["height"],
        )
    package = tmp_path / "dataset.zip"
    manifest = build_training_package(snapshot, media, package)
    prefix = f"projects/{uuid.uuid4()}/datasets/{uuid.uuid4()}/"

    def contains(key):
        if not key.startswith(prefix) or ".." in key.split("/") or "\\" in key:
            raise TrainingDatasetError(
                "training_storage_invalid_key", "Invalid", status_code=409
            )
        return True

    owner = SimpleNamespace(
        bucket="saved-training",
        prefix=prefix,
        key=lambda suffix: prefix + suffix,
        contains=contains,
    )
    version = {
        "status": "ready",
        "snapshot": snapshot,
        "manifest": manifest,
        "snapshot_digest": content_digest(snapshot),
        "package_key": prefix + f"attempts/{uuid.uuid4()}/dataset.zip",
        "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        "size_bytes": package.stat().st_size,
    }
    store = Store(owner.bucket)
    store.objects[version["package_key"]] = package.read_bytes()
    for sample, image in zip(snapshot["samples"], media, strict=True):
        key = owner.key("media/" + sample["task_id"])
        store.objects[key] = Path(image["path"]).read_bytes()
        store.metadata[key] = {
            "snapshot-digest": version["snapshot_digest"],
            "sha256": image["sha256"],
        }
        if request.param:
            store.objects[owner.key(f"masks/{sample['task_id']}.json")] = (
                instance_rle_text(
                    instance_rle_payload(
                        sample, snapshot["classes"], image["width"], image["height"]
                    )
                ).encode()
            )

    # Production adoption must use retained RLE only, including gzip references.
    def no_source(*args):
        pytest.fail("adoption cannot read current mask sources")

    monkeypatch.setattr(
        "app.services.raster_mask_storage.load_coco_rle_sync", no_source
    )
    return store, owner, version


def test_ready_verifies_zip_and_all_retained_members_without_source_reads(retained):
    store, owner, version = retained
    before = deepcopy(store.objects)
    evidence = adoption.verify(store, owner, version)
    assert evidence["scope"] == "current-retained-copy"
    assert evidence["object_count"] == len(before)
    assert set(store.reads) == set(before)
    assert store.objects == before


def test_partial_and_empty_stopped_versions_are_explicitly_verifiable(retained):
    store, owner, version = retained
    store.objects.pop(version["package_key"])
    version["status"] = "failed"
    assert adoption.verify(store, owner, version)["object_count"] == len(store.objects)
    store.objects.clear()
    version["status"] = "cancelled"
    assert adoption.verify(store, owner, version)["object_count"] == 0


def test_partial_self_reported_digest_is_not_independent_input_identity(retained):
    store, owner, version = retained
    store.objects.pop(version["package_key"])
    version["status"] = "failed"
    version["snapshot"]["samples"][0].pop("asset_reference")
    version["snapshot_digest"] = content_digest(version["snapshot"])
    for metadata in store.metadata.values():
        metadata["snapshot-digest"] = version["snapshot_digest"]
    with pytest.raises(TrainingDatasetError) as failure:
        adoption.verify(store, owner, version)
    assert failure.value.code == "legacy_content_identity_unproven"


@pytest.mark.parametrize(
    "change",
    [
        "extra-key",
        "unknown-attempt",
        "missing-media",
        "wrong-digest",
        "wrong-snapshot",
        "wrong-zip",
    ],
)
def test_unexplained_or_changed_content_preserves_objects_and_rejects(retained, change):
    store, owner, version = retained
    key = owner.key("media/" + version["snapshot"]["samples"][0]["task_id"])
    if change == "extra-key":
        store.objects[owner.key("unowned.bin")] = b"unowned"
    elif change == "unknown-attempt":
        store.objects[owner.key(f"attempts/{uuid.uuid4()}/dataset.zip")] = (
            b"old attempt"
        )
    elif change == "missing-media":
        store.objects.pop(key)
    elif change == "wrong-digest":
        image = io.BytesIO()
        Image.new("RGB", (4, 3), "yellow").save(image, "PNG")
        store.objects[key] = image.getvalue()
        store.metadata[key]["sha256"] = hashlib.sha256(image.getvalue()).hexdigest()
    elif change == "wrong-snapshot":
        version["snapshot_digest"] = "0" * 64
    else:
        store.objects[version["package_key"]] = b"not a zip"
    before = deepcopy(store.objects)
    with pytest.raises(TrainingDatasetError):
        adoption.verify(store, owner, version)
    assert store.objects == before


def test_live_metadata_change_after_a_read_cannot_publish_evidence(retained):
    store, owner, version = retained
    media = next(iter(store.metadata))

    def changed(key):
        if key == media:
            store.metadata[key] = {**store.metadata[key], "changed": "true"}

    store.after_read = changed
    with pytest.raises(TrainingDatasetError) as failure:
        adoption.verify(store, owner, version)
    assert failure.value.code == "legacy_storage_changed"


def test_close_uncertainty_and_real_byte_budget_do_not_return_evidence(retained):
    store, owner, version = retained
    store.close_failure = True
    with pytest.raises(TrainingDatasetError) as failure:
        adoption.verify(store, owner, version)
    assert failure.value.code == "legacy_storage_close_unsettled"
    store.close_failure = False
    with pytest.raises(TrainingDatasetError) as failure:
        adoption.verify(store, owner, version, max_bytes=1)
    assert failure.value.code == "legacy_storage_transfer_limit"


def test_zip_extra_member_rejected_even_with_updated_package_receipt(retained):
    store, owner, version = retained
    package = io.BytesIO(store.objects[version["package_key"]])
    with zipfile.ZipFile(package, "a") as archive:
        archive.writestr("images/train/extra.png", b"extra")
    data = package.getvalue()
    store.objects[version["package_key"]] = data
    version["package_sha256"], version["size_bytes"] = (
        hashlib.sha256(data).hexdigest(),
        len(data),
    )
    with pytest.raises(TrainingDatasetError):
        adoption.verify(store, owner, version)


def deduplicated(retained, tmp_path, *, independent):
    store, owner, version = retained
    snapshot = version["snapshot"]
    snapshot["deduplicate"] = True
    ordered = sorted(
        [sample for sample in snapshot["samples"] if sample["annotations"]],
        key=lambda sample: sample["task_id"],
    )
    first, last = ordered[0], ordered[-1]
    first_key, last_key = (
        owner.key("media/" + sample["task_id"]) for sample in [first, last]
    )
    store.objects[last_key] = store.objects[first_key]
    sha = hashlib.sha256(store.objects[last_key]).hexdigest()
    last["media"]["size_bytes"] = len(store.objects[last_key])
    if independent:
        last["asset_reference"] = {"sha256": sha}
    else:
        last.pop("asset_reference", None)
    version["snapshot_digest"] = content_digest(snapshot)
    media = []
    for sample in snapshot["samples"]:
        key = owner.key("media/" + sample["task_id"])
        data = store.objects[key]
        store.metadata[key] = {
            "snapshot-digest": version["snapshot_digest"],
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        image = tmp_path / (sample["task_id"] + ".png")
        image.write_bytes(data)
        item = {
            "task_id": sample["task_id"],
            "path": str(image),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "width": 4,
            "height": 3,
            "extension": ".png",
        }
        mask_key = owner.key("masks/" + sample["task_id"] + ".json")
        if mask_key in store.objects:
            mask = tmp_path / (sample["task_id"] + ".json")
            mask.write_bytes(store.objects[mask_key])
            item["mask_path"] = str(mask)
        media.append(item)
    package = tmp_path / "deduplicated.zip"
    version["manifest"] = build_training_package(snapshot, media, package)
    data = package.read_bytes()
    version["package_sha256"], version["size_bytes"] = (
        hashlib.sha256(data).hexdigest(),
        len(data),
    )
    store.objects[version["package_key"]] = data
    return last


def test_valid_dedup_keeps_every_independently_identified_retained_member(
    retained, tmp_path
):
    store, owner, version = retained
    deduplicated(retained, tmp_path, independent=True)
    assert len(version["manifest"]["samples"]) == 2
    assert adoption.verify(store, owner, version)["object_count"] == len(store.objects)


@pytest.mark.parametrize("remove", [False, True])
def test_dedup_cannot_infer_missing_identity_or_hide_the_unproven_copy(
    retained, tmp_path, remove
):
    store, owner, version = retained
    last = deduplicated(retained, tmp_path, independent=False)
    if remove:
        store.objects.pop(owner.key("media/" + last["task_id"]))
        store.objects.pop(owner.key("masks/" + last["task_id"] + ".json"), None)
    with pytest.raises(TrainingDatasetError) as failure:
        adoption.verify(store, owner, version)
    assert failure.value.code == (
        "legacy_storage_required_copy_missing"
        if remove
        else "legacy_content_identity_unproven"
    )


@pytest.mark.parametrize("change", ["missing-member", "no-val", "rle-receipt"])
def test_recomputed_package_receipts_do_not_hide_invalid_consumption_contract(
    retained, change
):
    store, owner, version = retained
    manifest = version["manifest"]
    if change == "rle-receipt" and not adoption.is_instance_rle(version["snapshot"]):
        pytest.skip("RLE receipts are not part of the detection contract")
    before = io.BytesIO(store.objects[version["package_key"]])
    replacements, removed = {}, set()
    with zipfile.ZipFile(before) as original:
        entries = {
            entry.filename: original.read(entry) for entry in original.infolist()
        }
    if change == "missing-member":
        member = manifest["samples"].pop()
        removed.update([member["image_path"], member["label_path"]])
    elif change == "no-val":
        for member in manifest["samples"]:
            for key in ["image_path", "label_path"]:
                old = member[key]
                member[key] = old.replace("/val/", "/train/")
                replacements[old] = member[key]
            member["split"] = "train"
    else:
        member = next(member for member in manifest["samples"] if member["instances"])
        member["semantic_label_sha256"] = "0" * 64
    manifest["content_digest"] = content_digest(
        {key: value for key, value in manifest.items() if key != "content_digest"}
    )
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as target:
        for name, data in entries.items():
            if name in removed:
                continue
            if name == "manifest.json":
                import json

                data = json.dumps(manifest).encode()
            target.writestr(replacements.get(name, name), data)
    data = package.getvalue()
    store.objects[version["package_key"]] = data
    version["package_sha256"], version["size_bytes"] = (
        hashlib.sha256(data).hexdigest(),
        len(data),
    )
    with pytest.raises(TrainingDatasetError):
        adoption.verify(store, owner, version)
