"""Owner-scoped managed-original writes for the Assets source runtime.

Every target is allocated by the native role/auxiliary allocation capability;
this module only performs the conditional write, the pinned staging copy or
adoption read, and the full saved-version readback that feeds the native
prepared/writing/written/verified lifecycle.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from typing import Any, BinaryIO, Callable

from yoao_assets_images.contract import ObjectIdentity
from yoao_assets_source import managed_original
from yoao_assets_source.managed_original_contract import (
    ManagedOriginalMaterial,
    ManagedOriginalProof,
)

from .ingest_storage_errors import ManagedStorageError

PART_BYTES = 8 * 1024 * 1024
IMAGE_HEADER_BYTES = 256 * 1024
TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class ManagedWrite:
    file_size: int
    content_hash: str
    dimensions: tuple[int, int] | None
    object_identity: ObjectIdentity | None
    proof: ManagedOriginalProof


@dataclass(frozen=True)
class ManagedRead:
    sha256: str
    md5: str
    size_bytes: int
    head_bytes: bytes
    dimensions: tuple[int, int] | None
    etag: str
    version_id: str | None
    observation: dict[str, Any]


class ManagedStorage:
    """One-attempt conditional writes against owner-allocated targets."""

    def __init__(
        self,
        platform_client: Any,
        *,
        timeout_seconds: float = TIMEOUT_SECONDS,
        client_for_target: Callable[[ManagedOriginalMaterial], Any] | None = None,
    ) -> None:
        self._platform_client = platform_client
        self._timeout = timeout_seconds
        self._client_for_target = client_for_target

    def _check(self, before_io: Callable[[], None] | None) -> None:
        if before_io is not None:
            before_io()

    @contextmanager
    def _client_scope(self, target: ManagedOriginalMaterial):
        """Own connection-backed clients for exactly one physical operation."""
        if target.destination is None:
            # The caller owns the platform client for its whole process.
            yield self._platform_client
            return
        if self._client_for_target is None:
            raise ManagedStorageError("storage_write_failed")
        try:
            client = self._client_for_target(target)
        except ManagedStorageError:
            raise
        except Exception:
            raise ManagedStorageError("storage_write_failed") from None
        try:
            yield client
        except BaseException as exc:
            try:
                _close_client(client)
            except ManagedStorageError:
                try:
                    exc.storage_operation_unsettled = True
                except Exception:
                    pass
            raise
        else:
            _close_client(client)

    async def publish_stream(
        self,
        stream: BinaryIO,
        target: ManagedOriginalMaterial,
        *,
        content_type: str,
        expected_size: int,
        expected_hash: str | None,
        image: bool,
        before_io: Callable[[], None] | None = None,
        on_prepared: Callable[[Any], Any] | None = None,
        on_writing: Callable[[], Any] | None = None,
        on_written: Callable[[Any], Any] | None = None,
    ) -> ManagedWrite:
        with self._client_scope(target) as client:
            return await self._publish_stream_with(
                client,
                stream,
                target,
                content_type=content_type,
                expected_size=expected_size,
                expected_hash=expected_hash,
                image=image,
                before_io=before_io,
                on_prepared=on_prepared,
                on_writing=on_writing,
                on_written=on_written,
            )

    async def _publish_stream_with(
        self,
        client: Any,
        stream: BinaryIO,
        target: ManagedOriginalMaterial,
        *,
        content_type: str,
        expected_size: int,
        expected_hash: str | None,
        image: bool,
        before_io: Callable[[], None] | None = None,
        on_prepared: Callable[[Any], Any] | None = None,
        on_writing: Callable[[], Any] | None = None,
        on_written: Callable[[Any], Any] | None = None,
    ) -> ManagedWrite:
        check = _deadline(before_io, self._timeout)
        physical = _Physical()
        prepared = receipt = None
        completion_started = False
        try:
            prepared = await physical.run(
                managed_original.prepare_stream,
                client,
                target.bucket,
                target.object_key,
                stream,
                content_type=content_type,
                max_bytes=max(expected_size, 0),
                timeout_seconds=self._timeout,
                _check=check,
            )
            physical.check()
            if prepared.size_bytes != expected_size:
                raise ManagedStorageError("archive_entry_read_failed")
            if expected_hash is not None and prepared.md5 != expected_hash:
                raise ManagedStorageError("archive_entry_read_failed")
            if on_prepared is not None:
                await on_prepared(prepared)
            if on_writing is not None:
                await on_writing()
            physical.check()
            completion_started = True
            receipt = await physical.run(
                managed_original.complete,
                client,
                target.bucket,
                target.object_key,
                prepared,
                content_type=content_type,
            )
            if on_written is not None:
                await on_written(receipt)
            physical.check()
            verified = await physical.run(
                managed_original.verify,
                client,
                target.bucket,
                target.object_key,
                receipt,
                sha256=prepared.sha256,
                size_bytes=prepared.size_bytes,
                max_bytes=max(expected_size, 0),
                timeout_seconds=self._timeout,
            )
            physical.check()
            return await _write_result(target, prepared, receipt, verified, image=image)
        except BaseException as exc:
            raise await _publication_failure(
                exc, client, target, prepared, receipt, completion_started
            ) from None

    async def publish_object(
        self,
        target: ManagedOriginalMaterial,
        *,
        source_client: Any,
        source_bucket: str,
        source_key: str,
        source_etag: str | None,
        source_version_id: str | None,
        content_type: str,
        max_bytes: int,
        image: bool,
        before_io: Callable[[], None] | None = None,
        on_prepared: Callable[[Any], Any] | None = None,
        on_writing: Callable[[], Any] | None = None,
        on_written: Callable[[Any], Any] | None = None,
    ) -> ManagedWrite:
        """Publish one staging object through a pinned, fully hashed stream."""
        with self._client_scope(target) as client:
            return await self._publish_object_with(
                client,
                target,
                source_client=source_client,
                source_bucket=source_bucket,
                source_key=source_key,
                source_etag=source_etag,
                source_version_id=source_version_id,
                content_type=content_type,
                max_bytes=max_bytes,
                image=image,
                before_io=before_io,
                on_prepared=on_prepared,
                on_writing=on_writing,
                on_written=on_written,
            )

    async def _publish_object_with(
        self,
        client: Any,
        target: ManagedOriginalMaterial,
        *,
        source_client: Any,
        source_bucket: str,
        source_key: str,
        source_etag: str | None,
        source_version_id: str | None,
        content_type: str,
        max_bytes: int,
        image: bool,
        before_io: Callable[[], None] | None = None,
        on_prepared: Callable[[Any], Any] | None = None,
        on_writing: Callable[[], Any] | None = None,
        on_written: Callable[[Any], Any] | None = None,
    ) -> ManagedWrite:
        self._check(before_io)
        physical = _Physical()
        prepared = receipt = None
        completion_started = False
        try:
            source = await physical.run(
                managed_original.inspect_object,
                source_client,
                source_bucket,
                source_key,
                max_bytes=max_bytes,
                timeout_seconds=self._timeout,
            )
            physical.check()
            if source_etag is not None and source.etag != source_etag:
                raise ManagedStorageError("source_changed")
            if source_version_id is not None and source.version_id != source_version_id:
                raise ManagedStorageError("source_changed")
            prepared = await physical.run(
                managed_original.prepare_copy,
                client,
                source_bucket,
                source_key,
                target.bucket,
                target.object_key,
                source,
                content_type=content_type,
                timeout_seconds=self._timeout,
                source_client=source_client,
            )
            physical.check()
            if on_prepared is not None:
                await on_prepared(prepared)
            if on_writing is not None:
                await on_writing()
            physical.check()
            completion_started = True
            receipt = await physical.run(
                managed_original.complete,
                client,
                target.bucket,
                target.object_key,
                prepared,
                content_type=content_type,
            )
            if on_written is not None:
                await on_written(receipt)
            physical.check()
            verified = await physical.run(
                managed_original.verify,
                client,
                target.bucket,
                target.object_key,
                receipt,
                sha256=prepared.sha256,
                size_bytes=prepared.size_bytes,
                max_bytes=max_bytes,
                timeout_seconds=self._timeout,
            )
            physical.check()
            return await _write_result(target, prepared, receipt, verified, image=image)
        except BaseException as exc:
            raise await _publication_failure(
                exc, client, target, prepared, receipt, completion_started
            ) from None

    async def adopt(
        self,
        target: ManagedOriginalMaterial,
        *,
        max_bytes: int,
        image: bool,
    ) -> ManagedWrite:
        """Fully read and pin bytes that already exist at the allocated key."""
        with self._client_scope(target) as client:
            return await self._adopt_with(
                client, target, max_bytes=max_bytes, image=image
            )

    async def _adopt_with(
        self,
        client: Any,
        target: ManagedOriginalMaterial,
        *,
        max_bytes: int,
        image: bool,
    ) -> ManagedWrite:
        try:
            observed = await _thread(
                managed_original.inspect_object,
                client,
                target.bucket,
                target.object_key,
                max_bytes=max_bytes,
                timeout_seconds=self._timeout,
            )
        except managed_original.WriterError as exc:
            raise _map_writer_error(exc) from None
        receipt = managed_original.WriteReceipt(
            etag=observed.etag,
            version_id=observed.version_id,
            request_id=None,
            response={
                "ETag": observed.etag,
                **(
                    {"VersionId": observed.version_id}
                    if observed.version_id is not None
                    else {}
                ),
            },
        )
        proof = ManagedOriginalProof.verified(
            mode="adopt",
            sha256=observed.sha256,
            md5=observed.md5,
            size_bytes=observed.size_bytes,
            etag=receipt.etag,
            version_id=receipt.version_id,
            request_id=None,
            prepared={
                "mode": "adopt",
                "bucket": target.bucket,
                "object_key": target.object_key,
                "sha256": observed.sha256,
                "md5": observed.md5,
                "size_bytes": observed.size_bytes,
            },
            verification={
                "sha256": observed.sha256,
                "md5": observed.md5,
                "size_bytes": observed.size_bytes,
                "etag": receipt.etag,
                "version_id": receipt.version_id,
                "observation": observed.observation,
            },
            observed={"observation": observed.observation},
        )
        dimensions = (
            await asyncio.to_thread(_image_dimensions, observed.head_bytes)
            if image
            else None
        )
        return ManagedWrite(
            file_size=observed.size_bytes,
            content_hash=observed.md5,
            dimensions=dimensions,
            object_identity=(
                _object_identity(observed.observation, observed.size_bytes)
                if image
                else None
            ),
            proof=proof,
        )

    async def verify(
        self,
        target: ManagedOriginalMaterial,
        *,
        sha256: str,
        size_bytes: int,
        image: bool = False,
        timeout_seconds: float | None = None,
    ) -> ManagedRead:
        """Re-read the exact saved version before reusing a verified creation."""
        if target.etag is None:
            raise ManagedStorageError("storage_write_failed")
        with self._client_scope(target) as client:
            try:
                verified = await _thread(
                    managed_original.verify,
                    client,
                    target.bucket,
                    target.object_key,
                    managed_original.WriteReceipt(
                        etag=target.etag,
                        version_id=target.version_id,
                        request_id=None,
                        response={},
                    ),
                    sha256=sha256,
                    size_bytes=size_bytes,
                    max_bytes=max(size_bytes, 1),
                    timeout_seconds=self._timeout
                    if timeout_seconds is None
                    else timeout_seconds,
                )
            except managed_original.WriterError as exc:
                raise _map_writer_error(exc) from None
            dimensions = (
                await asyncio.to_thread(_image_dimensions, verified.head_bytes)
                if image
                else None
            )
        return ManagedRead(
            sha256=verified.sha256,
            md5=verified.md5,
            size_bytes=verified.size_bytes,
            head_bytes=verified.head_bytes,
            dimensions=dimensions,
            etag=target.etag,
            version_id=target.version_id,
            observation=verified.observation,
        )


async def _write_result(
    target: ManagedOriginalMaterial,
    prepared: managed_original.Prepared,
    receipt: managed_original.WriteReceipt,
    verified: managed_original.Verified,
    *,
    image: bool,
) -> ManagedWrite:
    dimensions = (
        await asyncio.to_thread(_image_dimensions, verified.head_bytes)
        if image
        else None
    )
    proof = ManagedOriginalProof.verified(
        mode=target.mode,
        sha256=prepared.sha256,
        md5=prepared.md5,
        size_bytes=prepared.size_bytes,
        etag=receipt.etag,
        version_id=receipt.version_id,
        request_id=receipt.request_id,
        prepared=prepared.evidence(),
        verification={
            **verified.evidence(),
            "etag": receipt.etag,
            "version_id": receipt.version_id,
        },
        observed={"observation": verified.observation},
    )
    return ManagedWrite(
        file_size=prepared.size_bytes,
        content_hash=prepared.md5,
        dimensions=dimensions,
        object_identity=(
            _object_identity(verified.observation, prepared.size_bytes)
            if image
            else None
        ),
        proof=proof,
    )


def _object_identity(observation: dict, byte_size: int) -> ObjectIdentity | None:
    etag = observation.get("etag")
    if not isinstance(etag, str):
        return None
    etag = etag.strip().strip('"')
    version_id = observation.get("version_id")
    try:
        return ObjectIdentity(etag=etag, byte_size=byte_size, version_id=version_id)
    except (TypeError, ValueError):
        return None


def _image_dimensions(head: bytes) -> tuple[int, int] | None:
    if not head:
        return None
    try:
        from PIL import Image

        with Image.open(BytesIO(head)) as image:
            width, height = int(image.width), int(image.height)
            try:
                orientation = int(image.getexif().get(274, 1))
            except Exception:
                orientation = 1
            if width < 1 or height < 1:
                return None
            return (height, width) if orientation in {5, 6, 7, 8} else (width, height)
    except Exception:
        return None


def _close_client(client: Any) -> None:
    """A failed close is not proof that the client stopped owning bytes."""
    try:
        client.close()
    except BaseException:
        raise ManagedStorageError("storage_write_failed", uncertain=True) from None


def _map_writer_error(exc: managed_original.WriterError) -> ManagedStorageError:
    if exc.outcome == "unknown" or getattr(exc, "storage_operation_unsettled", False):
        return ManagedStorageError("storage_write_failed", uncertain=True)
    if exc.code == "creation_source_changed" or exc.outcome == "conflict":
        return ManagedStorageError("source_changed", outcome=exc.outcome)
    return ManagedStorageError("storage_write_failed")


def _deadline(before_io: Callable[[], None] | None, timeout: float):
    import time

    deadline = time.monotonic() + max(0.1, float(timeout))

    def check() -> None:
        if before_io is not None:
            before_io()
        if time.monotonic() >= deadline:
            raise ManagedStorageError("storage_write_failed")

    return check


class _Physical:
    """Adopt a late result before honoring cancellation or closing its owner."""

    def __init__(self) -> None:
        self.interrupted: BaseException | None = None

    async def run(self, function, /, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except BaseException as exc:
            if task.done() and (task.cancelled() or task.exception() is not None):
                return task.result()
            if self.interrupted is None:
                self.interrupted = exc
            while not task.done():
                try:
                    await asyncio.shield(task)
                except BaseException:
                    continue
            return task.result()

    def check(self) -> None:
        if self.interrupted is not None:
            raise self.interrupted


async def _publication_failure(
    exc, client, target, prepared, receipt, completion_started
):
    if prepared is not None and receipt is None and (
        not completion_started or getattr(exc, "outcome", None) in {"failed", "conflict"}
    ):
        try:
            await _thread(
                managed_original.abort_prepared,
                client,
                target.bucket,
                target.object_key,
                prepared,
            )
        except BaseException as abort_error:
            combined = BaseExceptionGroup(
                "Managed original failure and multipart abort failed", [exc, abort_error]
            )
            combined.storage_operation_unsettled = True
            return combined
    error = _map_writer_error(exc) if isinstance(exc, managed_original.WriterError) else exc
    if receipt is not None:
        error.physical_receipt = receipt.evidence()
    return error


async def _thread(function, /, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except BaseException:
                continue
        # Propagate the completed physical failure rather than hide it behind
        # cancellation; handles are no longer in use when this scope unwinds.
        task.result()
        raise
