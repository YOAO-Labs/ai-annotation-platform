"""Explicit verification of retained legacy copies, never discovery or repair.

The caller selects one registered candidate and fences the version execution.
This reader performs no writes and never treats self-reported metadata as an
independent frozen input digest. Its result describes current retained copies,
not their original creation location or exclusive ownership.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import PurePosixPath
import stat
import tempfile
import threading
import time
import uuid
import zipfile

from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image

from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    content_digest,
    contract_of,
    instance_rle_payload,
    instance_rle_semantic,
    instance_rle_text,
    native_labels,
)
from app.services.training_datasets import MAX_IMAGE_BYTES, MAX_DATASET_BYTES
from training_contract.dataset import (
    DatasetContractError,
    INSTANCE_RLE_LIMITS,
    instance_rle_evidence,
    is_instance_rle,
)

MAX_OBJECTS = 100_000
MAX_TRANSFER_BYTES = 64 * 1024**3
TIMEOUT_SECONDS = 600
CHUNK = 1024 * 1024
CAPACITY = threading.BoundedSemaphore(1)


def reject(code="legacy_storage_content_mismatch", status=409):
    error = TrainingDatasetError(
        code, "旧版本保留副本无法安全固定；原版本与对象均已保留", status_code=status
    )
    if code == "legacy_storage_close_unsettled":
        from app.services.storage_access import mark_unsettled

        mark_unsettled(error)
    raise error from None


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _observation(head):
    return {
        key: head.get(key)
        for key in ["ETag", "VersionId", "ContentLength", "LastModified", "Metadata"]
    }


class Reader:
    def __init__(self, client, owner, *, max_bytes, timeout_seconds):
        self.client, self.owner = client, owner
        self.remaining = max_bytes
        self.deadline = time.monotonic() + timeout_seconds
        self.observed = []
        self.heads = {}

    def check(self):
        if time.monotonic() >= self.deadline:
            reject("legacy_storage_verification_timeout", 503)

    def inventory(self):
        self.check()
        result = {}
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.owner.bucket, Prefix=self.owner.prefix
        ):
            self.check()
            for row in page.get("Contents", []):
                key = row["Key"]
                self.owner.contains(key)
                if key in result or len(result) >= MAX_OBJECTS:
                    reject("legacy_storage_inventory_limit")
                result[key] = {name: row.get(name) for name in ["ETag", "Size"]}
        return result

    def read(self, key, *, maximum, output=None):
        self.check()
        self.owner.contains(key)
        head = self.client.head_object(Bucket=self.owner.bucket, Key=key)
        size = head.get("ContentLength")
        if type(size) is not int or size < 0 or size > maximum or not head.get("ETag"):
            reject("legacy_storage_object_limit")
        params = {"Bucket": self.owner.bucket, "Key": key, "IfMatch": head["ETag"]}
        if head.get("VersionId") not in (None, "null"):
            params["VersionId"] = head["VersionId"]
        response = self.client.get_object(**params)
        body = response["Body"]
        digest, count, chunks = hashlib.sha256(), 0, []
        try:
            if _observation(response) != _observation(head):
                reject("legacy_storage_changed")
            while True:
                self.check()
                chunk = body.read(CHUNK)
                if not chunk:
                    break
                count += len(chunk)
                self.remaining -= len(chunk)
                if count > size or count > maximum or self.remaining < 0:
                    reject("legacy_storage_transfer_limit")
                digest.update(chunk)
                if output is None:
                    chunks.append(chunk)
                else:
                    output.write(chunk)
        finally:
            try:
                body.close()
            except BaseException:
                reject("legacy_storage_close_unsettled", 503)
        self.check()
        if count != size or _observation(
            self.client.head_object(Bucket=self.owner.bucket, Key=key)
        ) != _observation(head):
            reject("legacy_storage_changed")
        evidence = {"key": key, "sha256": digest.hexdigest(), "size_bytes": count}
        self.observed.append(evidence)
        self.heads[key] = _observation(head)
        return b"".join(chunks), evidence, head


def _sample_map(samples):
    result = {sample["task_id"]: sample for sample in samples}
    if len(result) != len(samples):
        reject()
    for identifier in result:
        if str(uuid.UUID(identifier)) != identifier:
            reject()
    return result


def _mask(data, sample, classes, width, height):
    payload = json.loads(data)
    retained = {}
    for instance in payload["instances"]:
        evidence = instance_rle_evidence(instance["rle"])
        retained[evidence["rle_sha256"]] = instance["rle"]

    def load(reference):
        rle = retained.get(reference["sha256"])
        if rle is None:
            reject("legacy_content_identity_unproven")
        from app.services.raster_mask_storage import canonical_rle_bytes

        canonical = canonical_rle_bytes(rle)
        if (
            _sha(canonical) != reference["sha256"]
            or len(canonical) != reference["bytes"]
            or len(rle["counts"]) != reference["runs"]
            or rle["size"] != reference["size"]
        ):
            reject("legacy_content_identity_unproven")
        return rle

    expected = instance_rle_text(
        instance_rle_payload(sample, classes, width, height, load_rle=load)
    ).encode()
    if expected != data:
        reject()
    return data


def _verify_zip(package, version, snapshot, manifest, samples, reader):
    if (
        manifest.get("source_digest") != version["snapshot_digest"]
        or manifest.get("content_digest")
        != content_digest(
            {key: value for key, value in manifest.items() if key != "content_digest"}
        )
        or contract_of(manifest) != contract_of(snapshot)
        or manifest.get("classes") != snapshot["classes"]
    ):
        reject()
    members = _sample_map(manifest["samples"])
    if (
        not set(members) <= set(samples)
        or (not snapshot.get("deduplicate") and set(members) != set(samples))
        or {member["split"] for member in members.values()} != {"train", "val"}
    ):
        reject()
    expected = {"data.yaml", "classes.json", "manifest.json"}
    for identifier, member in members.items():
        for folder, key in [("images", "image_path"), ("labels", "label_path")]:
            raw = member[key]
            path = PurePosixPath(raw)
            if (
                member["split"] not in {"train", "val"}
                or path.parts[:2] != (folder, member["split"])
                or len(path.parts) != 3
                or path.stem != identifier
                or raw in expected
                or "\\" in raw
                or ":" in raw
            ):
                reject()
            expected.add(raw)
    with zipfile.ZipFile(package) as archive:
        entries = archive.infolist()
        if (
            len(entries) > MAX_OBJECTS
            or {entry.filename for entry in entries} != expected
        ):
            reject()
        seen, total = set(), 0
        for entry in entries:
            reader.check()
            path = PurePosixPath(entry.filename)
            total += entry.file_size
            if (
                entry.filename in seen
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in entry.filename
                or entry.is_dir()
                or stat.S_ISLNK(entry.external_attr >> 16)
                or stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFREG}
                or entry.flag_bits & 1
                or total > MAX_DATASET_BYTES
            ):
                reject()
            seen.add(entry.filename)
        for name, limit in [
            ("manifest.json", 64 * 1024**2),
            ("classes.json", 1024**2),
            ("data.yaml", 1024**2),
        ]:
            if archive.getinfo(name).file_size > limit:
                reject("legacy_storage_object_limit")
        if (
            json.loads(archive.read("manifest.json")) != manifest
            or json.loads(archive.read("classes.json")) != snapshot["classes"]
        ):
            reject()
        # Match the existing package owner's deterministic YAML, without allowing
        # a second loader configuration or arbitrary download directive.
        yaml = (
            "train: images/train\nval: images/val\nnames: "
            + json.dumps(dict(enumerate(snapshot["classes"])), ensure_ascii=False)
            + "\n"
        ).encode()
        if archive.read("data.yaml") != yaml:
            reject()
        for identifier, member in members.items():
            source = samples[identifier]
            for path, expected_hash, maximum in [
                (member["image_path"], member["sha256"], MAX_IMAGE_BYTES),
                (
                    member["label_path"],
                    member["label_sha256"],
                    INSTANCE_RLE_LIMITS["max_sample_label_bytes"],
                ),
            ]:
                reader.check()
                if archive.getinfo(path).file_size > maximum:
                    reject()
                digest, count = hashlib.sha256(), 0
                with archive.open(path) as stream:
                    while chunk := stream.read(CHUNK):
                        reader.check()
                        count += len(chunk)
                        digest.update(chunk)
                if digest.hexdigest() != expected_hash or (
                    path == member["image_path"] and count != member["size_bytes"]
                ):
                    reject()
            label = archive.read(member["label_path"])
            if is_instance_rle(snapshot):
                _mask(
                    label,
                    source,
                    snapshot["classes"],
                    member["width"],
                    member["height"],
                )
                instances = [
                    {key: value for key, value in instance.items() if key != "rle"}
                    for instance in json.loads(label)["instances"]
                ]
                if member.get("instances") != instances or member.get(
                    "semantic_label_sha256"
                ) != instance_rle_semantic(instances):
                    reject()
            else:
                expected_labels, instances = native_labels(
                    source["annotations"], snapshot["classes"], contract_of(snapshot)
                )
                if label != expected_labels.encode() or (
                    instances is not None and member.get("instances") != instances
                ):
                    reject()
    return members


def verify(
    client,
    owner,
    version,
    *,
    max_bytes=MAX_TRANSFER_BYTES,
    timeout_seconds=TIMEOUT_SECONDS,
):
    """Return bounded evidence only after all required copies and inventory agree."""
    if (
        type(max_bytes) is not int
        or max_bytes <= 0
        or not 0 < timeout_seconds <= TIMEOUT_SECONDS
    ):
        reject("legacy_storage_invalid_budget")
    reader = Reader(client, owner, max_bytes=max_bytes, timeout_seconds=timeout_seconds)
    try:
        return _verify(reader, version, max_bytes)
    except TrainingDatasetError:
        raise
    except (ClientError, BotoCoreError):
        reject("legacy_storage_unavailable", 503)
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        DatasetContractError,
        zipfile.BadZipFile,
    ):
        reject()


def _verify(reader, version, max_bytes):
    snapshot, manifest = version["snapshot"], version["manifest"]
    if content_digest(snapshot) != version["snapshot_digest"]:
        reject()
    contract_of(snapshot)
    samples = _sample_map(snapshot["samples"])
    before = reader.inventory()
    members = {}
    if version["status"] == "ready":
        key = version["package_key"]
        reader.owner.contains(key)
        if key not in before:
            reject("legacy_storage_required_copy_missing")
        with tempfile.TemporaryFile() as package:
            _, evidence, _ = reader.read(key, maximum=MAX_DATASET_BYTES, output=package)
            if (
                evidence["sha256"] != version["package_sha256"]
                or evidence["size_bytes"] != version["size_bytes"]
            ):
                reject()
            package.seek(0)
            members = _verify_zip(package, version, snapshot, manifest, samples, reader)
    elif version["status"] not in {"failed", "cancelled"}:
        reject("legacy_storage_version_active")
    retained_samples = samples if version["status"] == "ready" else {}
    required = {
        reader.owner.key(f"media/{identifier}") for identifier in retained_samples
    }
    if is_instance_rle(snapshot):
        required.update(
            reader.owner.key(f"masks/{identifier}.json")
            for identifier in retained_samples
        )
    if not required <= set(before):
        reject("legacy_storage_required_copy_missing")
    retained = {}
    for key in sorted(before):
        reader.check()
        if key == version.get("package_key") and members:
            continue
        relative = key[len(reader.owner.prefix) :]
        parts = relative.split("/")
        if len(parts) != 2 or parts[0] not in {"media", "masks"}:
            reject("legacy_storage_unknown_object")
        identifier = parts[1].removesuffix(".json") if parts[0] == "masks" else parts[1]
        if identifier not in samples or (
            parts[0] == "masks"
            and (not is_instance_rle(snapshot) or not parts[1].endswith(".json"))
        ):
            reject("legacy_storage_unknown_object")
        if parts[0] == "masks":
            continue
        sample = samples[identifier]
        data, evidence, head = reader.read(key, maximum=MAX_IMAGE_BYTES)
        if (
            head.get("Metadata", {}).get("snapshot-digest")
            != version["snapshot_digest"]
            or head.get("Metadata", {}).get("sha256") != evidence["sha256"]
        ):
            reject()
        expected = (
            (members.get(identifier) or {}).get("sha256")
            or sample.get("asset_reference", {}).get("sha256")
            or sample.get("media", {}).get("sha256")
        )
        # Equal current bytes do not establish a missing historical association,
        # including samples removed from the manifest by old deduplication.
        if not expected:
            reject("legacy_content_identity_unproven")
        if (
            evidence["sha256"] != expected
            or evidence["size_bytes"] != sample["media"]["size_bytes"]
        ):
            reject()
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            # Use the same EXIF-oriented dimensions as the original builder.
            from app.services.storage import storage_service

            width, height = storage_service._logical_image_dimensions(image)
            if (
                image.format not in {"JPEG", "PNG", "WEBP", "BMP", "TIFF"}
                or width * height > 100_000_000
                or (sample.get("expected_width") and width != sample["expected_width"])
                or (
                    sample.get("expected_height")
                    and height != sample["expected_height"]
                )
            ):
                reject()
            image.load()
            extension = {
                "JPEG": ".jpg",
                "PNG": ".png",
                "WEBP": ".webp",
                "BMP": ".bmp",
                "TIFF": ".tif",
            }[image.format]
        if identifier in members and (width, height) != (
            members[identifier]["width"],
            members[identifier]["height"],
        ):
            reject()
        if identifier in members and not members[identifier]["image_path"].endswith(
            extension
        ):
            reject()
        retained[identifier] = (width, height, evidence["sha256"])
    if version["status"] == "ready" and snapshot.get("deduplicate"):
        representatives = {}
        for identifier in sorted(retained):
            representatives.setdefault(retained[identifier][2], identifier)
        if set(members) != set(representatives.values()):
            reject()
    for key in sorted(before):
        relative = key[len(reader.owner.prefix) :]
        if not relative.startswith("masks/"):
            continue
        identifier = relative.removeprefix("masks/").removesuffix(".json")
        if identifier not in retained:
            reject()
        data, evidence, _ = reader.read(
            key, maximum=INSTANCE_RLE_LIMITS["max_sample_label_bytes"]
        )
        _mask(data, samples[identifier], snapshot["classes"], *retained[identifier][:2])
        if (
            identifier in members
            and evidence["sha256"] != members[identifier]["label_sha256"]
        ):
            reject()
    if reader.inventory() != before:
        reject("legacy_storage_changed")
    for key, observed in reader.heads.items():
        reader.check()
        if (
            _observation(reader.client.head_object(Bucket=reader.owner.bucket, Key=key))
            != observed
        ):
            reject("legacy_storage_changed")
    return {
        "schema": 1,
        "scope": "current-retained-copy",
        "snapshot_digest": version["snapshot_digest"],
        "manifest_digest": content_digest(manifest),
        "object_count": len(before),
        "read_bytes": max_bytes - reader.remaining,
        "inventory_digest": content_digest(
            sorted(reader.observed, key=lambda row: row["key"])
        ),
    }
