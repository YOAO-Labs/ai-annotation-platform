from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from aap_labeling.mask_edit_contract import (
    MASK_EDIT_PROTOCOL,
    MAX_MASK_UPLOAD_KEYS_PER_COMMAND,
    ClaimRasterMaskUploads,
    CountUnlinkedMaskUploads,
    LockRasterMaskUploadTask,
    ReadMaskUploadKeys,
    ReserveRasterMaskUpload,
)

from aap_assets_mask_content.contract import (
    GetRequest,
    MaskContentFailure,
    MaskReference,
    PutRequest,
    RleContent,
)

from app.db.models.dataset import DatasetItem
from app.services.project_access import get_workspace_project
from app.db.models.project import Project
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.db.models.task import Task
from app.observability.metrics import record_raster_mask_content_operation
from app.services.raster_mask_capabilities import evaluate_raster_mask_capabilities
from app.services.raster_mask_contract import (
    RasterMaskContractError as RasterMaskContractError,
)
from app.services.mask_content import mask_content_owner
from app.services.labeling_core_read_relations import (
    get_core_row,
    lock_task_for_core_read,
    task_row,
)
from app.services.labeling_mask_edit import mask_edit_command
from app.services.mask_gc import (
    begin_mask_content_write,
    lock_mask_write_epochs,
    read_mask_write_epochs,
)
from app.services.storage import StorageService, storage_service
from app.services.video_context import VideoContext
from app.utils.raster_mask_gzip import (
    MAX_COMPRESSED_BYTES,
    MAX_EXPANSION_RATIO,
    compress_mask_gzip,
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
_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _VerifiedMaskContent:
    reference_signature: tuple[tuple[str, str], ...]
    foreground_keys: tuple[str, ...]
    rle_cache: tuple[tuple[str, dict[str, Any]], ...] = ()
    content_epochs: tuple[tuple[str, int], ...] = ()


def _unique_mask_references(value: Any) -> list[dict[str, Any]]:
    unique = {
        (
            str(reference["object_key"]),
            json.dumps(reference, sort_keys=True, separators=(",", ":")),
        ): reference
        for reference in collect_mask_references(value)
    }
    return [unique[key] for key in sorted(unique)]


def _mask_content_signature(
    value: Any, *, reference_value: Any | None = None
) -> _VerifiedMaskContent:
    foreground_keys = tuple(
        sorted(
            str(reference["object_key"])
            for geometry in collect_mask_geometries(value)
            if geometry.get("type") == "raster_mask"
            for reference in collect_mask_references(geometry)
        )
    )
    references = _unique_mask_references(
        value if reference_value is None else reference_value
    )
    signature = tuple(
        sorted(
            (
                str(reference["object_key"]),
                json.dumps(reference, sort_keys=True, separators=(",", ":")),
            )
            for reference in references
        )
    )
    return _VerifiedMaskContent(signature, foreground_keys)


async def verify_mask_payload_content(
    value: Any, *, db: AsyncSession, reference_value: Any | None = None
) -> _VerifiedMaskContent:
    """Verify immutable RLE objects before callers acquire business-row locks."""
    try:
        verified = _mask_content_signature(value, reference_value=reference_value)
    except (KeyError, TypeError, ValueError) as exc:
        raise RasterMaskContractError(
            status_code=409,
            reason="mask_reference_invalid",
            message=f"mask object is invalid: {exc}",
        ) from exc
    foreground_keys = set(verified.foreground_keys)
    references = _unique_mask_references(
        value if reference_value is None else reference_value
    )
    rle_cache: dict[str, dict[str, Any]] = {}
    try:
        epochs = await read_mask_write_epochs(
            db, (str(reference["object_key"]) for reference in references)
        )
        for reference in references:
            key = str(reference["object_key"])
            payload = await _observe_content_operation(
                "verify",
                _verify_mask_reference_content(
                    reference,
                    key=key,
                    value=value,
                    require_raster_foreground=key in foreground_keys,
                ),
            )
            rle_cache[
                str(reference.get("object_key") or reference.get("sha256") or "")
            ] = payload
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
    return _VerifiedMaskContent(
        verified.reference_signature,
        verified.foreground_keys,
        tuple(sorted(rle_cache.items())),
        tuple(sorted(epochs.items())),
    )


async def preflight_mask_payload_for_write(
    db: AsyncSession,
    task: Task | None,
    value: Any,
    *,
    reference_value: Any | None = None,
) -> _VerifiedMaskContent:
    """Check current Task eligibility, then verify bytes without row locks."""
    geometries = collect_mask_geometries(value)
    if not geometries:
        return _VerifiedMaskContent((), ())
    if task is None:
        raise RasterMaskContractError(
            status_code=409,
            reason="mask_task_context_missing",
            message="mask persistence requires an existing task",
        )
    current_task = await get_core_row(db, task_row, task.id, populate_existing=True)
    if current_task is None:
        raise RasterMaskContractError(
            status_code=409,
            reason="mask_task_context_missing",
            message="mask persistence requires an existing task",
        )
    await _validate_mask_payload_for_task(db, current_task, geometries)
    return await verify_mask_payload_content(
        value, db=db, reference_value=reference_value
    )


async def _validate_mask_payload_for_task(
    db: AsyncSession, task: Task, geometries: list[dict[str, Any]]
) -> None:
    if any(geometry.get("type") == "raster_mask" for geometry in geometries):
        project = await get_workspace_project(
            db, project_id=task.project_id, populate_existing=True
        )
        assert_raster_mask_write_enabled(project)

    for geometry in geometries:
        try:
            await validate_mask_geometry_for_task(db, task, geometry)
        except RasterMaskContractError:
            raise
        except ValueError as exc:
            raise RasterMaskContractError(
                status_code=422,
                reason="mask_geometry_invalid",
                message=f"mask geometry is invalid: {exc}",
            ) from exc


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
        if isinstance(item, MaskContentFailure):
            return (
                "storage_unavailable"
                if item.code
                in {"storage_unavailable", "invalid_response", "permission_denied"}
                else "invalid_payload"
            )
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
) -> dict[str, Any]:
    payload = await load_coco_rle(reference)
    if require_raster_foreground and not _rle_has_foreground(payload):
        raise RasterMaskContractError(
            status_code=422,
            reason="raster_mask_empty_foreground",
            message="raster mask annotation must contain foreground pixels",
        )
    return payload


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
    content_epochs: dict[str, int] | None = None,
) -> list[str]:
    """Serialize reference commits with GC until the current DB transaction ends."""
    references = {
        str(reference["object_key"]): reference
        for reference in collect_mask_references(value)
    }
    keys = sorted(references)
    for key in keys:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"aap:raster-mask:{key}"},
        )
    if keys:
        if content_epochs is None:
            await begin_mask_content_write(db, keys)
        else:
            if any(key not in content_epochs for key in keys):
                raise RasterMaskContractError(
                    status_code=409,
                    reason="mask_content_epoch_changed",
                    message="Mask verification is missing its content epoch",
                    retryable=True,
                )
            await lock_mask_write_epochs(db, {key: content_epochs[key] for key in keys})
    for key in keys:
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
    if task_id is not None and references:
        result = await mask_edit_command(
            db,
            ClaimRasterMaskUploads(
                schema=MASK_EDIT_PROTOCOL,
                operation="claim_raster_mask_uploads",
                task_id=task_id,
                object_keys=sorted(references),
            ),
        )
        _sync_cached_upload_fields(db, result.uploads, "linked_at")
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
            stored = await store_coco_rle_gzip(rle)
        else:
            stored = await store_coco_rle(rle)
        if stored["object_key"] != reference.get("object_key"):
            raise ValueError("AAP mask object storage does not match reference")
    await lock_raster_mask_references(db, value, task_id=task_id)


async def store_generated_mask_content(db, rle: dict[str, Any], *, writer=None):
    """Renew durable producer grace before storing bytes and linking their root."""
    expected = build_rle_reference(rle)
    await lock_raster_mask_references(db, expected, verify=False)
    reference = await (writer or store_coco_rle)(rle)
    validate_reference_for_rle(reference, rle)
    if reference["object_key"] != expected["object_key"]:
        raise ValueError("generated Mask storage changed its content address")
    return reference


async def reserve_raster_mask_upload(
    db: AsyncSession,
    *,
    task_id: Any,
    object_key: str,
    limit: int,
) -> int:
    """Reserve one task-owned anonymous upload under a transaction-scoped lock."""
    await begin_mask_content_write(db, [object_key])
    # The legacy ORM SELECT acquired this transaction lock before its implicit
    # autoflush. Keep that UOW boundary around the new owner call explicitly.
    with db.no_autoflush:
        await mask_edit_command(
            db,
            LockRasterMaskUploadTask(
                schema=MASK_EDIT_PROTOCOL,
                operation="lock_raster_mask_upload_task",
                task_id=task_id,
            ),
        )
    if db.autoflush:
        await db.flush()
    upload_id = uuid.uuid4()
    result = await mask_edit_command(
        db,
        ReserveRasterMaskUpload(
            schema=MASK_EDIT_PROTOCOL,
            operation="reserve_raster_mask_upload",
            task_id=task_id,
            upload_id=upload_id,
            object_key=object_key,
            limit=limit,
        ),
    )
    if not result.reserved:
        raise ValueError(f"mask orphan quota exceeded ({result.count}/{limit})")
    if result.upload is not None and result.upload.id != upload_id:
        _sync_cached_upload_fields(db, [result.upload], "created_at")
    await db.flush()
    return result.count


async def count_unlinked_mask_uploads(db: AsyncSession, task_id: Any) -> int:
    """Return the exact count used by the task's anonymous Mask upload quota."""
    result = await mask_edit_command(
        db,
        CountUnlinkedMaskUploads(
            schema=MASK_EDIT_PROTOCOL,
            operation="count_unlinked_mask_uploads",
            task_id=task_id,
        ),
    )
    return result.count


def _sync_cached_upload_fields(db, rows, field: str) -> None:
    """Synchronize only upload entities already present in the Session identity map."""
    for row in rows:
        cached = db.identity_map.get(identity_key(RasterMaskUpload, ident=row.id))
        if cached is not None:
            set_committed_value(cached, field, getattr(row, field))


async def validate_mask_geometry_for_task(
    db: AsyncSession,
    task: Task,
    geometry: dict[str, Any],
    *,
    video_context: VideoContext | None = None,
) -> None:
    """验证掩码几何是否与任务数据集项匹配。

    支持 video_mask / video_track_mask（视频）和 raster_mask（图片）。
    """
    geom_type = geometry.get("type")

    if geom_type == "raster_mask":
        # v0.23.6 · 图片掩码验证
        if task.dataset_item_id is None:
            raise RasterMaskContractError(
                status_code=422,
                reason="raster_mask_dataset_item_required",
                message="raster mask requires a primary dataset item",
            )
        item = await db.get(DatasetItem, task.dataset_item_id, populate_existing=True)
        if item is None or item.file_type != "image":
            raise RasterMaskContractError(
                status_code=422,
                reason="raster_mask_image_required",
                message="raster mask requires an image dataset item",
            )
        width = item.width
        height = item.height
        if not width or not height:
            raise RasterMaskContractError(
                status_code=409,
                reason="raster_mask_source_dimensions_missing",
                message=(
                    "source image width / height metadata is required for raster mask"
                ),
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
        return

    if geom_type in {"video_mask", "video_track_mask"}:
        if task.dataset_item_id is None:
            raise ValueError("video mask requires a primary dataset item")
        if video_context is None:
            from app.services.assets_media_summary import AssetsTaskMediaSummaryReader

            summary = await AssetsTaskMediaSummaryReader(db).current_video_summary(
                task.dataset_item_id
            )
            if summary is None:
                raise ValueError("video mask requires a video dataset item")
            width, height, video = summary
            frame_count = video.get("frame_count")
        else:
            if (
                video_context.task_id != task.id
                or video_context.dataset_item_id != task.dataset_item_id
            ):
                raise ValueError("video context task binding mismatch")
            width = video_context.source_width or video_context.metadata.width
            height = video_context.source_height or video_context.metadata.height
            frame_count = video_context.metadata.frame_count
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
    verified_content: _VerifiedMaskContent | None = None,
    require_verified_content: bool = False,
) -> None:
    """Gate, validate and claim every mask in a nested persistence payload.

    Prediction results wrap their geometry one level below the result item while
    annotation writes pass a geometry directly.  Keeping both paths here prevents
    a new persistence entry point from linking an uploaded object before applying
    the raster-mask create gate and task-context checks.
    """
    geometries = collect_mask_geometries(value)
    if not geometries:
        return

    if task is None:
        raise RasterMaskContractError(
            status_code=409,
            reason="mask_task_context_missing",
            message="mask persistence requires an existing task",
        )

    references_to_commit = value if reference_value is None else reference_value
    if require_verified_content and verified_content is None:
        raise RasterMaskContractError(
            status_code=409,
            reason="mask_reference_invalid",
            message="mask content was not verified before acquiring write locks",
        )
    verified_content = verified_content or await preflight_mask_payload_for_write(
        db, task, value, reference_value=references_to_commit
    )
    try:
        final_signature = _mask_content_signature(
            value, reference_value=references_to_commit
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RasterMaskContractError(
            status_code=409,
            reason="mask_reference_invalid",
            message=f"mask object is invalid: {exc}",
        ) from exc
    verified_references = set(verified_content.reference_signature)
    missing_or_changed = [
        reference_pair
        for reference_pair in final_signature.reference_signature
        if reference_pair not in verified_references
    ]
    committed_foreground_keys = set(final_signature.foreground_keys) & {
        key for key, _reference in final_signature.reference_signature
    }
    if missing_or_changed or not committed_foreground_keys.issubset(
        set(verified_content.foreground_keys)
    ):
        raise RasterMaskContractError(
            status_code=409,
            reason="mask_reference_invalid",
            message="mask content verification does not match the write payload",
        )

    # Verify bytes before taking a Task lock; only SQL reference/upload locks are
    # acquired below. Migrated callers pass a preflight receipt from before their
    # own business locks, while simple callers use the preflight above.
    task = await lock_task_for_core_read(
        db,
        task.id,
        lock_mode="update",
        nowait=False,
        populate_existing=True,
    )
    if task is None:
        raise RasterMaskContractError(
            status_code=409,
            reason="mask_task_context_missing",
            message="mask persistence requires an existing task",
        )

    # Repeat Task/image/dimension eligibility under the final Task lock.
    await _validate_mask_payload_for_task(db, task, geometries)

    # Verify all references in one globally sorted advisory-lock pass.  Atomic
    # video mutations may pass only their current-frame references: unchanged
    # historical keyframes are already linked and were compared byte-for-byte by
    # the mutation contract, so re-reading thousands of old objects is wasteful.
    foreground_keys = {
        str(reference["object_key"])
        for geometry in geometries
        if geometry.get("type") == "raster_mask"
        for reference in collect_mask_references(geometry)
    }
    try:
        committed_keys = await lock_raster_mask_references(
            db,
            references_to_commit,
            verify=False,
            foreground_object_keys=foreground_keys,
            content_epochs=dict(verified_content.content_epochs),
        )
        required = sorted(required_upload_keys or set())
        if required:
            owned: set[str] = set()
            for offset in range(0, len(required), MAX_MASK_UPLOAD_KEYS_PER_COMMAND):
                result = await mask_edit_command(
                    db,
                    ReadMaskUploadKeys(
                        schema=MASK_EDIT_PROTOCOL,
                        operation="read_reserved_mask_uploads",
                        task_id=task.id,
                        object_keys=required[
                            offset : offset + MAX_MASK_UPLOAD_KEYS_PER_COMMAND
                        ],
                        lock=True,
                    ),
                )
                owned.update(result.object_keys)
            missing = [key for key in required if key not in owned]
            if missing:
                raise RasterMaskContractError(
                    status_code=422,
                    reason="mask_reference_not_reserved",
                    message="mask reference was not reserved by this task",
                    object_keys=missing,
                )
        if committed_keys:
            result = await mask_edit_command(
                db,
                ClaimRasterMaskUploads(
                    schema=MASK_EDIT_PROTOCOL,
                    operation="claim_raster_mask_uploads",
                    task_id=task.id,
                    object_keys=committed_keys,
                ),
            )
            _sync_cached_upload_fields(db, result.uploads, "linked_at")
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
    verified_content: _VerifiedMaskContent | None = None,
    require_verified_content: bool = False,
) -> None:
    """Backward-compatible direct-geometry wrapper for annotation writes."""
    await prepare_mask_payload_for_write(
        db,
        task,
        geometry,
        verified_content=verified_content,
        require_verified_content=require_verified_content,
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


def _mask_content_failure(exc: MaskContentFailure) -> None:
    if exc.code == "object_missing":
        raise ValueError("stored mask RLE object is missing") from None
    if exc.code == "object_corrupt":
        raise ValueError(
            "stored mask RLE digest mismatch or invalid metadata"
        ) from None
    if exc.code in {"invalid_request", "invalid_reference", "resource_limit"}:
        raise ValueError("mask RLE reference or bounded content is invalid") from None
    # Availability failures must not enter existing ValueError/content-conflict handlers.
    raise exc from None


def _store_mask_content(rle: dict[str, Any], storage, storage_encoding: str):
    # Normalize permitted extra producer metadata before crossing the byte contract.
    content = RleContent.model_validate(json.loads(canonical_rle_bytes(rle)))
    try:
        with mask_content_owner(storage) as owner:
            result = owner.put(
                PutRequest(content=content, storage_encoding=storage_encoding)
            )
    except MaskContentFailure as exc:
        _mask_content_failure(exc)
    reference = result.reference.model_dump(mode="json", exclude_none=True)
    validate_reference_for_rle(reference, rle)
    if (
        storage_encoding == RLE_STORAGE_IDENTITY
        and reference.get("storage_encoding") == RLE_STORAGE_GZIP
    ):
        raise ValueError("mask content owner changed requested storage encoding")
    return reference


def store_coco_rle_sync(
    rle: dict[str, Any], *, storage: StorageService = storage_service
) -> dict[str, Any]:
    return _store_mask_content(rle, storage, RLE_STORAGE_IDENTITY)


async def store_coco_rle(
    rle: dict[str, Any], *, storage: StorageService = storage_service
) -> dict[str, Any]:
    """Async wrapper of :func:`store_coco_rle_sync` (boto3 I/O in ``to_thread``)."""
    return await _observe_content_operation(
        "store",
        asyncio.to_thread(store_coco_rle_sync, rle, storage=storage),
    )


def store_coco_rle_gzip_sync(
    rle: dict[str, Any], *, storage: StorageService = storage_service
) -> dict[str, Any]:
    return _store_mask_content(rle, storage, RLE_STORAGE_GZIP)


async def store_coco_rle_gzip(
    rle: dict[str, Any], *, storage: StorageService = storage_service
) -> dict[str, Any]:
    """Async gzip storage entry (boto3 I/O in ``to_thread``).

    Stores the ``.json.gz`` body and returns a reference with
    ``encoding == "coco_rle_gzip"``; ``object_key`` / ``sha256`` / ``runs`` /
    ``bytes`` / ``size`` are schema-identical to the uncompressed
    ``coco_rle_ref`` (``bytes`` = uncompressed canonical length).
    """
    return await _observe_content_operation(
        "store",
        asyncio.to_thread(store_coco_rle_gzip_sync, rle, storage=storage),
    )


def load_coco_rle_sync(
    reference: dict[str, Any], *, storage: StorageService = storage_service
) -> dict[str, Any]:
    try:
        request = GetRequest(reference=MaskReference.model_validate(reference))
    except ValidationError:
        raise ValueError("invalid mask reference") from None
    try:
        with mask_content_owner(storage) as owner:
            result = owner.get(request)
    except MaskContentFailure as exc:
        _mask_content_failure(exc)
    if result.reference != request.reference:
        raise ValueError("mask content owner changed requested reference")
    content = result.content.model_dump(mode="json")
    validate_reference_for_rle(reference, content)
    return content


async def load_coco_rle(
    reference: dict[str, Any], *, storage: StorageService = storage_service
) -> dict[str, Any]:
    """Async wrapper of :func:`load_coco_rle_sync` (boto3 I/O in ``to_thread``)."""
    return await _observe_content_operation(
        "load",
        asyncio.to_thread(load_coco_rle_sync, reference, storage=storage),
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
