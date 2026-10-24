from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable
from contextvars import ContextVar
from typing import Any, TypeVar

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import DatasetItem
from app.db.models.project import Project
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.db.models.task import Task
from app.services.content_snapshot import ContentError
from app.observability.metrics import record_raster_mask_content_operation
from app.services.raster_mask_capabilities import evaluate_raster_mask_capabilities
from app.services.storage import StorageService, storage_service
from app.services.storage_access import finish, mark_unsettled
from app.utils.raster_mask_gzip import (
    MAX_COMPRESSED_BYTES,
    MAX_EXPANSION_RATIO,
    compress_mask_gzip,
    decompress_mask_gzip,
)
from app.utils.raster_mask_rle import (
    MAX_IMAGE_MASK_DIMENSION,
    MAX_IMAGE_MASK_PIXELS,
    MAX_VIDEO_MASK_DIMENSION,
    MAX_VIDEO_MASK_PIXELS,
    coco_rle_area,
    validate_coco_rle,
)

MAX_RLE_OBJECT_BYTES = 4 * 1024 * 1024
RLE_OBJECT_PREFIX = "raster-masks/sha256"

# v0.23.5 · ADR-0052 §D6 · encoding markers carried inside ``coco_rle_ref``
# (additive — legacy uncompressed ``.json`` refs keep ``encoding="coco_rle_ref"``).
COCO_RLE_REF_ENCODING = "coco_rle_ref"
COCO_RLE_GZIP_ENCODING = "coco_rle_gzip"  # legacy request/reference marker
RLE_STORAGE_IDENTITY = "identity"
RLE_STORAGE_GZIP = "gzip"

_ACTIVE_CONTENT_OPERATION: ContextVar[str | None] = ContextVar(
    "raster_mask_content_operation",
    default=None,
)
_T = TypeVar("_T")


class RasterMaskContractError(ValueError):
    """Stable API-facing rejection raised at the mask persistence boundary."""

    def __init__(
        self,
        *,
        status_code: int,
        reason: str,
        message: str,
        **detail: Any,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"reason": reason, "message": message, **detail}


def classify_raster_mask_content_error(exc: Exception) -> str:
    """Map arbitrary content/storage failures onto a fixed metric vocabulary."""
    chain: list[Exception] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while isinstance(current, Exception) and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__

    for item in chain:
        code = getattr(item, "code", None)
        if isinstance(code, str) and code.startswith("raster_object_storage_"):
            return {
                "raster_object_storage_missing": "missing_object",
                "raster_object_storage_integrity_mismatch": "digest_mismatch",
                "raster_object_storage_unavailable": "storage_unavailable",
                "raster_object_storage_budget": "storage_unavailable",
            }.get(code, "invalid_payload")
        if isinstance(item, ClientError):
            code = str(item.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404", "NotFound"}:
                return "missing_object"

    messages = " ".join(str(item).lower() for item in chain)
    if "missing" in messages and ("object" in messages or "key" in messages):
        return "missing_object"
    if "digest mismatch" in messages:
        return "digest_mismatch"
    if "size mismatch" in messages:
        return "size_mismatch"
    if "run count mismatch" in messages or "runs mismatch" in messages:
        return "run_mismatch"
    if "byte count mismatch" in messages or "bytes mismatch" in messages:
        return "byte_mismatch"
    if any(
        marker in messages
        for marker in (
            "unsupported mask reference encoding",
            "unsupported mask reference storage_encoding",
            "mask reference must use .json",
        )
    ):
        return "invalid_encoding"

    for item in chain:
        if isinstance(item, RasterMaskContractError):
            reason = str(item.detail.get("reason") or "")
            if reason == "mask_storage_unavailable":
                return "storage_unavailable"
            return "invalid_payload"
        if isinstance(item, (BotoCoreError, OSError)):
            return "storage_unavailable"
        if isinstance(
            item,
            (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, ValueError),
        ):
            return "invalid_payload"
    return "unknown"


async def _observe_content_operation(
    operation: str,
    action: Awaitable[_T],
) -> _T:
    """Record one outermost operation, suppressing nested wrapper duplicates."""
    if _ACTIVE_CONTENT_OPERATION.get() is not None:
        return await action

    token = _ACTIVE_CONTENT_OPERATION.set(operation)
    try:
        result = await action
    except Exception as exc:
        record_raster_mask_content_operation(
            operation,
            "error",
            error_reason=classify_raster_mask_content_error(exc),
        )
        raise
    else:
        record_raster_mask_content_operation(operation, "success")
        return result
    finally:
        _ACTIVE_CONTENT_OPERATION.reset(token)


def assert_raster_mask_write_enabled(project: Project | None) -> None:
    capabilities = evaluate_raster_mask_capabilities(project)
    if not capabilities.write_enabled:
        raise RasterMaskContractError(
            status_code=409,
            reason="raster_mask_create_disabled",
            message="raster mask creation is disabled",
        )


def _rle_has_foreground(rle: dict[str, Any]) -> bool:
    return coco_rle_area(rle) > 0


def collect_mask_references(value: Any) -> list[dict[str, Any]]:
    """Collect every content-addressed RLE reference from a nested payload."""
    references: list[dict[str, Any]] = []
    if isinstance(value, dict):
        key = value.get("object_key")
        if isinstance(key, str) and key.startswith(f"{RLE_OBJECT_PREFIX}/"):
            references.append(value)
        for child in value.values():
            references.extend(collect_mask_references(child))
    elif isinstance(value, list):
        for child in value:
            references.extend(collect_mask_references(child))
    return references


def collect_mask_geometries(value: Any) -> list[dict[str, Any]]:
    """Collect persisted mask geometry objects from a nested write payload."""
    geometries: list[dict[str, Any]] = []
    if isinstance(value, dict):
        geometry_type = value.get("type")
        if geometry_type == "raster_mask" and (
            "mask" in value or "geometry" not in value
        ):
            geometries.append(value)
            return geometries
        if geometry_type == "video_mask" and (
            "mask" in value or "geometry" not in value
        ):
            geometries.append(value)
            return geometries
        if geometry_type == "video_track_mask" and (
            "keyframes" in value or "geometry" not in value
        ):
            geometries.append(value)
            return geometries
        for child in value.values():
            geometries.extend(collect_mask_geometries(child))
    elif isinstance(value, list):
        for child in value:
            geometries.extend(collect_mask_geometries(child))
    return geometries


async def _verify_mask_reference_content(
    reference: dict[str, Any],
    *,
    key: str,
    value: Any,
    require_raster_foreground: bool,
) -> None:
    from app.services.content_snapshot import ContentError
    from app.services.raster_object_storage import RasterObjectStorageError

    try:
        payload = await load_coco_rle(reference)
    except (RasterObjectStorageError, ContentError) as exc:
        raise raster_mask_storage_contract_error(exc) from exc
    if require_raster_foreground and not _rle_has_foreground(payload):
        raise RasterMaskContractError(
            status_code=422,
            reason="raster_mask_empty_foreground",
            message="raster mask annotation must contain foreground pixels",
        )


def resolve_mask_reference_objects(
    value: Any,
    mask_objects: dict[str, dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Resolve and validate portable RLE objects, deduplicated by object key."""
    resolved: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for reference in collect_mask_references(value):
        digest = reference.get("sha256")
        rle = mask_objects.get(str(digest))
        if rle is None:
            raise ValueError(f"AAP mask_objects missing sha256 {digest}")
        validate_reference_for_rle(reference, rle)
        resolved.setdefault(str(reference["object_key"]), (reference, rle))
    return list(resolved.values())


async def lock_raster_mask_references(
    db: AsyncSession,
    value: Any,
    *,
    verify: bool = True,
    task_id: Any | None = None,
    require_raster_foreground: bool = False,
    foreground_object_keys: set[str] | None = None,
) -> list[str]:
    """Serialize reference commits with GC until the current DB transaction ends."""
    references = {
        str(reference["object_key"]): reference
        for reference in collect_mask_references(value)
    }
    for key in sorted(references):
        acquired = (
            await db.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"aap:raster-mask:{key}"},
            )
        ).scalar_one()
        if acquired is not True:
            raise RasterMaskContractError(
                status_code=409,
                reason="mask_content_busy",
                message="mask content is being changed; retry the operation",
            )
        if verify:
            await _observe_content_operation(
                "verify",
                _verify_mask_reference_content(
                    references[key],
                    key=key,
                    value=value,
                    require_raster_foreground=(
                        require_raster_foreground
                        or key in (foreground_object_keys or set())
                    ),
                ),
            )
    if verify and references:
        from app.services.content_snapshot import ContentError
        from app.services.raster_mask_objects import bind_publications
        from app.services.raster_object_storage import RasterObjectStorageError

        try:
            await bind_publications(db, value)
        except (RasterObjectStorageError, ContentError) as exc:
            raise raster_mask_storage_contract_error(exc) from exc
    if task_id is not None and references:
        await db.execute(
            update(RasterMaskUpload)
            .where(
                RasterMaskUpload.task_id == task_id,
                RasterMaskUpload.object_key.in_(sorted(references)),
                RasterMaskUpload.linked_at.is_(None),
            )
            .values(linked_at=func.now())
        )
    return sorted(references)


async def store_mask_reference_objects(
    db: AsyncSession,
    value: Any,
    resolved: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    task_id: Any,
) -> None:
    """Store portable objects before linking their references to a DB row."""
    if not resolved:
        await lock_raster_mask_references(db, value, task_id=task_id)
        return
    await lock_raster_mask_references(db, value, verify=False)
    for reference, rle in resolved:
        if str(reference.get("object_key") or "").endswith(".json.gz"):
            stored = await store_coco_rle_gzip(rle, db=db)
        else:
            stored = await store_coco_rle(rle, db=db)
        if stored["object_key"] != reference.get("object_key"):
            raise ValueError("AAP mask object storage does not match reference")
    await lock_raster_mask_references(db, value, task_id=task_id)


async def reserve_raster_mask_upload(
    db: AsyncSession,
    *,
    task_id: Any,
    object_key: str,
    limit: int,
) -> int:
    """Reserve one task-owned anonymous upload under a transaction-scoped lock."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"aap:raster-mask-upload:{task_id}"},
    )
    existing = (
        await db.execute(
            select(RasterMaskUpload).where(
                RasterMaskUpload.task_id == task_id,
                RasterMaskUpload.object_key == object_key,
            )
        )
    ).scalar_one_or_none()
    current = int(
        (
            await db.execute(
                select(func.count(RasterMaskUpload.id)).where(
                    RasterMaskUpload.task_id == task_id,
                    RasterMaskUpload.linked_at.is_(None),
                )
            )
        ).scalar()
        or 0
    )
    if existing is not None:
        return current
    if current >= limit:
        raise ValueError(f"mask orphan quota exceeded ({current}/{limit})")
    db.add(RasterMaskUpload(task_id=task_id, object_key=object_key))
    await db.flush()
    return current + 1


def validate_image_mask_dimensions(geometry, width, height) -> None:
    """Compare a raster Mask with dimensions whose authority belongs to the caller."""
    if not width or not height:
        raise RasterMaskContractError(
            status_code=409,
            reason="raster_mask_source_dimensions_missing",
            message="source image width / height metadata is required for raster mask",
        )
    if (
        int(width) > MAX_IMAGE_MASK_DIMENSION
        or int(height) > MAX_IMAGE_MASK_DIMENSION
        or int(width) * int(height) > MAX_IMAGE_MASK_PIXELS
    ):
        raise RasterMaskContractError(
            status_code=422,
            reason="raster_mask_dimensions_exceeded",
            message=(
                "source image exceeds the raster mask limit "
                f"({MAX_IMAGE_MASK_DIMENSION}px / {MAX_IMAGE_MASK_PIXELS} pixels)"
            ),
            max_dimension=MAX_IMAGE_MASK_DIMENSION,
            max_pixels=MAX_IMAGE_MASK_PIXELS,
        )
    expected_size = [int(height), int(width)]
    mask = geometry.get("mask") or {}
    if list(mask.get("size") or []) != expected_size:
        raise RasterMaskContractError(
            status_code=422,
            reason="raster_mask_size_mismatch",
            message=f"mask size must match source image {expected_size}",
        )


async def validate_mask_geometry_for_task(
    db: AsyncSession,
    task: Task,
    geometry: dict[str, Any],
    *,
    native_original_receipt=None,
    repair_original_receipt=None,
    repair_read_receipt=None,
) -> None:
    """验证掩码几何是否与任务数据集项匹配。

    支持 video_mask / video_track_mask（视频）和 raster_mask（图片）。
    """
    geom_type = geometry.get("type")

    if repair_original_receipt is not None or repair_read_receipt is not None:
        from app.services.managed_repair_acceptance import validate_geometry_receipt

        if native_original_receipt is not None or (
            repair_original_receipt is not None and repair_read_receipt is not None
        ):
            raise ContentError("repair_original_receipt_invalid")
        await validate_geometry_receipt(
            db,
            task,
            repair_original_receipt
            if repair_original_receipt is not None
            else repair_read_receipt,
            geometry,
            write=repair_original_receipt is not None,
        )
        return

    if native_original_receipt is not None:
        from app.services.managed_native_inference import validate_native_receipt

        await validate_native_receipt(
            db,
            task.id,
            task.project_id,
            native_original_receipt,
            result=[{"geometry": geometry}],
        )
        return

    if geom_type == "raster_mask":
        # v0.23.6 · 图片掩码验证
        if task.dataset_item_id is None:
            raise RasterMaskContractError(
                status_code=422,
                reason="raster_mask_dataset_item_required",
                message="raster mask requires a primary dataset item",
            )
        item = await db.get(DatasetItem, task.dataset_item_id)
        if item is None or item.file_type != "image":
            raise RasterMaskContractError(
                status_code=422,
                reason="raster_mask_image_required",
                message="raster mask requires an image dataset item",
            )
        validate_image_mask_dimensions(geometry, item.width, item.height)
        return

    if geom_type in {"video_mask", "video_track_mask"}:
        if task.dataset_item_id is None:
            raise ValueError("video mask requires a primary dataset item")
        item = await db.get(DatasetItem, task.dataset_item_id)
        if item is None or item.file_type != "video":
            raise ValueError("video mask requires a video dataset item")
        video = (item.metadata_ or {}).get("video")
        video = video if isinstance(video, dict) else {}
        width = item.width or video.get("width")
        height = item.height or video.get("height")
        frame_count = video.get("frame_count")
        if not width or not height:
            raise ValueError(
                "source video width / height metadata is required for masks"
            )
        if (
            int(width) > MAX_VIDEO_MASK_DIMENSION
            or int(height) > MAX_VIDEO_MASK_DIMENSION
            or int(width) * int(height) > MAX_VIDEO_MASK_PIXELS
        ):
            raise RasterMaskContractError(
                status_code=422,
                reason="video_mask_dimensions_exceeded",
                message=(
                    "source video exceeds the raster mask limit "
                    f"({MAX_VIDEO_MASK_DIMENSION}px / {MAX_VIDEO_MASK_PIXELS} pixels)"
                ),
                max_dimension=MAX_VIDEO_MASK_DIMENSION,
                max_pixels=MAX_VIDEO_MASK_PIXELS,
            )
        expected_size = [int(height), int(width)]
        frames = (
            [geometry]
            if geom_type == "video_mask"
            else list(geometry.get("keyframes") or [])
        )
        for frame in frames:
            mask = frame.get("mask") or {}
            if list(mask.get("size") or []) != expected_size:
                raise ValueError(f"mask size must match source video {expected_size}")
            frame_index = frame.get("frame_index")
            if frame_count is not None and int(frame_index) >= int(frame_count):
                raise ValueError(
                    f"mask frame_index must be < source frame_count {frame_count}"
                )
        if geom_type == "video_track_mask" and frame_count is not None:
            for outside in geometry.get("outside") or []:
                if int(outside.get("to", 0)) >= int(frame_count):
                    raise ValueError(
                        f"outside range must be within source frame_count {frame_count}"
                    )
        return

    # 其他几何类型不需要验证
    return


async def prepare_mask_payload_for_write(
    db: AsyncSession,
    task: Task | None,
    value: Any,
    *,
    reference_value: Any | None = None,
    required_upload_keys: set[str] | None = None,
    portable_mask_meta: dict[str, Any] | None = None,
    native_original_receipt=None,
    repair_original_receipt=None,
) -> None:
    """Gate, validate and claim every mask in a nested persistence payload.

    Prediction results wrap their geometry one level below the result item while
    annotation writes pass a geometry directly.  Keeping both paths here prevents
    a new persistence entry point from linking an uploaded object before applying
    the raster-mask create gate and task-context checks.
    """
    geometries = collect_mask_geometries(value)
    if not geometries:
        if native_original_receipt is not None or repair_original_receipt is not None:
            raise ContentError("prediction_original_geometry_not_supported")
        if portable_mask_meta is not None:
            from app.services.controlled_segmentation import validate_portable_masks

            validate_portable_masks(value, portable_mask_meta)
        return

    if task is None:
        raise RasterMaskContractError(
            status_code=409,
            reason="mask_task_context_missing",
            message="mask persistence requires an existing task",
        )

    # Every mask persistence path takes the task row before content/upload locks.
    # Atomic mutations already own it; re-acquiring the same row is harmless and
    # keeps ordinary create/update/prediction paths on the same global lock order.
    task = (
        await db.execute(
            select(Task)
            .where(Task.id == task.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None:
        raise RasterMaskContractError(
            status_code=409,
            reason="mask_task_context_missing",
            message="mask persistence requires an existing task",
        )

    # Check every image-mask gate before object reads, advisory locks, or upload
    # association. Video mask tracks keep their existing deployment contract.
    if any(geometry.get("type") == "raster_mask" for geometry in geometries):
        project = await db.get(Project, task.project_id, populate_existing=True)
        assert_raster_mask_write_enabled(project)

    for geometry in geometries:
        try:
            await validate_mask_geometry_for_task(
                db,
                task,
                geometry,
                native_original_receipt=native_original_receipt,
                repair_original_receipt=repair_original_receipt,
            )
        except RasterMaskContractError:
            raise
        except ContentError:
            raise
        except ValueError as exc:
            raise RasterMaskContractError(
                status_code=422,
                reason="mask_geometry_invalid",
                message=f"mask geometry is invalid: {exc}",
            ) from exc

    # Verify all references in one globally sorted advisory-lock pass.  Atomic
    # video mutations may pass only their current-frame references: unchanged
    # historical keyframes are already linked and were compared byte-for-byte by
    # the mutation contract, so re-reading thousands of old objects is wasteful.
    references_to_commit = value if reference_value is None else reference_value
    foreground_keys = {
        str(reference["object_key"])
        for geometry in geometries
        if geometry.get("type") == "raster_mask"
        for reference in collect_mask_references(geometry)
    }
    try:
        if portable_mask_meta is not None:
            from app.services.controlled_segmentation import validate_portable_masks

            # Validate the entire envelope before the first PUT. Shared SHA
            # objects remain GC-owned if a later storage/DB operation fails.
            resolved = validate_portable_masks(value, portable_mask_meta)
            await store_mask_reference_objects(db, value, resolved, task_id=task.id)
        committed_keys = await lock_raster_mask_references(
            db,
            references_to_commit,
            foreground_object_keys=foreground_keys,
        )
        required = sorted(required_upload_keys or set())
        if required:
            owned = set(
                (
                    await db.execute(
                        select(RasterMaskUpload.object_key)
                        .where(
                            RasterMaskUpload.task_id == task.id,
                            RasterMaskUpload.object_key.in_(required),
                        )
                        .order_by(RasterMaskUpload.object_key)
                        .with_for_update()
                    )
                ).scalars()
            )
            missing = [key for key in required if key not in owned]
            if missing:
                raise RasterMaskContractError(
                    status_code=422,
                    reason="mask_reference_not_reserved",
                    message="mask reference was not reserved by this task",
                    object_keys=missing,
                )
        if committed_keys:
            await db.execute(
                update(RasterMaskUpload)
                .where(
                    RasterMaskUpload.task_id == task.id,
                    RasterMaskUpload.object_key.in_(committed_keys),
                    RasterMaskUpload.linked_at.is_(None),
                )
                .values(linked_at=func.now())
            )
    except RasterMaskContractError:
        raise
    except (KeyError, ValueError) as exc:
        raise RasterMaskContractError(
            status_code=409,
            reason="mask_reference_invalid",
            message=f"mask object is invalid: {exc}",
        ) from exc
    except Exception as exc:
        raise RasterMaskContractError(
            status_code=503,
            reason="mask_storage_unavailable",
            message="mask object storage is unavailable",
        ) from exc


async def prepare_mask_geometry_for_annotation_write(
    db: AsyncSession,
    task: Task | None,
    geometry: dict[str, Any],
    *,
    native_original_receipt=None,
    repair_original_receipt=None,
) -> None:
    """Backward-compatible direct-geometry wrapper for annotation writes."""
    await prepare_mask_payload_for_write(
        db,
        task,
        geometry,
        native_original_receipt=native_original_receipt,
        repair_original_receipt=repair_original_receipt,
    )


def canonical_rle_bytes(rle: dict[str, Any]) -> bytes:
    height, width, counts = validate_coco_rle(rle)
    payload = {
        "encoding": "coco_rle",
        "size": [height, width],
        "counts": counts,
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(data) > MAX_RLE_OBJECT_BYTES:
        raise ValueError("mask RLE canonical JSON must be <= 4 MiB")
    return data


def canonical_rle_bytes_gzip(rle: dict[str, Any]) -> tuple[bytes, bytes]:
    """v0.23.5 · ADR-0052 §D6 · canonical bytes + gzip wrapper.

    SHA-256 is taken over the **uncompressed canonical bytes** (``data``) so
    that the digest matches the legacy uncompressed ``.json`` path; only the
    stored bytes are gzipped (``gzip_bytes``). Returning both keeps callers
    from re-deriving either side and lets the byte-count / sha checks in
    ``load_coco_rle`` operate on identical canonical content regardless of
    transport.
    """
    data = canonical_rle_bytes(rle)
    gzip_bytes = compress_mask_gzip(data)
    if len(gzip_bytes) > MAX_COMPRESSED_BYTES:
        # Compressed bytes are capped so a decompressor never has to read more
        # than ``MAX_COMPRESSED_BYTES`` before it can reject a payload.
        raise ValueError("mask RLE gzip payload exceeds MAX_COMPRESSED_BYTES")
    return data, gzip_bytes


def rle_object_key(digest: str) -> str:
    return f"{RLE_OBJECT_PREFIX}/{digest[:2]}/{digest[2:4]}/{digest}.json"


def rle_gzip_object_key(digest: str) -> str:
    """v0.23.5 · ADR-0052 §D6 · ``.json.gz`` content-addressed key (same digest)."""
    return f"{RLE_OBJECT_PREFIX}/{digest[:2]}/{digest[2:4]}/{digest}.json.gz"


def build_rle_reference(rle: dict[str, Any]) -> dict[str, Any]:
    data = canonical_rle_bytes(rle)
    digest = hashlib.sha256(data).hexdigest()
    height, width, counts = validate_coco_rle(rle)
    return {
        "encoding": COCO_RLE_REF_ENCODING,
        "size": [height, width],
        "object_key": rle_object_key(digest),
        "sha256": digest,
        "runs": len(counts),
        "bytes": len(data),
    }


def build_rle_gzip_reference(rle: dict[str, Any]) -> dict[str, Any]:
    """v0.23.5 · ADR-0052 §D6 · gzip variant of :func:`build_rle_reference`.

    ``object_key`` ends in ``.json.gz`` and ``storage_encoding`` is ``gzip``;
    reference ``encoding`` remains ``coco_rle_ref``. ``sha256`` / ``bytes`` / ``runs`` / ``size`` are
    identical to the uncompressed reference (the digest is over the
    **uncompressed** canonical bytes, ``bytes`` is the uncompressed length).
    """
    data, gzip_bytes = canonical_rle_bytes_gzip(rle)
    if len(data) > len(gzip_bytes) * MAX_EXPANSION_RATIO:
        raise ValueError("mask RLE exceeds the bounded gzip expansion ratio")
    digest = hashlib.sha256(data).hexdigest()
    height, width, counts = validate_coco_rle(rle)
    return {
        "encoding": COCO_RLE_REF_ENCODING,
        "storage_encoding": RLE_STORAGE_GZIP,
        "size": [height, width],
        "object_key": rle_gzip_object_key(digest),
        "sha256": digest,
        "runs": len(counts),
        "bytes": len(data),
    }


async def _run_content_io(call, *args, **kwargs):
    """A cancelled await must not release its caller's locks before thread exit."""
    task = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as interrupted:
        try:
            await finish(task)
        except BaseException as late:
            if getattr(late, "storage_operation_unsettled", False):
                mark_unsettled(interrupted)
        raise


def store_coco_rle_sync(
    rle: dict[str, Any], *, storage: StorageService = storage_service
) -> dict[str, Any]:
    """Owned synchronous entry for worker threads without an active loop."""
    _require_sync_thread()
    return asyncio.run(store_coco_rle(rle, storage=storage))


def _require_sync_thread():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("Use asynchronous mask storage inside an active event loop")


def _encode_rle(rle, *, compressed=False):
    if compressed:
        canonical, encoded = canonical_rle_bytes_gzip(rle)
        if len(canonical) <= len(encoded) * MAX_EXPANSION_RATIO:
            return build_rle_gzip_reference(rle), encoded
    return build_rle_reference(rle), canonical_rle_bytes(rle)


def raster_mask_storage_contract_error(exc):
    """Keep owned storage and admission failures outside input-validation errors."""
    status = getattr(exc, "status_code", getattr(exc, "status", 409))
    return RasterMaskContractError(
        status_code=503 if status >= 500 else 409,
        reason=exc.code,
        message="mask storage is unavailable"
        if status >= 500
        else "mask ownership or storage admission changed; retry the operation",
        retryable=True,
    )


async def _store_owned(rle, *, compressed=False, db=None, storage=storage_service):
    from app.services import raster_mask_objects
    from app.services.content_snapshot import ContentError
    from app.services.raster_object_storage import RasterObjectStorageError

    reference, data = await _run_content_io(_encode_rle, rle, compressed=compressed)
    try:
        return await raster_mask_objects.store(reference, data, db=db, storage=storage)
    except (RasterObjectStorageError, ContentError) as exc:
        raise raster_mask_storage_contract_error(exc) from exc


async def store_coco_rle(
    rle: dict[str, Any], *, storage: StorageService = storage_service, db=None
) -> dict[str, Any]:
    """Create or reuse independently owned content, preserving caller publication."""
    return await _observe_content_operation(
        "store",
        _store_owned(rle, db=db, storage=storage),
    )


def store_coco_rle_gzip_sync(
    rle: dict[str, Any], *, storage: StorageService = storage_service
) -> dict[str, Any]:
    """Owned gzip entry for worker threads without an active loop."""
    _require_sync_thread()
    return asyncio.run(store_coco_rle_gzip(rle, storage=storage))


async def store_coco_rle_gzip(
    rle: dict[str, Any], *, storage: StorageService = storage_service, db=None
) -> dict[str, Any]:
    """Preserve canonical identity while independently owning compressed bytes."""
    return await _observe_content_operation(
        "store",
        _store_owned(rle, compressed=True, db=db, storage=storage),
    )


def load_coco_rle_sync(
    reference: dict[str, Any], *, storage: StorageService = storage_service
) -> dict[str, Any]:
    """Owned read for training/evaluation threads; never guess a current bucket."""
    _require_sync_thread()
    return asyncio.run(load_coco_rle(reference, storage=storage))


def decode_rle_bytes(reference: dict[str, Any], raw: bytes) -> dict[str, Any]:
    """Validate portable canonical content independently of its physical locator."""
    key = str(reference["object_key"])
    storage_encoding = reference.get("storage_encoding")
    gzip_key = key.endswith(".json.gz")
    if storage_encoding not in (None, RLE_STORAGE_IDENTITY, RLE_STORAGE_GZIP):
        raise ValueError("unsupported mask reference storage_encoding")
    if storage_encoding == RLE_STORAGE_GZIP and not gzip_key:
        raise ValueError("gzip mask reference must use .json.gz")
    if storage_encoding == RLE_STORAGE_IDENTITY and gzip_key:
        raise ValueError("identity mask reference must use .json")
    if reference.get("encoding") not in (
        COCO_RLE_REF_ENCODING,
        COCO_RLE_GZIP_ENCODING,
    ):
        raise ValueError("unsupported mask reference encoding")
    if gzip_key:
        data = decompress_mask_gzip(
            raw,
            max_compressed=MAX_COMPRESSED_BYTES,
            max_uncompressed=MAX_RLE_OBJECT_BYTES,
        )
    else:
        if len(raw) > MAX_RLE_OBJECT_BYTES:
            raise ValueError("stored mask RLE exceeds 4 MiB")
        data = raw
    if len(data) > MAX_RLE_OBJECT_BYTES:
        raise ValueError("stored mask RLE exceeds 4 MiB")
    digest = hashlib.sha256(data).hexdigest()
    expected_key = (
        rle_gzip_object_key(digest)
        if key.endswith(".json.gz")
        else rle_object_key(digest)
    )
    if digest != reference.get("sha256") or key != expected_key:
        raise ValueError("stored mask RLE digest mismatch")
    if len(data) != reference.get("bytes"):
        raise ValueError("stored mask RLE byte count mismatch")
    payload = json.loads(data)
    height, width, counts = validate_coco_rle(payload)
    if [height, width] != list(reference.get("size") or []):
        raise ValueError("stored mask RLE size mismatch")
    if len(counts) != reference.get("runs"):
        raise ValueError("stored mask RLE run count mismatch")
    return payload


async def load_coco_rle(
    reference: dict[str, Any], *, storage: StorageService = storage_service, db=None
) -> dict[str, Any]:
    """Resolve a unique ready owner before reading and validating saved bytes."""
    from app.services import raster_mask_objects

    return await _observe_content_operation(
        "load",
        raster_mask_objects.load(reference, db=db, storage=storage),
    )


def validate_reference_for_rle(reference: dict[str, Any], rle: dict[str, Any]) -> None:
    """Validate portable AAP object metadata independent of storage encoding."""
    data = canonical_rle_bytes(rle)
    digest = hashlib.sha256(data).hexdigest()
    height, width, counts = validate_coco_rle(rle)
    key = str(reference.get("object_key") or "")
    expected = (
        rle_gzip_object_key(digest)
        if key.endswith(".json.gz")
        else rle_object_key(digest)
    )
    if (
        reference.get("encoding") not in (COCO_RLE_REF_ENCODING, COCO_RLE_GZIP_ENCODING)
        or key != expected
        or reference.get("sha256") != digest
        or list(reference.get("size") or []) != [height, width]
        or reference.get("runs") != len(counts)
        or reference.get("bytes") != len(data)
    ):
        raise ValueError(f"AAP mask object metadata mismatch for sha256 {digest}")
