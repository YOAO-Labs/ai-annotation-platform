"""Per-object media facts, published only after the owning native work closes."""

import asyncio
from contextlib import contextmanager
import logging

from app.services import storage_access, storage_usage_accounting as accounting

log = logging.getLogger(__name__)


class Attempt:
    def __init__(self, recorder, handle, key, receipt, fixture_version=None):
        self.recorder, self.handle, self.key = recorder, handle, key
        self.receipt = dict(receipt)
        self.fixture_version = fixture_version
        self.fixture_etag = self.fixture_returned_version = None
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
        if self.fixture_version is not None:
            etag = response.get("ETag") if isinstance(response, dict) else None
            version = response.get("VersionId") if isinstance(response, dict) else None
            if (
                not isinstance(response, dict)
                or response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
                or not isinstance(etag, str)
                or not etag
                or version is not None
                and (not isinstance(version, str) or not version)
            ):
                raise storage_access.mark_unsettled(
                    RuntimeError("fixture_media_write_receipt_invalid")
                )
            self.fixture_etag, self.fixture_returned_version = etag, version
            self.recorder.fixture_call(
                self.recorder.fixture.record_version(
                    self.fixture_version, etag, version
                )
            )

    def verified(self, client, version):
        if self.fixture_version is not None:
            try:
                params = {"Bucket": self.recorder.bucket, "Key": self.key}
                if self.fixture_returned_version is not None:
                    params["VersionId"] = self.fixture_returned_version
                observed = client.head_object(**params)
                if (
                    observed.get("ETag") != self.fixture_etag
                    or observed.get("VersionId") != self.fixture_returned_version
                    or observed.get("ContentLength") != self.receipt["size_bytes"]
                ):
                    raise RuntimeError("fixture_media_write_receipt_changed")
                self.recorder.fixture_call(
                    self.recorder.fixture.verify_version(
                        self.fixture_version, self.fixture_returned_version
                    )
                )
            except BaseException as exc:
                raise storage_access.mark_unsettled(exc)
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
    def __init__(
        self,
        factory,
        producer,
        owner_id,
        endpoint_url,
        bucket,
        *,
        purpose="media_cache",
    ):
        self.factory, self.producer, self.owner_id = factory, producer, owner_id
        self.endpoint_url, self.bucket = endpoint_url, bucket
        self.purpose = purpose
        self.loop = asyncio.get_running_loop()
        self.attempts = []
        from app.services import fixture_media_execution

        self.fixture = fixture_media_execution.current()

    def fixture_call(self, coroutine):
        try:
            pending = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        except BaseException as exc:
            coroutine.close()
            raise storage_access.mark_unsettled(exc)
        try:
            return pending.result()
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)

    async def _begin(self, key, receipt):
        async with self.factory() as db:
            handle = await accounting.begin_in_transaction(
                db,
                endpoint_url=self.endpoint_url,
                bucket=self.bucket,
                object_key=key,
                producer=self.producer,
                owner_id=self.owner_id,
                purpose=self.purpose,
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
        coroutine = self._begin(key, receipt)
        try:
            pending = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        except BaseException:
            coroutine.close()
            raise
        handle = pending.result()
        fixture_version = None
        if self.fixture is not None:
            owner_kind = {
                "thumbnail_outputs": "thumbnail",
                "image_pyramid_outputs": "pyramid_output",
                "video_derivative_outputs": "video_output",
            }.get(self.producer)
            if owner_kind is None:
                raise RuntimeError("fixture_media_owner_invalid")
            fixture_version = self.fixture_call(
                self.fixture.begin_version(
                    owner_kind,
                    self.owner_id,
                    self.endpoint_url,
                    self.bucket,
                    key,
                    receipt,
                )
            )
        result = Attempt(self, handle, key, receipt, fixture_version)
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
