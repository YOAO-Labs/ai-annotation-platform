"""Managed object identity and logical paths, independent of display names."""

from __future__ import annotations

import uuid

MANAGED_DATASET_PREFIX = "__aap_managed_datasets__"
MANAGED_OBJECTS_DIR = "_objects"


def managed_dataset_prefix(dataset_id: uuid.UUID) -> str:
    return f"{MANAGED_DATASET_PREFIX}/{dataset_id}/"


def normalize_dataset_relative_path(relative_path: str) -> str:
    path = relative_path.replace("\\", "/").strip("/")
    if not path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("Invalid dataset relative path")
    return path


def managed_dataset_key(dataset_id: uuid.UUID, relative_path: str) -> str:
    # Keep the logical suffix intact, while each write gets its own identity.
    path = normalize_dataset_relative_path(relative_path)
    return f"{managed_dataset_prefix(dataset_id)}{MANAGED_OBJECTS_DIR}/{uuid.uuid4()}/{path}"


def dataset_relative_path(file_path: str, dataset_name: str = "") -> str:
    path = (file_path or "").lstrip("/")
    parts = path.split("/", 4)
    if len(parts) >= 3 and parts[0] == MANAGED_DATASET_PREFIX:
        try:
            uuid.UUID(parts[1])
        except ValueError:
            pass
        else:
            if len(parts) == 5 and parts[2] == MANAGED_OBJECTS_DIR:
                try:
                    uuid.UUID(parts[3])
                except ValueError:
                    pass
                else:
                    return parts[4]
            return "/".join(parts[2:])
    prefix = f"{dataset_name}/"
    return path[len(prefix) :] if dataset_name and path.startswith(prefix) else path


def dataset_item_relative_path(item, dataset_name: str = "") -> str:
    """New items persist aliases; historical items retain path-based fallback."""
    metadata = getattr(item, "metadata_", None) or {}
    logical_path = metadata.get("storage_relative_path")
    if isinstance(logical_path, str):
        try:
            return normalize_dataset_relative_path(logical_path)
        except ValueError:
            pass
    return dataset_relative_path(item.file_path, dataset_name)
