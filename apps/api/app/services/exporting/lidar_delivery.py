"""Deliver frozen LiDAR media from independently owned, verified bundle copies."""

from __future__ import annotations

import uuid

from app.services import export_media_bundles as bundles
from app.services import export_media_storage as physical
from app.services import export_media_transfer as transfer
from app.services.content_snapshot import ContentError
from app.services.exporting import lidar_inputs
from app.services.exporting.lidar_frozen import FrozenLidar
from app.services.exporting.lidar_media_paths import logical_sources
from app.services.exporting.lidar_sources import canonical_identity, media_type
from app.services.exporting.lidar_pcd import (
    MAX_PCD_BYTES,
    MAX_PCD_POINTS,
    PcdInfo,
    inspect_pcd,
)

MAX_JOB_POINTS = 100_000_000


def _reject(code="lidar_export_delivery_changed", status=409):
    raise ContentError(code, status)


def _task_id(value):
    if not isinstance(value, (uuid.UUID, str)):
        _reject("lidar_export_delivery_scope_unknown", 422)
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        _reject("lidar_export_delivery_scope_unknown", 422)


class Delivery(FrozenLidar):
    """Frozen business rows plus private receipts, never a current-bucket lookup."""

    def __init__(self, db, snapshot, data, guard):
        super().__init__(data)
        self.expires_at = snapshot.expires_at
        self._db, self._bundle, self._guard = db, snapshot, guard
        self._task_ids = {task.id for task in self.tasks}
        self._receipts = {}
        self._point_counts = {}
        self._pcd_relatives = {}

    def entry(self, task_id, role):
        key = (_task_id(task_id), role)
        if not isinstance(role, str) or key not in self._receipts:
            _reject("lidar_export_delivery_scope_unknown", 422)
        receipt = self._receipts[key]
        return {
            "presigned_url": receipt.url,
            "expires_at": self.expires_at.isoformat(),
            "sha256": receipt.original_sha,
            "size_bytes": receipt.size,
        }

    def point_count(self, task_id):
        identifier = _task_id(task_id)
        if identifier not in self._point_counts:
            _reject("lidar_export_delivery_scope_unknown", 422)
        return self._point_counts[identifier]

    async def pcd_bytes(self, task_id):
        identifier = _task_id(task_id)
        if identifier not in self._pcd_relatives:
            _reject("lidar_export_delivery_scope_unknown", 422)
        receipt = self._receipts[(identifier, "primary_lidar")]

        def read(file):
            data = file.read(MAX_PCD_BYTES + 1)
            if not isinstance(data, bytes) or len(data) != receipt.size:
                _reject("lidar_export_delivery_copy_invalid")
            return data

        return await transfer.read_copy(
            self._db,
            self._bundle,
            relative=self._pcd_relatives[identifier],
            guard=self._guard,
            read=read,
        )


async def prepare(db, bundle_snapshot, *, guard) -> Delivery:
    """Copy each source once; retain every failed attempt's saved responsibility."""
    if not callable(guard):
        _reject("export_media_callbacks_required", 422)
    data = bundle_snapshot.input_snapshot
    objects = bundles.source_objects(bundle_snapshot, logical_sources(data))
    owner = physical.resolve(bundle_snapshot)
    delivery = Delivery(db, bundle_snapshot, data, guard)
    bindings, sources, relatives, references = [], {}, {}, {}
    seen_bindings = set()
    for binding in data["media"]:
        identifier = _task_id(binding["task_id"])
        role, source_key, source = (
            binding["role"],
            binding["source_key"],
            binding["source"],
        )
        if (
            identifier not in delivery._task_ids
            or not isinstance(role, str)
            or (identifier, role) in seen_bindings
            or source_key not in objects
            or media_type(role) is None
            or source.get("media_type") != media_type(role)
        ):
            _reject("lidar_export_delivery_scope_unknown", 422)
        seen_bindings.add((identifier, role))
        previous = sources.setdefault(source_key, source)
        if canonical_identity(previous) != canonical_identity(source):
            _reject()
        key = objects[source_key]
        owner.contains(key)
        relatives[source_key] = key[len(owner.prefix) :]
        bindings.append((identifier, role, source_key, binding))
        references.setdefault(source_key, []).append(binding)
    if set(sources) != set(objects):
        _reject()

    receipts, points, total_points = {}, {}, 0
    for identifier, role, source_key, binding in bindings:
        source = sources[source_key]
        if source_key not in receipts:
            object_guard = guard
            if len(references[source_key]) > 1:

                async def object_guard(owned, others=tuple(references[source_key][1:])):
                    await guard(owned)
                    for reference in others:
                        await lidar_inputs.check_source(owned, reference)

            receipt = await transfer.copy_source(
                db,
                bundle_snapshot,
                binding=binding,
                relative=relatives[source_key],
                resolve_source=lidar_inputs.check_source,
                guard=object_guard,
                inspect=inspect_pcd if source["media_type"] == "point_cloud" else None,
            )
            if (
                receipt.original_sha != source["sha256"]
                or type(receipt.size) is not int
                or receipt.size != source["size_bytes"]
                or not 0 < receipt.size <= MAX_PCD_BYTES
            ):
                _reject()
            if source["media_type"] == "point_cloud":
                info = receipt.inspection
                if (
                    not isinstance(info, PcdInfo)
                    or type(info.point_count) is not int
                    or not 0 <= info.point_count <= MAX_PCD_POINTS
                    or info.size_bytes != receipt.size
                ):
                    _reject("lidar_export_pcd_inspection_invalid", 422)
                points[source_key] = info.point_count
            receipts[source_key] = receipt
        delivery._receipts[(identifier, role)] = receipts[source_key]
        if role == "primary_lidar":
            count = points[source_key]
            total_points += count
            if total_points > MAX_JOB_POINTS:
                _reject("lidar_export_point_budget_exceeded", 422)
            delivery._point_counts[identifier] = count
            delivery._pcd_relatives[identifier] = relatives[source_key]
    return delivery
