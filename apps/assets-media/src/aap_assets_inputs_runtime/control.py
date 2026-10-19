"""Short control operations for durable input acceptance and version-bound URLs."""

from __future__ import annotations

from aap_assets_images import contract as image
from aap_assets_inputs import contract as c


class InputControl:
    def __init__(
        self, database, image_control, storage, *, url_expiry_seconds: int = 900
    ):
        if not 1 <= url_expiry_seconds <= 3600:
            raise ValueError("invalid input URL lifetime")
        self.database = database
        self.images = image_control
        self.storage = storage
        self.url_expiry_seconds = url_expiry_seconds

    async def find(self, request: c.FindInputRequest) -> c.FoundInput:
        return await self.database.call("find_accepted_image_input", request)

    async def accept(self, request: c.FindInputRequest) -> c.InputRecord:
        existing = await self.find(request)
        if existing.input is not None:
            return existing.input
        await self.storage.assert_versioning()
        try:
            source = await self.images.observe_source(
                image.ReadImageSourceRequest(
                    actor_id=request.actor_id, task_id=request.task_id
                )
            )
        except image.ImageFailure as exc:
            raise c.InputFailure(exc.code) from None
        if (
            source.object_identity is None
            or source.source_drifted
            or not source.upload_ready
        ):
            raise c.InputFailure("source_not_ready")
        result = await self.database.call(
            "reserve_accepted_image_input",
            c.ReserveInputRequest(
                **request.model_dump(),
                source_snapshot=source.source_snapshot,
                object_identity=source.object_identity,
            ),
        )
        try:
            c.require_scope(request, result)
        except ValueError:
            raise c.InputFailure("invalid_response") from None
        return result

    async def record(self, request: c.InputRequest) -> c.InputRecord:
        return await self.database.call("read_accepted_image_input", request)

    async def _ready_work(self, request: c.InputRequest) -> c.InputWork:
        work = await self.database.call("read_accepted_image_input_work", request)
        if work.input.status == "expired":
            raise c.InputFailure("input_expired")
        if work.input.status == "failed":
            raise c.InputFailure(work.input.error or "input_corrupt")
        if work.input.status != "ready" or work.blob is None:
            raise c.InputFailure("input_not_ready")
        return work

    async def reference(self, request: c.InputRequest) -> c.InputReference:
        work = await self._ready_work(request)
        await self.storage.verify_blob(work.blob)
        url = await self.storage.sign_blob(
            work.blob, expires_in=self.url_expiry_seconds
        )
        return c.InputReference(
            input=work.input,
            url=url,
            version_id=work.blob.version_id,
            expires_in=self.url_expiry_seconds,
        )

    async def verify_source(self, request: c.InputRequest) -> c.InputRecord:
        work = await self._ready_work(request)
        try:
            observed = await self.images.observe_internal_source(
                image.InternalImageSourceRequest(
                    owner=work.source.owner,
                    expected_source_snapshot=work.source.source_snapshot,
                    object_identity=work.source.object_identity,
                )
            )
        except image.ImageFailure as exc:
            raise c.InputFailure(exc.code) from None
        if (
            observed.owner != work.source.owner
            or observed.source_generation != work.source.source_generation
            or observed.source_snapshot != work.source.source_snapshot
            or observed.object_identity != work.source.object_identity
            or observed.source_drifted
            or not observed.upload_ready
        ):
            raise c.InputFailure("source_changed")
        await self.storage.verify_blob(work.blob)
        return work.input
