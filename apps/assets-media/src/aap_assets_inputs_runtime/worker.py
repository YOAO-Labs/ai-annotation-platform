"""Materialize accepted bytes without holding business locks across object I/O."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
import random
import shutil
import tempfile
import time
from uuid import uuid4

from aap_assets_images import contract as image
from aap_assets_images.policy import ImagePolicy
from aap_assets_inputs import contract as c


class _Lease:
    def __init__(self, database, claim, policy):
        self.database, self.claim, self.policy = database, claim, policy
        self.lease_seconds = min(policy.lease_seconds, 7200)
        self.deadline = time.monotonic() + policy.job_timeout_seconds
        self.stopped = asyncio.Event()
        self.lost = asyncio.Event()
        self.task = None

    async def beat(self):
        await self.database.call(
            "heartbeat_accepted_image_input",
            c.HeartbeatInputRequest(
                input_id=self.claim.input.input_id,
                generation=self.claim.generation,
                lease_token=self.claim.lease_token,
                lease_seconds=self.lease_seconds,
            ),
        )

    async def start(self):
        await self.beat()
        self.task = asyncio.create_task(self.run())

    async def run(self):
        try:
            while not self.stopped.is_set():
                try:
                    await asyncio.wait_for(
                        self.stopped.wait(),
                        timeout=max(1, self.lease_seconds / 3),
                    )
                    return
                except TimeoutError:
                    pass
                await self.beat()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.lost.set()

    def check(self):
        if self.lost.is_set():
            raise c.InputFailure("lease_lost")
        if time.monotonic() >= self.deadline:
            raise c.InputFailure("timeout")

    async def call(self, operation, *args, **kwargs):
        self.check()
        try:
            async with asyncio.timeout_at(self.deadline):
                result = await operation(*args, **kwargs)
        except TimeoutError:
            raise c.InputFailure("timeout") from None
        self.check()
        return result

    async def close(self):
        self.stopped.set()
        if self.task is not None:
            with suppress(asyncio.CancelledError):
                await self.task


class InputExecutor:
    def __init__(
        self,
        database,
        storage,
        image_control,
        *,
        worker_id=None,
        policy=None,
        temp_root=None,
    ):
        self.database, self.storage, self.images = database, storage, image_control
        self.worker_id = worker_id or uuid4()
        self.policy = policy or ImagePolicy()
        self.temp_root = Path(temp_root) if temp_root is not None else None

    async def _materialize(self, claim, lease):
        source = claim.source
        expected = source.object_identity
        if expected is None:
            raise c.InputFailure("source_changed")
        if (
            expected.byte_size > self.policy.max_source_bytes
            or expected.byte_size > self.policy.max_temp_bytes
        ):
            raise c.InputFailure("resource_limit")
        await lease.call(self.storage.assert_versioning)
        if self.temp_root is not None:
            self.temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(
            prefix="accepted-input-", dir=self.temp_root
        ) as workspace:
            if (
                shutil.disk_usage(workspace).free
                < expected.byte_size + 64 * 1024 * 1024
            ):
                raise c.InputFailure("resource_limit")
            destination = Path(workspace) / "source"
            measured = await lease.call(
                self.storage.download_source, source, destination
            )
            if (
                measured.identity != expected
                or measured.byte_size != expected.byte_size
            ):
                raise c.InputFailure("source_changed")
            blob = await lease.call(
                self.storage.upload_blob,
                destination,
                claim.blob_key,
                sha256=measured.sha256,
                byte_size=measured.byte_size,
            )
            if blob.sha256 != measured.sha256 or blob.byte_size != measured.byte_size:
                raise c.InputFailure("input_corrupt")
            observed = await lease.call(
                self.images.observe_internal_source,
                image.InternalImageSourceRequest(
                    owner=source.owner,
                    expected_source_snapshot=source.source_snapshot,
                    object_identity=expected,
                ),
            )
            if (
                observed.owner != source.owner
                or observed.source_generation != source.source_generation
                or observed.source_snapshot != source.source_snapshot
                or observed.object_identity != expected
                or observed.source_drifted
                or not observed.upload_ready
            ):
                raise c.InputFailure("source_changed")
            return blob

    async def execute_once(self, *, input_id=None):
        claimed = await self.database.call(
            "claim_accepted_image_input",
            c.ClaimInputRequest(
                worker_id=self.worker_id,
                input_id=input_id,
                lease_seconds=min(self.policy.lease_seconds, 7200),
            ),
        )
        if claimed.claim is None:
            return None
        claim = claimed.claim
        lease = _Lease(self.database, claim, self.policy)
        try:
            await lease.start()
            try:
                blob = await self._materialize(claim, lease)
            except image.ImageFailure as exc:
                raise c.InputFailure(exc.code) from None
            finish = c.FinishInputRequest(
                input_id=claim.input.input_id,
                generation=claim.generation,
                lease_token=claim.lease_token,
                blob=blob,
            )
            # Reuse the verified Blob when persistence is briefly busy; never copy
            # another current source or upload another version inside this attempt.
            failures = 0
            while True:
                lease.check()
                try:
                    return await self.database.call(
                        "finish_accepted_image_input", finish
                    )
                except c.InputFailure as exc:
                    if exc.code not in {
                        "source_busy",
                        "input_busy",
                        "job_busy",
                        "reference_busy",
                        "task_busy",
                        "storage_unavailable",
                    }:
                        raise
                    failures += 1
                    delay = min(2, 0.05 * 2 ** min(failures, 6)) * random.uniform(
                        0.75, 1.25
                    )
                    if time.monotonic() + delay >= lease.deadline:
                        raise
                    await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except c.InputFailure as exc:
            return await self._fail(claim, exc.code)
        except Exception:
            return await self._fail(claim, "storage_unavailable")
        finally:
            await lease.close()

    async def _fail(self, claim, code):
        if code == "lease_lost":
            return None
        try:
            return await self.database.call(
                "fail_accepted_image_input",
                c.FailInputRequest(
                    input_id=claim.input.input_id,
                    generation=claim.generation,
                    lease_token=claim.lease_token,
                    error_code=code,
                ),
            )
        except c.InputFailure as failure:
            if failure.code == "lease_lost":
                return None
            raise


class InputGarbageCollector:
    def __init__(self, database, storage, *, worker_id=None):
        self.database, self.storage = database, storage
        self.worker_id = worker_id or uuid4()

    async def collect_once(self):
        batch = await self.database.call(
            "claim_accepted_input_garbage",
            c.ClaimInputGarbageRequest(worker_id=self.worker_id),
        )
        completed = 0
        for garbage in batch.items:
            await self.storage.delete_attempt_versions(garbage.prefix)
            result = await self.database.call(
                "complete_accepted_input_garbage",
                c.CompleteInputGarbageRequest(
                    garbage_id=garbage.garbage_id, lease_token=garbage.lease_token
                ),
            )
            completed += int(result.completed)
        return completed
