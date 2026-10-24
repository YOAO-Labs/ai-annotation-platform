"""Allocate immutable media responsibility and fence a single publication.

The caller commits allocation and claim before physical work. These helpers do
no original/object I/O, acquire no authority, and never commit caller work.
"""

from collections import Counter
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db.models.export_media_bundle import ExportMediaBundle as Bundle
from app.services import export_media_storage as physical
from app.services.content_snapshot import ContentError
from app.services.exporting.lidar_sources import canonical_identity

TARGETS = frozenset({"kitti", "pointmask", "coco-multicamera"})
MAX_JSON_BYTES = 256 * 1024**2
MAX_IMAGES = 20_000
MAX_PCDS = 1_000
MAX_IMAGE_BYTES = 20 * 1024**3
MAX_PCD_BYTES = 4 * 1024**3
LEASE_SECONDS = 3600
_SHA = re.compile(r"[0-9a-f]{64}")
_SCALARS = (
    "id",
    "project_id",
    "job_id",
    "zip_output_id",
    "input_digest",
    "object_count",
    "size_bytes",
    "created_at",
    "expires_at",
    "storage_ref",
    "state",
    "lease_token",
    "lease_expires_at",
    "published_at",
    "retired_at",
)
_FACTS = tuple(
    name
    for name in _SCALARS
    if name
    not in {"state", "lease_token", "lease_expires_at", "published_at", "retired_at"}
)
_DOCUMENTS = ("input_snapshot", "logical_paths", "inventory")


class ExportMediaBundleError(ContentError):
    def __init__(self, code="changed", status=409):
        super().__init__("export_media_bundle_" + code, status)
        self.status_code = status


def reject(code="changed", status=409):
    raise ExportMediaBundleError(code, status)


def now():
    return datetime.now(timezone.utc)


def _uuid(value):
    if not isinstance(value, (uuid.UUID, str)):
        reject("invalid")
    try:
        result = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        reject("invalid")
    if str(result) != str(value):
        reject("invalid")
    return result


def _instant(value):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        reject("invalid")
    return value.astimezone(timezone.utc)


def _sha(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        reject("invalid")
    return value


def _json_info(data, *, limit=MAX_JSON_BYTES):
    """Match the collector's exact encoding without allocating another JSON string."""
    hashed, size = hashlib.sha256(), 0
    try:
        encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False, allow_nan=False)
        for part in encoder.iterencode(data):
            encoded = part.encode("utf-8")
            size += len(encoded)
            if size > limit:
                reject("budget", 422)
            hashed.update(encoded)
    except ExportMediaBundleError:
        raise
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        reject("invalid", 422)
    pending = [iter((data,))]
    while pending:
        try:
            value = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        if isinstance(value, dict):
            if any(not isinstance(key, str) or "\x00" in key for key in value):
                reject("invalid", 422)
            pending.append(iter(value.values()))
        elif isinstance(value, list):
            pending.append(iter(value))
        elif isinstance(value, str):
            if "\x00" in value:
                reject("invalid", 422)
        elif value is not None and type(value) not in {bool, int, float}:
            reject("invalid", 422)
    return hashed.hexdigest(), size


def _relative(path):
    if (
        not isinstance(path, str)
        or not path
        or len(path.encode("utf-8")) > 4096
        or "\\" in path
        or ":" in path.split("/")[0]
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        reject("invalid_path", 422)
    return path


def _sources(data, project_id):
    if (
        not isinstance(data, dict)
        or type(data.get("schema")) is not int
        or data["schema"] not in {1, 2}
        or data.get("project_id") != str(project_id)
        or not isinstance(data.get("targets"), list)
        or not data["targets"]
        or any(not isinstance(target, str) for target in data["targets"])
        or len(set(data["targets"])) != len(data["targets"])
        or not isinstance(data.get("tasks"), list)
        or not data["tasks"]
        or not isinstance(data.get("media"), list)
    ):
        reject("invalid", 422)
    modern = data["schema"] == 2
    targets = (TARGETS | ({"nuscenes"} if modern else set())).intersection(
        data["targets"]
    )
    if not targets:
        reject("invalid", 422)
    # Historical mixed bundles own only private PCD support; retain their facts.
    if "nuscenes" in data["targets"]:
        targets = targets | {"nuscenes"}
    tasks = set()
    for task in data["tasks"]:
        if not isinstance(task, dict) or task.get("project_id") != str(project_id):
            reject("invalid", 422)
        identifier = str(_uuid(task.get("id")))
        if identifier in tasks:
            reject("invalid", 422)
        tasks.add(identifier)
    if modern and "nuscenes" in targets and len(tasks) > MAX_PCDS:
        reject("budget", 422)
    options = data.get("options")
    if not isinstance(options, dict) or not isinstance(options.get("lidar", {}), dict):
        reject("invalid", 422)
    selected = options.get("lidar", {}).get("kitti_camera_role")
    if "kitti" in targets and (
        not isinstance(selected, str) or not selected.startswith("camera_")
    ):
        reject("invalid", 422)
    sources, bindings, cameras_by_task = {}, {}, {task: set() for task in tasks}
    count, total = Counter(), Counter()
    for entry in data["media"]:
        if not isinstance(entry, dict) or set(entry) != {
            "task_id",
            "role",
            "link_id",
            "source_key",
            "source",
        }:
            reject("invalid", 422)
        task, role, source = entry["task_id"], entry["role"], entry["source"]
        if (
            task not in tasks
            or not isinstance(role, str)
            or (task, role) in bindings
            or not isinstance(source, dict)
        ):
            reject("invalid", 422)
        if role == "primary_lidar":
            media_type = "point_cloud"
            if entry["link_id"] is not None:
                reject("invalid", 422)
        elif role.startswith("camera_"):
            media_type = "image"
            _uuid(entry["link_id"])
        elif modern and "nuscenes" in targets and role in {"raw_lidar", "map"}:
            media_type = role
            if (
                entry["link_id"] is not None
                or source.get("owner_kind") != "dataset_auxiliary"
                or not isinstance(source.get("ownership_facts"), dict)
                or source["ownership_facts"].get("kind") != role
            ):
                reject("invalid", 422)
        else:
            reject("invalid", 422)
        size = source.get("size_bytes")
        _sha(source.get("sha256"))
        if (
            source.get("media_type") != media_type
            or type(size) is not int
            or not 0 < size <= physical.MAX_OBJECT_BYTES
        ):
            reject("invalid", 422)
        key = _sha(entry["source_key"])
        identity = canonical_identity(source)
        if _json_info(identity)[0] != key or (
            key in sources and sources[key] != identity
        ):
            reject("source_changed")
        if key not in sources:
            sources[key] = identity
            count[media_type] += 1
            total[media_type] += size
        bindings[task, role] = key
        if media_type == "image":
            cameras_by_task[task].add(key)
    if (
        count["image"] > MAX_IMAGES
        or count["point_cloud"] > MAX_PCDS
        or total["image"] > MAX_IMAGE_BYTES
        or total["point_cloud"] > MAX_PCD_BYTES
        or sum(count.values()) > physical.MAX_OBJECTS
        or sum(total.values()) > physical.MAX_TOTAL_BYTES
    ):
        reject("budget", 422)
    required = {target: set() for target in targets}
    for task in tasks:
        cameras = cameras_by_task[task]
        primary = bindings.get((task, "primary_lidar"))
        if {"kitti", "pointmask", "nuscenes"}.intersection(targets) and primary is None:
            reject("coverage", 422)
        if "kitti" in targets:
            camera = bindings.get((task, selected))
            if camera is None:
                reject("coverage", 422)
            required["kitti"].update({primary, camera})
        if "pointmask" in targets:
            required["pointmask"].update({primary, *cameras})
        if "nuscenes" in targets:
            required["nuscenes"].add(primary)
            if modern:
                raw, map_source = (
                    bindings.get((task, "raw_lidar")),
                    bindings.get((task, "map")),
                )
                if raw is None or map_source is None:
                    reject("coverage", 422)
                required["nuscenes"].update({raw, map_source, *cameras})
        if "coco-multicamera" in targets:
            if not cameras:
                reject("coverage", 422)
            required["coco-multicamera"].update(cameras)
    if set(sources) != set().union(*required.values()):
        reject("coverage", 422)
    return sources, required


def _logical_sources(logical_sources, required):
    if not isinstance(logical_sources, dict) or set(logical_sources) != set(required):
        reject("coverage", 422)
    for target, paths in logical_sources.items():
        if not isinstance(paths, dict):
            reject("coverage", 422)
        for path, source_key in paths.items():
            _relative(path)
            _sha(source_key)
        if set(paths.values()) != required[target]:
            reject("coverage", 422)


@dataclass(frozen=True)
class BundleFence:
    id: uuid.UUID
    project_id: uuid.UUID
    job_id: uuid.UUID
    zip_output_id: uuid.UUID
    input_digest: str
    object_count: int
    size_bytes: int
    created_at: datetime
    expires_at: datetime
    state: str
    lease_token: uuid.UUID | None
    lease_expires_at: datetime | None
    published_at: datetime | None
    retired_at: datetime | None
    _storage_ref: dict = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "_storage_ref", deepcopy(self._storage_ref))

    @property
    def storage_ref(self):
        return deepcopy(self._storage_ref)


@dataclass(frozen=True)
class BundleSnapshot(BundleFence):
    _input_snapshot: dict = field(repr=False)
    _logical_paths: dict = field(repr=False)
    _inventory: dict = field(repr=False)

    def __post_init__(self):
        super().__post_init__()
        for name in _DOCUMENTS:
            object.__setattr__(self, "_" + name, deepcopy(getattr(self, "_" + name)))

    @property
    def input_snapshot(self):
        return deepcopy(self._input_snapshot)

    @property
    def logical_paths(self):
        return deepcopy(self._logical_paths)

    @property
    def inventory(self):
        return deepcopy(self._inventory)


def _field(row, name):
    return row.get(name) if isinstance(row, dict) else getattr(row, name, None)


def _fence(row):
    facts = {name: _field(row, name) for name in _SCALARS}
    for name in ("id", "project_id", "job_id", "zip_output_id"):
        facts[name] = _uuid(facts[name])
    _sha(facts["input_digest"])
    for name, limit in (
        ("object_count", physical.MAX_OBJECTS),
        ("size_bytes", physical.MAX_TOTAL_BYTES),
    ):
        if type(facts[name]) is not int or not 0 <= facts[name] <= limit:
            reject("invalid")
    created, expires = (_instant(facts[name]) for name in ("created_at", "expires_at"))
    if expires != created + timedelta(hours=168) or facts["state"] not in {
        "building",
        "ready",
        "retiring",
        "deleted",
    }:
        reject("invalid")
    token, lease = facts["lease_token"], facts["lease_expires_at"]
    if (token is None) != (lease is None):
        reject("invalid")
    if token is not None and (
        not isinstance(token, uuid.UUID) or not created < _instant(lease) <= expires
    ):
        reject("invalid")
    published, retired = facts["published_at"], facts["retired_at"]
    if (
        (facts["state"] == "building" and published is not None)
        or (facts["state"] == "ready" and (published is None or token is None))
        or (published is not None and not created <= _instant(published) < expires)
        or ((facts["state"] in {"retiring", "deleted"}) != (retired is not None))
        or (retired is not None and _instant(retired) < created)
    ):
        reject("invalid")
    facts["_storage_ref"] = facts.pop("storage_ref")
    result = BundleFence(**facts)
    physical.resolve(result)
    return result


def _receipt(source):
    return source["sha256"], source["size_bytes"]


def snapshot(row):
    """Validate and copy the complete immutable input once, outside per-object loops."""
    fence = _fence(row)
    data, logical, inventory = (_field(row, name) for name in _DOCUMENTS)
    digest, size = _json_info(data, limit=MAX_JSON_BYTES)
    if digest != fence.input_digest:
        reject("input_changed")
    sources, required = _sources(data, fence.project_id)
    objects = physical.validate_inventory(row)
    if fence.object_count != len(objects) or fence.size_bytes != sum(
        entry["size_bytes"] for entry in objects.values()
    ):
        reject("invalid")
    if (
        not isinstance(logical, dict)
        or set(logical) != {"schema", "targets"}
        or type(logical["schema"]) is not int
        or logical["schema"] != 1
        or not isinstance(logical["targets"], dict)
        or set(logical["targets"]) != set(required)
    ):
        reject("coverage")
    used = set()
    for target, paths in logical["targets"].items():
        if not isinstance(paths, dict):
            reject("coverage")
        keys = set()
        for path, key in paths.items():
            _relative(path)
            if not isinstance(key, str) or key not in objects:
                reject("coverage")
            keys.add(key)
        if Counter(_receipt(objects[key]) for key in keys) != Counter(
            _receipt(sources[key]) for key in required[target]
        ):
            reject("coverage")
        used.update(keys)
    if used != set(objects) or len(objects) != len(sources):
        reject("coverage")
    for document in (logical, inventory):
        size += _json_info(document, limit=MAX_JSON_BYTES - size)[1]
    values = {name: getattr(fence, name) for name in _SCALARS if name != "storage_ref"}
    return BundleSnapshot(
        **values,
        _storage_ref=fence.storage_ref,
        _input_snapshot=data,
        _logical_paths=logical,
        _inventory=inventory,
    )


def source_objects(receipt, logical_sources):
    """Align reproduced logical names once; no byte identity is guessed from SHA."""
    sources, required = _sources(receipt._input_snapshot, receipt.project_id)
    _logical_sources(logical_sources, required)
    targets, objects = receipt._logical_paths["targets"], receipt._inventory["objects"]
    result, owners = {}, {}
    for target, paths in logical_sources.items():
        if set(paths) != set(targets[target]):
            reject("coverage")
        for path, key in paths.items():
            destination = targets[target][path]
            if (
                (key in result and result[key] != destination)
                or (destination in owners and owners[destination] != key)
                or objects[destination]
                != {
                    "sha256": sources[key]["sha256"],
                    "size_bytes": sources[key]["size_bytes"],
                }
            ):
                reject("coverage")
            result[key], owners[destination] = destination, key
    if set(result) != set(sources) or set(owners) != set(objects):
        reject("coverage")
    return result


def same(row, receipt):
    current = row if isinstance(row, BundleSnapshot) else snapshot(row)
    return all(
        getattr(current, name) == getattr(receipt, name) for name in _FACTS
    ) and all(
        getattr(current, "_" + name) == getattr(receipt, "_" + name)
        for name in _DOCUMENTS
    )


async def allocate(
    db, *, project_id, job_id, input_snapshot, input_digest, logical_sources
):
    """Flush responsibility with the new Job; the HTTP caller commits both."""
    project_id, job_id = _uuid(project_id), _uuid(job_id)
    digest, size = _json_info(input_snapshot, limit=MAX_JSON_BYTES)
    if _sha(input_digest) != digest:
        reject("input_changed", 422)
    _json_info(logical_sources, limit=MAX_JSON_BYTES - size)
    sources, required = _sources(input_snapshot, project_id)
    _logical_sources(logical_sources, required)
    identifier, output_id, started = uuid.uuid4(), uuid.uuid4(), now()
    owner_ref = physical.make_ref(identifier, bucket=settings.minio_export_bucket)
    keys, objects = {}, {}
    for source_key in sorted(sources):
        key = physical.prefix_for(identifier) + str(uuid.uuid4()) + ".bin"
        if key in objects:
            reject("invalid")
        keys[source_key] = key
        source = sources[source_key]
        objects[key] = {"sha256": source["sha256"], "size_bytes": source["size_bytes"]}
    row = Bundle(
        id=identifier,
        project_id=project_id,
        job_id=job_id,
        zip_output_id=output_id,
        input_snapshot=deepcopy(input_snapshot),
        input_digest=input_digest,
        logical_paths={
            "schema": 1,
            "targets": {
                target: {path: keys[key] for path, key in paths.items()}
                for target, paths in logical_sources.items()
            },
        },
        inventory={"schema": 1, "objects": objects},
        object_count=len(objects),
        size_bytes=sum(entry["size_bytes"] for entry in objects.values()),
        storage_ref=owner_ref,
        state="building",
        created_at=started,
        expires_at=started + timedelta(hours=168),
        lease_token=None,
        lease_expires_at=None,
        published_at=None,
        retired_at=None,
    )
    result = snapshot(row)
    source_objects(result, logical_sources)
    db.add(row)
    await db.flush()
    return result


@asynccontextmanager
async def _reads(db, lock):
    # Use a connection savepoint: Session.begin_nested() flushes caller work.
    with db.no_autoflush:
        if not lock:
            yield
            return
        connection = await db.connection()
        savepoint = await connection.begin_nested()
        try:
            yield
        except BaseException as exc:
            await savepoint.rollback()
            if isinstance(exc, DBAPIError) and (
                getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
            ) in {"55P03", "40P01", "40001"}:
                reject("busy")
            raise
        else:
            await savepoint.commit()


def _query(statement, identifier, lock):
    statement = statement.where(Bundle.id == _uuid(identifier))
    if lock:
        statement = statement.with_for_update(nowait=True)
    return statement.execution_options(populate_existing=True)


async def _row(db, identifier, *, lock):
    async with _reads(db, lock):
        current = await db.scalar(_query(select(Bundle), identifier, lock))
    if current is None:
        reject("missing", 404)
    return current


async def claim(db, identifier):
    """Claim at most once, without takeover or renewal, then let the caller commit."""
    row = await _row(db, identifier, lock=True)
    current = snapshot(row)
    if current.lease_token is not None:
        return None
    if current.state != "building":
        reject("lease_lost")
    started = now()
    if current.expires_at <= started:
        reject("expired")
    row.lease_token = uuid.uuid4()
    row.lease_expires_at = min(
        started + timedelta(seconds=LEASE_SECONDS), current.expires_at
    )
    await db.flush()
    return snapshot(row)


def _check(current, receipt):
    if any(getattr(current, name) != getattr(receipt, name) for name in _FACTS):
        reject()
    if (
        current.state != "building"
        or receipt.state != "building"
        or receipt.lease_token is None
        or current.lease_token != receipt.lease_token
        or current.lease_expires_at != receipt.lease_expires_at
    ):
        reject("lease_lost")
    instant = now()
    if current.expires_at <= instant or current.lease_expires_at <= instant:
        reject("expired")


async def check_claim(db, receipt, *, lock=False):
    """Small scalar fence; immutable DB guards protect documents between full checks."""
    async with _reads(db, lock):
        result = await db.execute(
            _query(
                select(*(getattr(Bundle, name) for name in _SCALARS)), receipt.id, lock
            )
        )
        row = result.mappings().one_or_none()
    if row is None:
        reject("missing", 404)
    current = _fence(dict(row))
    _check(current, receipt)
    return current


async def publish(db, receipt):
    """Join the final short transaction; the caller publishes its ZIP pair with us."""
    row = await _row(db, receipt.id, lock=True)
    current = snapshot(row)
    _check(current, receipt)
    if not same(current, receipt):
        reject()
    # Check natural expiry again after the complete immutable JSON comparison.
    _check(current, receipt)
    row.state, row.published_at = "ready", now()
    await db.flush()
    return row
