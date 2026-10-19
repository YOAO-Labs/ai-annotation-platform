"""Lease-bound retained video preparation and original-version frame extraction."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
import random
import shutil
import tempfile
import time
from typing import get_args
from uuid import uuid4

from aap_assets_images.contract import ImageFailure
from aap_assets_inputs.contract import InputFailure
from aap_assets_media.contract import MediaFailure
from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs import values as v

from .processing import (
    VideoExecutionPolicy,
    extract_retained_frame,
    probe_retained_video,
)

_TRANSIENT = frozenset(
    {
        "account_busy",
        "source_busy",
        "task_busy",
        "job_busy",
        "input_busy",
        "manifest_busy",
        "frame_busy",
        "reference_busy",
        "storage_unavailable",
    }
)
_ERRORS = frozenset(get_args(c.ErrorCode))


class _Lease:
    def __init__(self, database, claim, policy):
        self.database, self.claim, self.policy = database, claim, policy
        self.kind = "input" if isinstance(claim, c.VideoInputClaim) else "frame"
        self.fields = {
            self.kind + "_id": claim.input.input_id
            if self.kind == "input"
            else claim.frame.frame_id,
            "generation": claim.generation,
            "lease_token": claim.lease_token,
        }
        self.deadline = time.monotonic() + policy.job_timeout_seconds
        self.interval = policy.lease_seconds / 3
        self.stopped, self.lost = asyncio.Event(), asyncio.Event()
        self.task = None

    async def beat(self):
        model = (
            c.HeartbeatVideoInputRequest
            if self.kind == "input"
            else c.HeartbeatVideoFrameRequest
        )
        await self.database.call(
            "heartbeat_accepted_video_" + self.kind,
            model(**self.fields, lease_seconds=self.policy.lease_seconds),
        )

    async def start(self):
        try:
            await self.beat()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.lost.set()
            raise c.VideoInputFailure("lease_lost") from None
        self.task = asyncio.create_task(self.run())

    async def run(self):
        try:
            while not self.stopped.is_set():
                try:
                    await asyncio.wait_for(self.stopped.wait(), timeout=self.interval)
                    return
                except TimeoutError:
                    await self.beat()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.lost.set()

    def check(self):
        if self.lost.is_set():
            raise c.VideoInputFailure("lease_lost")
        if time.monotonic() >= self.deadline:
            raise c.VideoInputFailure("timeout")

    async def call(self, operation, *args, **kwargs):
        self.check()
        task = asyncio.create_task(operation(*args, **kwargs))
        lost = asyncio.create_task(self.lost.wait())
        try:
            async with asyncio.timeout_at(self.deadline):
                await asyncio.wait((task, lost), return_when=asyncio.FIRST_COMPLETED)
                if self.lost.is_set():
                    raise c.VideoInputFailure("lease_lost")
                result = await task
        except TimeoutError:
            raise c.VideoInputFailure("timeout") from None
        finally:
            lost.cancel()
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
            with suppress(asyncio.CancelledError):
                await lost
        self.check()
        return result

    async def close(self):
        self.stopped.set()
        if self.task is not None:
            with suppress(asyncio.CancelledError):
                await self.task


class VideoInputExecutor:
    def __init__(
        self, database, storage, *, worker_id=None, policy=None, temp_root=None
    ):
        self.database, self.storage = database, storage
        self.worker_id = worker_id or uuid4()
        self.policy = policy or VideoExecutionPolicy()
        self.temp_root = Path(temp_root) if temp_root is not None else None

    async def _persist(self, lease, operation, request):
        failures = 0
        while True:
            lease.check()
            try:
                return await lease.call(self.database.call, operation, request)
            except c.VideoInputFailure as exc:
                if exc.code not in _TRANSIENT:
                    raise
                failures += 1
                delay = min(2, 0.05 * 2 ** min(failures, 6)) * random.uniform(
                    0.75, 1.25
                )
                if time.monotonic() + delay >= lease.deadline:
                    raise
                await asyncio.sleep(delay)

    async def _original(self, claim, lease, workspace):
        record, expected = claim.input, claim.input.source.object_identity
        path = workspace / "source"
        measured = await lease.call(self.storage.download_source, claim.source, path)
        if measured.identity != expected or measured.byte_size != expected.byte_size:
            raise c.VideoInputFailure("source_changed")
        blob = await lease.call(
            self.storage.upload_blob,
            path,
            claim.blob_key,
            sha256=measured.sha256,
            byte_size=measured.byte_size,
        )
        if not isinstance(blob, v.VideoBlob) or (
            blob.key != claim.blob_key
            or blob.sha256 != measured.sha256
            or blob.byte_size != measured.byte_size
        ):
            raise c.VideoInputFailure("input_corrupt")
        probed = await lease.call(
            probe_retained_video,
            path,
            record.sampling,
            workspace / "selected-times.jsonl",
            policy=self.policy,
        )
        video = v.FrozenVideoInput(
            input_id=record.input_id,
            job_id=record.job_id,
            actor_id=record.actor_id,
            task=record.task,
            source=record.source,
            blob=blob,
            video=probed.video,
            created_at=record.created_at,
        )
        try:
            plan = v.FrozenFramePlan.from_entries(
                video, record.sampling, probed.entries()
            )
        except (ValueError, TypeError):
            raise c.VideoInputFailure("timebase_incomplete") from None
        cursor = 0
        for page in probed.pages():
            await self._persist(
                lease,
                "stage_accepted_video_times",
                c.StageVideoTimesRequest(**lease.fields, cursor=cursor, entries=page),
            )
            cursor += len(page)
        if cursor != plan.selected_count:
            raise c.VideoInputFailure("timebase_incomplete")
        if await lease.call(self.storage.head_source, claim.source) != expected:
            raise c.VideoInputFailure("source_changed")
        return c.FinishVideoInputRequest(**lease.fields, plan=plan)

    async def _frame(self, claim, lease, workspace):
        frame = claim.frame
        path = workspace / "source"
        await lease.call(self.storage.download_blob, frame.plan.input.blob, path)
        output = await lease.call(
            extract_retained_frame,
            path,
            workspace / ("frame." + frame.plan.sampling.format),
            frame.plan,
            frame.entry,
            policy=self.policy,
        )
        blob = await lease.call(
            self.storage.upload_blob,
            output.path,
            claim.blob_key,
            sha256=output.sha256,
            byte_size=output.byte_size,
        )
        if not isinstance(blob, v.FrameBlob) or (
            blob.key != claim.blob_key
            or blob.sha256 != output.sha256
            or blob.byte_size != output.byte_size
        ):
            raise c.VideoInputFailure("input_corrupt")
        return c.FinishVideoFrameRequest(**lease.fields, blob=blob)

    async def execute_once(self, *, input_id=None):
        request = c.ClaimVideoInputRequest(
            worker_id=self.worker_id,
            input_id=input_id,
            lease_seconds=self.policy.lease_seconds,
        )
        return await self._execute("input", request)

    async def execute_frame_once(self, *, frame_id=None):
        request = c.ClaimVideoFrameRequest(
            worker_id=self.worker_id,
            frame_id=frame_id,
            lease_seconds=self.policy.lease_seconds,
        )
        return await self._execute("frame", request)

    async def _execute(self, kind, request):
        claimed = await self.database.call("claim_accepted_video_" + kind, request)
        if claimed.claim is None:
            return None
        claim = claimed.claim
        lease = _Lease(self.database, claim, self.policy)
        try:
            await lease.start()
            size = (
                claim.input.source.object_identity.byte_size
                if kind == "input"
                else claim.frame.plan.input.blob.byte_size
            )
            required = size + self.policy.max_timebase_bytes + 64 * 1024**2
            if (
                not 0 < size <= self.policy.max_source_bytes
                or required > self.policy.max_temp_bytes
            ):
                raise c.VideoInputFailure("resource_limit")
            await lease.call(self.storage.assert_versioning)
            if self.temp_root is not None:
                self.temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(
                prefix="accepted-video-", dir=self.temp_root
            ) as directory:
                if shutil.disk_usage(directory).free < required:
                    raise c.VideoInputFailure("resource_limit")
                finish = await (self._original if kind == "input" else self._frame)(
                    claim, lease, Path(directory)
                )
                # Keep the exact measured output and attempt across response loss
                # and contention; never read another source or create a new version.
                return await self._persist(
                    lease, "finish_accepted_video_" + kind, finish
                )
        except asyncio.CancelledError:
            raise
        except (c.VideoInputFailure, InputFailure, ImageFailure, MediaFailure) as exc:
            return await self._fail(
                lease, exc.code if exc.code in _ERRORS else "storage_unavailable"
            )
        except Exception:
            return await self._fail(lease, "storage_unavailable")
        finally:
            await lease.close()

    async def _fail(self, lease, code):
        if code == "lease_lost":
            return None
        model = (
            c.FailVideoInputRequest
            if lease.kind == "input"
            else c.FailVideoFrameRequest
        )
        try:
            return await self.database.call(
                "fail_accepted_video_" + lease.kind,
                model(**lease.fields, error_code=code),
            )
        except c.VideoInputFailure as exc:
            if exc.code == "lease_lost":
                return None
            raise
