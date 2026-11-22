"""Own one managed-image ROI and its frozen parent annotation.

The source, input and parent checks reuse the synchronous image operation. The
parent is only write-locked after canonical source locks in the final caller
transaction; crop encoding and delivery never retain business locks.
"""

from contextlib import asynccontextmanager
from copy import deepcopy
import math
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError

from app.db.models.annotation import Annotation
from app.services import prediction_originals as originals
from app.services.content_snapshot import ContentError
from app.services.dataset_content import key_authority
from app.services.labeling_core_read_relations import lock_annotation_rows_for_core_read
from app.services.managed_image_inference import (
    ManagedImageOperation,
    managed_inference_http_error,
)
from app.services.predictions_import import internal_geometry_to_ls_shape
from app.services.project_write_guard import _busy_lock_error
from yoao_inference.roi import CropBatch, crop_inputs_from_boxes


def _annotation_facts(annotation):
    """Freeze all persisted parent facts, including structural relationships."""
    return originals.bounded_proof(
        {
            column.key: getattr(annotation, column.key)
            for column in Annotation.__table__.columns
        }
    )


class ManagedSecondaryOperation(ManagedImageOperation):
    def __init__(self, db, task, actor, request, annotation):
        super().__init__(db, task, actor, request, "secondary_inference")
        self.annotation_id = annotation.id
        self._parent_facts = _annotation_facts(annotation)
        self._locked_parent = None
        self._crop_prepared = False
        self._finalized = False

    @property
    def parent_facts(self):
        return deepcopy(self._parent_facts)

    async def _business(self, db, task, actor):
        await super()._business(db, task, actor)
        await key_authority(db, actor, self.key_id, "annotations:write")

    async def _parent(self, db, task, *, final=False):
        project_id = (
            self.project_id if self._parent_facts["project_id"] is not None else None
        )
        parents = await lock_annotation_rows_for_core_read(
            db,
            [self.annotation_id],
            task_id=self.task_id,
            project_id=project_id,
            lock_mode="update" if final else "share",
            nowait=True,
            populate_existing=True,
        )
        parent = parents[0] if parents else None
        if (
            parent is None
            or parent.task_id != task.id
            or parent.project_id not in (None, self.project_id)
            or not parent.is_active
            or parent.was_cancelled
            or parent.is_locked
            or _annotation_facts(parent) != self._parent_facts
        ):
            raise ContentError("secondary_annotation_changed")
        if final:
            self._locked_parent = parent

    async def _freeze_extra(self, db, task):
        await self._parent(db, task)

    async def _check_extra(self, db, task, *, final=False):
        await self._parent(db, task, final=final)

    def _input_source_ref(self):
        return originals.bounded_proof(
            {**self.binding, "annotation": self._parent_facts}
        )

    async def prepare_crop(self, ls_box, *, pad):
        self._check_open()
        if self._crop_prepared:
            raise ContentError("prediction_input_already_open")
        expected = internal_geometry_to_ls_shape(
            self._parent_facts["geometry"],
            self._parent_facts["class_name"],
            self._parent_facts["confidence"],
        )
        if originals.bounded_proof(ls_box) != expected:
            raise ContentError("secondary_annotation_changed")
        if (
            type(pad) not in (int, float)
            or not math.isfinite(pad)
            or not 0 <= pad <= 0.5
        ):
            raise ContentError("secondary_crop_invalid", 422)
        self._crop_prepared = True
        await self.validate()
        await self.db.commit()

        def encode():
            image = self.inputs.load_image(SimpleNamespace(id=self.task_id))
            return crop_inputs_from_boxes(
                image,
                [deepcopy(ls_box)],
                pad=pad,
                delivery="deferred",
                min_crop_side_px=32,
            )

        try:
            batch: CropBatch = await originals.physical(encode)
            for item in batch.inputs:
                index = item["id"]
                item["file_path"] = await self.inputs.upload_crop(
                    SimpleNamespace(id=self.task_id),
                    int(index),
                    batch.contents.pop(index),
                )
            await self.validate()
            return batch
        except BaseException as failure:
            self._remember(failure)
            raise

    async def before_dispatch(self, client=None):
        """Recheck the frozen source immediately before transport and release SQL state.

        The portable execution owner performs routing selection inside
        ``ModelExecutionPort.execute``; callers therefore recheck here directly
        before dispatch. The final fence below still re-validates the source
        under the caller's write locks.
        """
        self._check_open()
        if not self._crop_prepared or self._finalized:
            raise ContentError("prediction_original_receipt_invalid")
        await self.validate()
        await self.db.commit()

    async def final(self):
        if not self._crop_prepared or self._finalized:
            raise ContentError("prediction_original_receipt_invalid")
        task = await super().final()
        self._finalized = True
        return task, self._locked_parent

    async def recheck_credentials(self):
        """Check original credentials after writes, without rereading old parent facts."""
        self._check_open()
        if not self._finalized or not self.db.in_transaction():
            raise ContentError("prediction_original_receipt_invalid")
        actor = await originals.reauthorize(
            self.db, self.credentials, self.actor_id, self.key_id
        )
        await key_authority(self.db, actor, self.key_id, "annotations:write")


@asynccontextmanager
async def managed_secondary_operation(db, task, actor, request, annotation, factory):
    owner = None
    failure = None
    try:
        owner = ManagedSecondaryOperation(db, task, actor, request, annotation)
        await owner.open(factory, upload_full=False)
        yield owner
    except BaseException as exc:
        failure = exc
        if isinstance(exc, DBAPIError) and _busy_lock_error(exc):
            raise HTTPException(
                409,
                detail={
                    "code": "managed_inference_busy",
                    "message": "标注或原图正在更新，请稍后重试二次推理。",
                },
            ) from None
        if isinstance(exc, Exception):
            raise managed_inference_http_error(exc) from None
        raise
    finally:
        try:
            await originals.settled(db.rollback())
        finally:
            if owner is not None:
                await owner.close(failure)
