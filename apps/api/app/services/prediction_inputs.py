"""Own temporary ML inputs; authorization and result qualification stay with callers."""

import asyncio
from contextlib import asynccontextmanager
import contextvars
from datetime import datetime, timedelta, timezone
from functools import partial
import hashlib
import json
import logging
import re
import uuid

from botocore.config import Config
from PIL import Image, ImageOps
from sqlalchemy import or_, select

from app.db.models.prediction_input import PredictionInputLease as InputRecord
from app.services.content_snapshot import ContentError, MAX_BYTES, Snapshot
from app.services.storage import StorageService

log = logging.getLogger(__name__)
ACTIVE_SECONDS = 2 * 60 * 60
RECOVERY_DAYS = 7
RETRY_SECONDS = 60
MAX_OBJECTS = 10_000
MAX_TOTAL_BYTES = 4 * 1024**3
URL_SECONDS = 60 * 60


def now():
    return datetime.now(timezone.utc)


def input_prefix(identity):
    return f"prediction-inputs/{uuid.UUID(str(identity))}/"


def make_storage():
    return StorageService(
        client_config=Config(
            connect_timeout=3,
            read_timeout=15,
            # Retrying PUT can hide an earlier request which still completes late.
            retries={"total_max_attempts": 1, "mode": "standard"},
        )
    )


def storage_identity(storage):
    # No credentials or signed URLs. A changed server must not receive old deletions.
    configuration = [
        storage.client.meta.endpoint_url.rstrip("/"),
        storage.import_bucket,
    ]
    return hashlib.sha256(json.dumps(configuration).encode()).hexdigest()


async def settled_io(call, *args, **kwargs):
    """Do not close an input file while an admitted SDK thread still reads it."""
    # An executor Future is not a Task cancelled by asyncio.run's shutdown sweep.
    context = contextvars.copy_context()
    pending = asyncio.get_running_loop().run_in_executor(
        None, context.run, partial(call, *args, **kwargs)
    )
    cancelled = False
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        try:
            pending.result()
        except BaseException:
            pass
        raise asyncio.CancelledError()
    return pending.result()


class PredictionInputs:
    def __init__(self, factory, record, storage):
        self.factory, self.record, self.storage = factory, record, storage
        self.prefix = input_prefix(record.id)
        self._objects = {}
        self._uploaded = set()
        self._total_bytes = 0
        self._snapshot = None
        self._images = []
        self._closed = False
        self._uncertain_write = False

    @classmethod
    async def create(cls, factory, *, owner_id, owner_kind, task_id, storage=None):
        if owner_kind not in {
            "batch_predict",
            "prediction_retry",
            "interactive_predict",
            "predict_test",
            "mask_repair",
        }:
            raise ValueError("unsupported prediction input owner")
        owner_id, task_id = uuid.UUID(str(owner_id)), uuid.UUID(str(task_id))
        storage = storage or make_storage()
        created = now()
        expires = created + timedelta(seconds=ACTIVE_SECONDS)
        record = InputRecord(
            id=uuid.uuid4(),
            owner_id=owner_id,
            owner_kind=owner_kind,
            task_id=task_id,
            bucket=storage.import_bucket,
            storage_identity=storage_identity(storage),
            state="active",
            uncertain_write=False,
            expires_at=expires,
            retain_until=expires + timedelta(days=RECOVERY_DAYS),
            next_cleanup_at=expires,
        )
        try:
            async with factory() as db:
                db.add(record)
                await db.commit()
            return cls(factory, record, storage)
        except BaseException:
            storage.client.close()
            raise

    def _reserve(self, key, size):
        if self._closed or now() >= self.record.expires_at:
            raise ContentError("prediction_input_expired")
        if not key.startswith(self.prefix):
            raise ContentError("prediction_input_scope_invalid")
        if key in self._objects:
            return key not in self._uploaded
        if size <= 0 or size > MAX_BYTES:
            raise ContentError("prediction_input_too_large", 413)
        if (
            len(self._objects) >= MAX_OBJECTS
            or self._total_bytes + size > MAX_TOTAL_BYTES
        ):
            raise ContentError("prediction_input_budget_exceeded", 413)
        self._objects[key] = size
        self._total_bytes += size
        return True

    def _url(self, key):
        seconds = min(
            URL_SECONDS, int((self.record.expires_at - now()).total_seconds())
        )
        if seconds <= 0:
            raise ContentError("prediction_input_expired")
        try:
            url = self.storage.generate_download_url(
                key, expires_in=seconds, bucket=self.record.bucket, align=False
            )
            return self.storage.rewrite_host_for_ml_backend(url)
        except Exception:
            raise ContentError("prediction_input_url_failed", 503) from None

    def upload_crop(self, task, box_index, content):
        if (
            uuid.UUID(str(task.id)) != self.record.task_id
            or type(box_index) is not int
            or box_index < 0
        ):
            raise ContentError("prediction_input_scope_invalid")
        digest = hashlib.sha256(content).hexdigest()
        key = f"{self.prefix}crops/{box_index}-{digest}.jpg"
        if self._reserve(key, len(content)):
            try:
                self.storage.put_bytes(
                    key, content, content_type="image/jpeg", bucket=self.record.bucket
                )
                self._uploaded.add(key)
            except BaseException as exc:
                self._uncertain_write = True
                if isinstance(exc, Exception):
                    raise ContentError("prediction_input_upload_failed", 503) from None
                raise
        return self._url(key)

    async def upload_snapshot(self, snapshot):
        """Transfer ownership of an already strictly verified snapshot, never its source."""
        if self._snapshot is not None or not isinstance(snapshot, Snapshot):
            raise ContentError("prediction_input_snapshot_invalid")
        suffix = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(
            snapshot.media_type
        )
        if (
            snapshot._closed
            or snapshot.inspection_error
            or suffix is None
            or not re.fullmatch(r"[0-9a-f]{64}", snapshot.sha256)
        ):
            raise ContentError("prediction_input_snapshot_invalid")
        key = f"{self.prefix}original/{snapshot.sha256}.{suffix}"
        self._reserve(key, snapshot.size)
        self._snapshot = snapshot
        snapshot.file.seek(0)
        try:
            await settled_io(
                self.storage.client.put_object,
                Bucket=self.record.bucket,
                Key=key,
                Body=snapshot.file,
                ContentLength=snapshot.size,
                ContentType=snapshot.media_type,
            )
            self._uploaded.add(key)
        except BaseException as exc:
            self._uncertain_write = True
            if isinstance(exc, Exception):
                raise ContentError("prediction_input_upload_failed", 503) from None
            raise
        return self._url(key)

    def load_image(self, task):
        if (
            self._closed
            or now() >= self.record.expires_at
            or uuid.UUID(str(task.id)) != self.record.task_id
            or not self._snapshot
        ):
            raise ContentError("prediction_input_snapshot_invalid")
        self._snapshot.file.seek(0)
        with Image.open(self._snapshot.file) as image:
            oriented = ImageOps.exif_transpose(image)
            try:
                result = oriented.convert("RGB")
            finally:
                oriented.close()
        if result.size != (self._snapshot.width, self._snapshot.height):
            result.close()
            raise ContentError("image_size_mismatch")
        self._images.append(result)
        return result

    async def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            async with self.factory() as db:
                record = await db.get(InputRecord, self.record.id)
                if record:
                    record.state = "cleanup_pending"
                    record.uncertain_write = (
                        record.uncertain_write or self._uncertain_write
                    )
                    record.next_cleanup_at = now()
                    await db.commit()
            await purge_input(self.factory, self.record.id, storage=self.storage)
        except Exception:
            # The independent record was committed before the first PUT.
            log.warning("Prediction input cleanup deferred input=%s", self.record.id)
        finally:
            for image in self._images:
                image.close()
            if self._snapshot:
                self._snapshot.close()
            self.storage.client.close()


@asynccontextmanager
async def prediction_inputs(factory, **owner):
    inputs = await PredictionInputs.create(factory, **owner)
    try:
        yield inputs
    finally:
        await inputs.close()


async def purge_input(factory, input_id, *, storage=None):
    owned_storage = storage is None
    storage = storage or make_storage()
    try:
        async with factory() as db:
            record = await db.scalar(
                select(InputRecord)
                .where(InputRecord.id == input_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            checked = now()
            if (
                not record
                or record.next_cleanup_at > checked
                or (record.state == "active" and record.expires_at > checked)
            ):
                return "skipped"
            if record.state == "active":
                record.state = "cleanup_pending"
                record.uncertain_write = True
            record.last_cleanup_at = checked
            record.next_cleanup_at = checked + timedelta(seconds=RETRY_SECONDS)
            matched = (
                record.bucket == storage.import_bucket
                and record.storage_identity == storage_identity(storage)
            )
            record.error_code = None if matched else "storage_identity_changed"
            bucket = record.bucket
            await db.commit()
            if not matched:
                return "deferred"
        try:
            await settled_io(
                storage.delete_prefix, input_prefix(input_id), bucket=bucket
            )
        except Exception:
            async with factory() as db:
                record = await db.get(InputRecord, input_id)
                if record:
                    record.error_code = "storage_cleanup_failed"
                    await db.commit()
            log.warning("Prediction input deletion deferred input=%s", input_id)
            return "deferred"
        async with factory() as db:
            record = await db.scalar(
                select(InputRecord).where(InputRecord.id == input_id).with_for_update()
            )
            if record:
                if record.uncertain_write and record.retain_until > now():
                    record.error_code = None
                    await db.commit()
                    return "retained"
                await db.delete(record)
                await db.commit()
        return "removed"
    finally:
        if owned_storage:
            storage.client.close()


async def maintain(factory, *, limit=50):
    async with factory() as db:
        ids = list(
            (
                await db.scalars(
                    select(InputRecord.id)
                    .where(
                        InputRecord.next_cleanup_at <= now(),
                        or_(
                            InputRecord.state == "cleanup_pending",
                            InputRecord.expires_at <= now(),
                        ),
                    )
                    .order_by(InputRecord.next_cleanup_at, InputRecord.id)
                    .limit(limit)
                )
            ).all()
        )
    counts = {"removed": 0, "retained": 0, "deferred": 0, "skipped": 0}
    for identity in ids:
        try:
            counts[await purge_input(factory, identity)] += 1
        except Exception:
            counts["deferred"] += 1
            log.warning("Prediction input recovery deferred input=%s", identity)
    return counts
