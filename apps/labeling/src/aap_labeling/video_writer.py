"""Atomic policy for finalizing frozen video-tracker executions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, AsyncContextManager, Protocol
from uuid import UUID

from aap_preannotation_results.video_execution import ExecutionProof
from aap_preannotation_results.video_execution_plan import (
    VideoExecutionPlan,
    VideoExecutionWindow,
    VideoFrameResultRow,
)

from .video_writer_contract import (
    VideoWriterApplyArguments,
    VideoWriterCommand,
    VideoWriterFailure,
    VideoWriterFacts,
    VideoWriterLockMasksArguments,
    VideoWriterLockMasksData,
    VideoWriterPrepareArguments,
    VideoWriterPrepareData,
    VideoWriterPublicationArguments,
    VideoWriterReadWindowArguments,
    VideoWriterReadWindowData,
    VideoWriterReceipt,
    VideoWriterReceiptData,
    VideoWriterRejected,
    VideoWriterStatusArguments,
    VideoWriterStatusData,
    VideoWriterUnavailable,
    _staged_result,
    _validate_facts_for_command,
    collect_video_writer_mask_references,
    decode_video_writer_command,
    decode_video_writer_data_reply,
    make_video_writer_status_request,
    video_writer_command_digest,
    video_writer_data_arguments,
    video_writer_staged_json,
    validate_video_writer_checkpoint,
)

MaskVerifier = Callable[[dict[str, Any]], Awaitable[None]]


class VideoWriterTransaction(Protocol):
    """One borrowed transaction over the Labeling data function."""

    async def call(self, action: str, arguments: Any) -> Any:
        """Call one closed Native action and return its JSON envelope."""


class VideoWriterData(Protocol):
    """Open a borrowed transaction; the policy never commits independently."""

    def transaction(self) -> AsyncContextManager[VideoWriterTransaction]: ...


class VideoWriterManager:
    """Verify accepted execution facts and publish exactly its sealed output."""

    def __init__(
        self,
        data: VideoWriterData,
        *,
        verify_masks: MaskVerifier | None = None,
    ):
        self.data = data
        self.verify_masks = verify_masks

    async def execute(self, value: VideoWriterCommand | Any) -> VideoWriterReceipt:
        try:
            return await self._execute(value)
        except VideoWriterFailure:
            raise
        except Exception:
            # Includes an ambiguous transaction commit. Callers may query the
            # same operation ID and digest; inference is never retried here.
            raise VideoWriterUnavailable() from None

    async def _execute(self, value: VideoWriterCommand | Any) -> VideoWriterReceipt:
        command = decode_video_writer_command(value)
        request_digest = video_writer_command_digest(command)
        metadata = self._metadata(command, request_digest)

        async with self.data.transaction() as transaction:
            prepared = await self._call(
                transaction,
                "prepare",
                VideoWriterPrepareArguments.model_validate(metadata),
            )
            assert isinstance(prepared, VideoWriterPrepareData)
            if prepared.receipt is not None:
                return prepared.receipt
            facts = prepared.facts
            if facts is None:
                raise VideoWriterUnavailable()
            facts = _validate_facts_for_command(command, facts)

            rows: tuple[VideoFrameResultRow, ...] = ()
            if self._must_read_windows(command, facts):
                rows = await self._read_plan_rows(
                    transaction,
                    metadata=metadata,
                    command=command,
                    facts=facts,
                )

            if (
                facts.effective_outcome == "completed"
                and facts.job.job_kind == "correction"
                and not rows
            ):
                raise VideoWriterRejected("correction_empty_result")

            staged = _staged_result(command, facts, rows)
            references = collect_video_writer_mask_references(staged)
            object_keys = tuple(
                sorted({reference.object_key for reference in references})
            )
            if staged is not None:
                locked = await self._call(
                    transaction,
                    "lock_masks",
                    VideoWriterLockMasksArguments.model_validate(
                        {**metadata, "object_keys": list(object_keys)}
                    ),
                )
                assert isinstance(locked, VideoWriterLockMasksData)
                if object_keys:
                    if self.verify_masks is None:
                        raise VideoWriterUnavailable()
                    try:
                        await self.verify_masks(staged)
                    except VideoWriterFailure:
                        raise
                    except Exception:
                        # Mask client diagnostics may contain object paths or
                        # credentials. Return only a closed failure to callers.
                        raise VideoWriterUnavailable() from None

            staged_json, staged_digest, staged_size = video_writer_staged_json(staged)
            apply_arguments = VideoWriterApplyArguments.model_validate(
                {
                    **metadata,
                    "staged_result_json": staged_json,
                    "staged_sha256": staged_digest,
                    "staged_byte_size": staged_size,
                }
            )
            applied = await self._call(transaction, "apply", apply_arguments)
            assert isinstance(applied, VideoWriterReceiptData)
            return applied.receipt

    async def status(
        self,
        operation_id: UUID,
        execution: ExecutionProof | Mapping[str, Any],
        request_digest: str,
    ) -> VideoWriterReceipt | None:
        try:
            return await self._status(operation_id, execution, request_digest)
        except VideoWriterFailure:
            raise
        except Exception:
            raise VideoWriterUnavailable() from None

    async def publication(self, execution: ExecutionProof) -> VideoWriterReceipt | None:
        """Read an earlier generation's committed publication under current proof."""
        try:
            arguments = VideoWriterPublicationArguments(execution=execution)
            async with self.data.transaction() as transaction:
                result = await self._call(transaction, "read_publication", arguments)
                assert isinstance(result, VideoWriterStatusData)
                return result.receipt
        except VideoWriterFailure:
            raise
        except Exception:
            raise VideoWriterUnavailable() from None

    async def _status(
        self,
        operation_id: UUID,
        execution: ExecutionProof | Mapping[str, Any],
        request_digest: str,
    ) -> VideoWriterReceipt | None:
        request = make_video_writer_status_request(
            operation_id, execution, request_digest
        )
        arguments = VideoWriterStatusArguments.model_validate(
            request.model_dump(mode="json", by_alias=True, exclude={"schema_"})
        )
        async with self.data.transaction() as transaction:
            result = await self._call(transaction, "status", arguments)
            assert isinstance(result, VideoWriterStatusData)
            return result.receipt

    @staticmethod
    def _metadata(command: VideoWriterCommand, request_digest: str) -> dict[str, Any]:
        return {
            "operation_id": str(command.operation_id),
            "execution": command.execution.model_dump(mode="json", by_alias=True),
            "outcome": command.outcome,
            "request_digest": request_digest,
            "expected_input": (
                None
                if command.expected_input is None
                else command.expected_input.model_dump(mode="json", by_alias=True)
            ),
            "error_message": command.error_message,
        }

    @staticmethod
    def _must_read_windows(
        command: VideoWriterCommand, facts: VideoWriterFacts
    ) -> bool:
        if facts.execution_plan is None:
            return False
        if (
            facts.effective_outcome == "cancelled"
            and facts.job.job_kind == "correction"
        ):
            return False
        if command.outcome == "completed":
            return True
        # The Native owner derives accepted input from the frozen plan after
        # a crash; a redelivery need not carry the lost worker's local facts.
        return (
            command.outcome == "cancelled"
            and facts.job.job_kind == "tracking"
            and facts.acceptance is not None
        )

    async def _read_plan_rows(
        self,
        transaction: VideoWriterTransaction,
        *,
        metadata: dict[str, Any],
        command: VideoWriterCommand,
        facts: VideoWriterFacts,
    ) -> tuple[VideoFrameResultRow, ...]:
        plan = facts.execution_plan
        if plan is None:
            raise VideoWriterUnavailable()
        manifests: dict[int, Any] = {}
        attempts: dict[int, Any] = {}
        rows: list[VideoFrameResultRow] = []
        missing = False
        found_missing = False

        for window in plan.windows:
            read_arguments = VideoWriterReadWindowArguments.model_validate(
                {
                    "operation_id": metadata["operation_id"],
                    "execution": metadata["execution"],
                    "request_digest": metadata["request_digest"],
                    "window_ordinal": window.ordinal,
                }
            )
            data = await self._call(transaction, "read_window", read_arguments)
            assert isinstance(data, VideoWriterReadWindowData)
            attempt = data.attempt
            checkpoint = data.checkpoint
            if attempt is not None:
                if attempt.window_ordinal != window.ordinal:
                    raise VideoWriterUnavailable()
                attempts[window.ordinal] = attempt
            if checkpoint is None:
                missing = True
                found_missing = True
                continue
            if found_missing:
                # A cancelled output is only the frozen sealed prefix. A gap
                # followed by a later checkpoint cannot be published as one.
                raise VideoWriterRejected("checkpoint_incomplete")
            predecessor = self._predecessor_manifest(plan, window, manifests)
            manifest, payload = validate_video_writer_checkpoint(
                plan,
                checkpoint,
                window_ordinal=window.ordinal,
                predecessor=predecessor,
            )
            manifests[window.ordinal] = manifest
            if window.phase == "track":
                rows.extend(payload.frame_results)

        if facts.effective_outcome == "completed":
            if missing:
                raise VideoWriterRejected("checkpoint_incomplete")
            if command.frame_results and tuple(command.frame_results) != tuple(rows):
                raise VideoWriterRejected("candidate_invalid")
            return tuple(rows)

        if facts.effective_outcome != "cancelled":
            raise VideoWriterUnavailable()
        if facts.job.job_kind != "tracking":
            return ()

        sealed_prefix = tuple(rows)
        supplied = tuple(command.frame_results)
        if not supplied:
            return sealed_prefix
        if (
            len(supplied) < len(sealed_prefix)
            or supplied[: len(sealed_prefix)] != sealed_prefix
        ):
            raise VideoWriterRejected("candidate_invalid")
        tail = supplied[len(sealed_prefix) :]
        if not tail:
            return sealed_prefix

        tail_window = self._current_tail_window(
            plan,
            attempts,
            command.execution.generation,
            sealed_ordinals=set(manifests),
        )
        first_missing = next(
            (window for window in plan.windows if window.ordinal not in manifests),
            None,
        )
        if first_missing is None or tail_window.ordinal != first_missing.ordinal:
            raise VideoWriterRejected("candidate_invalid")
        for row in tail:
            if not tail_window.from_frame <= row.frame_index <= tail_window.to_frame:
                raise VideoWriterRejected("candidate_invalid")
            if (
                row.frame_index == plan.correction_frame
                and plan.job_kind == "correction"
            ):
                raise VideoWriterRejected("candidate_invalid")
            if row.geometry and row.geometry.get("type") != plan.output.geometry:
                raise VideoWriterRejected("candidate_invalid")
        return (*sealed_prefix, *tail)

    @staticmethod
    def _predecessor_manifest(plan, window, manifests):
        if window.phase == "discovery" or window.seed_source in {
            "none",
            "prompt",
            "source_geometry",
            "correction",
        }:
            return None
        if window.seed_source == "discovery":
            predecessor = next(
                (item for item in plan.windows if item.phase == "discovery"), None
            )
        elif window.seed_source in {"continuation", "session"}:
            candidates = [
                item
                for item in plan.windows
                if item.phase == "track"
                and item.branch == window.branch
                and item.ordinal < window.ordinal
            ]
            predecessor = candidates[-1] if candidates else None
        else:
            raise VideoWriterUnavailable()
        if predecessor is None:
            raise VideoWriterUnavailable()
        manifest = manifests.get(predecessor.ordinal)
        if manifest is None:
            raise VideoWriterRejected("checkpoint_incomplete")
        return manifest

    @staticmethod
    def _current_tail_window(
        plan: VideoExecutionPlan,
        attempts: Mapping[int, Any],
        generation: int,
        *,
        sealed_ordinals: set[int],
    ) -> VideoExecutionWindow:
        candidates = [
            window
            for ordinal, attempt in attempts.items()
            for window in plan.windows
            if window.ordinal == ordinal
            and ordinal not in sealed_ordinals
            and window.phase == "track"
            and attempt.generation == generation
            and attempt.mode == "execute"
            and attempt.state in {"started", "capturing"}
        ]
        if len(candidates) != 1:
            raise VideoWriterRejected("candidate_invalid")
        return candidates[0]

    async def _call(
        self,
        transaction: VideoWriterTransaction,
        action: str,
        arguments: Any,
    ):
        typed = video_writer_data_arguments(action, arguments)
        try:
            raw = await transaction.call(action, typed)
        except VideoWriterFailure:
            raise
        except Exception:
            raise VideoWriterUnavailable() from None
        return decode_video_writer_data_reply(action, typed, raw)


__all__ = [
    "VideoWriterData",
    "VideoWriterManager",
    "VideoWriterTransaction",
]
