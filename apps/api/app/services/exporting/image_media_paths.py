"""Plan image archive names independently of physical object namespaces."""

import re
import uuid

from app.services.content_snapshot import ContentError
from aap_workspace_contract import derive_classes_list

TARGETS = frozenset(
    {
        "coco",
        "yolo",
        "yolo-det",
        "yolo-obb",
        "yolo-seg",
        "aap_json",
        "label-studio-brush",
        "binary-png",
        "indexed-png",
    }
)
YOLO_TARGETS = frozenset({"yolo", "yolo-det", "yolo-obb", "yolo-seg"})


def reject():
    raise ContentError("image_export_archive_path_conflict", 422)


def _safe(path):
    if (
        not isinstance(path, str)
        or not path
        or len(path.encode("utf-8")) > 4096
        or any(char in path for char in ("\\", ":"))
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        reject()
    return path


def _id(raw):
    try:
        result = uuid.UUID(str(raw))
        if str(result) != str(raw):
            reject()
        return result
    except (ValueError, TypeError, AttributeError):
        reject()


def _name(raw, *, reference=False):
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        reject()
    filename = raw.replace("\\", "/").rsplit("/", 1)[-1]
    stem, separator, extension = filename.rpartition(".")
    if reference and (
        not separator or extension.lower() not in {"png", "jpg", "jpeg", "webp"}
    ):
        raise ContentError("reference_image_extension_invalid")
    if not separator:
        stem, extension = filename, ""
    stem = re.sub(r"[^\w.-]+", "_", stem)[:120].strip(".") or "image"
    extension = re.sub(r"[^A-Za-z0-9]+", "", extension)[:20].lower()
    return _safe(stem + ("." + extension if extension else ""))


def paths(snapshot):
    """A shared source has one physical copy and a distinct name per Task."""
    references = snapshot.get("references", {})
    if not isinstance(references, dict):
        reject()
    result = {}
    for task in snapshot["tasks"]:
        task_id = _id(task["id"])
        if task_id in result:
            reject()
        reference = references.get(str(task_id))
        name = _name(task.get("file_name"), reference=reference is not None)
        if reference is not None:
            dataset_id, item_id = (
                _id(reference["dataset_id"]),
                _id(task["dataset_item_id"]),
            )
            relative = f"{dataset_id}/{item_id}/{task_id}/{name}"
        else:
            relative = f"tasks/{task_id}/{name}"
        result[task_id] = _safe(relative)
    if not set(references).issubset({str(key) for key in result}):
        reject()
    return result


def archive_paths(snapshot):
    """Destinations used by the root manifest and each YOLO training root."""
    relative = paths(snapshot)
    multi = len(snapshot["targets"]) > 1
    return {
        identifier: sorted(
            {
                f"images/{path}",
                *(
                    f"{target}/images/{path}"
                    for target in snapshot["targets"]
                    if multi and target in YOLO_TARGETS
                ),
            }
        )
        for identifier, path in relative.items()
    }


def logical_sources(snapshot):
    """Validate all generated names, returning only managed copy mappings."""
    targets = snapshot["targets"]
    if (
        not isinstance(targets, list)
        or not targets
        or any(target not in TARGETS for target in targets)
        or len(set(targets)) != len(targets)
    ):
        reject()
    relative, destinations = paths(snapshot), archive_paths(snapshot)
    references = snapshot.get("references", {})
    managed = {}
    for binding in snapshot["media"]:
        identifier = _id(binding["task_id"])
        key = binding.get("source_key")
        if (
            identifier not in relative
            or identifier in managed
            or str(identifier) in references
            or binding.get("role") != "image"
            or binding.get("link_id") is not None
            or not isinstance(key, str)
            or re.fullmatch(r"[0-9a-f]{64}", key) is None
        ):
            reject()
        managed[identifier] = key
    if set(managed) != set(relative) - {_id(key) for key in references}:
        reject()
    members = set()

    def generated(path):
        path = _safe(path)
        if path in members:
            reject()
        members.add(path)

    for name in (
        "data_contract.json",
        "classes.txt",
        "images_manifest.json",
        "fetch_images.py",
    ):
        generated(name)
    include_attributes = snapshot.get("options", {}).get("include_attributes", True)
    if include_attributes:
        generated("attribute_schema.json")
    for destinations_for_task in destinations.values():
        for name in destinations_for_task:
            generated(name)
    multi = len(targets) > 1
    classes = set(derive_classes_list(snapshot["project"].get("tool_bindings") or {}))
    masks = {}
    for annotation in snapshot.get("annotations", []):
        if (
            annotation.get("class_name") in classes
            and (annotation.get("geometry") or {}).get("type") == "raster_mask"
        ):
            masks.setdefault(_id(annotation["task_id"]), []).append(annotation)
    for target in targets:
        prefix = f"{target}/" if multi else ""
        if target in YOLO_TARGETS:
            generated(prefix + "data.yaml")
            for path in relative.values():
                stem = (
                    path.rsplit(".", 1)[0] if "." in path.rsplit("/", 1)[-1] else path
                )
                generated(prefix + "labels/" + stem + ".txt")
                if include_attributes:
                    generated(prefix + "labels/" + stem + ".attrs.json")
        elif target in {"coco", "aap_json", "label-studio-brush"}:
            generated(prefix + "annotations.json")
        else:
            generated(prefix + "manifest.json")
            for task in snapshot["tasks"]:
                annotations = masks.get(_id(task["id"]), [])
                if target == "indexed-png" and annotations:
                    generated(prefix + f"masks/{task['display_id']}.png")
                elif target == "binary-png":
                    for annotation in annotations:
                        generated(
                            prefix
                            + f"masks/{task['display_id']}/{_id(annotation['id'])}.png"
                        )
    if not multi and not YOLO_TARGETS.intersection(targets):
        generated("data.yaml")
    from app.config import settings

    if len(members) > settings.mask_format_max_archive_files:
        raise ContentError("image_export_input_budget_exceeded", 422)
    return {
        target: {relative[key]: value for key, value in managed.items()}
        for target in targets
    }
