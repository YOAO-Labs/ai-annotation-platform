"""Observe, prepare and sign exact retained tracker-video source versions."""

from __future__ import annotations

from typing import get_args

from aap_assets_images.contract import ImageFailure
from aap_assets_inputs.contract import InputFailure
from aap_assets_media.contract import MediaFailure
from aap_assets_video_inputs import execution as e
from aap_assets_video_inputs import tracker as t

from .tracker_database import TrackerVideoInputDatabase


_ERRORS = frozenset(get_args(t.TrackerErrorCode))
_TERMINAL_SOURCE = frozenset(
    {"source_missing", "source_changed", "source_not_ready", "not_video"}
)


class TrackerVideoInputControl:
    def __init__(
        self,
        database: TrackerVideoInputDatabase,
        storage,
        *,
        url_expiry_seconds=900,
    ):
        if (
            type(url_expiry_seconds) is not int
            or not 1 <= url_expiry_seconds <= t.URL_EXPIRY_SECONDS
        ):
            raise ValueError("invalid tracker source URL lifetime")
        self.database, self.storage = database, storage
        self.url_expiry_seconds = url_expiry_seconds

    async def _storage_call(self, operation, *args, **kwargs):
        try:
            return await operation(*args, **kwargs)
        except (ImageFailure, InputFailure, MediaFailure, e.VideoInputFailure) as exc:
            code = exc.code if exc.code in _ERRORS else "storage_unavailable"
            raise t.TrackerFailure(code) from None

    @staticmethod
    def _read_request(request: t.TrackerVideoInputRequest):
        return t.TrackerInputReadRequest(
            actor_id=request.actor_id,
            tracker_job_id=request.tracker_job_id,
            task_id=request.task_id,
        )

    async def input(
        self, request: t.TrackerInputReadRequest
    ) -> t.TrackerVideoInputStatus:
        work = await self.database.call("read_video_tracker_input", request)
        return t.TrackerVideoInputStatus.from_record(work.record)

    async def prepare(
        self, request: t.TrackerInputRequest
    ) -> t.TrackerVideoInputStatus:
        work = await self.database.call(
            "read_video_tracker_input", self._read_request(request)
        )
        record = work.record
        if (
            record.input_id != request.input_id
            or record.scope.request_digest != request.request_digest
        ):
            raise t.TrackerFailure("input_scope_mismatch")
        if record.status != "pending":
            return t.TrackerVideoInputStatus.from_record(record)

        source = work.source_facts
        await self._storage_call(self.storage.assert_versioning)
        identity, terminal_error = None, None
        try:
            identity = await self._storage_call(self.storage.head_source, source)
        except t.TrackerFailure as exc:
            code = "source_missing" if exc.code == "object_missing" else exc.code
            if code not in _TERMINAL_SOURCE:
                raise
            terminal_error = code
        if identity is not None and not isinstance(identity, e.ObjectIdentity):
            raise t.TrackerFailure("invalid_response")
        if identity is not None and (
            (source.object_identity is not None and identity != source.object_identity)
            or (source.file_size is not None and identity.byte_size != source.file_size)
        ):
            identity, terminal_error = None, "source_changed"
        elif identity is not None and identity.byte_size == 0:
            identity, terminal_error = None, "source_missing"

        prepared = t.PrepareTrackerInputRequest(
            **request.model_dump(),
            source_snapshot=source.source_snapshot,
            timetable_revision=source.timetable_revision,
            object_identity=identity,
            terminal_error=terminal_error,
        )
        updated = await self.database.call("prepare_video_tracker_input", prepared)
        return t.TrackerVideoInputStatus.from_record(updated)

    async def reference(
        self, request: t.TrackerSourceReferenceRequest
    ) -> t.TrackerVideoSourceReference:
        material = await self.database.call("reference_video_tracker_source", request)
        record = material.input
        plan = record.plan
        if record.status == "expired":
            raise t.TrackerFailure("input_expired")
        if record.status == "failed":
            raise t.TrackerFailure(record.error)
        if plan is None or record.status != "ready":
            raise t.TrackerFailure("input_not_ready")
        if plan.plan_digest != request.plan_digest:
            raise t.TrackerFailure("input_scope_mismatch")

        try:
            observed = await self._storage_call(
                self.storage.head_source, material.source_facts
            )
        except t.TrackerFailure as exc:
            if exc.code == "object_missing":
                raise t.TrackerFailure("source_missing") from None
            raise
        if observed != plan.input.source.object_identity:
            raise t.TrackerFailure("source_changed")
        await self._storage_call(self.storage.verify_blob, plan.input.blob)
        url = await self._storage_call(
            self.storage.sign_blob,
            plan.input.blob,
            expires_in=self.url_expiry_seconds,
        )
        blob = plan.input.blob
        return t.TrackerVideoSourceReference(
            input=t.TrackerVideoInputStatus.from_record(record),
            source_version=t.TrackerVideoObjectVersion(
                version_id=blob.version_id,
                etag=blob.etag,
                byte_size=blob.byte_size,
                sha256=blob.sha256,
            ),
            url=url,
            expires_in=self.url_expiry_seconds,
        )
