"""Per-object media facts, published only after the owning native work closes."""

import asyncio
from contextlib import contextmanager
import logging

from app.services import storage_access, storage_usage_accounting as accounting

log = logging.getLogger(__name__)


class Attempt:
    def __init__(self, recorder, handle, key, receipt):
        self.recorder, self.handle, self.key = recorder, handle, key
        self.receipt = dict(receipt)
        self.evidence = {"physical_outcome": "unknown"}

    def written(self, response):
        if (
            isinstance(response, dict)
            and response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 200
        ):
            self.evidence.update(
                physical_outcome="written",
                physical_size_bytes=self.receipt["size_bytes"],
                returned_version=response.get("VersionId"),
            )

    def verified(self, client, version):
        returned = self.evidence.get("returned_version")
        valid = isinstance(returned, str) and bool(returned) and returned != "null"
        self.evidence["full_readback"] = valid and version == returned
        if self.evidence["full_readback"]:
            current = accounting.current_matches(
                client,
                self.recorder.bucket,
                self.key,
                returned,
                self.receipt["size_bytes"],
            )
            self.evidence["current_head_matches"] = current
            if current and self.evidence["physical_outcome"] == "written":
                self.evidence["current_delta_bytes"] = self.receipt["size_bytes"]


class Recorder:
    def __init__(self, factory, producer, owner_id, endpoint_url, bucket):
        self.factory, self.producer, self.owner_id = factory, producer, owner_id
        self.endpoint_url, self.bucket = endpoint_url, bucket
        self.loop = asyncio.get_running_loop()
        self.attempts = []

    async def _begin(self, key, receipt):
        async with self.factory() as db:
            handle = await accounting.begin_in_transaction(
                db,
                endpoint_url=self.endpoint_url,
                bucket=self.bucket,
                object_key=key,
                producer=self.producer,
                owner_id=self.owner_id,
                purpose="media_cache",
                kind="create",
                new_request=True,
                evidence={
                    "expected_sha256": receipt["sha256"],
                    "expected_size_bytes": receipt["size_bytes"],
                },
            )
            await db.commit()
        return handle

    def begin(self, key, receipt):
        # Only the owner loop touches its Engine/Session; the SDK thread waits
        # for a durable short transaction before it may submit this exact PUT.
        coroutine = self._begin(key, receipt)
        try:
            pending = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        except BaseException:
            coroutine.close()
            raise
        result = Attempt(self, pending.result(), key, receipt)
        self.attempts.append(result)
        return result

    async def finish(self, *, settled):
        async def publish():
            for attempt in self.attempts:
                try:
                    await accounting.publish(
                        self.factory, attempt.handle, attempt.evidence, settled=settled
                    )
                except Exception:
                    log.warning(
                        "Media accounting publication deferred producer=%s",
                        self.producer,
                    )

        pending = asyncio.create_task(publish())
        try:
            await asyncio.shield(pending)
        except BaseException:
            try:
                await storage_access.finish(pending)
            except Exception:
                log.warning(
                    "Media accounting drain deferred producer=%s", self.producer
                )
            raise


@contextmanager
def intent(recorder, key, receipt):
    attempt = recorder.begin(key, receipt) if recorder is not None else None
    try:
        yield attempt
    except BaseException as exc:
        if attempt is not None:
            response = getattr(exc, "response", {})
            if response.get("Error", {}).get("Code") in {"412", "PreconditionFailed"}:
                attempt.evidence.update(
                    physical_outcome="not_written", current_delta_bytes=0
                )
        raise


def put(recorder, client, key, receipt, *, submitting=None, **params):
    with intent(recorder, key, receipt) as attempt:
        if submitting is not None:
            submitting()
        response = client.put_object(**params)
        if attempt is not None:
            attempt.written(response)
        return response, attempt
