"""Coordinate ZIP and legacy bucket ingestion without owning a transaction."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from pathlib import PurePosixPath
import logging
import re
from typing import Any, BinaryIO
from uuid import UUID, uuid4

from aap_assets_source.archive import ArchiveLimits, ArchiveSafetyError, SafeZipArchive
from aap_assets_source.frame_layout import (
    PreparedDatasetItem,
)
from aap_assets_source.scene_layout import (
    SceneLayoutInput,
    is_role_dir_name,
    plan_scenes,
)
from aap_assets_source.managed_original_contract import (
    ManagedOriginalFailure,
    ManagedOriginalFailureEvidence,
    ManagedOriginalMaterial,
    observed_current_head_match,
)
from aap_assets_source.ingest_contract import (
    INGEST_BATCH_SIZE,
    SCAN_REQUEST_DIGEST,
    ZIP_ENTRY_MAX_BYTES,
    ZIP_MAX_BYTES,
    ZIP_MAX_CENTRAL_DIRECTORY_ENTRIES,
    ZIP_MAX_FILES,
    IngestCommand,
    IngestBatchPlan,
    IngestDescriptor,
    IngestEntryCompletion,
    IngestFailure,
    IngestFailureDetail,
    IngestFinish,
    IngestFrameAssignment,
    IngestLayoutItem,
    IngestPlannedEntry,
    IngestPlannedScene,
    IngestProgress,
    IngestReceipt,
    IngestSceneMetadata,
    IngestScenePlan,
    IngestSourceEntry,
    IngestDataPort,
    require_ingest_scope,
)
from aap_assets_images.contract import ImageFailure

from .ingest_storage import (
    IngestEntryFailure,
    IngestStorage,
    IngestStorageFacts,
    run_in_thread,
)
from .image_admission import ImageAdmissionPort

logger = logging.getLogger("aap_assets_source_runtime.ingest_workflow")

_READ_BYTES = 8 * 1024 * 1024
_SCAN_IMAGE_EXTENSIONS = frozenset(
    {"jpg", "jpeg", "png", "bmp", "webp", "tiff", "tif", "gif", "svg"}
)
_SCAN_VIDEO_EXTENSIONS = frozenset({"mp4", "avi", "mov", "mkv", "wmv", "flv", "webm"})
_SCAN_POINT_CLOUD_EXTENSIONS = frozenset({"pcd", "bin", "las", "ply"})
_ERROR_MESSAGES = {
    "invalid_source_object": "Invalid source object path",
    "archive_entry_read_failed": "Archive entry could not be read",
    "storage_write_failed": "Source object could not be stored",
    "source_changed": "Source object changed during ingest",
    "source_missing": "Source object is missing",
    "source_read_failed": "Source object could not be read",
}


class IngestWorkflow:
    """Run all source work through the supplied five-capability data port."""

    def __init__(
        self,
        *,
        data: IngestDataPort,
        storage: IngestStorage,
        image_admission: ImageAdmissionPort,
    ) -> None:
        self._data = data
        self._storage = storage
        self._image_admission = image_admission

    async def upload_zip(
        self, command: IngestCommand, source: BinaryIO
    ) -> IngestReceipt:
        archive_digest, _compressed_size = await _sha256_stream(source)
        try:
            archive = SafeZipArchive(
                source,
                ArchiveLimits(
                    max_files=ZIP_MAX_FILES,
                    max_entry_bytes=ZIP_ENTRY_MAX_BYTES,
                    max_total_bytes=ZIP_MAX_BYTES,
                    max_compression_ratio=100,
                    max_central_directory_entries=ZIP_MAX_CENTRAL_DIRECTORY_ENTRIES,
                ),
                skip_unsafe_paths=True,
            )
        except ArchiveSafetyError as exc:
            raise _archive_failure(exc) from None

        try:
            visible_entries = [
                entry
                for entry in archive.entries
                if not _is_hidden(entry.normalized_path)
            ]
            hidden_count = len(archive.entries) - len(visible_entries)
            top_level_dirs = sorted(
                {
                    PurePosixPath(entry.normalized_path).parts[0]
                    for entry in visible_entries
                    if len(PurePosixPath(entry.normalized_path).parts) >= 2
                }
            )
            descriptor = IngestDescriptor(
                kind="zip",
                request_digest=archive_digest,
                total_in_zip=len(archive.entries),
                planned_entries=len(visible_entries),
                hidden_count=hidden_count,
                unsafe_count=len(archive.skipped_paths),
                top_level_dirs=top_level_dirs,
            )
            context = await self._data.begin(command, descriptor)
            _validate_context(command, descriptor, context)
            if context.state == "completed":
                assert context.result is not None
                return context.result

            progress = _initial_progress(context)
            cursor = context.next_index
            expected_errors: list[tuple[str, str]] = []
            for offset in range(0, len(visible_entries), INGEST_BATCH_SIZE):
                archive_batch = visible_entries[offset : offset + INGEST_BATCH_SIZE]
                proposals = []
                for archive_entry in archive_batch:
                    proposals.append(await _zip_source_entry(archive, archive_entry))
                batch_id = uuid4()
                plan = await self._data.prepare_batch(command, batch_id, proposals)
                _validate_plan(command, batch_id, proposals, plan, cursor, progress)

                completions: list[IngestEntryCompletion] = []
                image_facts: dict[UUID, IngestStorageFacts] = {}
                for archive_entry, proposal, planned in zip(
                    archive_batch, proposals, plan.entries, strict=True
                ):
                    if planned.status in {"pending", "added"}:
                        _validate_zip_target(command, proposal, planned)
                    if planned.status == "error":
                        assert planned.error_code is not None
                        expected_errors.append(
                            (_safe_label(proposal.source_name), planned.error_code)
                        )
                        continue
                    if planned.status != "pending":
                        continue
                    target: ManagedOriginalMaterial | None = None
                    operation = None
                    try:
                        target = await self._data.allocate(
                            _ingest_original_request(command, planned, batch_id)
                        )
                        operation = await self._data.open_operation(target.creation_id)
                        if operation.mode == "read":
                            facts = await self._storage.verify_managed(
                                target,
                                sha256=target.sha256,
                                size_bytes=target.size_bytes,
                                image=proposal.file_type == "image",
                            )
                            _validate_published_facts(proposal, facts)
                            await self._close_read_operation(target, operation)
                        else:
                            with archive.open(archive_entry.normalized_path) as member:
                                facts, proof = await self._storage.publish(
                                    member,
                                    target,
                                    content_type=planned.content_type,
                                    expected_size=proposal.file_size,
                                    expected_hash=proposal.content_hash,
                                    image=proposal.file_type == "image",
                                    on_prepared=lambda prepared: (
                                        self._data.original_prepared(
                                            target.creation_id, prepared.evidence()
                                        )
                                    ),
                                    on_writing=lambda: self._data.original_writing(
                                        target.creation_id
                                    ),
                                    on_written=lambda receipt: (
                                        self._data.original_written(
                                            target.creation_id,
                                            {
                                                "etag": receipt.etag,
                                                "version_id": receipt.version_id,
                                                "request_id": receipt.request_id,
                                                "response": receipt.response,
                                            },
                                        )
                                    ),
                                )
                            _validate_published_facts(proposal, facts)
                            await self._finish_original(target, proof)
                    except ManagedOriginalFailure as exc:
                        completions.append(
                            _failed_completion(planned, "storage_write_failed")
                        )
                        expected_errors.append(
                            (_safe_label(proposal.source_name), "storage_write_failed")
                        )
                        logger.info(
                            "Ingest original refused operation=%s code=%s",
                            command.operation_id,
                            exc.code,
                        )
                    except IngestEntryFailure as exc:
                        await self._fail_original(target, "write", exc.code, exc)
                        completions.append(_failed_completion(planned, exc.code))
                        expected_errors.append(
                            (_safe_label(proposal.source_name), exc.code)
                        )
                    except Exception as exc:  # noqa: BLE001 - ZIP reader failures are per-entry.
                        await self._fail_original(
                            target, "write", "archive_entry_read_failed", exc
                        )
                        completions.append(
                            _failed_completion(planned, "archive_entry_read_failed")
                        )
                        expected_errors.append(
                            (
                                _safe_label(proposal.source_name),
                                "archive_entry_read_failed",
                            )
                        )
                    else:
                        completions.append(_ready_completion(planned, facts))
                        if proposal.file_type == "image":
                            image_facts[planned.entry_id] = facts

                settled = await self._data.complete_batch(
                    command, batch_id, completions
                )
                _validate_completion(command, batch_id, plan, completions, settled)
                await self._admit_batch_images(plan, completions, image_facts)
                progress = settled.progress
                cursor += len(proposals)

            if (
                cursor != descriptor.planned_entries
                or progress.staged != cursor
                or progress.settled != cursor
            ):
                raise IngestFailure("invalid_response")

            scene_plan = None
            layout_digest = None
            scene_notes: list[str] = []
            if progress.added:
                layout_digest, scene_plan, scene_notes = await self._scene_layout(
                    command,
                    context.dataset_name,
                    context.is_temporal,
                    descriptor.top_level_dirs,
                )
            finish = IngestFinish(
                layout_snapshot_digest=layout_digest,
                scene_plan=scene_plan,
                scene_inference_notes=scene_notes,
            )
            receipt = await self._data.finish(command, finish)
            _validate_receipt(
                command, descriptor, progress, finish, receipt, expected_errors
            )
            return receipt
        finally:
            archive.close()

    async def scan(self, command: IngestCommand) -> IngestReceipt:
        descriptor = IngestDescriptor(
            kind="scan",
            request_digest=SCAN_REQUEST_DIGEST,
            total_in_zip=None,
            planned_entries=None,
        )
        context = await self._data.begin(command, descriptor)
        _validate_context(command, descriptor, context)
        if context.state == "completed":
            assert context.result is not None
            return context.result

        prefix = context.source_prefix
        progress = _initial_progress(context)
        cursor = context.next_index
        proposals: list[IngestSourceEntry] = []
        listed: list[tuple[str, str | None, int | None]] = []

        async for item in self._storage.iter_objects(prefix):
            if not item.key.startswith(prefix) or item.key.endswith("/"):
                continue
            relative = item.key[len(prefix) :]
            if not relative:
                continue
            proposals.append(
                _scan_source_entry(relative, item.key, item.size, item.etag)
            )
            listed.append((item.key, item.etag, item.size))
            if len(proposals) == INGEST_BATCH_SIZE:
                progress = await self._scan_batch(
                    command, prefix, proposals, listed, cursor, progress
                )
                cursor += len(proposals)
                proposals, listed = [], []

        if proposals:
            progress = await self._scan_batch(
                command, prefix, proposals, listed, cursor, progress
            )
            cursor += len(proposals)
        if progress.staged != cursor or progress.settled != cursor:
            raise IngestFailure("invalid_response")

        finish = IngestFinish(layout_snapshot_digest=None, scene_plan=None)
        receipt = await self._data.finish(command, finish)
        _validate_receipt(command, descriptor, progress, finish, receipt)
        return receipt

    async def _scan_batch(
        self,
        command: IngestCommand,
        prefix: str,
        proposals: list[IngestSourceEntry],
        listed: list[tuple[str, str | None, int | None]],
        cursor: int,
        progress: IngestProgress,
    ) -> IngestProgress:
        batch_id = uuid4()
        plan = await self._data.prepare_batch(command, batch_id, proposals)
        _validate_plan(command, batch_id, proposals, plan, cursor, progress)
        completions: list[IngestEntryCompletion] = []
        image_facts: dict[UUID, IngestStorageFacts] = {}
        for proposal, planned, (key, etag, size) in zip(
            proposals, plan.entries, listed, strict=True
        ):
            if planned.status == "error":
                continue
            if planned.status != "pending":
                continue
            if key != proposal.source_key or planned.file_path != key:
                raise IngestFailure("invalid_response")
            target: ManagedOriginalMaterial | None = None
            operation = None
            try:
                target = await self._data.allocate(
                    _ingest_original_request(command, planned, batch_id)
                )
                operation = await self._data.open_operation(target.creation_id)
                if operation.mode == "read":
                    facts = await self._storage.verify_managed(
                        target,
                        sha256=target.sha256,
                        size_bytes=target.size_bytes,
                        image=proposal.file_type == "image",
                    )
                    _validate_observed_facts(proposal, facts)
                    await self._close_read_operation(target, operation)
                else:
                    if target.mode == "adopt":
                        facts, proof = await self._storage.adopt(
                            target,
                            expected_size=proposal.file_size,
                            image=proposal.file_type == "image",
                        )
                    else:
                        facts, proof = await self._storage.publish_from_existing(
                            proposal.source_key,
                            target,
                            content_type=proposal.content_type,
                            expected_size=proposal.file_size,
                            expected_hash=proposal.content_hash,
                            image=proposal.file_type == "image",
                            on_prepared=lambda prepared: self._data.original_prepared(
                                target.creation_id, prepared.evidence()
                            ),
                            on_writing=lambda: self._data.original_writing(
                                target.creation_id
                            ),
                            on_written=lambda receipt: self._data.original_written(
                                target.creation_id,
                                {
                                    "etag": receipt.etag,
                                    "version_id": receipt.version_id,
                                    "request_id": receipt.request_id,
                                    "response": receipt.response,
                                },
                            ),
                        )
                    _validate_observed_facts(proposal, facts)
                    if target.mode == "adopt":
                        await self._data.original_prepared(
                            target.creation_id, proof.prepared
                        )
                        await self._data.original_writing(target.creation_id)
                        await self._data.original_written(
                            target.creation_id,
                            {
                                "etag": proof.etag,
                                "version_id": proof.version_id,
                                "request_id": proof.request_id,
                                "response": {},
                            },
                        )
                    await self._finish_original(target, proof)
            except ManagedOriginalFailure as exc:
                logger.info(
                    "Ingest adoption refused operation=%s code=%s",
                    command.operation_id,
                    exc.code,
                )
                completions.append(_failed_completion(planned, "source_read_failed"))
            except IngestEntryFailure as exc:
                await self._fail_original(target, "write", exc.code, exc)
                completions.append(_failed_completion(planned, exc.code))
            except Exception as exc:  # noqa: BLE001 - per-entry storage failures
                await self._fail_original(target, "write", "source_read_failed", exc)
                completions.append(_failed_completion(planned, "source_read_failed"))
            else:
                completions.append(_ready_completion(planned, facts))
                if proposal.file_type == "image":
                    image_facts[planned.entry_id] = facts
        settled = await self._data.complete_batch(command, batch_id, completions)
        _validate_completion(command, batch_id, plan, completions, settled)
        await self._admit_batch_images(plan, completions, image_facts)
        return settled.progress

    async def _finish_original(self, target, proof) -> None:
        verification = {
            "sha256": proof.sha256,
            "md5": proof.md5,
            "size_bytes": proof.size_bytes,
            "etag": proof.etag,
            "version_id": proof.version_id,
            "observation": (proof.verification or {}).get("observation"),
            "physical_settled": True,
        }
        matched = observed_current_head_match(
            proof.verification, size_bytes=proof.size_bytes
        )
        if matched is not None:
            verification["current_head_matches"] = matched
        await self._data.original_verified(target.creation_id, verification)

    async def _close_read_operation(self, target, operation) -> None:
        """Settle the read permit only after the readback handle has drained."""
        await self._data.close_operation(
            target.creation_id, operation.operation_id, True
        )

    async def _fail_original(
        self,
        target: ManagedOriginalMaterial | None,
        stage: str,
        code: str,
        exc: BaseException | None,
    ) -> None:
        """Durably settle a failed attempt; never publish its bytes."""
        if target is None:
            return
        evidence = _failure_evidence(stage, exc)
        try:
            await self._data.fail_original(
                target.creation_id,
                "unknown" if not evidence["physical_settled"] else "failed",
                _safe_error_code("ingest_" + code),
                evidence,
            )
        except Exception:
            logger.warning("Ingest creation failure could not be settled code=%s", code)

    async def _admit_batch_images(
        self,
        plan: IngestBatchPlan,
        completions: list[IngestEntryCompletion],
        facts_by_entry: dict[UUID, IngestStorageFacts],
    ) -> None:
        ready_ids = {
            completion.entry_id
            for completion in completions
            if completion.status == "ready"
        }
        for planned in plan.entries:
            if (
                planned.status != "pending"
                or planned.file_type != "image"
                or planned.entry_id not in ready_ids
            ):
                continue
            facts = facts_by_entry.get(planned.entry_id)
            if (
                facts is None
                or facts.object_identity is None
                or planned.item_id is None
                or planned.file_path is None
                or planned.publication_id is None
            ):
                raise IngestFailure("invalid_response")
            try:
                await self._image_admission.admit_dataset_item(
                    item_id=planned.item_id,
                    publication_id=planned.publication_id,
                    file_path=planned.file_path,
                    file_size=facts.file_size,
                    content_hash=facts.content_hash,
                    object_identity=facts.object_identity,
                    missing_is_skipped=True,
                )
            except ImageFailure as exc:
                raise _as_ingest_image_failure(exc) from None

    async def _scene_layout(
        self,
        command: IngestCommand,
        dataset_name: str,
        is_temporal: bool,
        top_level_dirs: list[str],
    ) -> tuple[str, IngestScenePlan | None, list[str]]:
        first_page = await self._data.read_layout(
            command, after_item_id=None, limit=500
        )
        require_ingest_scope(command, first_page)
        _validate_layout_page(command, None, first_page, None, None, None, None, None)
        snapshot = first_page.snapshot_digest
        scene_count = first_page.scene_count
        assigned_count = first_page.assigned_count
        total_items = first_page.total_items
        if total_items == 0:
            raise IngestFailure("invalid_response")
        if scene_count or assigned_count:
            portable = await run_in_thread(
                plan_scenes,
                SceneLayoutInput(
                    dataset_name=dataset_name,
                    items=(),
                    scene_count=scene_count,
                    assigned_count=assigned_count,
                    item_count=total_items,
                    mode="auto",
                    dry_run=False,
                ),
            )
            plan = _contract_scene_plan(portable)
            notes = _with_top_level_role_note(list(plan.notes), top_level_dirs)
            return snapshot, plan.model_copy(update={"notes": notes}), notes

        facts: list[IngestLayoutItem] = list(first_page.items)
        cursor: UUID | None = first_page.next_item_id
        previous_id: UUID | None = None
        while cursor is not None:
            page = await self._data.read_layout(
                command, after_item_id=cursor, limit=500
            )
            require_ingest_scope(command, page)
            _validate_layout_page(
                command,
                cursor,
                page,
                snapshot,
                scene_count,
                assigned_count,
                total_items,
                previous_id,
            )
            facts.extend(page.items)
            if page.next_item_id is None:
                cursor = None
            else:
                previous_id = cursor
                cursor = page.next_item_id
        if len(facts) != total_items:
            raise IngestFailure("invalid_response")

        input_facts = [
            PreparedDatasetItem(
                id=item.id,
                file_name=item.file_name,
                file_path=item.file_path,
                file_type=item.file_type,
                scene_id=item.scene_id,
                frame_index=item.frame_index,
                logical_path=item.logical_path,
            )
            for item in facts
        ]
        try:
            portable = await run_in_thread(
                plan_scenes,
                SceneLayoutInput(
                    dataset_name=dataset_name,
                    items=tuple(input_facts),
                    scene_count=scene_count,
                    assigned_count=assigned_count,
                    item_count=total_items,
                    mode="auto",
                    dry_run=False,
                ),
            )
        except ValueError as exc:
            if " scenes > 100;" not in str(exc):
                raise IngestFailure("invalid_response") from None
            if is_temporal:
                raise IngestFailure("temporal_scenes_missing") from None
            note = f"scene_inference skipped: {exc}"[:1024]
            return snapshot, None, _with_top_level_role_note([note], top_level_dirs)
        plan = _contract_scene_plan(portable)
        notes = _with_top_level_role_note(list(plan.notes), top_level_dirs)
        return snapshot, plan.model_copy(update={"notes": notes}), notes


def _initial_progress(context) -> IngestProgress:
    if context.next_index != 0:
        raise IngestFailure("invalid_response")
    return IngestProgress(
        operation_id=context.operation_id,
        actor_id=context.actor_id,
        dataset_id=context.dataset_id,
        staged=0,
        settled=0,
        added=0,
        deduped=0,
        skipped=0,
        error_count=0,
        linked_tasks=0,
    )


def _validate_context(command, descriptor, context) -> None:
    require_ingest_scope(command, context)
    if context.source_prefix != context.dataset_name + "/":
        raise IngestFailure("invalid_response")
    if context.state == "completed":
        if (
            context.result is None
            or context.descriptor.kind != descriptor.kind
            or context.descriptor.request_digest != descriptor.request_digest
        ):
            raise IngestFailure("invalid_response")
        _validate_receipt(command, descriptor, None, None, context.result)
    elif (
        context.result is not None
        or context.next_index != 0
        or context.descriptor != descriptor
    ):
        if context.result is None and context.descriptor != descriptor:
            raise IngestFailure("operation_conflict")
        raise IngestFailure("invalid_response")


def _validate_plan(command, batch_id, proposals, plan, cursor, previous) -> None:
    require_ingest_scope(command, plan)
    if plan.batch_id != batch_id or len(plan.entries) != len(proposals):
        raise IngestFailure("invalid_response")
    for index, (proposal, planned) in enumerate(
        zip(proposals, plan.entries, strict=True)
    ):
        if planned.index != cursor + index or not _same_source(proposal, planned):
            raise IngestFailure("invalid_response")
        if planned.status == "pending":
            if (
                not planned.item_id
                or (proposal.source_key is None and not planned.publication_id)
                or (
                    proposal.source_key is not None
                    and planned.publication_id is not None
                )
            ):
                raise IngestFailure("invalid_response")
        elif planned.status == "error" and planned.error_code is None:
            raise IngestFailure("invalid_response")
    expected_staged = cursor + len(proposals)
    immediate = [item.status for item in plan.entries]
    if plan.progress.staged != expected_staged:
        raise IngestFailure("invalid_response")
    if plan.progress.settled != previous.settled + sum(
        status != "pending" for status in immediate
    ):
        raise IngestFailure("invalid_response")
    expected = (
        previous.added + immediate.count("added"),
        previous.deduped + immediate.count("deduped"),
        previous.skipped + immediate.count("skipped"),
        previous.error_count + immediate.count("error"),
    )
    if (
        plan.progress.added,
        plan.progress.deduped,
        plan.progress.skipped,
        plan.progress.error_count,
    ) != expected:
        raise IngestFailure("invalid_response")
    if plan.progress.linked_tasks != previous.linked_tasks:
        raise IngestFailure("invalid_response")


def _validate_completion(command, batch_id, plan, completions, result) -> None:
    require_ingest_scope(command, result)
    if result.batch_id != batch_id:
        raise IngestFailure("invalid_response")
    ready = sum(item.status == "ready" for item in completions)
    failed = sum(item.status == "error" for item in completions)
    expected = plan.progress
    progress = result.progress
    added = progress.added - expected.added
    deduped = progress.deduped - expected.deduped
    skipped = progress.skipped - expected.skipped
    errors = progress.error_count - expected.error_count
    if (
        progress.staged != expected.staged
        or progress.settled != expected.settled + len(completions)
        or min(added, deduped, skipped, errors) < 0
        or added + deduped + skipped != ready
        or errors != failed
        or progress.linked_tasks < expected.linked_tasks
        or progress.settled != progress.staged
    ):
        raise IngestFailure("invalid_response")


def _validate_layout_page(
    command,
    cursor,
    page,
    snapshot,
    expected_scenes,
    expected_assigned,
    expected_total,
    previous_id,
) -> None:
    require_ingest_scope(command, page)
    if len(page.items) > 500:
        raise IngestFailure("invalid_response")
    if snapshot is not None and (
        page.snapshot_digest != snapshot
        or page.scene_count != expected_scenes
        or page.assigned_count != expected_assigned
        or page.total_items != expected_total
    ):
        raise IngestFailure("invalid_response")
    ids = [item.id for item in page.items]
    if ids != sorted(set(ids)) or (ids and cursor is not None and ids[0] <= cursor):
        raise IngestFailure("invalid_response")
    if previous_id is not None and ids and ids[0] <= previous_id:
        raise IngestFailure("invalid_response")
    if page.next_item_id is not None and (
        not ids or page.next_item_id != ids[-1] or page.next_item_id == cursor
    ):
        raise IngestFailure("invalid_response")


def _validate_receipt(
    command, descriptor, progress, finish, receipt, expected_errors=None
) -> None:
    require_ingest_scope(command, receipt)
    if (
        receipt.kind != descriptor.kind
        or receipt.request_digest != descriptor.request_digest
    ):
        raise IngestFailure("invalid_response")
    if progress is not None and receipt.total_in_zip != descriptor.total_in_zip:
        raise IngestFailure("invalid_response")
    if (
        finish is not None
        and receipt.scene_inference_notes != finish.scene_inference_notes
    ):
        raise IngestFailure("invalid_response")
    if descriptor.kind == "zip":
        if len(receipt.errors) != receipt.error_count or any(
            len(error.name) > 512
            or len(error.error) > 512
            or not error.error
            or _safe_label(error.name) != error.name
            or error.error not in _ERROR_MESSAGES.values()
            or any(
                ord(char) < 32 and char not in "\t" for char in error.name + error.error
            )
            for error in receipt.errors
        ):
            raise IngestFailure("invalid_response")
        if expected_errors is not None and [
            (error.name, error.error) for error in receipt.errors
        ] != [(name, _ERROR_MESSAGES[code]) for name, code in expected_errors]:
            raise IngestFailure("invalid_response")
    elif receipt.errors:
        raise IngestFailure("invalid_response")
    if progress is not None:
        expected_skipped = progress.skipped
        if descriptor.kind == "zip":
            expected_skipped += descriptor.hidden_count + descriptor.unsafe_count
        if (
            receipt.added != progress.added
            or receipt.deduped != progress.deduped
            or receipt.skipped != expected_skipped
            or receipt.error_count != progress.error_count
            or receipt.linked_tasks != progress.linked_tasks
        ):
            raise IngestFailure("invalid_response")


def _same_source(source: IngestSourceEntry, planned: IngestPlannedEntry) -> bool:
    return all(
        getattr(source, field) == getattr(planned, field)
        for field in (
            "source_name",
            "relpath",
            "source_key",
            "file_type",
            "content_type",
            "file_size",
            "content_hash",
            "width",
            "height",
            "error_code",
        )
    )


async def _zip_source_entry(archive: SafeZipArchive, entry) -> IngestSourceEntry:
    relpath = entry.normalized_path
    basename = PurePosixPath(relpath).name
    content_type = mimetypes.guess_type(basename)[0] or "application/octet-stream"
    file_type = _mime_file_type(content_type)
    digest = hashlib.md5(usedforsecurity=False)
    header = bytearray()
    total = 0
    try:
        with archive.open(relpath) as member:
            while True:
                chunk = member.read(_READ_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > ZIP_ENTRY_MAX_BYTES or total > entry.size_bytes:
                    raise ValueError("entry size mismatch")
                digest.update(chunk)
                if len(header) < 256 * 1024:
                    header.extend(chunk[: 256 * 1024 - len(header)])
                await asyncio.sleep(0)
        if total != entry.size_bytes:
            raise ValueError("entry size mismatch")
    except Exception:  # noqa: BLE001 - public errors never include ZIP reader internals.
        return IngestSourceEntry(
            source_name=_safe_label(entry.source_name),
            relpath=relpath,
            source_key=None,
            file_type=file_type,
            content_type=content_type,
            file_size=entry.size_bytes,
            content_hash=None,
            width=None,
            height=None,
            error_code="archive_entry_read_failed",
        )
    width, height = (None, None)
    if file_type == "image":
        from .ingest_storage import _dimensions

        width, height = _dimensions(bytes(header))
    return IngestSourceEntry(
        source_name=_safe_label(entry.source_name),
        relpath=relpath,
        source_key=None,
        file_type=file_type,
        content_type=content_type,
        file_size=total,
        content_hash=digest.hexdigest(),
        width=width,
        height=height,
    )


def _scan_source_entry(
    relative: str, key: str, size: int | None, etag: str | None
) -> IngestSourceEntry:
    basename = PurePosixPath(relative).name
    content_type = mimetypes.guess_type(basename)[0] or "application/octet-stream"
    extension = basename.rsplit(".", 1)[-1].lower() if "." in basename else ""
    if extension in _SCAN_IMAGE_EXTENSIONS:
        file_type = "image"
    elif extension in _SCAN_VIDEO_EXTENSIONS:
        file_type = "video"
    elif extension in _SCAN_POINT_CLOUD_EXTENSIONS:
        file_type = "point_cloud"
    else:
        file_type = "other"
    valid_size = size if type(size) is int and size >= 0 else 0
    return IngestSourceEntry(
        source_name=_safe_label(relative),
        relpath=relative,
        source_key=key,
        file_type=file_type,
        content_type=content_type,
        file_size=valid_size,
        content_hash=_md5_etag(etag),
        width=None,
        height=None,
        error_code=None
        if size is not None and valid_size == size
        else "source_read_failed",
    )


def _mime_file_type(content_type: str) -> str:
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    return "other"


def _is_hidden(relpath: str) -> bool:
    return relpath.startswith("__MACOSX/") or any(
        part.startswith(".") for part in PurePosixPath(relpath).parts
    )


def _validate_zip_target(
    command: IngestCommand,
    source: IngestSourceEntry,
    planned: IngestPlannedEntry,
) -> None:
    if (
        planned.item_id is None
        or planned.publication_id is None
        or planned.file_path
        != "dataset-sources/"
        + str(command.dataset_id)
        + "/"
        + str(planned.item_id)
        + "/"
        + str(planned.publication_id)
        + "/"
        + source.relpath
        or planned.file_name != PurePosixPath(source.relpath).name
    ):
        raise IngestFailure("invalid_response")


def _safe_error_code(code: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]+", "_", (code or "").lower()).strip("_")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"ingest_{cleaned or 'io_failed'}"
    return cleaned[:80]


def _failure_evidence(
    stage: str,
    exc: BaseException | None,
    *,
    receipt: dict[str, Any] | None = None,
    observation: dict[str, Any] | None = None,
    sha256: str | None = None,
    md5: str | None = None,
    size_bytes: int | None = None,
    current_head_matches: bool | None = None,
) -> dict[str, Any]:
    uncertain = bool(
        exc is not None
        and (
            getattr(exc, "storage_operation_unsettled", False)
            or getattr(exc, "outcome", None) == "unknown"
        )
    )
    if uncertain:
        physical_outcome = "unknown"
    elif receipt is not None:
        physical_outcome = "written"
    else:
        physical_outcome = "not_written"
    return ManagedOriginalFailureEvidence.build(
        physical_settled=not uncertain,
        physical_outcome=physical_outcome,
        stage=stage,
        receipt=receipt,
        observation=observation,
        sha256=sha256,
        md5=md5,
        size_bytes=size_bytes,
        physical_size_bytes=size_bytes,
        current_head_matches=current_head_matches,
    )


def _ingest_original_request(
    command: IngestCommand,
    planned: IngestPlannedEntry,
    batch_id: UUID,
) -> dict[str, Any]:
    mode = "write" if planned.source_key is None else "adopt"
    publication_id = planned.publication_id or uuid4()
    authority = {
        "kind": "dataset_source_ingest",
        "operation_id": str(command.operation_id),
        "batch_id": str(batch_id),
        "entry_index": planned.index,
        "item_id": str(planned.item_id),
    }
    if planned.publication_id is not None:
        authority["publication_id"] = str(planned.publication_id)
    return {
        "dataset_id": str(command.dataset_id),
        "item_id": str(planned.item_id),
        "publication_id": str(publication_id),
        "origin": "zip" if mode == "write" else "directory_copy",
        "mode": mode,
        "file_path": planned.file_path,
        "logical_path": planned.relpath,
        "file_type": planned.file_type,
        "content_type": planned.content_type,
        "max_bytes": planned.file_size,
        "authority": authority,
        "staging": None,
        "requested_by": str(command.actor_id),
    }


def _ready_completion(
    planned: IngestPlannedEntry, facts: IngestStorageFacts
) -> IngestEntryCompletion:
    return IngestEntryCompletion(
        entry_id=planned.entry_id,
        status="ready",
        publication_id=planned.publication_id,
        file_size=facts.file_size,
        content_hash=facts.content_hash,
        width=facts.width,
        height=facts.height,
        error_code=None,
    )


def _as_ingest_image_failure(exc: ImageFailure) -> IngestFailure:
    if exc.code in {"storage_unavailable", "source_unavailable"}:
        return IngestFailure("storage_unavailable")
    if exc.code in {
        "account_busy",
        "dataset_busy",
        "source_busy",
        "operation_conflict",
    }:
        return IngestFailure(exc.code)
    if exc.code in {
        "source_changed",
        "source_missing",
        "source_not_ready",
        "owner_changed",
        "dependency_changed",
    }:
        return IngestFailure("operation_conflict")
    if exc.code == "resource_budget_exceeded":
        return IngestFailure("resource_budget_exceeded")
    return IngestFailure("invalid_response")


def _failed_completion(planned: IngestPlannedEntry, code) -> IngestEntryCompletion:
    return IngestEntryCompletion(
        entry_id=planned.entry_id,
        status="error",
        publication_id=planned.publication_id,
        file_size=None,
        content_hash=None,
        width=None,
        height=None,
        error_code=code,
    )


def _validate_published_facts(
    proposal: IngestSourceEntry, facts: IngestStorageFacts
) -> None:
    if (
        facts.file_size != proposal.file_size
        or facts.content_hash != proposal.content_hash
    ):
        raise IngestEntryFailure("archive_entry_read_failed")
    if proposal.width is not None and (facts.width, facts.height) != (
        proposal.width,
        proposal.height,
    ):
        raise IngestEntryFailure("archive_entry_read_failed")
    if proposal.file_type != "image" and (
        facts.width is not None or facts.height is not None
    ):
        raise IngestEntryFailure("archive_entry_read_failed")


def _validate_observed_facts(
    proposal: IngestSourceEntry, facts: IngestStorageFacts
) -> None:
    if facts.file_size != proposal.file_size:
        raise IngestEntryFailure("source_changed")
    if (
        proposal.content_hash is not None
        and facts.content_hash != proposal.content_hash
    ):
        raise IngestEntryFailure("source_changed")
    if proposal.file_type != "image" and (
        facts.width is not None or facts.height is not None
    ):
        raise IngestEntryFailure("source_read_failed")


def _archive_failure(exc: ArchiveSafetyError) -> IngestFailure:
    reason = str(getattr(exc, "code", "archive_invalid"))
    detail_data = getattr(exc, "detail", None)
    if not isinstance(detail_data, dict):
        detail_data = {}
    budget_failure = any(
        marker in reason for marker in ("budget", "compression_ratio", "too_many")
    )
    code = "resource_budget_exceeded" if budget_failure else "archive_unsafe"
    if reason in {"archive_invalid", "invalid_zip", "zip_invalid"}:
        code = "archive_invalid"
    detail = IngestFailureDetail(
        reason=_safe_reason(reason),
        message=_safe_text(str(exc), 4096) or "ZIP archive could not be inspected",
        budget=_safe_optional_text(detail_data.get("budget"), 64),
        limit=_safe_number(detail_data.get("limit")),
        observed=_safe_number(detail_data.get("observed")),
        path=_safe_optional_text(detail_data.get("path"), 4096),
        conflicts_with=_safe_optional_text(detail_data.get("conflicts_with"), 4096),
    )
    return IngestFailure(code, detail)


async def _sha256_stream(source: BinaryIO) -> tuple[str, int]:
    try:
        source.seek(0)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = source.read(_READ_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > ZIP_MAX_BYTES:
                raise IngestFailure(
                    "resource_budget_exceeded",
                    IngestFailureDetail(
                        reason="compressed_size_exceeded",
                        message="ZIP archive exceeds the compressed byte limit",
                        budget="compressed_bytes",
                        limit=ZIP_MAX_BYTES,
                        observed=total,
                    ),
                )
            digest.update(chunk)
            await asyncio.sleep(0)
        source.seek(0)
        return digest.hexdigest(), total
    except IngestFailure:
        raise
    except Exception:
        raise IngestFailure("invalid_request") from None


def _md5_etag(etag: str | None) -> str | None:
    if not etag:
        return None
    value = etag.strip().strip('"')
    if len(value) != 32 or any(char not in "0123456789abcdefABCDEF" for char in value):
        return None
    return value.lower()


def _safe_text(value: str, limit: int) -> str:
    cleaned = "".join(
        char if char.isprintable() and ord(char) not in {0x7F} else "�"
        for char in value
    )
    return cleaned[:limit]


def _safe_label(value: str) -> str:
    return _safe_text(value, 512) or "source"


def _safe_optional_text(value, limit: int):
    if not isinstance(value, str):
        return None
    value = _safe_text(value, limit)
    return value or None


def _safe_reason(value: str) -> str:
    reason = "".join(
        char.lower() if char.isascii() and char.isalnum() else "_" for char in value
    ).strip("_")
    if not reason or not reason[0].isalpha():
        reason = "archive_" + reason
    return reason[:64]


def _safe_number(value):
    if type(value) is int:
        return value
    if type(value) is float and value == value and abs(value) != float("inf"):
        return value
    return None


def _contract_scene_plan(portable) -> IngestScenePlan:
    return IngestScenePlan(
        mode=portable.mode,
        dry_run=portable.dry_run,
        scenes=[
            IngestPlannedScene(
                id=scene.id,
                name=scene.name,
                source_format=scene.source_format,
                source_metadata=IngestSceneMetadata(
                    mode=scene.source_metadata["mode"],
                    group_key=scene.source_metadata["group_key"],
                ),
            )
            for scene in portable.scenes
        ],
        assignments=[
            IngestFrameAssignment(
                item_id=assignment.item_id,
                scene_id=assignment.scene_id,
                frame_index=assignment.frame_index,
            )
            for assignment in portable.assignments
        ],
        created_scenes=portable.created_scenes,
        assigned_items=portable.assigned_items,
        skipped_items=portable.skipped_items,
        notes=list(portable.notes),
    )


def _with_top_level_role_note(notes: list[str], top_level_dirs: list[str]) -> list[str]:
    reserved = sorted(name for name in top_level_dirs if is_role_dir_name(name))
    scene_dirs = sorted(name for name in top_level_dirs if not is_role_dir_name(name))
    if reserved and scene_dirs:
        notes.insert(
            0,
            "ZIP 顶层同时包含保留角色目录 "
            f"({', '.join(reserved)}) 与 scene 目录 ({', '.join(scene_dirs)}); "
            "多 scene 顶层目录不要使用 lidar/camera/calib/image/video 等角色名。"
            "格式说明见 docs-site/user-guide/datasets/import-formats.md",
        )
    return notes
