"""Lease-fenced video media executor and standalone polling entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import signal
import stat
import tempfile
from typing import AsyncIterator, Sequence
from uuid import UUID, uuid4

from aap_assets_media.contract import (
    AdvanceBackfillRequest,
    BackfillSpec,
    ClaimGarbageRequest,
    ClaimRequest,
    ClaimedWork,
    CompleteGarbageRequest,
    ExpireRequest,
    FailRequest,
    FinishRequest,
    HeartbeatRequest,
    MediaClaim,
    MediaFailure,
    MediaSource,
    MediaUsage,
    ObjectIdentity,
    StageTimetableRequest,
    UsageRequest,
    WorkOutcome,
)

from .config import MediaExecutionSettings, load_config
from .database import MediaDatabase
from .processing import (
    MediaProbe,
    MediaProcessingError,
    ProcessedArtifact,
    ProcessedWork,
    process_claim,
)
from .process import FairWorkerPoller, create_worker_runtime
from .storage import MediaStorage

_MD5_HASH = re.compile(r"^[0-9a-fA-F]{32}$")
_SHA256_HASH = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_BACKFILL_CALLS = 1_000_000
_MAINTENANCE_INTERVAL_SECONDS = 24 * 60 * 60
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _FileFingerprint:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class VerifiedSource:
    source: MediaSource
    identity: ObjectIdentity
    path: Path
    sha256: str
    fingerprint: _FileFingerprint


class VerifiedSourceCache:
    """One-entry source cache revalidated on every lease and before publish."""

    def __init__(self, *, directory: str | Path | None = None) -> None:
        self._temporary = (
            tempfile.TemporaryDirectory(prefix="aap-video-source-")
            if directory is None
            else None
        )
        self._directory = (
            Path(self._temporary.name) if self._temporary else Path(directory)  # type: ignore[arg-type]
        )
        self._directory.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._key: tuple[object, ...] | None = None
        self._path: Path | None = None
        self._identity: ObjectIdentity | None = None
        self._digest: str | None = None
        self._fingerprint: _FileFingerprint | None = None
        self._closed = False

    @asynccontextmanager
    async def lease(
        self, source: MediaSource, storage: MediaStorage
    ) -> AsyncIterator[VerifiedSource]:
        async with self._lock:
            if self._closed:
                raise RuntimeError("verified source cache is closed")
            if source.source_drifted:
                self._invalidate()
                raise MediaFailure("source_changed")
            try:
                identity = await storage.head_source(source.file_path)
            except MediaFailure as exc:
                if exc.code in {"object_missing", "source_changed"}:
                    self._invalidate()
                raise
            if (
                source.object_identity is not None
                and identity != source.object_identity
            ):
                self._invalidate()
                raise MediaFailure("source_changed")
            key = (
                source.dataset_item_id,
                source.dataset_id,
                source.source_generation,
                source.source_snapshot,
                source.file_path,
                source.file_name,
                source.file_type,
                source.file_size,
                source.content_hash,
                identity,
            )
            if self._key != key:
                self._invalidate()
                path = self._directory / f"source{_source_suffix(source.file_name)}"
                observed_source = source.model_copy(
                    update={"object_identity": identity}
                )
                try:
                    await storage.download_source(observed_source, path)
                    path.chmod(stat.S_IRUSR)
                    size, digest, md5 = await _source_digests_async(
                        path,
                        need_md5=_declares_md5(source.content_hash),
                    )
                    if size != identity.byte_size or (
                        source.file_size is not None and size != source.file_size
                    ):
                        raise MediaFailure("source_changed")
                    if not _matches_declared_content_hash(
                        source.content_hash, digest, md5
                    ):
                        raise MediaFailure("source_changed")
                    if await storage.head_source(source.file_path) != identity:
                        raise MediaFailure("source_changed")
                    fingerprint = _file_fingerprint(path)
                except BaseException:
                    path.unlink(missing_ok=True)
                    raise
                self._key = key
                self._path = path
                self._identity = identity
                self._digest = digest
                self._fingerprint = fingerprint
            else:
                assert self._path is not None
                assert self._identity is not None
                assert self._digest is not None
                assert self._fingerprint is not None
                try:
                    current_fingerprint = _file_fingerprint(self._path)
                except MediaFailure:
                    self._invalidate()
                    raise
                if current_fingerprint != self._fingerprint:
                    self._invalidate()
                    raise MediaFailure("source_changed")
                if self._fingerprint.size != self._identity.byte_size:
                    self._invalidate()
                    raise MediaFailure("source_changed")
            assert self._path is not None
            assert self._identity is not None
            assert self._digest is not None
            assert self._fingerprint is not None
            yield VerifiedSource(
                source,
                self._identity,
                self._path,
                self._digest,
                self._fingerprint,
            )

    async def verify_current(
        self, verified: VerifiedSource, storage: MediaStorage
    ) -> None:
        if await storage.head_source(verified.source.file_path) != verified.identity:
            self._invalidate()
            raise MediaFailure("source_changed")
        try:
            current_fingerprint = _file_fingerprint(verified.path)
        except MediaFailure:
            self._invalidate()
            raise
        if current_fingerprint != verified.fingerprint:
            self._invalidate()
            raise MediaFailure("source_changed")

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._invalidate()
            self._closed = True
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None

    def _invalidate(self) -> None:
        if self._path is not None:
            self._path.unlink(missing_ok=True)
        self._key = None
        self._path = None
        self._identity = None
        self._digest = None
        self._fingerprint = None


class _LeaseHeartbeat:
    def __init__(
        self,
        database: MediaDatabase,
        claim: MediaClaim,
        settings: MediaExecutionSettings,
    ) -> None:
        self.database = database
        self.claim = claim
        self.settings = settings
        self.error: BaseException | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="assets-media-heartbeat")

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.settings.heartbeat_interval_seconds)
                result = await self.database.call(
                    "heartbeat_video_media",
                    HeartbeatRequest(
                        job_id=self.claim.job_id,
                        generation=self.claim.generation,
                        lease_token=self.claim.lease_token,
                        lease_seconds=self.settings.lease_seconds,
                    ),
                )
                if not result.active:
                    self.error = MediaFailure("lease_lost")
                    return
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.error = exc

    def check(self) -> None:
        if self.error is not None:
            raise self.error

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None


class MediaExecutor:
    """Claim, execute, and publish media work using only worker capabilities."""

    def __init__(
        self,
        database: MediaDatabase,
        storage: MediaStorage,
        worker_id: UUID | None = None,
        *,
        settings: MediaExecutionSettings | None = None,
        source_cache: VerifiedSourceCache | None = None,
        probe_client: MediaProbe | None = None,
    ) -> None:
        self.database = database
        self.storage = storage
        self.worker_id = worker_id or uuid4()
        self.settings = settings or MediaExecutionSettings()
        self.probe_client = probe_client
        self.source_cache = source_cache or VerifiedSourceCache()
        self._owns_source_cache = source_cache is None

    async def execute_once(
        self,
        job_id: UUID | None = None,
        kinds: Sequence[str] | None = None,
    ) -> WorkOutcome | None:
        request = ClaimRequest(
            worker_id=self.worker_id,
            job_id=job_id,
            lease_seconds=self.settings.lease_seconds,
            kinds=list(kinds)
            if kinds is not None
            else ClaimRequest.model_fields["kinds"].default_factory(),
        )
        claimed = await self.database.call("claim_video_media", request)
        assert isinstance(claimed, ClaimedWork)
        claim = claimed.claim
        if claim is None:
            return None

        heartbeat = _LeaseHeartbeat(self.database, claim, self.settings)
        heartbeat.start()
        try:
            if isinstance(claim.spec, BackfillSpec):
                return await self._advance_backfill(claim, heartbeat)
            return await self._execute_claim(claim, heartbeat)
        except MediaProcessingError as exc:
            return await self._fail_claim(claim, heartbeat, exc.code, exc.detail)
        except MediaFailure as exc:
            if exc.code == "storage_unavailable":
                raise
            if exc.code == "object_missing":
                return await self._fail_claim(
                    claim, heartbeat, "source_missing", "Source object is missing"
                )
            if exc.code in {
                "invalid_request",
                "resource_budget_exceeded",
                "source_missing",
                "source_changed",
                "metadata_not_ready",
                "invalid_frame_range",
                "asset_missing",
                "dependency_changed",
            }:
                return await self._fail_claim(claim, heartbeat, exc.code, exc.code)
            raise
        finally:
            await heartbeat.close()

    async def _execute_claim(
        self, claim: MediaClaim, heartbeat: _LeaseHeartbeat
    ) -> WorkOutcome:
        source = claim.source
        if source is None:
            raise MediaFailure("invalid_response")
        expected_snapshot = getattr(claim.spec, "expected_source_snapshot", None)
        if (
            expected_snapshot is not None
            and expected_snapshot != source.source_snapshot
        ):
            raise MediaFailure("source_changed")
        async with self.source_cache.lease(source, self.storage) as verified:
            heartbeat.check()
            with tempfile.TemporaryDirectory(prefix="aap-video-output-") as output:
                try:
                    processing = asyncio.create_task(
                        asyncio.to_thread(
                            process_claim,
                            claim,
                            verified.path,
                            Path(output),
                            verified.identity,
                            verified.sha256,
                            probe_client=self.probe_client,
                        )
                    )
                    try:
                        processed = await asyncio.shield(processing)
                    except asyncio.CancelledError:
                        with suppress(BaseException):
                            await processing
                        raise
                except MediaProcessingError:
                    raise
                except Exception as exc:
                    raise MediaProcessingError(
                        "media_processing_failed", type(exc).__name__
                    ) from None
                heartbeat.check()
                for artifact in processed.artifacts:
                    await self._upload_artifact(artifact)
                    heartbeat.check()
                if processed.timetable_rows:
                    await self._stage_timetable(claim, processed, heartbeat)
                heartbeat.check()
                try:
                    await self.source_cache.verify_current(verified, self.storage)
                except MediaFailure as exc:
                    if exc.code == "object_missing":
                        return await self._fail_claim(
                            claim,
                            heartbeat,
                            "source_missing",
                            "Source object is missing",
                        )
                    raise
                heartbeat.check()
                result = await self.database.call(
                    "finish_video_media",
                    FinishRequest(
                        job_id=claim.job_id,
                        generation=claim.generation,
                        lease_token=claim.lease_token,
                        result=processed.result,
                    ),
                )
                assert isinstance(result, WorkOutcome)
                return result

    async def _upload_artifact(self, artifact: ProcessedArtifact) -> None:
        observed = await self.storage.upload_cache(artifact.path, artifact.artifact.key)
        if observed != artifact.artifact:
            raise MediaFailure("storage_unavailable")

    async def _stage_timetable(
        self,
        claim: MediaClaim,
        processed: ProcessedWork,
        heartbeat: _LeaseHeartbeat,
    ) -> None:
        rows = processed.timetable_rows
        for start in range(0, len(rows), self.settings.timetable_page_size):
            heartbeat.check()
            batch = rows[start : start + self.settings.timetable_page_size]
            result = await self.database.call(
                "stage_video_timetable",
                StageTimetableRequest(
                    job_id=claim.job_id,
                    generation=claim.generation,
                    lease_token=claim.lease_token,
                    rows=list(batch),
                ),
            )
            if result.staged_count < len(batch):
                raise MediaFailure("invalid_response")

    async def _advance_backfill(
        self, claim: MediaClaim, heartbeat: _LeaseHeartbeat
    ) -> WorkOutcome:
        previous_cursor: UUID | None = None
        for _ in range(_MAX_BACKFILL_CALLS):
            heartbeat.check()
            progress = await self.database.call(
                "advance_video_backfill",
                AdvanceBackfillRequest(
                    job_id=claim.job_id,
                    generation=claim.generation,
                    lease_token=claim.lease_token,
                    batch_size=self.settings.timetable_page_size,
                ),
            )
            heartbeat.check()
            if progress.complete:
                return WorkOutcome(
                    job_id=claim.job_id,
                    generation=claim.generation,
                    outcome="published",
                )
            cursor = progress.next_item_id
            if cursor is None or cursor == previous_cursor:
                raise MediaFailure("invalid_response")
            previous_cursor = cursor
        raise MediaFailure("resource_budget_exceeded")

    async def _fail_claim(
        self,
        claim: MediaClaim,
        heartbeat: _LeaseHeartbeat,
        error_code: str,
        error_detail: str,
    ) -> WorkOutcome:
        heartbeat.check()
        result = await self.database.call(
            "fail_video_media",
            FailRequest(
                job_id=claim.job_id,
                generation=claim.generation,
                lease_token=claim.lease_token,
                error_code=error_code[:64],
                error_detail=(error_detail or error_code)[:2000],
            ),
        )
        assert isinstance(result, WorkOutcome)
        return result

    async def maintenance_once(self) -> MediaUsage:
        """Expire derived assets, clean one leased garbage batch, and read usage."""
        while True:
            expired = await self.database.call(
                "expire_video_media",
                ExpireRequest(
                    frame_ttl_days=self.settings.frame_ttl_days,
                    chunk_ttl_days=self.settings.chunk_ttl_days,
                    batch_size=self.settings.timetable_page_size,
                ),
            )
            if not expired.more:
                break

        batch = await self.database.call(
            "claim_video_media_garbage",
            ClaimGarbageRequest(
                worker_id=self.worker_id,
                lease_seconds=self.settings.garbage_lease_seconds,
                batch_size=self.settings.garbage_batch_size,
            ),
        )
        for item in batch.items:
            if item.prefix is not None:
                await self.storage.delete_cache_prefix(item.prefix)
            else:
                await self.storage.delete_cache_keys(item.keys)
            await self.database.call(
                "complete_video_media_garbage",
                CompleteGarbageRequest(
                    garbage_id=item.garbage_id,
                    lease_token=item.lease_token,
                ),
            )
        usage = await self.database.call("read_video_media_usage", UsageRequest())
        assert isinstance(usage, MediaUsage)
        return usage

    async def close(self) -> None:
        if self._owns_source_cache:
            await self.source_cache.close()

    async def __aenter__(self) -> MediaExecutor:
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.close()


def _source_suffix(file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ".bin"


def _file_fingerprint(path: Path) -> _FileFingerprint:
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError:
        raise MediaFailure("source_changed") from None
    if not stat.S_ISREG(observed.st_mode):
        raise MediaFailure("source_changed")
    return _FileFingerprint(
        device=observed.st_dev,
        inode=observed.st_ino,
        mode=observed.st_mode,
        size=observed.st_size,
        mtime_ns=observed.st_mtime_ns,
        ctime_ns=observed.st_ctime_ns,
    )


async def _source_digests_async(
    path: Path, *, need_md5: bool
) -> tuple[int, str, str | None]:
    task = asyncio.create_task(
        asyncio.to_thread(_source_digests, path, need_md5=need_md5)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(BaseException):
            await task
        raise


def _source_digests(
    path: Path, *, need_md5: bool = False
) -> tuple[int, str, str | None]:
    digest = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False) if need_md5 else None
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
            if md5 is not None:
                md5.update(chunk)
    return size, digest.hexdigest(), md5.hexdigest() if md5 is not None else None


def _declares_md5(expected: str | None) -> bool:
    if expected is None:
        return False
    value = expected.strip().lower()
    if value.startswith("md5:"):
        value = value[4:]
    return len(value) == 32


def _matches_declared_content_hash(
    expected: str | None, sha256: str, md5: str | None
) -> bool:
    if expected is None:
        return True
    value = expected.strip()
    if value.lower().startswith("sha256:"):
        value = value[7:]
        return bool(_SHA256_HASH.fullmatch(value) and value.lower() == sha256)
    if value.lower().startswith("md5:"):
        value = value[4:]
        return bool(
            md5 is not None and _MD5_HASH.fullmatch(value) and value.lower() == md5
        )
    if len(value) == 32:
        return bool(
            md5 is not None and _MD5_HASH.fullmatch(value) and value.lower() == md5
        )
    if len(value) == 64:
        return bool(_SHA256_HASH.fullmatch(value) and value.lower() == sha256)
    return True


async def _run_worker(
    *,
    once: bool,
    job_id: UUID | None = None,
    input_id: UUID | None = None,
    frame_id: UUID | None = None,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    if sum(value is not None for value in (job_id, input_id, frame_id)) > 1:
        raise ValueError("worker selectors are mutually exclusive")
    config = load_config()
    runtime = await create_worker_runtime(config)
    poller = FairWorkerPoller(runtime.media_executor, runtime.video_executor)
    stop_requested = shutdown_event or asyncio.Event()
    try:
        if once:
            if stop_requested.is_set():
                return
            await poller.execute_once(
                job_id=job_id,
                input_id=input_id,
                frame_id=frame_id,
            )
            return
        next_maintenance = 0.0
        while not stop_requested.is_set():
            did_work = None
            now = asyncio.get_running_loop().time()
            if now >= next_maintenance:
                try:
                    await runtime.media_executor.maintenance_once()
                except Exception as exc:
                    _LOGGER.error(
                        "Assets media maintenance failed (%s)", type(exc).__name__
                    )
                try:
                    await runtime.maintenance_once()
                except Exception as exc:
                    _LOGGER.error(
                        "Accepted video maintenance failed (%s)", type(exc).__name__
                    )
                next_maintenance = (
                    asyncio.get_running_loop().time() + _MAINTENANCE_INTERVAL_SECONDS
                )
            if stop_requested.is_set():
                break
            try:
                did_work = await poller.execute_once(
                    job_id=job_id,
                    input_id=input_id,
                    frame_id=frame_id,
                )
            except Exception as exc:
                _LOGGER.error(
                    "Assets media worker iteration failed (%s)", type(exc).__name__
                )
            if stop_requested.is_set():
                break
            if did_work is None:
                try:
                    await asyncio.wait_for(
                        stop_requested.wait(), timeout=config.poll_seconds
                    )
                except asyncio.TimeoutError:
                    pass
    finally:
        await runtime.close()


async def _run_worker_cli(
    *,
    once: bool,
    job_id: UUID | None = None,
    input_id: UUID | None = None,
    frame_id: UUID | None = None,
) -> None:
    """Install CLI-only SIGTERM handling and drain the current claimed unit."""
    loop = asyncio.get_running_loop()
    stop_requested = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop_requested.set)
    try:
        await _run_worker(
            once=once,
            job_id=job_id,
            input_id=input_id,
            frame_id=frame_id,
            shutdown_event=stop_requested,
        )
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


async def _healthcheck() -> None:
    from .database import AsyncpgMediaDatabase
    from .health import check_capabilities

    config = load_config()
    database = await AsyncpgMediaDatabase.connect(
        config.database_url, allowed_roles={"worker"}
    )
    try:
        if not await check_capabilities(database.pool, role="worker"):
            raise SystemExit("Assets media worker SQL capabilities are not valid")
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Assets video media worker")
    parser.add_argument(
        "--once", action="store_true", help="execute at most one claimed unit"
    )
    selectors = parser.add_mutually_exclusive_group()
    selectors.add_argument(
        "--job-id", type=UUID, help="claim only this legacy media job"
    )
    selectors.add_argument(
        "--input-id", type=UUID, help="claim only this retained video original"
    )
    selectors.add_argument(
        "--frame-id", type=UUID, help="claim only this retained video frame"
    )
    parser.add_argument(
        "--healthcheck", action="store_true", help="verify the worker SQL login"
    )
    args = parser.parse_args()
    if args.healthcheck:
        asyncio.run(_healthcheck())
        return
    # Leave SIGINT to asyncio.run; only container SIGTERM requests a drain.
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    try:
        asyncio.run(
            _run_worker_cli(
                once=args.once,
                job_id=args.job_id,
                input_id=args.input_id,
                frame_id=args.frame_id,
            )
        )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    main()
