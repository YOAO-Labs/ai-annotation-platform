"""Persistent observations for built-in detectors; reads never load weights."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from training_contract.assets import resolve_prepared_asset

from aap_protocol_v2.model_metadata import (
    ModelClassMetadata,
    ModelMetadataResponse,
    classes_digest,
)

from class_names import COCO80
from model_registry import MODEL_MATRIX, resolve_weight_filename


def file_signature(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


class ModelMetadataStore:
    def __init__(self, checkpoints: Path, *, model_assets: Path | None = None):
        self.checkpoints = checkpoints
        self.root = checkpoints / ".model-metadata"
        self.model_assets = model_assets

    def _index(self, series: str, size: str) -> Path:
        key = hashlib.sha256(f"detect:{series}:{size}".encode()).hexdigest()
        return self.root / f"{key}.json"

    def observe(self, path, model, *, series, size, loader_version, before=None):
        path = Path(path)
        start = file_signature(path)
        if before is not None and start != before:
            raise ValueError("Checkpoint changed during model loading")
        names = getattr(model, "names", None)
        if (
            getattr(model, "task", None) != "detect"
            or not isinstance(names, dict)
            or set(names) != set(range(len(names)))
            or any(type(i) is not int for i in names)
        ):
            raise ValueError("Expected complete native detection classes")
        ordered = [names[i] for i in range(len(names))]
        with path.open("rb") as stream:
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
        if file_signature(path) != start:
            raise ValueError("Checkpoint changed during metadata inspection")
        item = ModelClassMetadata(
            model_variants={"series": series, "size": size},
            verification_status="verified" if ordered == COCO80 else "mismatch",
            artifact_sha256=sha,
            classes=[{"index": i, "name": name} for i, name in enumerate(ordered)],
            classes_digest=classes_digest(ordered),
            loader_version=loader_version,
            inspected_at=datetime.now(timezone.utc),
        )
        model._aap_class_metadata = item.model_dump(mode="json")
        self.root.mkdir(parents=True, exist_ok=True)
        loader_digest = hashlib.sha256(loader_version.encode()).hexdigest()[:16]
        artifact = f"{sha}-{loader_digest}-1.json"
        self._write(
            self.root / artifact,
            item.model_dump(mode="json", exclude={"model_variants"}),
        )
        self._write(
            self._index(series, size), {"artifact": artifact, "signature": start}
        )
        return item

    @staticmethod
    def _write(path, payload):
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".metadata-")
        try:
            with os.fdopen(handle, "w") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def read(self, series, size, *, loader_version=None):
        variants = {"series": series, "size": size}
        status = "uninspected"
        try:
            filename = resolve_weight_filename("detect", series, size)
            prepared = (
                resolve_prepared_asset(self.model_assets, filename)
                if self.model_assets is not None
                else None
            )
            path = prepared if prepared is not None else self.checkpoints / filename
            signature = file_signature(path)
            index = json.loads(self._index(series, size).read_text())
            artifact = index["artifact"]
            if (
                not isinstance(artifact, str)
                or Path(artifact).name != artifact
                or index["signature"] != signature
            ):
                raise ValueError("Checkpoint observation is stale")
            item = ModelClassMetadata.model_validate(
                {
                    **json.loads((self.root / artifact).read_text()),
                    "model_variants": variants,
                }
            )
            if item.verification_status not in {"verified", "mismatch"}:
                raise ValueError("Invalid checkpoint observation")
            # Some filesystems coalesce timestamps for rapid in-place writes.
            # Streaming bytes verifies identity without deserializing a checkpoint.
            with path.open("rb") as stream:
                actual_sha = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual_sha != item.artifact_sha256 or file_signature(path) != signature:
                raise ValueError("Checkpoint content changed")
            if (
                artifact
                != f"{item.artifact_sha256}-{hashlib.sha256(item.loader_version.encode()).hexdigest()[:16]}-1.json"
                or (
                    loader_version is not None and item.loader_version != loader_version
                )
            ):
                raise ValueError("Checkpoint loader/selection differs")
            return item
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            status = "unavailable"
        return ModelClassMetadata(model_variants=variants, verification_status=status)

    def snapshot(self, *, loader_version=None):
        return ModelMetadataResponse(
            models=[
                self.read(series, size, loader_version=loader_version)
                for series, sizes in MODEL_MATRIX["detect"].items()
                for size in sizes
            ]
        )
