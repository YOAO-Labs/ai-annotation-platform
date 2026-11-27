from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, NoReturn, TypeVar

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from yoao_labeling.mask_edit_contract import (
    MASK_EDIT_PROTOCOL,
    ClaimRasterMaskUploads,
    CountUnlinkedMaskUploads,
    LockRasterMaskUploadTask,
    ReserveRasterMaskUpload,
)

from yoao_assets_mask_content.contract import (
    GetRequest,
    MaskContentFailure,
    MaskReference,
    PutRequest,
    RleContent,
)

from app.db.models.project import Project
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.db.models.task import Task
from app.schemas._jsonb_types import RasterMaskGeometry
from app.services.content_snapshot import ContentError
from app.observability.metrics import record_raster_mask_content_operation
from app.services.raster_mask_capabilities import evaluate_raster_mask_capabilities
from app.services.raster_mask_references import (
    RLE_OBJECT_PREFIX as RLE_OBJECT_PREFIX,
    collect_mask_references as collect_mask_references,
)
from app.services.raster_mask_contract import (
    RasterMaskContractError as RasterMaskContractError,
)
from app.services.labeling_mask_edit import mask_edit_command
from app.services.mask_content import mask_content_owner, mask_content_owner_selected
from app.services.mask_gc import (
    begin_mask_content_write,
    lock_mask_write_epochs,
    read_mask_write_epochs,
)
from app.services.storage import StorageService, storage_service
from app.services.video_context import VideoContext
from app.services.storage_access import failure_unsettled, finish, mark_unsettled
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

MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_MASK_PIXELS = 16_777_216
MAX_MASK_ELEMENTS = 128_000_000
MAX_INSTANCES = 300


MAX_RLE_OBJECT_BYTES = 4 * 1024 * 1024

COCO_RLE_REF_ENCODING = "coco_rle_ref"
COCO_RLE_GZIP_ENCODING = "coco_rle_gzip"
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
    value: Any,
    *,
    db: AsyncSession,
    reference_value: Any | None = None,
    portable_mask_meta: dict[str, Any] | None = None,
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
        if portable_mask_meta is not None:
            resolved = validate_portable_masks(value, portable_mask_meta)
            await _store_resolved_mask_objects(db, resolved)
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
            raise ValueError(f"mask_objects missing sha256 {digest}")
        validate_reference_for_rle(reference, rle)
        resolved.setdefault(str(reference["object_key"]), (reference, rle))
    return list(resolved.values())


def validate_portable_masks(result, meta):
    """Validate a complete, possibly enriched payload without remapping classes.

    Transport validates the original model response once. Pipeline enrichment can
    add vector shapes or remove instances, so persistence must validate the final
    object envelope without treating its positions as native instance indices.
    """
    objects = meta.get("mask_objects")
    geometries = collect_mask_geometries(result)
    if (
        not isinstance(objects, dict)
        or len(geometries) > MAX_INSTANCES
        or len(objects) > len(geometries)
        or any(geometry.get("type") != "raster_mask" for geometry in geometries)
    ):
        raise ValueError("Invalid portable image mask envelope")
    referenced = set()
    elements = 0
    for geometry in geometries:
        mask = RasterMaskGeometry.model_validate(geometry).mask
        height, width = mask.size
        pixels = height * width
        if pixels > MAX_MASK_PIXELS:
            raise ValueError("Segmentation dimensions or instance budget exceeded")
        elements += pixels
        referenced.add(mask.sha256)
    if elements > MAX_MASK_ELEMENTS:
        raise ValueError("Segmentation dimensions or instance budget exceeded")
    if set(objects) != referenced:
        raise ValueError("Missing or unreferenced segmentation mask objects")
    if (
        len(json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode())
        > MAX_METADATA_BYTES
    ):
        raise ValueError("Segmentation metadata exceeds the byte budget")
    resolved = resolve_mask_reference_objects(result, objects)
    if any(coco_rle_area(rle) <= 0 for _, rle in resolved):
        raise ValueError("Segmentation instances must have foreground pixels")
    return resolved


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
    if verify and references:
        from app.services.content_snapshot import ContentError
        from app.services.raster_mask_objects import bind_publications
        from app.services.raster_object_storage import RasterObjectStorageError

        try:
            await bind_publications(db, value)
        except (RasterObjectStorageError, ContentError) as exc:
            raise raster_mask_storage_contract_error(exc) from exc
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


async def _store_resolved_mask_objects(db, resolved) -> None:
    """Publish owned bytes without acquiring Labeling's reference locks."""
    for reference, rle in resolved:
        if str(reference.get("object_key") or "").endswith(".json.gz"):
            stored = await store_coco_rle_gzip(rle, db=db)
        else:
            stored = await store_coco_rle(rle, db=db)
        if stored["object_key"] != reference.get("object_key"):
            raise ValueError("stored mask object does not match reference")


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
    await _store_resolved_mask_objects(db, resolved)
    await lock_raster_mask_references(db, value, task_id=task_id)


async def store_generated_mask_content(db, rle: dict[str, Any], *, writer=None):
    """Renew durable producer grace before storing bytes and linking their root."""
    expected = build_rle_reference(rle)
    await lock_raster_mask_references(db, expected, verify=False)
    reference = await (writer or store_coco_rle)(rle, db=db)
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


async def validate_task_mask_dimensions(
    db: AsyncSession,
    task: Task,
    geometry: dict[str, Any],
    *,
    video_context: VideoContext | None = None,
) -> None:
    """Check image or video dimensions recorded on the task's primary item."""
    geom_type = geometry.get("type")

    if geom_type == "raster_mask":
        if task.dataset_item_id is None:
            raise RasterMaskContractError(
                status_code=422,
                reason="raster_mask_dataset_item_required",
                message="raster mask requires a primary dataset item",
            )
        from app.services.assets_media_summary import AssetsTaskMediaSummaryReader

        summary = await AssetsTaskMediaSummaryReader(db).current_image_summary(
            task.dataset_item_id
        )
        if summary is None:
            raise RasterMaskContractError(
                status_code=422,
                reason="raster_mask_image_required",
                message="raster mask requires an image dataset item",
            )
        width, height = summary
        validate_image_mask_dimensions(geometry, width, height)
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

    return


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
    """ADR-0052 §D6 · canonical bytes + gzip wrapper.

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
        raise ValueError("mask RLE gzip payload exceeds MAX_COMPRESSED_BYTES")
    return data, gzip_bytes


def rle_object_key(digest: str) -> str:
    return f"{RLE_OBJECT_PREFIX}/{digest[:2]}/{digest[2:4]}/{digest}.json"


def rle_gzip_object_key(digest: str) -> str:
    """ADR-0052 §D6 · ``.json.gz`` content-addressed key (same digest)."""
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
    """ADR-0052 §D6 · gzip variant of :func:`build_rle_reference`.

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


def _mask_content_failure(exc: MaskContentFailure) -> NoReturn:
    """Map closed owner codes onto the API's existing Mask content contract."""
    if exc.code == "object_missing":
        raise ValueError("stored mask RLE object is missing") from None
    if exc.code == "object_corrupt":
        raise ValueError(
            "stored mask RLE digest mismatch or invalid metadata"
        ) from None
    if exc.code in {"invalid_request", "invalid_reference", "resource_limit"}:
        raise ValueError("mask RLE reference or bounded content is invalid") from None
    raise exc from None


def _store_mask_content(
    rle: dict[str, Any], storage: StorageService, storage_encoding: str
) -> dict[str, Any]:
    """Write one canonical object through the explicitly selected byte owner."""
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


def _load_mask_content(
    reference: dict[str, Any], storage: StorageService
) -> dict[str, Any]:
    """Read one portable reference only from the explicitly selected owner."""
    try:
        request = GetRequest(reference=MaskReference.model_validate(reference))
    except ValidationError:
        raise ValueError("invalid mask reference") from None
    try:
        with mask_content_owner(storage) as owner:
            result = owner.get(request)
    except MaskContentFailure as exc:
        _mask_content_failure(exc)
    content = result.content.model_dump(mode="json")
    validate_reference_for_rle(reference, content)
    return content


async def _run_content_io(call, *args, **kwargs):
    """A cancelled await must not release its caller's locks before thread exit."""
    task = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as interrupted:
        try:
            await finish(task)
        except BaseException as late:
            combined = BaseExceptionGroup(
                "Raster content operation and cancellation failed", [interrupted, late]
            )
            if failure_unsettled(late):
                mark_unsettled(combined)
            raise combined from None
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
    failure = RasterMaskContractError(
        status_code=503 if status >= 500 else 409,
        reason=exc.code,
        message="mask storage is unavailable"
        if status >= 500
        else "mask ownership or storage admission changed; retry the operation",
        retryable=True,
    )
    if getattr(exc, "storage_operation_unsettled", False):
        mark_unsettled(failure)
    return failure


async def _store_owned(rle, *, compressed=False, db=None, storage=storage_service):
    if mask_content_owner_selected():
        return await _run_content_io(
            _store_mask_content,
            rle,
            storage,
            RLE_STORAGE_GZIP if compressed else RLE_STORAGE_IDENTITY,
        )
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
    """Read through the selected owner; unselected mode uses saved ownership."""
    if mask_content_owner_selected():
        return await _observe_content_operation(
            "load",
            _run_content_io(_load_mask_content, reference, storage),
        )
    from app.services import raster_mask_objects

    return await _observe_content_operation(
        "load",
        raster_mask_objects.load(reference, db=db, storage=storage),
    )


def validate_reference_for_rle(reference: dict[str, Any], rle: dict[str, Any]) -> None:
    """Validate portable mask object metadata independent of storage encoding."""
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
        raise ValueError(f"mask object metadata mismatch for sha256 {digest}")
