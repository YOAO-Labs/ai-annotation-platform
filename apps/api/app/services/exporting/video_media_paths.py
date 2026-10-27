"""Plan stable video and sequence names independently of physical owners."""

import re
import uuid

from app.services.content_snapshot import ContentError

TARGETS = frozenset(
    {
        "video_json",
        "aap_json",
        "mot",
        "kitti",
        "yolo-frames-det",
        "yolo-frames-seg",
        "coco-frames-seg",
        "davis",
        "youtube-vos",
        "mots",
    }
)


def reject():
    raise ContentError("video_export_archive_path_conflict", 422)


def _id(raw):
    try:
        result = uuid.UUID(str(raw))
        if str(result) != str(raw):
            reject()
        return result
    except (ValueError, TypeError, AttributeError):
        reject()


def _name(raw):
    if (
        not isinstance(raw, str)
        or not raw
        or any(ord(c) < 32 or ord(c) == 127 for c in raw)
    ):
        reject()
    filename = raw.replace("\\", "/").rsplit("/", 1)[-1]
    stem, separator, extension = filename.rpartition(".")
    if not separator:
        stem, extension = filename, ""
    stem = re.sub(r"[^\w.-]+", "_", stem)[:120].strip(".") or "video"
    extension = re.sub(r"[^A-Za-z0-9]+", "", extension)[:20].lower()
    return stem, stem + ("." + extension if extension else "")


def _tasks(snapshot):
    tasks = snapshot.get("tasks")
    if not isinstance(tasks, list):
        reject()
    result = {}
    for task in tasks:
        if not isinstance(task, dict):
            reject()
        identifier = _id(task.get("id"))
        if identifier in result:
            reject()
        result[identifier] = _name(task.get("file_name"))
    return result


def paths(snapshot):
    return {
        identifier: f"tasks/{identifier}/{name}"
        for identifier, (_, name) in _tasks(snapshot).items()
    }


def sequence_paths(snapshot):
    return {
        identifier: f"tasks/{identifier}/{stem}"
        for identifier, (stem, _) in _tasks(snapshot).items()
    }


def logical_sources(snapshot):
    """Each target uses all originals, with a separate alias for every Task."""
    targets = snapshot.get("targets")
    if (
        not isinstance(targets, list)
        or not targets
        or any(
            not isinstance(target, str) or target not in TARGETS for target in targets
        )
        or len(set(targets)) != len(targets)
        or snapshot.get("references") != {}
    ):
        reject()
    relative, managed = paths(snapshot), {}
    media = snapshot.get("media")
    if not isinstance(media, list):
        reject()
    for binding in media:
        if not isinstance(binding, dict):
            reject()
        identifier = _id(binding.get("task_id"))
        key = binding.get("source_key")
        if (
            identifier not in relative
            or identifier in managed
            or binding.get("role") != "video"
            or binding.get("link_id") is not None
            or not isinstance(key, str)
            or re.fullmatch(r"[0-9a-f]{64}", key) is None
        ):
            reject()
        managed[identifier] = key
    if set(managed) != set(relative):
        reject()
    return {
        target: {relative[key]: source for key, source in managed.items()}
        for target in targets
    }
