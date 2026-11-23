"""Lease-fenced, bounded execution of accepted Dataset imports."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, suppress
from datetime import datetime, timezone
import logging
import os
import re
import threading
import time
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from yoao_assets_source.connectors import SourceChangedError
from yoao_assets_source.import_contract import (
    ImportEntry,
    ImportEntryCompletion,
    ImportFailure,
    ImportSourceObject,
    ImportState,
)
from yoao_assets_source.import_verification_contract import (
    ImportVerificationRequest,
    ImportVerificationResult,
)
from yoao_assets_images.contract import ImageFailure, ObjectIdentity
from yoao_assets_source.managed_original_contract import (
    ManagedOriginalFailure,
    ManagedOriginalFailureEvidence,
)
from yoao_assets_source.saved_io import SavedIOError

from .config import ImportConfig
from .database import ImportDatabase, ImportOriginalMaterial
from .leased_source import LeasedSource, as_import_failure
from .storage import (
    DestinationStorage,
    DestinationWriteError,
    TransferResult,
    SourceReadError,
    _object_identity_from_verified,
    _thread,
)
from .verification import (
    ExpectedOriginal,
    ImportVerifier,
    VerificationFailed,
    VerificationRejected,
    VerificationUnavailable,
)

logger = logging.getLogger("yoao_assets_source_import.workflow")
_CHUNKED_PART_ERROR_CODES = {"account_busy", "dataset_busy"}
_TRANSIENT_SQLSTATES = {
    "08000",
    "08001",
    "08003",
    "08006",
    "40001",
    "40P01",
    "55P03",
    "57014",
}
_HEARTBEAT_RETRY_BASE_SECONDS = 0.05
_HEARTBEAT_RETRY_MAX_SECONDS = 2.0
_IMAGE_EXTS = {"jpg", "jpeg", "png", "bmp", "webp", "tiff", "tif", "gif", "svg"}
_VIDEO_EXTS = {"mp4", "avi", "mov", "mkv", "wmv", "flv", "webm"}
_POINTCLOUD_EXTS = {"pcd", "bin", "las", "ply"}
_SOURCE_ITEM_ERRORS = {
    "invalid_source_object": "invalid_source_object",
    "source_not_found": "source_not_found",
    "source_changed": "source_changed",
    "source_read_failed": "source_read_failed",
    "download_interrupted": "source_read_failed",
    "destination_write_failed": "destination_write_failed",
}


class WorkerStopping(Exception):
    """The process is shutting down before it can safely claim more work."""


class LeaseUnavailable(Exception):
    """The worker can no longer confirm ownership and must leave the job leased."""


class PersistenceBusy(Exception):
    """Settlement contention exhausted retries without advancing the cursor."""


class LeaseKeeper:
    """Renew a native lease while long-running connector and object I/O is active."""

    def __init__(
        self,
        database: ImportDatabase,
        config: ImportConfig,
        state: ImportState,
        shutdown: asyncio.Event,
    ) -> None:
        assert state.lease_token is not None
        self._database = database
        self._config = config
        self._state = state
        self._shutdown = shutdown
        self._thread_stop = threading.Event()
        self._interrupted: ImportFailure | None = None
        self._unavailable = False
        self._task: asyncio.Task[None] | None = None
        self._lease_deadline = self._deadline_for(state.lease_expires_at)

    @staticmethod
    def _deadline_for(expires_at: datetime | None) -> float:
        if expires_at is None:
            return time.monotonic()
        remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
        return time.monotonic() + max(0.0, remaining)

    def _remaining_lease(self) -> float:
        return self._lease_deadline - time.monotonic()

    def _stop_unavailable(self) -> None:
        self._unavailable = True
        self._thread_stop.set()

    async def _retry_delay(self, failures: int) -> bool:
        """Retry transient contention only while the confirmed lease can be live."""
        remaining = self._remaining_lease()
        if remaining <= 0:
            self._stop_unavailable()
            return False
        delay = min(
            _HEARTBEAT_RETRY_BASE_SECONDS * 2 ** min(failures - 1, 6),
            _HEARTBEAT_RETRY_MAX_SECONDS,
        )
        if delay >= remaining:
            self._stop_unavailable()
            return False
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=delay)
        except TimeoutError:
            if self._remaining_lease() <= 0:
                self._stop_unavailable()
                return False
            return True
        self._thread_stop.set()
        return False

    async def __aenter__(self) -> LeaseKeeper:
        self._task = asyncio.create_task(
            self._run(), name=f"import-lease-{self._state.job_id}"
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        self._thread_stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    def check(self) -> None:
        if self._interrupted is not None:
            raise self._interrupted
        if self._unavailable:
            raise LeaseUnavailable
        if self._shutdown.is_set():
            self._thread_stop.set()
            raise WorkerStopping
        if self._thread_stop.is_set():
            raise WorkerStopping
        if self._remaining_lease() <= 0:
            self._stop_unavailable()
            raise LeaseUnavailable

    async def _run(self) -> None:
        while not self._shutdown.is_set():
            remaining = self._remaining_lease()
            if remaining <= 0:
                self._stop_unavailable()
                return
            if remaining > self._config.heartbeat_seconds:
                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(),
                        timeout=self._config.heartbeat_seconds,
                    )
                    self._thread_stop.set()
                    return
                except TimeoutError:
                    pass

            failures = 0
            while not self._shutdown.is_set():
                if self._remaining_lease() <= 0:
                    self._stop_unavailable()
                    return
                try:
                    async with asyncio.timeout_at(self._lease_deadline):
                        async with self._database.transaction() as data:
                            if self._shutdown.is_set():
                                self._thread_stop.set()
                                return
                            renewed = await data.heartbeat(
                                self._state.job_id,
                                self._state.lease_token,
                                self._config.lease_seconds,
                            )
                            if renewed.lease_token != self._state.lease_token:
                                raise ImportFailure("lease_lost")
                            _require_same_scope(self._state, renewed)
                    if renewed.lease_expires_at is None:
                        raise ImportFailure("lease_lost")
                    self._lease_deadline = self._deadline_for(renewed.lease_expires_at)
                    if self._remaining_lease() <= 0:
                        raise ImportFailure("lease_lost")
                    break
                except ImportFailure as exc:
                    if exc.code != "dataset_busy":
                        self._interrupted = exc
                        self._thread_stop.set()
                        return
                    failures += 1
                    if not await self._retry_delay(failures):
                        return
                except TimeoutError:
                    self._stop_unavailable()
                    return
                except Exception:
                    self._stop_unavailable()
                    return
        self._thread_stop.set()


class ImportWorkflow:
    """Execute a claimed import; only the builtin destination is written locally."""

    def __init__(
        self,
        database: ImportDatabase,
        storage: DestinationStorage,
        config: ImportConfig,
        *,
        source: LeasedSource | None = None,
        shutdown: asyncio.Event | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._config = config
        self._source = source
        self._verifier = None
        self._shutdown = shutdown or asyncio.Event()

    def _leased_source(self) -> LeasedSource:
        if self._source is None:
            self._source = LeasedSource(self._config)
        return self._source

    async def run_claimed(self, state: ImportState) -> None:
        """Run one claimed lease; database transport failures leave it recoverable."""
        if state.lease_token is None or state.stage not in {"enumerating", "importing"}:
            raise ImportFailure("invalid_response")
        async with LeaseKeeper(
            self._database, self._config, state, self._shutdown
        ) as keeper:
            try:
                if state.stage == "enumerating":
                    objects = await _thread(
                        self._leased_source().list, state, keeper.check
                    )
                    keeper.check()
                    state = await self._save_manifest(state, objects)

                if state.stage != "importing" or state.total is None:
                    raise ImportFailure("invalid_response")
                await self._copy_manifest(state, keeper)
                keeper.check()
                await self._finish_completed(state)
            except ValidationError:
                raise ImportFailure("invalid_response") from None

    async def _save_manifest(
        self, state: ImportState, objects: list[ImportSourceObject]
    ) -> ImportState:
        assert state.lease_token is not None
        async with self._database.transaction() as data:
            saved = await data.save_manifest(state.job_id, state.lease_token, objects)
            _require_same_scope(state, saved)
        return saved

    async def _next_entry(
        self,
        state: ImportState,
        publication_id: UUID,
    ) -> ImportEntry | None:
        assert state.lease_token is not None
        async with self._database.transaction() as data:
            entry = await data.next_entry(
                state.job_id, state.lease_token, publication_id
            )
            if entry is None:
                if state.total is None or state.cursor != state.total:
                    raise ImportFailure("invalid_response")
            else:
                if state.total is None or state.cursor >= state.total:
                    raise ImportFailure("invalid_response")
                _validate_entry_scope(entry, state, publication_id)
        return entry

    async def _finish_completed(self, state: ImportState) -> ImportState:
        assert state.lease_token is not None
        async with self._database.transaction() as data:
            finished = await data.finish(state.job_id, state.lease_token, None)
            _require_same_scope(state, finished)
            if finished.stage != "completed":
                raise ImportFailure("invalid_response")
        return finished

    async def _copy_manifest(
        self,
        initial_state: ImportState,
        keeper: LeaseKeeper,
    ) -> None:
        assert initial_state.lease_token is not None
        state = initial_state
        attempted_bytes = 0
        while state.cursor < (state.total or 0):
            keeper.check()
            publication_id = uuid4()
            try:
                entry = await self._next_entry(state, publication_id)
            except ImportFailure as exc:
                if exc.code != "invalid_source_object":
                    raise
                state = await self._complete_pending_error_with_retry(
                    state,
                    publication_id,
                    _item_error("invalid_source_object"),
                    keeper,
                )
                continue
            if entry is None:
                raise ImportFailure("invalid_response")
            file_type = _infer_file_type(entry.file_name)
            stream_bytes = 0
            object_identity: ObjectIdentity | None = None
            if file_type != entry.file_type:
                completion = ImportEntryCompletion(
                    status="error", error_code="invalid_source_object"
                )
            else:
                completion, stream_bytes, object_identity = await self._transfer_entry(
                    entry,
                    keeper,
                    state=state,
                    byte_budget=max(
                        0,
                        state.settings_snapshot.dataset_import_max_total_bytes
                        - state.committed_bytes
                        - attempted_bytes,
                    ),
                )
            if (
                entry.file_type == "image"
                and completion.status == "copied"
                and object_identity is None
            ):
                raise ImportFailure("invalid_response")

            previous_committed_bytes = state.committed_bytes
            state = await self._complete_with_retry(
                state,
                entry,
                completion,
                keeper,
                object_identity=object_identity,
            )
            if stream_bytes and state.committed_bytes == previous_committed_bytes:
                attempted_bytes += stream_bytes
        if state.cursor != state.total:
            raise ImportFailure("invalid_response")

    async def _transfer_entry(
        self,
        entry: ImportEntry,
        keeper: LeaseKeeper,
        *,
        state: ImportState,
        byte_budget: int,
    ) -> tuple[ImportEntryCompletion, int, ObjectIdentity | None]:
        attempted = 0
        written_receipt = None
        scope: dict[str, Any] | None = None

        def count_bytes(amount: int) -> None:
            nonlocal attempted
            attempted += amount

        async def record_written(receipt) -> None:
            nonlocal written_receipt
            written_receipt = receipt.evidence()
            await self._database.original_written(scope, written_receipt)

        target: ImportOriginalMaterial | None = None
        operation = None
        try:
            keeper.check()
            target = await self._database.allocate(
                entry, _original_request(entry, state, byte_budget)
            )
            if (
                target.dataset_id != state.dataset_id
                or target.file_path != entry.file_path
                or target.publication_id != entry.publication_id
            ):
                raise ImportFailure("invalid_response")
            scope = {
                "job_id": str(entry.job_id),
                "lease_token": str(entry.lease_token),
                "entry_index": entry.index,
                "publication_id": str(entry.publication_id),
                "item_id": str(entry.item_id),
                "creation_id": str(target.creation_id),
                "operation_id": None,
            }
            keeper.check()
            if target.status in {"written", "verified"}:
                verification_scope = {
                    "job_id": str(entry.job_id),
                    "lease_token": str(entry.lease_token),
                    "entry_index": entry.index,
                    "item_id": str(entry.item_id),
                    "creation_id": str(target.creation_id),
                }
                if target.status == "written":
                    operation = await self._database.resume_original(verification_scope)
                else:
                    verification_scope["reservation_id"] = str(uuid4())
                    for attempt in range(5):
                        keeper.check()
                        try:
                            operation = await self._database.reserve_original_read(
                                verification_scope
                            )
                            break
                        except Exception as exc:
                            if not _transient_database_error(exc):
                                raise
                            if attempt == 4:
                                raise LeaseUnavailable from None
                            await asyncio.sleep(
                                min(
                                    0.1 * (2**attempt),
                                    max(0, keeper._remaining_lease()),
                                )
                            )
                    else:
                        raise LeaseUnavailable
            else:
                operation = await self._database.open_operation(
                    entry, target.creation_id
                )
            scope["operation_id"] = str(operation.operation_id)
            if operation.mode == "read" or target.status == "written":
                result = await self._verified_reuse(target, operation, entry, keeper)
                return (
                    ImportEntryCompletion(
                        status="copied",
                        verification_operation_id=operation.operation_id,
                        file_size=result.file_size,
                        content_hash=result.content_hash,
                        width=result.dimensions[0] if result.dimensions else None,
                        height=result.dimensions[1] if result.dimensions else None,
                    ),
                    attempted,
                    result.object_identity,
                )
            keeper.check()
            source = self._leased_source()
            if target.connection_id is not None:
                attempted = entry.source.size
                expected = await _thread(source.copy, scope, size=entry.source.size)
                keeper.check()
                verified = await self._verify_original(
                    entry, target, operation, keeper, expected
                )
                result = _verified_result(verified, entry.file_type)
            else:
                stream_scope = ExitStack()
                try:
                    stream = await _thread(
                        stream_scope.enter_context,
                        source.open(scope, size=entry.source.size),
                    )
                    keeper.check()
                    result = await self._storage.transfer(
                        stream,
                        entry,
                        target=target,
                        byte_budget=byte_budget,
                        before_io=keeper.check,
                        on_bytes=count_bytes,
                        on_prepared=lambda prepared: self._database.original_prepared(
                            scope, prepared.evidence()
                        ),
                        on_writing=lambda: self._database.original_writing(scope),
                        on_written=record_written,
                        on_verify=lambda expected: self._verify_original(
                            entry, target, operation, keeper, expected
                        ),
                    )
                except BaseException as exc:
                    await _drain_source(stream_scope, exc)
                    raise
                else:
                    await _drain_source(stream_scope, None)
            if entry.file_type == "image":
                keeper.check()
                if result.object_identity is None:
                    raise ImportFailure("invalid_response")
            return (
                ImportEntryCompletion(
                    status="copied",
                    verification_operation_id=operation.operation_id,
                    file_size=result.file_size,
                    content_hash=result.content_hash,
                    width=result.dimensions[0] if result.dimensions else None,
                    height=result.dimensions[1] if result.dimensions else None,
                ),
                attempted,
                result.object_identity,
            )
        except SavedIOError as exc:
            code = _SOURCE_ITEM_ERRORS.get(exc.code)
            await self._fail_original(
                target,
                scope,
                "write",
                code or "source_read_failed",
                exc,
                written_receipt,
            )
            if code is None:
                raise as_import_failure(exc) from None
            return _item_error(code), attempted, None
        except SourceChangedError as exc:
            await self._fail_original(
                target, scope, "write", "source_changed", exc, written_receipt
            )
            return _item_error("source_changed"), attempted, None
        except SourceReadError as exc:
            await self._fail_original(
                target, scope, "write", "source_read_failed", exc, written_receipt
            )
            return _item_error("source_read_failed"), attempted, None
        except DestinationWriteError as exc:
            await self._fail_original(
                target, scope, "write", "destination_write_failed", exc, written_receipt
            )
            return _item_error("destination_write_failed"), attempted, None
        except ManagedOriginalFailure as exc:
            logger.info(
                "Dataset import allocation refused job=%s code=%s",
                entry.job_id,
                exc.code,
            )
            await self._fail_original(
                target, scope, "open", "allocation_refused", exc, written_receipt
            )
            return _item_error("destination_write_failed"), attempted, None
        except (WorkerStopping, LeaseUnavailable, ImportFailure):
            raise
        except ValidationError:
            raise ImportFailure("invalid_response") from None
        except Exception as exc:
            logger.info(
                "Dataset import item I/O failed job=%s kind=%s",
                entry.job_id,
                type(exc).__name__,
            )
            await self._fail_original(
                target, scope, "write", "source_read_failed", exc, written_receipt
            )
            return _item_error("source_read_failed"), attempted, None

    async def _verified_reuse(
        self,
        target: ImportOriginalMaterial,
        operation,
        entry: ImportEntry,
        keeper: LeaseKeeper,
    ):
        """Read back an already verified creation; never claim it again."""
        if (
            target.sha256 is None
            or target.size_bytes is None
            or target.size_bytes != entry.source.size
        ):
            raise ImportFailure("invalid_response")
        verified = await self._verify_original(
            entry,
            target,
            operation,
            keeper,
            ExpectedOriginal(
                sha256=target.sha256,
                size_bytes=target.size_bytes,
                etag=target.etag,
                version_id=target.version_id,
            ),
        )
        keeper.check()
        return _verified_result(verified, entry.file_type)

    async def _verify_original(self, entry, target, operation, keeper, expected):
        request = ImportVerificationRequest(
            job_id=entry.job_id,
            lease_token=entry.lease_token,
            entry_index=entry.index,
            item_id=entry.item_id,
            creation_id=target.creation_id,
            operation_id=operation.operation_id,
            mode=operation.mode,
        )
        if self._verifier is None:
            self._verifier = ImportVerifier(self._config)
        try:
            return await self._verifier.verify(
                request,
                expected=expected,
                check=keeper.check,
                remaining_lease=keeper._remaining_lease,
            )
        except BaseException as exc:
            if operation.mode == "read":
                try:
                    await self._database.cancel_unstarted_original_read(
                        request.model_dump(mode="json")
                    )
                except Exception:
                    logger.warning(
                        "Could not settle unstarted import verification job=%s",
                        entry.job_id,
                    )
            if isinstance(exc, VerificationFailed):
                raise ImportFailure("storage_unavailable") from None
            if isinstance(exc, VerificationRejected):
                raise ImportFailure(exc.code) from None
            if isinstance(exc, VerificationUnavailable):
                raise LeaseUnavailable from None
            raise

    async def _fail_original(
        self,
        target: ImportOriginalMaterial | None,
        scope: dict[str, Any] | None,
        stage: str,
        code: str,
        exc: BaseException,
        receipt: dict[str, Any] | None = None,
    ) -> None:
        """Settle only the builtin write this process actually owns."""
        if target is None or scope is None or target.connection_id is not None:
            return
        evidence = _failure_evidence(stage, exc, receipt=receipt)
        outcome = "unknown" if not evidence["physical_settled"] else "failed"
        try:
            await self._database.fail_original(
                scope,
                outcome,
                _safe_error_code("import_" + code),
                evidence,
            )
        except Exception:
            logger.warning(
                "Dataset import creation failure could not be settled code=%s", code
            )

    async def _complete_with_retry(
        self,
        state: ImportState,
        entry: ImportEntry,
        completion: ImportEntryCompletion,
        keeper: LeaseKeeper,
        *,
        object_identity: ObjectIdentity | None = None,
    ) -> ImportState:
        return await self._settle_entry_with_retry(
            state,
            entry.index,
            entry.publication_id,
            completion,
            keeper,
            entry=entry,
            object_identity=object_identity,
        )

    async def _complete_pending_error_with_retry(
        self,
        state: ImportState,
        publication_id: UUID,
        completion: ImportEntryCompletion,
        keeper: LeaseKeeper,
    ) -> ImportState:
        """Settle a rejected pending manifest row without inventing an entry DTO."""
        return await self._settle_entry_with_retry(
            state,
            state.cursor,
            publication_id,
            completion,
            keeper,
            entry=None,
            object_identity=None,
        )

    async def _settle_entry_with_retry(
        self,
        state: ImportState,
        index: int,
        publication_id: UUID,
        completion: ImportEntryCompletion,
        keeper: LeaseKeeper,
        *,
        entry: ImportEntry | None,
        object_identity: ObjectIdentity | None,
    ) -> ImportState:
        assert state.lease_token is not None
        for attempt in range(5):
            keeper.check()
            try:
                async with self._database.transaction() as data:
                    completed = await data.complete_entry(
                        state.job_id,
                        state.lease_token,
                        index,
                        publication_id,
                        completion,
                    )
                    if completed.cursor != index + 1:
                        raise ImportFailure("invalid_response")
                    _require_same_scope(state, completed)
                    if (
                        entry is not None
                        and entry.file_type == "image"
                        and completion.status == "copied"
                        and object_identity is not None
                        and completed.added == state.added + 1
                    ):
                        try:
                            await data.image_admission.admit_dataset_item(
                                item_id=entry.item_id,
                                operation_namespace=entry.publication_id,
                                file_path=entry.file_path,
                                file_size=completion.file_size,
                                content_hash=completion.content_hash,
                                object_identity=object_identity,
                                missing_is_skipped=True,
                            )
                        except ImageFailure as exc:
                            raise _as_import_image_failure(exc) from None
                return completed
            except ImportFailure as exc:
                if exc.code not in _CHUNKED_PART_ERROR_CODES:
                    raise
                transient = True
            except Exception as exc:
                if not _transient_database_error(exc):
                    raise
                transient = True
            if attempt == 4:
                raise PersistenceBusy from None
            await asyncio.sleep(0.1 * (2**attempt))
            if transient:
                keeper.check()
        raise PersistenceBusy


async def claim_next(
    database: ImportDatabase,
    config: ImportConfig,
    *,
    requested_job: UUID | None = None,
) -> ImportState | None:
    """Claim one recoverable job using a process-local lease token."""
    async with database.transaction() as data:
        return await data.claim(uuid4(), config.lease_seconds, requested_job)


def _validate_entry_scope(
    entry: ImportEntry,
    state: ImportState,
    requested_publication_id: UUID,
) -> None:
    if state.lease_token is None:
        raise ImportFailure("lease_lost")
    if (
        entry.job_id != state.job_id
        or entry.lease_token != state.lease_token
        or entry.index != state.cursor
        or entry.publication_id != requested_publication_id
    ):
        raise ImportFailure("invalid_response")
    if entry.file_path != (
        f"dataset-sources/{state.dataset_id}/{entry.item_id}/"
        f"{entry.publication_id}/{entry.file_name}"
    ):
        raise ImportFailure("invalid_response")
    if entry.source.size > state.settings_snapshot.dataset_import_max_total_bytes:
        raise ImportFailure("invalid_response")


def _infer_file_type(file_name: str) -> str:
    extension = os.path.basename(file_name).rsplit(".", 1)[-1].lower()
    if extension in _IMAGE_EXTS:
        return "image"
    if extension in _VIDEO_EXTS:
        return "video"
    if extension in _POINTCLOUD_EXTS:
        return "point_cloud"
    return "other"


def _verified_result(
    verified: ImportVerificationResult, file_type: str
) -> TransferResult:
    return TransferResult(
        file_size=verified.size_bytes,
        content_hash=verified.md5,
        dimensions=(
            (verified.width, verified.height) if verified.width is not None else None
        ),
        object_identity=(
            _object_identity_from_verified(verified, byte_size=verified.size_bytes)
            if file_type == "image"
            else None
        ),
    )


def _item_error(code: str) -> ImportEntryCompletion:
    return ImportEntryCompletion(status="error", error_code=code)


def _original_request(
    entry: ImportEntry, state: ImportState, byte_budget: int
) -> dict[str, Any]:
    """Describe only facts the owner can verify against its own entry row."""
    return {
        "kind": "role_media",
        "dataset_id": str(state.dataset_id),
        "item_id": str(entry.item_id),
        "publication_id": str(entry.publication_id),
        "origin": "connector",
        "mode": "write",
        "file_path": entry.file_path,
        "logical_path": entry.file_name,
        "file_type": entry.file_type,
        "content_type": _content_type(entry.file_name),
        "max_bytes": max(0, byte_budget),
        "authority": {
            "kind": "dataset_import",
            "job_id": str(entry.job_id),
            "entry_index": entry.index,
            "publication_id": str(entry.publication_id),
            "item_id": str(entry.item_id),
        },
        "requested_by": str(state.actor_id),
    }


def _content_type(file_name: str) -> str:
    import mimetypes

    return mimetypes.guess_type(file_name)[0] or "application/octet-stream"


def _safe_error_code(code: str) -> str:

    cleaned = re.sub(r"[^a-z0-9_]+", "_", (code or "").lower()).strip("_")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"import_{cleaned or 'io_failed'}"
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


async def _drain_source(handle: ExitStack, pending: BaseException | None) -> None:
    """Drain one source handle; a failed close leaves settlement unknown."""
    try:
        await _thread(handle.close)
    except Exception:
        if pending is not None:
            try:
                pending.storage_operation_unsettled = True
            except Exception:
                pass
            return
        error = SourceReadError("source handle close did not settle")
        error.storage_operation_unsettled = True
        raise error from None


def _require_same_scope(previous: ImportState, returned: ImportState) -> None:
    if (
        previous.job_id,
        previous.actor_id,
        previous.dataset_id,
        previous.connection_id,
        previous.source_path,
        previous.recursive,
        previous.include_globs,
        previous.settings_snapshot,
    ) != (
        returned.job_id,
        returned.actor_id,
        returned.dataset_id,
        returned.connection_id,
        returned.source_path,
        returned.recursive,
        returned.include_globs,
        returned.settings_snapshot,
    ):
        raise ImportFailure("invalid_response")


def _transient_database_error(exc: Exception) -> bool:
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate in _TRANSIENT_SQLSTATES:
        return True
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def _as_import_image_failure(exc: ImageFailure) -> ImportFailure:
    if exc.code in {"storage_unavailable", "source_unavailable"}:
        return ImportFailure("storage_unavailable")
    if exc.code in {
        "source_changed",
        "source_missing",
        "source_not_ready",
        "owner_changed",
        "dependency_changed",
    }:
        return ImportFailure("source_changed")
    if exc.code in {"account_busy", "dataset_busy", "operation_conflict"}:
        return ImportFailure(exc.code)
    if exc.code == "source_busy":
        return ImportFailure("dataset_busy")
    return ImportFailure("invalid_response")
