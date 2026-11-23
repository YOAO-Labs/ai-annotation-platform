"""Finalize a claimed tracker outcome in the tracking worker's transaction."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from yoao_labeling.video_writer import VideoWriterManager
from yoao_labeling.video_writer_contract import (
    VideoWriterExpectedInput,
    VideoWriterFailure,
    VideoWriterRejected,
    VideoWriterUnavailable,
    collect_video_writer_mask_references,
    decode_video_writer_data_reply,
    make_video_writer_command,
    video_writer_command_digest,
    video_writer_data_arguments_bytes,
)

from app.services.raster_mask_contract import RasterMaskContractError
from app.services.raster_mask_storage import load_coco_rle

from .execution import TrackerExecutionDeferred, TrackerExecutionLost, execution_proof

_STEP = text("SELECT yoao_labeling_v1.video_writer_step(:action,:arguments)")
_LOST_CODES = {"lease_lost", "realm_mismatch", "job_missing", "job_not_active"}


class SqlAlchemyVideoWriterData:
    """Borrow exactly the current caller transaction without flushing ORM writes."""

    def __init__(self, db):
        self.db = db
        self._transaction = self._connection = self._xid = None

    @asynccontextmanager
    async def transaction(self):
        if self._transaction is not None:
            raise VideoWriterUnavailable()
        try:
            self._connection = await self.db.connection()
            self._transaction = self.db.get_transaction()
            self._xid = await self._connection.scalar(
                text("SELECT pg_catalog.pg_current_xact_id()::text")
            )
            decode_video_writer_data_reply("policy", {}, await self.call("policy", {}))
            yield self
        except (SQLAlchemyError, OSError):
            raise VideoWriterUnavailable() from None
        finally:
            self._transaction = self._connection = self._xid = None

    async def call(self, action, arguments):
        transaction = self.db.get_transaction()
        if (
            self._connection is None
            or self._transaction is None
            or transaction is not self._transaction
            or not transaction.is_active
        ):
            raise VideoWriterUnavailable()
        try:
            xid = await self._connection.scalar(
                text("SELECT pg_catalog.pg_current_xact_id()::text")
            )
            if xid != self._xid:
                raise VideoWriterUnavailable()
            with self.db.no_autoflush:
                return await self.db.scalar(
                    _STEP,
                    {
                        "action": action,
                        "arguments": video_writer_data_arguments_bytes(
                            action, arguments
                        ).decode("utf-8"),
                    },
                )
        except (SQLAlchemyError, OSError):
            raise VideoWriterUnavailable() from None


async def _verify_candidate_masks(staged):
    for reference in collect_video_writer_mask_references(staged):
        try:
            await load_coco_rle(reference.model_dump(mode="json"))
        except RasterMaskContractError as exc:
            if exc.status_code >= 500 or exc.detail.get("retryable"):
                raise VideoWriterUnavailable() from None
            raise VideoWriterRejected("mask_content_invalid") from None


async def commit_tracker_outcome(
    db,
    job_id,
    outcome,
    *,
    frame_results=(),
    status=None,
    source_version=None,
    error_message=None,
    recover_cancelled=False,
):
    """Publish one stable command, holding results through transient contention."""
    expected_input = None
    if status is not None and source_version is not None:
        expected_input = VideoWriterExpectedInput(
            input_id=status.input_id,
            request_digest=status.request_digest,
            plan_digest=status.plan_digest,
            source_version_id=source_version.version_id,
            source_sha256=source_version.sha256,
        )
    command = make_video_writer_command(
        execution_proof(job_id),
        outcome=outcome,
        expected_input=expected_input,
        frame_results=frame_results,
        error_message=error_message,
    )
    digest = video_writer_command_digest(command)
    try:
        await db.commit()
    except (SQLAlchemyError, OSError):
        await db.rollback()
        raise TrackerExecutionDeferred("video_writer_unavailable") from None
    manager = VideoWriterManager(
        SqlAlchemyVideoWriterData(db), verify_masks=_verify_candidate_masks
    )
    failures, delay = 0, 0.05
    while True:
        execution_proof(job_id)
        try:
            try:
                if recover_cancelled:
                    receipt = await manager.publication(command.execution)
                    await db.commit()
                    if receipt is not None:
                        return receipt
                receipt = await manager.execute(command)
                await db.commit()
                return receipt
            except (VideoWriterUnavailable, SQLAlchemyError, OSError):
                await db.rollback()
                receipt = await manager.status(
                    command.operation_id, command.execution, digest
                )
                await db.commit()
                if receipt is not None:
                    return receipt
                raise VideoWriterUnavailable()
        except VideoWriterFailure as exc:
            await db.rollback()
            if exc.code in _LOST_CODES:
                raise TrackerExecutionLost() from None
            if exc.code.endswith("_busy") or exc.code == "execution_busy":
                await asyncio.sleep(delay)
                delay = min(delay * 2, 1.0)
                continue
            if isinstance(exc, VideoWriterRejected):
                raise
            if failures >= 2:
                raise TrackerExecutionDeferred("video_writer_unavailable") from None
            failures += 1
            await asyncio.sleep(0.2)
        except (SQLAlchemyError, OSError):
            await db.rollback()
            raise TrackerExecutionDeferred("video_writer_unavailable") from None
