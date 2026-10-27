"""Copy managed images once while retaining every frozen Task binding."""

import uuid

from app.services import export_media_bundles as bundles
from app.services import export_media_storage as physical
from app.services import export_media_transfer as transfer
from app.services.content_snapshot import ContentError
from app.services.exporting import image_inputs
from app.services.exporting.image_frozen import FrozenImage
from app.services.exporting.image_media_paths import (
    archive_paths,
    logical_sources,
    paths,
)
from app.services.exporting.lidar_sources import canonical_identity

MAX_IMAGE_BYTES = 256 * 1024 * 1024


def _reject(code="image_export_delivery_changed", status=409):
    raise ContentError(code, status)


class Delivery(FrozenImage):
    def __init__(self, snapshot):
        super().__init__(snapshot.input_snapshot)
        self.expires_at = snapshot.expires_at
        self.paths = paths(snapshot.input_snapshot)
        self.archive_paths = archive_paths(snapshot.input_snapshot)
        self.media_paths = {
            identifier: "images/" + relative
            for identifier, relative in self.paths.items()
        }
        self._receipts = {}

    def entry(self, task_id):
        try:
            identifier = uuid.UUID(str(task_id))
        except (TypeError, ValueError, AttributeError):
            _reject("image_export_delivery_scope_unknown", 422)
        if identifier not in self._receipts:
            _reject("image_export_delivery_scope_unknown", 422)
        receipt = self._receipts[identifier]
        return {
            "presigned_url": receipt.url,
            "expires_at": self.expires_at.isoformat(),
            "sha256": receipt.original_sha,
            "size_bytes": receipt.size,
        }

    def service(self, db, *, media_paths=None):
        if media_paths is not None and media_paths != self.media_paths:
            _reject()
        return super().service(db, media_paths=self.media_paths)


async def prepare(db, receipt, *, guard) -> Delivery:
    if not callable(guard):
        _reject("export_media_callbacks_required", 422)
    data = receipt.input_snapshot
    objects = bundles.source_objects(receipt, logical_sources(data))
    owner = physical.resolve(receipt)
    delivery = Delivery(receipt)
    tasks = {task.id for task in delivery.tasks}
    seen, sources, references, relatives = set(), {}, {}, {}
    for binding in data["media"]:
        try:
            identifier = uuid.UUID(binding["task_id"])
            key, source = binding["source_key"], binding["source"]
        except (KeyError, ValueError, TypeError, AttributeError):
            _reject("image_export_delivery_scope_unknown", 422)
        if (
            identifier not in tasks
            or identifier in seen
            or binding.get("role") != "image"
            or binding.get("link_id") is not None
            or source.get("media_type") != "image"
            or key not in objects
        ):
            _reject("image_export_delivery_scope_unknown", 422)
        seen.add(identifier)
        previous = sources.setdefault(key, source)
        if canonical_identity(previous) != canonical_identity(source):
            _reject()
        owner.contains(objects[key])
        relatives[key] = objects[key][len(owner.prefix) :]
        references.setdefault(key, []).append(binding)
    if set(sources) != set(objects):
        _reject()
    for key, bindings in references.items():

        async def object_guard(owned, bindings=tuple(bindings)):
            await guard(owned)
            # The transfer independently checks the first binding too. All
            # duplicate uses must remain authorized before and after real I/O.
            for binding in bindings:
                await image_inputs.check_source(owned, binding)

        copied = await transfer.copy_source(
            db,
            receipt,
            binding=bindings[0],
            relative=relatives[key],
            resolve_source=image_inputs.check_source,
            guard=object_guard,
        )
        source = sources[key]
        if (
            copied.original_sha != source["sha256"]
            or type(copied.size) is not int
            or copied.size != source["size_bytes"]
            or not 0 < copied.size <= MAX_IMAGE_BYTES
        ):
            _reject()
        for binding in bindings:
            delivery._receipts[uuid.UUID(binding["task_id"])] = copied
    return delivery
