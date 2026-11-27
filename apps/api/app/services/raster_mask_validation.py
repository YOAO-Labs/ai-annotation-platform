"""Validate task-bound mask receipts and link verified content in labeling writes."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yoao_labeling.mask_edit_contract import (
    MASK_EDIT_PROTOCOL,
    MAX_MASK_UPLOAD_KEYS_PER_COMMAND,
    ClaimRasterMaskUploads,
    ReadMaskUploadKeys,
)

from app.db.models.task import Task
from app.services.content_snapshot import ContentError
from app.services.labeling_core_read_relations import (
    get_core_row,
    lock_task_for_core_read,
    task_row,
)
from app.services.labeling_mask_edit import mask_edit_command
from app.services.raster_mask_objects import bind_publications
from app.services.raster_mask_storage import (
    RasterMaskContractError,
    _VerifiedMaskContent,
    _mask_content_signature,
    _sync_cached_upload_fields,
    assert_raster_mask_write_enabled,
    collect_mask_geometries,
    collect_mask_references,
    lock_raster_mask_references,
    validate_portable_masks,
    validate_task_mask_dimensions,
    verify_mask_payload_content,
)
from app.services.project_access import get_workspace_project
from app.services.video_context import VideoContext


async def preflight_mask_payload_for_write(
    db: AsyncSession,
    task: Task | None,
    value: Any,
    *,
    reference_value: Any | None = None,
    portable_mask_meta: dict[str, Any] | None = None,
    native_original_receipt=None,
    repair_original_receipt=None,
    video_result_receipt=None,
    video_acceptance_receipt=None,
) -> _VerifiedMaskContent:
    """Check current Task eligibility, then verify bytes without row locks."""
    geometries = collect_mask_geometries(value)
    if not geometries:
        if portable_mask_meta is not None:
            return await verify_mask_payload_content(
                value,
                db=db,
                reference_value=reference_value,
                portable_mask_meta=portable_mask_meta,
            )
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
    await _validate_mask_payload_for_task(
        db,
        current_task,
        geometries,
        native_original_receipt=native_original_receipt,
        repair_original_receipt=repair_original_receipt,
        video_result_receipt=video_result_receipt,
        video_acceptance_receipt=video_acceptance_receipt,
    )
    return await verify_mask_payload_content(
        value,
        db=db,
        reference_value=reference_value,
        portable_mask_meta=portable_mask_meta,
    )


async def _validate_mask_payload_for_task(
    db: AsyncSession,
    task: Task,
    geometries: list[dict[str, Any]],
    *,
    native_original_receipt=None,
    repair_original_receipt=None,
    video_result_receipt=None,
    video_acceptance_receipt=None,
) -> None:
    if any(geometry.get("type") == "raster_mask" for geometry in geometries):
        project = await get_workspace_project(
            db, project_id=task.project_id, populate_existing=True
        )
        assert_raster_mask_write_enabled(project)

    for geometry in geometries:
        try:
            await validate_mask_geometry_for_task(
                db,
                task,
                geometry,
                native_original_receipt=native_original_receipt,
                repair_original_receipt=repair_original_receipt,
                video_result_receipt=video_result_receipt,
                video_acceptance_receipt=video_acceptance_receipt,
            )
        except (RasterMaskContractError, ContentError):
            raise
        except ValueError as exc:
            raise RasterMaskContractError(
                status_code=422,
                reason="mask_geometry_invalid",
                message=f"mask geometry is invalid: {exc}",
            ) from exc


async def validate_mask_geometry_for_task(
    db: AsyncSession,
    task: Task,
    geometry: dict[str, Any],
    *,
    video_context: VideoContext | None = None,
    native_original_receipt=None,
    repair_original_receipt=None,
    repair_read_receipt=None,
    video_read_receipt=None,
    video_result_receipt=None,
    video_acceptance_receipt=None,
) -> None:
    """验证掩码几何是否与任务数据集项匹配。

    支持 video_mask / video_track_mask（视频）和 raster_mask（图片）。
    """
    geom_type = geometry.get("type")

    if video_read_receipt is not None:
        if (
            any(
                value is not None
                for value in (
                    native_original_receipt,
                    repair_original_receipt,
                    repair_read_receipt,
                    video_result_receipt,
                    video_acceptance_receipt,
                )
            )
            or geom_type != "video_track_mask"
        ):
            raise ContentError("prediction_video_read_receipt_invalid")
        from app.services.video_prediction_reads import validate_geometry_receipt

        await validate_geometry_receipt(db, task, video_read_receipt, geometry)
        return

    if video_result_receipt is not None or video_acceptance_receipt is not None:
        if (
            (video_result_receipt is not None and video_acceptance_receipt is not None)
            or any(
                value is not None
                for value in (
                    native_original_receipt,
                    repair_original_receipt,
                    repair_read_receipt,
                )
            )
            or geom_type not in {"video_mask", "video_track_mask"}
        ):
            raise ContentError("prediction_video_receipt_invalid")
        if video_result_receipt is not None:
            from app.services.video_prediction_references import (
                validate_result_geometry,
            )

            await validate_result_geometry(db, task, video_result_receipt, geometry)
        else:
            from app.services.video_prediction_acceptance import (
                validate_geometry_receipt,
            )

            await validate_geometry_receipt(
                db, task, video_acceptance_receipt, geometry
            )
        return

    if repair_original_receipt is not None or repair_read_receipt is not None:
        from app.services.managed_mask_repair import validate_geometry_receipt

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

    await validate_task_mask_dimensions(db, task, geometry, video_context=video_context)


async def prepare_mask_payload_for_write(
    db: AsyncSession,
    task: Task | None,
    value: Any,
    *,
    reference_value: Any | None = None,
    required_upload_keys: set[str] | None = None,
    verified_content: _VerifiedMaskContent | None = None,
    require_verified_content: bool = False,
    portable_mask_meta: dict[str, Any] | None = None,
    native_original_receipt=None,
    repair_original_receipt=None,
    video_result_receipt=None,
    video_acceptance_receipt=None,
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
            validate_portable_masks(value, portable_mask_meta)
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
        db,
        task,
        value,
        reference_value=references_to_commit,
        portable_mask_meta=portable_mask_meta,
        native_original_receipt=native_original_receipt,
        repair_original_receipt=repair_original_receipt,
        video_result_receipt=video_result_receipt,
        video_acceptance_receipt=video_acceptance_receipt,
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

    await _validate_mask_payload_for_task(
        db,
        task,
        geometries,
        native_original_receipt=native_original_receipt,
        repair_original_receipt=repair_original_receipt,
        video_result_receipt=video_result_receipt,
        video_acceptance_receipt=video_acceptance_receipt,
    )

    foreground_keys = {
        str(reference["object_key"])
        for geometry in geometries
        if geometry.get("type") == "raster_mask"
        for reference in collect_mask_references(geometry)
    }
    try:
        if portable_mask_meta is not None:
            validate_portable_masks(value, portable_mask_meta)
        committed_keys = await lock_raster_mask_references(
            db,
            references_to_commit,
            verify=False,
            foreground_object_keys=foreground_keys,
            content_epochs=dict(verified_content.content_epochs),
        )
        if (
            portable_mask_meta is not None
            or video_result_receipt is not None
            or video_acceptance_receipt is not None
        ):
            await bind_publications(db, references_to_commit)
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
    native_original_receipt=None,
    repair_original_receipt=None,
) -> None:
    """Backward-compatible direct-geometry wrapper for annotation writes."""
    await prepare_mask_payload_for_write(
        db,
        task,
        geometry,
        verified_content=verified_content,
        require_verified_content=require_verified_content,
        native_original_receipt=native_original_receipt,
        repair_original_receipt=repair_original_receipt,
    )
