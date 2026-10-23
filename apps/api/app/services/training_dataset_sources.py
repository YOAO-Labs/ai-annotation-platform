"""Adapt verified asset reads to the existing immutable training snapshot."""

import uuid

from fastapi import HTTPException
from training_contract.dataset import DatasetContractError
from training_contract.lineage import SOURCE_LINEAGE_CONTRACT, validate_source_lineage

from app.services.content_snapshot import ContentError
from app.services.dataset_references import reference_context, reference_snapshot
from app.services.reference_keys import parse_reference
from app.services.training_dataset_bridge import TrainingDatasetError


async def capture_source_lineage(db, context):
    """Freeze only persisted, safe facts; physical locators never cross this boundary."""
    from app.db.models.dataset_source import DatasetScanRun, DatasetSourceRevision

    scan = await db.get(DatasetScanRun, context["run_id"], populate_existing=True)
    accepted = (
        await db.get(
            DatasetSourceRevision,
            context["accepted_revision_id"],
            populate_existing=True,
        )
        if context["accepted_revision_id"]
        else None
    )

    def timestamp(value):
        return value.isoformat() if value is not None else None

    return {
        "source_id": str(context["source_id"]),
        "location_id": str(context["location_id"]),
        "path_digest": context["path_digest"],
        "scan_run_id": str(context["run_id"]),
        "accepted_revision_id": str(accepted.id) if accepted else None,
        "revision_number": accepted.revision_number if accepted else None,
        "previous_revision_id": str(accepted.previous_revision_id)
        if accepted and accepted.previous_revision_id
        else None,
        "accepted_at": timestamp(accepted.accepted_at) if accepted else None,
        "scan_completed_at": timestamp(scan.completed_at) if scan else None,
        "content_verified_at": context["content_receipt"].get("verified_at"),
        "source_config_revision": context["source_revision"],
        "location_config_revision": context["location_revision"],
    }


async def training_reference_snapshot(
    db, sample, actor, *, api_key_id=None, node_id=None, freeze_lineage=False
):
    """Resolve current authorized content, fenced by the pinned business asset."""
    try:
        item_id, sha256 = parse_reference(sample["source_key"])
        if str(item_id) != sample["dataset_item_id"]:
            raise ContentError("content_binding_changed")
        context = await reference_context(
            db, item_id, actor, task_id=uuid.UUID(sample["task_id"]), node_id=node_id
        )
        identity = {
            "schema": 1,
            "dataset_id": sample["dataset_id"],
            "dataset_item_id": str(item_id),
            "asset_id": str(context["asset_id"]),
            "sha256": context["sha256"],
            "size_bytes": context["size"],
        }
        if sha256 != context["sha256"] or (
            sample.get("asset_reference") is not None
            and sample["asset_reference"] != identity
        ):
            raise ContentError("content_binding_changed")
        lineage = None
        if freeze_lineage:
            lineage = await capture_source_lineage(db, context)
            validate_source_lineage(
                {
                    "source_lineage_contract": SOURCE_LINEAGE_CONTRACT,
                    "samples": [
                        {"asset_reference": identity, "source_lineage": lineage}
                    ],
                }
            )
        snapshot = await reference_snapshot(
            db,
            item_id,
            actor,
            task_id=uuid.UUID(sample["task_id"]),
            expected_sha256=sha256,
            api_key_id=api_key_id,
            scope="datasets:read",
            node_id=node_id,
        )
        try:
            if (
                await reference_context(
                    db,
                    item_id,
                    actor,
                    task_id=uuid.UUID(sample["task_id"]),
                    node_id=node_id,
                )
                != context
            ):
                raise ContentError("content_binding_changed")
            return snapshot, identity, lineage
        except BaseException:
            snapshot.close()
            raise
    except ContentError as exc:
        raise TrainingDatasetError(
            exc.code,
            "引用原图不可用或内容已变化，请核对来源后重新预检",
            status_code=exc.status,
        ) from exc
    except DatasetContractError as exc:
        raise TrainingDatasetError(exc.code, str(exc)) from exc
    except HTTPException as exc:
        raise TrainingDatasetError(
            "reference_access_revoked",
            "引用原图的读取权限已失效",
            status_code=exc.status_code,
        ) from exc
    except Exception as exc:
        raise TrainingDatasetError(
            "reference_media_unavailable", "无法读取引用原图，请核对来源与节点连接"
        ) from exc
