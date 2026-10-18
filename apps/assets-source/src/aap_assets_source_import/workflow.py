"""Lease-fenced, bounded execution of accepted Dataset imports."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Callable
from contextlib import suppress
import json
import logging
import os
import posixpath
import re
import threading
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from aap_assets_source.connectors import (
    ConnectorCryptoError,
    ConnectorCryptoNotConfigured,
    ConnectorHostDenied,
    SourceAdapter,
    SourceChangedError,
    SourceNotFoundError,
    SourcePathError,
    build_source_adapter,
)
from aap_assets_source.connectors.crypto import decrypt_secret
from aap_assets_source.import_contract import (
    ImportConnection,
    ImportEntry,
    ImportEntryCompletion,
    ImportFailure,
    ImportSourceObject,
    ImportState,
)
from aap_assets_images.contract import ImageFailure, ObjectIdentity

from .config import ImportConfig
from .database import ImportDatabase
from .storage import DestinationStorage, DestinationWriteError, SourceReadError

logger = logging.getLogger("aap_assets_source_import.workflow")
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
_IMAGE_EXTS = {"jpg", "jpeg", "png", "bmp", "webp", "tiff", "tif", "gif", "svg"}
_VIDEO_EXTS = {"mp4", "avi", "mov", "mkv", "wmv", "flv", "webm"}
_POINTCLOUD_EXTS = {"pcd", "bin", "las", "ply"}
# Keep a manifest below this wire budget so a valid 50k-file limit cannot
# exhaust the worker with thousands of maximum-length connector paths.
_MAX_MANIFEST_WIRE_BYTES = 16 * 1024 * 1024


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

    def stop_requested_from_thread(self) -> bool:
        return self._thread_stop.is_set()

    async def _run(self) -> None:
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=self._config.heartbeat_seconds
                )
                self._thread_stop.set()
                return
            except TimeoutError:
                pass
            try:
                async with self._database.transaction() as data:
                    renewed = await data.heartbeat(
                        self._state.job_id,
                        self._state.lease_token,
                        self._config.lease_seconds,
                    )
                    if renewed.lease_token != self._state.lease_token:
                        raise ImportFailure("lease_lost")
                    _require_same_scope(self._state, renewed)
            except ImportFailure as exc:
                self._interrupted = exc
                self._thread_stop.set()
                return
            except Exception:
                # Ownership is uncertain; stop all I/O and let the lease expire.
                self._unavailable = True
                self._thread_stop.set()
                return


class ImportWorkflow:
    """Execute one job using only the LOGIN role's seven public capabilities."""

    def __init__(
        self,
        database: ImportDatabase,
        storage: DestinationStorage,
        config: ImportConfig,
        *,
        shutdown: asyncio.Event | None = None,
    ) -> None:
        self._database = database
        self._storage = storage
        self._config = config
        self._shutdown = shutdown or asyncio.Event()

    async def run_claimed(self, state: ImportState) -> None:
        """Run one claimed lease; database transport failures leave it recoverable."""
        if state.lease_token is None or state.stage not in {"enumerating", "importing"}:
            raise ImportFailure("invalid_response")
        async with LeaseKeeper(
            self._database, self._config, state, self._shutdown
        ) as keeper:
            adapter: SourceAdapter | None = None
            try:
                connection = await self._read_connection(state)
                secret = self._decrypt_connection(connection)
                keeper.check()
                try:
                    adapter = await asyncio.to_thread(
                        build_source_adapter,
                        connection.kind,
                        connection.config,
                        secret,
                        connection.allowlist,
                        source_path=state.source_path,
                        known_hosts_file=self._config.sftp_known_hosts_file,
                        sftp_timeout_seconds=self._config.sftp_timeout_seconds,
                        require_conditional_reads=True,
                    )
                except (ConnectorHostDenied, SourcePathError):
                    raise
                except ValueError:
                    raise ImportFailure("connection_changed") from None
                except Exception as exc:
                    logger.warning(
                        "Dataset import connector could not connect job=%s kind=%s",
                        state.job_id,
                        type(exc).__name__,
                    )
                    raise ImportFailure("storage_unavailable") from None

                if state.stage == "enumerating":
                    try:
                        objects = await asyncio.to_thread(
                            _collect_manifest,
                            adapter,
                            state,
                            keeper.stop_requested_from_thread,
                        )
                    except (WorkerStopping, ImportFailure, SourcePathError):
                        raise
                    except Exception as exc:
                        logger.warning(
                            "Dataset import source enumeration failed job=%s kind=%s",
                            state.job_id,
                            type(exc).__name__,
                        )
                        raise ImportFailure("storage_unavailable") from None
                    keeper.check()
                    state = await self._save_manifest(state, objects)

                if state.stage != "importing" or state.total is None:
                    raise ImportFailure("invalid_response")
                await self._copy_manifest(state, adapter, connection.config, keeper)
                keeper.check()
                await self._finish_completed(state)
            except (WorkerStopping, LeaseUnavailable, PersistenceBusy):
                raise
            except ImportFailure:
                raise
            except (ConnectorCryptoError, ConnectorCryptoNotConfigured):
                raise ImportFailure("connection_changed") from None
            except ConnectorHostDenied:
                raise ImportFailure("connection_changed") from None
            except SourcePathError:
                raise ImportFailure("invalid_request") from None
            except ValidationError:
                raise ImportFailure("invalid_response") from None
            finally:
                if adapter is not None:
                    with suppress(Exception):
                        await asyncio.to_thread(adapter.close)

    async def _read_connection(self, state: ImportState) -> ImportConnection:
        assert state.lease_token is not None
        async with self._database.transaction() as data:
            return await data.read_connection(
                state.job_id,
                state.lease_token,
                self._config.connector_host_allowlist,
            )

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
        connector_config: dict[str, Any],
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
                _validate_entry_scope(entry, state, connector_config, publication_id)
        return entry

    async def _finish_completed(self, state: ImportState) -> ImportState:
        assert state.lease_token is not None
        async with self._database.transaction() as data:
            finished = await data.finish(state.job_id, state.lease_token, None)
            _require_same_scope(state, finished)
            if finished.stage != "completed":
                raise ImportFailure("invalid_response")
        return finished

    def _decrypt_connection(self, connection: ImportConnection) -> dict[str, Any]:
        try:
            token = base64.b64decode(connection.secret_enc, validate=True)
            secret = decrypt_secret(token, self._config.connector_encryption_key)
        except (ConnectorCryptoError, ConnectorCryptoNotConfigured):
            raise
        except binascii.Error as exc:
            raise ConnectorCryptoError(
                "connector secret encoding is malformed"
            ) from exc
        if not isinstance(secret, dict):
            raise ConnectorCryptoError("connector credentials are malformed")
        return secret

    async def _copy_manifest(
        self,
        initial_state: ImportState,
        adapter: SourceAdapter,
        connector_config: dict[str, Any],
        keeper: LeaseKeeper,
    ) -> None:
        assert initial_state.lease_token is not None
        state = initial_state
        attempted_bytes = 0
        while state.cursor < (state.total or 0):
            keeper.check()
            publication_id = uuid4()
            try:
                entry = await self._next_entry(state, connector_config, publication_id)
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
                    adapter,
                    keeper,
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
                # Failed reads are not persisted in committed_bytes. Track their
                # consumed source bytes only until this worker lease is reclaimed.
                attempted_bytes += stream_bytes
        if state.cursor != state.total:
            raise ImportFailure("invalid_response")

    async def _transfer_entry(
        self,
        entry: ImportEntry,
        adapter: SourceAdapter,
        keeper: LeaseKeeper,
        *,
        byte_budget: int,
    ) -> tuple[ImportEntryCompletion, int, ObjectIdentity | None]:
        attempted = 0

        def count_bytes(amount: int) -> None:
            nonlocal attempted
            attempted += amount

        stream: Any | None = None
        try:
            keeper.check()
            stream = await asyncio.to_thread(
                adapter.open, entry.source.relpath, etag=entry.source.etag
            )
            keeper.check()
            size, content_hash, dimensions = await self._storage.transfer(
                stream,
                entry,
                byte_budget=byte_budget,
                before_io=keeper.check,
                on_bytes=count_bytes,
            )
            object_identity = None
            if entry.file_type == "image":
                keeper.check()
                object_identity = await self._storage.head_image_identity(
                    entry.file_path, expected_size=size
                )
                keeper.check()
            return (
                ImportEntryCompletion(
                    status="copied",
                    file_size=size,
                    content_hash=content_hash,
                    width=dimensions[0] if dimensions else None,
                    height=dimensions[1] if dimensions else None,
                ),
                attempted,
                object_identity,
            )
        except SourceChangedError:
            return _item_error("source_changed"), attempted, None
        except SourceNotFoundError:
            return _item_error("source_not_found"), attempted, None
        except SourcePathError:
            return _item_error("invalid_source_object"), attempted, None
        except SourceReadError:
            return _item_error("source_read_failed"), attempted, None
        except DestinationWriteError:
            return _item_error("destination_write_failed"), attempted, None
        except (WorkerStopping, LeaseUnavailable, ImportFailure):
            raise
        except Exception as exc:
            logger.info(
                "Dataset import item I/O failed job=%s kind=%s",
                entry.job_id,
                type(exc).__name__,
            )
            return _item_error("source_read_failed"), attempted, None
        finally:
            if stream is not None:
                with suppress(Exception):
                    await asyncio.to_thread(stream.close)

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
                                publication_id=entry.publication_id,
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


def _collect_manifest(
    adapter: SourceAdapter,
    state: ImportState,
    stop_requested: Callable[[], bool],
) -> list[ImportSourceObject]:
    """Enumerate into a bounded manifest using the accepted immutable budgets."""
    limits = state.settings_snapshot
    collected: list[ImportSourceObject] = []
    seen: set[str] = set()
    total_bytes = 0
    manifest_bytes = 2  # []
    for source_object in adapter.list(
        state.source_path, state.recursive, state.include_globs
    ):
        if stop_requested():
            raise WorkerStopping
        if len(collected) >= limits.dataset_import_max_files:
            raise ImportFailure("invalid_request")
        if source_object.relpath in seen:
            raise ImportFailure("invalid_response")
        try:
            current = ImportSourceObject.model_validate(
                {
                    "relpath": source_object.relpath,
                    "size": max(0, source_object.size),
                    "mtime": source_object.mtime,
                    "etag": source_object.etag,
                }
            )
        except ValidationError:
            raise ImportFailure("invalid_response") from None
        seen.add(current.relpath)
        total_bytes += current.size
        if total_bytes > limits.dataset_import_max_total_bytes:
            raise ImportFailure("invalid_request")
        serialized = json.dumps(
            current.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        manifest_bytes += len(serialized) + (1 if collected else 0)
        if manifest_bytes > _MAX_MANIFEST_WIRE_BYTES:
            raise ImportFailure("invalid_request")
        collected.append(current)
    return collected


def _validate_entry_scope(
    entry: ImportEntry,
    state: ImportState,
    connector_config: dict[str, Any],
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
    parts = entry.file_path.split("/", 4)
    if (
        len(parts) != 5
        or parts[0] != "dataset-sources"
        or parts[1] != str(state.dataset_id)
    ):
        raise ImportFailure("invalid_response")
    if entry.source.size > state.settings_snapshot.dataset_import_max_total_bytes:
        raise ImportFailure("invalid_response")
    base = (
        connector_config.get("base_prefix") or connector_config.get("base_path") or ""
    )
    expected_name = destination_relative_path(
        entry.source.relpath, state.source_path, str(base)
    ) or posixpath.basename(entry.source.relpath)
    expected_dir, _, expected_basename = expected_name.rpartition("/")
    actual_dir, _, actual_basename = entry.file_name.rpartition("/")
    if expected_dir != actual_dir or not _matches_unique_name(
        expected_basename or expected_name, actual_basename
    ):
        raise ImportFailure("invalid_response")


def destination_relative_path(
    relpath: str, source_path: str, base_prefix: str = ""
) -> str:
    """Strip the selected source directory while preserving its subdirectory tree."""
    relative = _normalize_path(relpath)
    source = _normalize_path(source_path)
    base = _normalize_path(base_prefix)
    if base and (source == base or source.startswith(f"{base}/")):
        source = source[len(base) :].lstrip("/")
    if source and (relative == source or relative.startswith(f"{source}/")):
        relative = relative[len(source) :].lstrip("/")
    return relative


def _normalize_path(value: str) -> str:
    return (value or "").replace("\\", "/").strip().strip("/")


def _matches_unique_name(expected: str, actual: str) -> bool:
    if actual == expected:
        return True
    stem, extension = posixpath.splitext(expected)
    return (
        re.fullmatch(rf"{re.escape(stem)}-[1-9][0-9]*{re.escape(extension)}", actual)
        is not None
    )


def _infer_file_type(file_name: str) -> str:
    extension = os.path.basename(file_name).rsplit(".", 1)[-1].lower()
    if extension in _IMAGE_EXTS:
        return "image"
    if extension in _VIDEO_EXTS:
        return "video"
    if extension in _POINTCLOUD_EXTS:
        return "point_cloud"
    return "other"


def _item_error(code: str) -> ImportEntryCompletion:
    return ImportEntryCompletion(status="error", error_code=code)


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
