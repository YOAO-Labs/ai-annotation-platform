"""Bounded owner workflow for replay-safe bulk input manifest preparation."""

from __future__ import annotations

from pydantic import BaseModel

from aap_assets_images import contract as image
from aap_assets_input_manifests import contract as c

_TERMINAL_SOURCE_ERRORS = frozenset(
    {
        "source_missing",
        "source_changed",
        "source_not_ready",
        "not_image",
        "task_scope_changed",
    }
)


class ManifestControl:
    def __init__(self, database, images):
        self.database = database
        self.images = images

    async def _call(self, operation: str, request: BaseModel) -> BaseModel:
        return await self.database.call(operation, request)

    async def record(self, request: c.ManifestScope) -> c.ManifestHeader:
        return await self._call("read_accepted_image_input_manifest", request)

    async def page(self, request: c.ManifestPageRequest) -> c.ManifestPage:
        return await self._call("read_accepted_image_input_manifest_page", request)

    async def prepare(
        self, request: c.PrepareManifestMemberRequest
    ) -> c.ManifestMemberReceipt:
        page_request = c.ManifestPageRequest(
            job_id=request.job_id,
            actor_id=request.actor_id,
            project_id=request.project_id,
            selection_digest=request.selection_digest,
            cursor=request.ordinal - 1,
            page_size=1,
        )
        page = await self.page(page_request)
        if request.ordinal > page.manifest.selected_count:
            raise c.ManifestFailure("manifest_cursor_invalid")
        if (
            len(page.members) != 1
            or page.members[0].ordinal != request.ordinal
            or page.members[0].task_id != request.task_id
        ):
            raise c.ManifestFailure("manifest_scope_mismatch")
        member = page.members[0]
        if member.state != "unprepared":
            return c.ManifestMemberReceipt(
                job_id=request.job_id,
                actor_id=request.actor_id,
                project_id=request.project_id,
                selection_digest=request.selection_digest,
                member=member,
            )

        source = member.source
        if source is None:
            raise c.ManifestFailure("manifest_scope_mismatch")
        try:
            observed = await self.images.observe_source(
                image.ReadImageSourceRequest(
                    actor_id=request.actor_id, task_id=request.task_id
                ),
                expected_source_snapshot=source.source_snapshot,
            )
        except image.ImageFailure as exc:
            if exc.code in _TERMINAL_SOURCE_ERRORS:
                return await self._prepare_observation(request, source, None, exc.code)
            raise c.ManifestFailure(exc.code) from None
        if not isinstance(observed, image.ImageSource):
            raise c.ManifestFailure("invalid_response")
        if (
            observed.owner != source.owner
            or observed.task_id != source.task_id
            or observed.project_id != source.project_id
            or observed.source_generation != source.source_generation
            or observed.source_snapshot != source.source_snapshot
            or observed.file_path != source.file_path
            or observed.file_type != source.file_type
            or (
                source.object_identity is not None
                and observed.object_identity != source.object_identity
            )
            or observed.object_identity is None
            or observed.source_drifted
            or not observed.upload_ready
        ):
            return await self._prepare_observation(
                request, source, None, "source_changed"
            )
        return await self._prepare_observation(
            request, source, observed.object_identity, None
        )

    async def _prepare_observation(
        self,
        request: c.PrepareManifestMemberRequest,
        source: image.ImageSource,
        identity: image.ObjectIdentity | None,
        terminal_error: str | None,
    ) -> c.ManifestMemberReceipt:
        native_request = c.NativePrepareManifestMemberRequest(
            job_id=request.job_id,
            actor_id=request.actor_id,
            project_id=request.project_id,
            selection_digest=request.selection_digest,
            ordinal=request.ordinal,
            task_id=request.task_id,
            source_snapshot=source.source_snapshot,
            object_identity=identity,
            terminal_error=terminal_error,
        )
        return await self._call(
            "prepare_accepted_image_input_manifest_member", native_request
        )

    async def seal(self, request: c.SealManifestRequest) -> c.ManifestHeader:
        return await self._call("seal_accepted_image_input_manifest", request)
