"""Saved function-only proof for Mask edit, revision, and AI accept owners."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from sqlalchemy import delete, select

from aap_labeling.ai_mask_accept_contract import (
    AI_MASK_ACCEPT_PROTOCOL,
    AiMaskAcceptDataClient,
    RecordAiMaskDecision,
    RefineAiMaskAnnotation,
)
from aap_labeling.mask_edit_contract import (
    MASK_EDIT_PROTOCOL,
    ApplyMaskMutations,
    ApplyPolygonSlice,
    FindOperation,
    MaskEditDataClient,
    ReadMaskUploadKeys,
    ReadMaskVideoSegments,
    UpdateMaskAnnotation,
    WriteAnnotationOperation,
)
from aap_labeling.mask_revision_contract import (
    MASK_REVISION_PROTOCOL,
    MaskRevisionDataClient,
    MaskRevisionReference,
    ProtectMaskRevisions,
    ResolveMaskRevisions,
)
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.ai_mask_accept_decision import AiMaskAcceptDecision
from app.db.models.annotation import Annotation
from app.db.models.annotation_operation import AnnotationOperation
from app.db.models.dataset import Dataset, DatasetItem, VideoSegment
from app.db.models.mask_annotation_revision import MaskAnnotationRevision
from app.db.models.prediction import Prediction
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.db.models.task import Task
from app.services.annotation_slice_contracts import annotation_slice_digest
from tests.factory import create_task
from tests.test_workspace_project_access_step_native import _cleanup, _seed
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable Labeling Mask edit capability",
    ),
]

_ROLE = "labeling_mask_edit_client"
_FUNCTIONS = {
    ("mask_edit_command", "jsonb"),
    ("mask_revision_command", "jsonb"),
    ("ai_mask_accept_command", "jsonb"),
}


async def test_mask_edit_and_acceptance_owners_share_transaction_and_hide_private_rows(
    test_engine,
):
    maker, world = await _seed(test_engine)
    task_id = None
    video_task_id = None
    dataset_id = None
    item_id = None
    connection = None
    transaction = None
    polygon_id = uuid.uuid4()
    created_polygon_id = uuid.uuid4()
    mask_id = uuid.uuid4()
    prediction_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    source_geometry = {"type": "polygon", "points": [[0, 0], [5, 0], [0, 5]]}
    kept_geometry = {"type": "polygon", "points": [[0, 0], [2, 0], [0, 2]]}
    created_geometry = {"type": "polygon", "points": [[2, 0], [5, 0], [0, 5]]}
    old_mask = {"type": "raster_mask", "mask": {"sha256": "a" * 64}}
    current_mask = {"type": "raster_mask", "mask": {"sha256": "b" * 64}}
    updated_mask = {"type": "raster_mask", "mask": {"sha256": "c" * 64}}
    context = None
    try:
        async with maker.begin() as db:
            task = await create_task(
                db, project_id=world["project_id"], status="in_progress"
            )
            task_id = task.id
            dataset = Dataset(
                display_id=f"MV-{uuid.uuid4().hex[:10]}",
                name="Mask edit video segments",
                data_type="video",
                created_by=world["member_id"],
            )
            db.add(dataset)
            await db.flush()
            dataset_id = dataset.id
            item = DatasetItem(
                dataset_id=dataset.id,
                file_name="mask-edit.mp4",
                file_path="mask-edit/video.mp4",
                file_type="video",
            )
            db.add(item)
            await db.flush()
            item_id = item.id
            video_task = await create_task(
                db, project_id=world["project_id"], status="in_progress"
            )
            video_task.file_type = "video"
            video_task.dataset_item_id = item.id
            video_task_id = video_task.id
            db.add_all(
                [
                    VideoSegment(
                        dataset_item_id=item.id,
                        segment_index=1,
                        start_frame=10,
                        end_frame=19,
                        status="open",
                    ),
                    VideoSegment(
                        dataset_item_id=item.id,
                        segment_index=0,
                        start_frame=0,
                        end_frame=9,
                        status="open",
                    ),
                ]
            )
            await db.flush()
            polygon = Annotation(
                id=polygon_id,
                task_id=task.id,
                project_id=task.project_id,
                user_id=world["member_id"],
                annotation_type="polygon",
                tool_unit_id="region",
                class_name="car",
                geometry=source_geometry,
                attributes={},
                attributes_meta={},
            )
            mask = Annotation(
                id=mask_id,
                task_id=task.id,
                project_id=task.project_id,
                user_id=world["member_id"],
                annotation_type="raster_mask",
                tool_unit_id="region",
                class_name="car",
                geometry=current_mask,
                version=2,
                attributes={},
                attributes_meta={},
            )
            prediction = Prediction(
                id=prediction_id,
                task_id=task.id,
                project_id=task.project_id,
                tool_unit_id="region",
                source="interactive_accept",
                result={},
                created_at=now,
            )
            db.add_all([polygon, mask, prediction])
            db.add(
                RasterMaskUpload(
                    task_id=task.id,
                    object_key="raster-masks/sha256/" + "d" * 64 + ".json",
                )
            )
            await db.flush()
            db.add(
                MaskAnnotationRevision(
                    project_id=task.project_id,
                    task_id=task.id,
                    annotation_id=mask.id,
                    annotation_version=1,
                    geometry=old_mask,
                    geometry_digest=annotation_slice_digest(old_mask),
                    source_kind="manual",
                    expires_at=now - timedelta(minutes=1),
                )
            )
            await db.flush()
            video_context = {
                "task_id": video_task.id,
                "project_id": video_task.project_id,
                "actor_id": world["member_id"],
                "dataset_item_id": item.id,
            }
            context = {
                "task_id": task.id,
                "project_id": task.project_id,
                "actor_id": world["member_id"],
                "expected_batch_id": task.batch_id,
                "expected_status": task.status,
                "expected_review_round_id": task.review_round_id,
            }

        connection = await asyncpg.connect(
            _dsn(test_engine, "AAP_MODULE_DATA_LABELING_MASK_EDIT_URL", _ROLE),
            timeout=5,
            command_timeout=10,
        )
        assert await connection.fetchval("SELECT current_user") == _ROLE
        await verify_function_only_role(
            connection, schema="aap_labeling_v1", functions=_FUNCTIONS, role=_ROLE
        )

        async def execute(function: str, payload: str):
            return await connection.fetchval(
                f"SELECT aap_labeling_v1.{function}($1::jsonb)", payload
            )

        edits = MaskEditDataClient(
            lambda payload: execute("mask_edit_command", payload)
        )
        revisions = MaskRevisionDataClient(
            lambda payload: execute("mask_revision_command", payload)
        )
        accepts = AiMaskAcceptDataClient(
            lambda payload: execute("ai_mask_accept_command", payload)
        )
        transaction = connection.transaction()
        await transaction.start()
        owned_uploads = await edits.command(
            ReadMaskUploadKeys(
                schema=MASK_EDIT_PROTOCOL,
                operation="read_reserved_mask_uploads",
                task_id=task_id,
                object_keys=[
                    "raster-masks/sha256/" + "d" * 64 + ".json",
                    "raster-masks/missing",
                ],
            )
        )
        assert owned_uploads.object_keys == [
            "raster-masks/sha256/" + "d" * 64 + ".json"
        ]
        all_segments = await edits.command(
            ReadMaskVideoSegments(
                schema=MASK_EDIT_PROTOCOL,
                operation="read_video_segments",
                **video_context,
                segment_id=None,
                lock=False,
            )
        )
        assert [row.segment_index for row in all_segments.segments] == [0, 1]
        locked_segment = await edits.command(
            ReadMaskVideoSegments(
                schema=MASK_EDIT_PROTOCOL,
                operation="read_video_segments",
                **video_context,
                segment_id=all_segments.segments[1].id,
                lock=True,
            )
        )
        assert [row.segment_index for row in locked_segment.segments] == [1]
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch(
                "SELECT locked_at FROM public.video_segments LIMIT 1"
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch("SELECT id FROM public.raster_mask_uploads LIMIT 1")
        polygon_write = await edits.command(
            ApplyPolygonSlice(
                schema=MASK_EDIT_PROTOCOL,
                operation="apply_polygon_slice",
                **context,
                source_annotation_id=polygon_id,
                created_annotation_id=created_polygon_id,
                expected_version=1,
                source_geometry=kept_geometry,
                created_geometry=created_geometry,
            )
        )
        assert (polygon_write.source.version, polygon_write.created.version) == (2, 1)
        await edits.command(
            WriteAnnotationOperation(
                schema=MASK_EDIT_PROTOCOL,
                operation="write_operation",
                **context,
                operation_id=operation_id,
                kind="slice_polygon",
                idempotency_key="mask-owner-polygon-slice",
                request_digest="d" * 64,
                scope_fingerprint="e" * 64,
                source_versions={str(polygon_id): 1},
                result_versions={str(polygon_id): 2, str(created_polygon_id): 1},
                report={"slice_snapshot_schema": 1},
                response_json={"operation_id": str(operation_id)},
                created_at=now,
                completed_at=now,
                lineage=[
                    {
                        "source_annotation_id": polygon_id,
                        "result_annotation_id": created_polygon_id,
                        "relation": "split",
                        "source_version": 1,
                        "result_version": 1,
                        "frame_index": None,
                    }
                ],
                original_operation_id=None,
                original_report=None,
            )
        )
        assert (
            await edits.command(
                FindOperation(
                    schema=MASK_EDIT_PROTOCOL,
                    operation="find_operation",
                    task_id=task_id,
                    actor_id=world["member_id"],
                    idempotency_key="mask-owner-polygon-slice",
                )
            )
        ).found
        changed_mask = await edits.command(
            ApplyMaskMutations(
                schema=MASK_EDIT_PROTOCOL,
                operation="apply_mask_mutations",
                **context,
                mutation_kind="split_components",
                expected_versions={str(mask_id): 2},
                changes=[
                    UpdateMaskAnnotation(
                        action="update",
                        annotation_id=mask_id,
                        geometry=updated_mask,
                        track_id=None,
                    )
                ],
            )
        )
        assert changed_mask.annotations[0].version == 3
        revision_ref = MaskRevisionReference(
            annotation_id=mask_id,
            annotation_version=1,
            geometry_digest=annotation_slice_digest(old_mask),
        )
        await revisions.command(
            ProtectMaskRevisions(
                schema=MASK_REVISION_PROTOCOL,
                operation="protect",
                task_id=task_id,
                references=[revision_ref],
                expires_at=now + timedelta(days=30),
            )
        )
        assert (
            await revisions.command(
                ResolveMaskRevisions(
                    schema=MASK_REVISION_PROTOCOL,
                    operation="resolve",
                    task_id=task_id,
                    references=[revision_ref],
                    lock=True,
                )
            )
        ).geometries[0].geometry == old_mask
        refined = await accepts.command(
            RefineAiMaskAnnotation(
                schema=AI_MASK_ACCEPT_PROTOCOL,
                operation="refine_annotation",
                **context,
                annotation_id=mask_id,
                expected_version=3,
                video_segment_id=None,
                annotation_type="raster_mask",
                prediction_id=prediction_id,
                class_name="car",
                geometry=updated_mask,
                track_id=None,
                confidence=0.9,
            )
        )
        assert refined.version == 4
        await accepts.command(
            RecordAiMaskDecision(
                schema=AI_MASK_ACCEPT_PROTOCOL,
                operation="record_decision",
                **context,
                idempotency_key="mask-owner-accept",
                request_digest="f" * 64,
                candidate_id="mask:test",
                content_digest="c" * 64,
                prediction_id=prediction_id,
                prediction_created_at=now,
                annotation_id=mask_id,
                source_annotation_id=mask_id,
                source_version=3,
                result_version=4,
                response_json={"result_version": 4},
            )
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch("SELECT id FROM public.annotations LIMIT 1")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch(
                "SELECT id FROM public.annotation_operations LIMIT 1"
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch(
                "SELECT id FROM public.ai_mask_accept_decisions LIMIT 1"
            )
        await transaction.rollback()
        transaction = None

        async with maker() as db:
            source = await db.get(Annotation, polygon_id)
            mask = await db.get(Annotation, mask_id)
            assert source.geometry == source_geometry and source.version == 1
            assert mask.geometry == current_mask and mask.version == 2
            assert await db.get(Annotation, created_polygon_id) is None
            assert (
                await db.scalar(
                    select(AnnotationOperation.id).where(
                        AnnotationOperation.id == operation_id
                    )
                )
                is None
            )
            assert (
                await db.scalar(
                    select(AiMaskAcceptDecision.id).where(
                        AiMaskAcceptDecision.idempotency_key == "mask-owner-accept"
                    )
                )
                is None
            )
    finally:
        if transaction is not None:
            await transaction.rollback()
        if connection is not None:
            await connection.close()
        async with maker.begin() as db:
            if task_id is not None:
                await db.execute(
                    delete(Annotation).where(Annotation.task_id == task_id)
                )
                await db.execute(
                    delete(Prediction).where(Prediction.task_id == task_id)
                )
                await db.execute(delete(Task).where(Task.id == task_id))
            if video_task_id is not None:
                await db.execute(delete(Task).where(Task.id == video_task_id))
            if item_id is not None:
                await db.execute(
                    delete(VideoSegment).where(VideoSegment.dataset_item_id == item_id)
                )
                await db.execute(delete(DatasetItem).where(DatasetItem.id == item_id))
            if dataset_id is not None:
                await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
        await _cleanup(maker, world)
