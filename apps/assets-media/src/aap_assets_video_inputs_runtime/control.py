"""Bounded video admission observations and exact-version frame references."""

from __future__ import annotations

from typing import get_args

from aap_assets_images.contract import ImageFailure
from aap_assets_inputs.contract import InputFailure
from aap_assets_media.contract import MediaFailure
from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs import values as v

_TERMINAL_SOURCE = frozenset(
    {"source_missing", "source_changed", "source_not_ready", "not_video"}
)
_ERRORS = frozenset(get_args(c.ErrorCode))


class VideoInputControl:
    def __init__(self, database, storage, *, url_expiry_seconds=900):
        if type(url_expiry_seconds) is not int or not 1 <= url_expiry_seconds <= 3600:
            raise ValueError("invalid video frame URL lifetime")
        self.database, self.storage = database, storage
        self.url_expiry_seconds = url_expiry_seconds

    async def _storage_call(self, operation, *args, **kwargs):
        try:
            return await operation(*args, **kwargs)
        except (ImageFailure, InputFailure, MediaFailure) as exc:
            raise c.VideoInputFailure(
                exc.code if exc.code in _ERRORS else "storage_unavailable"
            ) from None

    async def manifest(self, request: c.VideoManifestScope) -> c.VideoManifestHeader:
        return await self.database.call("read_accepted_video_manifest", request)

    async def members(self, request: c.VideoManifestPageRequest) -> c.VideoManifestPage:
        return await self.database.call("read_accepted_video_manifest_page", request)

    async def prepare(
        self, request: c.PrepareVideoMemberRequest
    ) -> c.VideoMemberReceipt:
        page = await self.members(
            c.VideoManifestPageRequest(
                job_id=request.job_id,
                actor_id=request.actor_id,
                project_id=request.project_id,
                selection_digest=request.selection_digest,
                cursor=request.ordinal - 1,
                page_size=1,
            )
        )
        if not page.members or (
            page.members[0].task.task_id != request.task_id
            or page.members[0].ordinal != request.ordinal
        ):
            raise c.VideoInputFailure("manifest_scope_mismatch")
        member = page.members[0]
        if member.state != "unprepared":
            return c.VideoMemberReceipt(manifest=page.manifest, member=member)
        source = member.source
        if source is None:
            raise c.VideoInputFailure("invalid_response")
        await self._storage_call(self.storage.assert_versioning)
        identity, error = None, None
        try:
            identity = await self._storage_call(self.storage.head_source, source)
        except (ImageFailure, InputFailure, MediaFailure, c.VideoInputFailure) as exc:
            code = "source_missing" if exc.code == "object_missing" else exc.code
            if code not in _TERMINAL_SOURCE:
                raise c.VideoInputFailure(code) from None
            error = code
        if identity is not None and not isinstance(identity, v.ObjectIdentity):
            raise c.VideoInputFailure("invalid_response")
        if identity is not None and (
            (source.object_identity is not None and identity != source.object_identity)
            or (source.file_size is not None and identity.byte_size != source.file_size)
        ):
            identity, error = None, "source_changed"
        elif identity is not None and identity.byte_size == 0:
            identity, error = None, "source_missing"
        observation = c.ObserveVideoMemberRequest(
            **request.model_dump(),
            source_snapshot=source.source_snapshot,
            timetable_revision=source.timetable_revision,
            object_identity=identity,
            terminal_error=error,
        )
        return await self.database.call("prepare_accepted_video_member", observation)

    async def seal(self, request: c.SealVideoManifestRequest) -> c.VideoManifestHeader:
        return await self.database.call("seal_accepted_video_manifest", request)

    async def input(self, request: c.ReadVideoInputRequest) -> c.VideoInputRecord:
        return await self.database.call("read_accepted_video_input", request)

    async def verify(self, request: c.LockVideoInputRequest) -> c.VideoInputRecord:
        work = await self.database.call(
            "read_accepted_video_input_work",
            c.ReadVideoInputRequest(**request.model_dump(exclude={"plan_digest"})),
        )
        record = work.input
        if record.status == "expired":
            raise c.VideoInputFailure("input_expired")
        if record.status == "failed":
            raise c.VideoInputFailure(record.error)
        if record.status != "ready" or record.plan is None:
            raise c.VideoInputFailure("input_not_ready")
        if record.plan.plan_digest != request.plan_digest:
            raise c.VideoInputFailure("input_scope_mismatch")
        try:
            observed = await self._storage_call(self.storage.head_source, work.source)
        except c.VideoInputFailure as exc:
            if exc.code == "object_missing":
                raise c.VideoInputFailure("source_missing") from None
            raise
        if observed != record.source.object_identity:
            raise c.VideoInputFailure("source_changed")
        await self._storage_call(self.storage.verify_blob, record.plan.input.blob)
        return record

    async def times(self, request: v.FramePageRequest) -> v.FrameTimePage:
        return await self.database.call("read_accepted_video_times", request)

    async def require_frame(self, request: c.VideoFrameRequest) -> c.VideoFrameRecord:
        return await self.database.call("require_accepted_video_frame", request)

    async def frame(self, request: c.VideoFrameRequest) -> c.VideoFrameRecord:
        return await self.database.call("read_accepted_video_frame", request)

    async def reference(self, request: c.VideoFrameRequest) -> v.FrameReference:
        frame = await self.frame(request)
        if frame.status == "expired":
            raise c.VideoInputFailure("input_expired")
        if frame.status == "failed":
            raise c.VideoInputFailure(frame.error)
        if frame.status != "ready" or frame.blob is None:
            raise c.VideoInputFailure("frame_not_ready")
        await self._storage_call(self.storage.verify_blob, frame.blob)
        url = await self._storage_call(
            self.storage.sign_blob, frame.blob, expires_in=self.url_expiry_seconds
        )
        return v.FrameReference(
            plan=frame.plan,
            entry=frame.entry,
            blob=frame.blob,
            url=url,
            expires_in=self.url_expiry_seconds,
        )
