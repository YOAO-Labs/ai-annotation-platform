"""Durable batch execution, immutable result capture, and receipt recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone

from app.services.batch_result_data import (
    IndependentBatchResultData,
    SqlAlchemyBatchResultData,
)
from app.services.labeling_prediction_contract import ApplyPredictionResultRequest
from app.services.labeling_write_scope import LabelingWriteScope


class PreannotationResultDeferred(RuntimeError):
    """Retry delivery against the saved execution, rather than record a failure."""


def _json_value(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("snapshot timestamp must be aware")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    raise TypeError("unsupported snapshot value")


def _json_text(value):
    return json.dumps(
        value,
        default=_json_value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def preannotation_request_digest(request) -> str:
    """Bind every queued model/input parameter independently of broker identity."""
    value = {field.name: getattr(request, field.name) for field in fields(request)}
    # The Job ID is already a separate immutable part of the execution scope.
    value.pop("async_job_id")
    return hashlib.sha256(_json_text(value).encode("utf-8")).hexdigest()


def scope_json(scope: LabelingWriteScope) -> dict:
    return json.loads(
        _json_text({field.name: getattr(scope, field.name) for field in fields(scope)})
    )


_SCOPE_UUID_FIELDS = frozenset(
    {
        "task_id",
        "project_id",
        "actor_id",
        "assignee_id",
        "review_round_id",
        "source_annotation_id",
        "expected_batch_id",
        "job_id",
        "source_dataset_item_id",
        "accepted_input_id",
    }
)


def restore_write_scope(value) -> LabelingWriteScope:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if set(value) != {field.name for field in fields(LabelingWriteScope)}:
        raise ValueError("stored Labeling scope is incomplete")
    return LabelingWriteScope(
        **{
            key: uuid.UUID(item)
            if key in _SCOPE_UUID_FIELDS and item is not None
            else item
            for key, item in value.items()
        }
    )


def _request_json(request: ApplyPredictionResultRequest) -> dict:
    value = {
        field.name: getattr(request, field.name)
        for field in fields(request)
        if field.name != "batch_result_identity"
    }
    value["write_scope"] = scope_json(request.write_scope)
    if request.accepted_video_input is not None:
        value["accepted_video_input"] = request.accepted_video_input.model_dump(
            mode="json", by_alias=True
        )
    return value


def _restore_request(value: dict) -> ApplyPredictionResultRequest:
    expected = {
        field.name
        for field in fields(ApplyPredictionResultRequest)
        if field.name != "batch_result_identity"
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("stored prediction request has an invalid shape")
    value = dict(value)
    for key in (
        "task_id",
        "project_id",
        "ml_backend_id",
        "ml_backend_pool_id",
        "accepted_input_job_id",
    ):
        if value[key] is not None:
            value[key] = uuid.UUID(value[key])
    value["write_scope"] = restore_write_scope(value["write_scope"])
    if value["accepted_video_input"] is not None:
        raise ValueError("ordinary batch snapshot cannot contain a video frame result")
    return ApplyPredictionResultRequest(**value)


def _request(operation: str, value: dict):
    from aap_preannotation_results.contract import OPERATIONS

    return OPERATIONS[operation].request.model_validate_json(_json_text(value))


@dataclass(frozen=True, slots=True)
class _SnapshotFact:
    sha256: str
    byte_size: int
    part_count: int
    offset: int


class BatchResultExecution:
    """One renewable worker lease; all saved facts survive this Python process."""

    def __init__(
        self, request, session_factory, *, execution_id, executor_session_factory=None
    ):
        self.session_factory = session_factory
        self.database = IndependentBatchResultData(
            executor_session_factory or session_factory
        )
        self.scope = {
            "job_id": uuid.UUID(request.async_job_id),
            "actor_id": uuid.UUID(request.user_id),
            "project_id": uuid.UUID(request.project_id),
            "request_digest": preannotation_request_digest(request),
        }
        self.worker_id = uuid.uuid4()
        self.execution_id = execution_id
        self.lease = None
        self.progress = None
        self._heartbeat = None
        self._lost = None

    def _values(self, *, leased=True, **values):
        result = dict(self.scope)
        if leased:
            self.ensure_owned()
            result.update(
                generation=self.lease.generation, lease_token=self.lease.lease_token
            )
        result.update(values)
        return result

    def ensure_owned(self):
        if self._lost is not None:
            raise PreannotationResultDeferred(self._lost)
        if self.lease is None:
            raise PreannotationResultDeferred("lease_lost")

    async def call(self, operation, *, leased=True, db=None, **values):
        from aap_preannotation_results.contract import BatchResultFailure

        request = _request(operation, self._values(leased=leased, **values))
        try:
            result = await (
                self.database
                if db is None
                else SqlAlchemyBatchResultData(db, writer=True)
            ).call(operation, request)
        except BatchResultFailure as exc:
            if exc.code in {
                "storage_unavailable",
                "execution_busy",
                "lease_lost",
                "content_deleting",
                "content_epoch_changed",
                "invalid_response",
            }:
                raise PreannotationResultDeferred(exc.code) from None
            raise
        if (
            hasattr(result, "progress")
            and operation != "heartbeat_batch_result_execution"
        ):
            self.progress = result.progress
        return result

    async def claim(self):
        result = await self.call(
            "claim_batch_result_execution",
            leased=False,
            worker_id=self.worker_id,
            execution_id=self.execution_id,
            lease_seconds=120,
        )
        self.lease = result.lease
        self._heartbeat = asyncio.create_task(self._renew())
        return self

    async def _renew(self):
        delay = 30
        try:
            while True:
                await asyncio.sleep(delay)
                try:
                    result = await self.call(
                        "heartbeat_batch_result_execution", lease_seconds=120
                    )
                except PreannotationResultDeferred as exc:
                    if str(exc) == "lease_lost":
                        self._lost = "lease_lost"
                        return
                    # Our own checkpoint transaction can briefly hold the Job
                    # lock. Each write still validates owner time/token in SQL.
                    delay = 5
                    continue
                self.lease = result.lease
                delay = 30
        except asyncio.CancelledError:
            raise
        except Exception:
            # Unknown protocol failures cannot establish continued ownership.
            self._lost = "lease_lost"

    async def close(self):
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await self._heartbeat
        if self.lease is not None and self._lost is None:
            with suppress(Exception):
                await self.call("release_batch_result_execution")

    async def members(self, *, after_ordinal=0):
        while True:
            page = await self.call(
                "read_batch_result_execution",
                leased=False,
                after_ordinal=after_ordinal,
                limit=100,
            )
            for member in page.members:
                yield member
            if page.next_ordinal is None:
                return
            after_ordinal = page.next_ordinal

    async def read_result(
        self, ordinal, *, stage="raw", request_index=0, part_index=None
    ):
        return await self.call(
            "read_batch_result",
            leased=False,
            ordinal=ordinal,
            stage=stage,
            request_index=request_index,
            part_index=part_index,
        )

    async def capture(self, ordinal, requests, *, stage, context):
        from aap_preannotation_results.codec import (
            bundle_digest,
            encode_part,
            snapshot_file,
        )
        from aap_preannotation_results.contract import MAX_PART_BYTES, Scope
        from app.services.prediction import derive_tool_unit_from_result

        # One archive keeps the number of open files constant for multi-result
        # Tasks. Request bodies remain separate values with their own hashes.
        with tempfile.TemporaryFile(mode="w+b") as archive:
            snapshots = []
            for request in requests:
                with snapshot_file(_request_json(request)) as snapshot:
                    fact = _SnapshotFact(
                        snapshot.sha256,
                        snapshot.byte_size,
                        snapshot.part_count,
                        archive.tell(),
                    )
                    for part in snapshot.iter_parts():
                        archive.write(part)
                    snapshots.append(fact)
                await asyncio.sleep(0)
            digest = bundle_digest(
                Scope(**self.scope),
                ordinal,
                stage,
                (snapshot.sha256 for snapshot in snapshots),
            )
            await self.call(
                "begin_batch_result",
                ordinal=ordinal,
                task_id=requests[0].task_id if requests else context["task_id"],
                result_count=len(requests),
                bundle_digest=digest,
                stage=stage,
                context=context,
                restart_incomplete=stage == "raw",
            )
            for index, (snapshot, request) in enumerate(
                zip(snapshots, requests, strict=True)
            ):
                archive.seek(snapshot.offset)
                remaining = snapshot.byte_size
                for part_index in range(snapshot.part_count):
                    content = archive.read(min(MAX_PART_BYTES, remaining))
                    if not content:
                        raise PreannotationResultDeferred("storage_unavailable")
                    remaining -= len(content)
                    await self.call(
                        "put_batch_result_part",
                        ordinal=ordinal,
                        stage=stage,
                        request_index=index,
                        sha256=snapshot.sha256,
                        byte_size=snapshot.byte_size,
                        part_count=snapshot.part_count,
                        part_index=part_index,
                        content=encode_part(content),
                        tool_unit_id=(
                            derive_tool_unit_from_result(request.result)
                            if stage == "prepared"
                            else None
                        ),
                    )
            return await self.seal(ordinal, stage=stage, requests=requests)

    async def seal(self, ordinal, *, stage, requests=None):
        from app.services.mask_gc import read_mask_write_epochs, lock_mask_write_epochs
        from app.services.raster_mask_storage import (
            collect_mask_references,
            load_coco_rle,
        )
        from sqlalchemy.exc import SQLAlchemyError

        if requests is None:
            _result, requests = await self.load(ordinal, stage=stage)
        references = [
            reference
            for request in requests
            for reference in collect_mask_references(_request_json(request))
        ]
        if not references:
            return await self.call("seal_batch_result", ordinal=ordinal, stage=stage)
        # Snapshot roots need a fresh byte verification after any prior GC epoch.
        # The executor cannot mint this business-writer permit. Read epochs
        # before object I/O, then verify and link in one provider transaction.
        async with self.session_factory() as db:
            try:
                epochs = await read_mask_write_epochs(
                    db, (reference["object_key"] for reference in references)
                )
                verified = set()
                for reference in references:
                    signature = _json_text(reference)
                    if signature not in verified:
                        await load_coco_rle(reference)
                        verified.add(signature)
                await lock_mask_write_epochs(db, epochs)
                result = await self.call(
                    "seal_batch_result", db=db, ordinal=ordinal, stage=stage
                )
                await db.commit()
                return result
            except SQLAlchemyError:
                await db.rollback()
                raise PreannotationResultDeferred("storage_unavailable") from None

    async def load(self, ordinal, *, stage):
        from aap_preannotation_results.codec import PayloadAssembler
        from app.services.labeling_prediction_contract import (
            BatchResultPredictionIdentity,
        )

        first = await self.read_result(ordinal, stage=stage)
        result = first.result
        if result is None:
            return None, ()
        requests = []
        for index in range(result.result_count):
            metadata = (
                first
                if index == 0
                else await self.read_result(ordinal, stage=stage, request_index=index)
            )
            payload = metadata.payload
            if payload is None or not payload.complete:
                raise PreannotationResultDeferred("result_incomplete")
            metadata_request = _request(
                "read_batch_result",
                self._values(
                    leased=False,
                    ordinal=ordinal,
                    stage=stage,
                    request_index=index,
                    part_index=None,
                ),
            )
            with PayloadAssembler(metadata_request, metadata) as snapshot:
                for part_index in range(payload.part_count):
                    part = await self.read_result(
                        ordinal, stage=stage, request_index=index, part_index=part_index
                    )
                    part_request = metadata_request.model_copy(
                        update={"part_index": part_index}
                    )
                    snapshot.add_part(part_request, part)
                request = _restore_request(json.load(snapshot.file))
            if stage == "prepared":
                request = replace(
                    request,
                    batch_result_identity=BatchResultPredictionIdentity(
                        prediction_id=payload.prediction_id,
                        prediction_created_at=payload.prediction_created_at,
                    ),
                )
            requests.append(request)
        return result, tuple(requests)


@asynccontextmanager
async def batch_result_execution(
    request, session_factory, *, execution_id, executor_session_factory=None
):
    execution = BatchResultExecution(
        request,
        session_factory,
        execution_id=execution_id,
        executor_session_factory=executor_session_factory,
    )
    try:
        await execution.claim()
        yield execution
    finally:
        await execution.close()
