"""Retire exact saved Native pyramid outputs in the configured S3 store."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
import threading
from typing import Protocol
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from yoao_assets_images.contract import (
    ClaimNativePyramidRetirementRequest,
    ClaimedNativePyramidRetirement,
    CompleteNativePyramidRetirementPageRequest,
    NativePyramidRetirementAck,
    NativePyramidRetirementClaim,
    NativePyramidRetirementMember,
    NativePyramidRetirementPage,
    NativePyramidRetirementStore,
    NativePyramidRetirementTarget,
    PrepareNativePyramidRetirementPageRequest,
)

from .config import CleanupConfig
from .data import DataContractError
from .health import HealthState
from .object_store import CleanupStopped

log = logging.getLogger("aap.assets_cleanup")

_CHUNK = 1024 * 1024
_MAX_KEY_BYTES = 1024
_MAX_PAGE_SIZE = 100
NATIVE_RETIREMENT_LEASE_SECONDS = 300


class NativeTargetRefused(RuntimeError):
    """The claimed saved output is outside the configured cleanup store."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class NativeStorageError(RuntimeError):
    """One exact member or prefix could not be proven settled."""

    def __init__(self, code: str, *, unsettled: bool = False):
        super().__init__(code)
        self.code = code
        self.unsettled = unsettled


class NativeRetirementDataPort(Protocol):
    async def claim_native_pyramid_retirement(
        self, request: ClaimNativePyramidRetirementRequest
    ) -> ClaimedNativePyramidRetirement: ...

    async def prepare_native_pyramid_retirement_page(
        self, request: PrepareNativePyramidRetirementPageRequest
    ) -> NativePyramidRetirementPage: ...

    async def complete_native_pyramid_retirement_page(
        self, request: CompleteNativePyramidRetirementPageRequest
    ) -> NativePyramidRetirementAck: ...


class NativeRetirementStoragePort(Protocol):
    def verify_target(
        self, output_id: UUID, target: NativePyramidRetirementTarget
    ) -> None: ...

    def delete_member(
        self,
        prefix: str,
        member: NativePyramidRetirementMember,
        stop: threading.Event,
    ) -> None: ...

    def verify_prefix_absent(self, prefix: str, stop: threading.Event) -> None: ...

    def close(self) -> None: ...


def _code(exc: BaseException) -> str:
    return str(getattr(exc, "code", type(exc).__name__))[:64]


def _check_stop(stop: threading.Event) -> None:
    if stop.is_set():
        raise CleanupStopped("shutdown before the exact native scope was settled")


class S3NativeRetirementStore:
    """Head, stream, conditionally delete and prove absence of saved members."""

    def __init__(self, config: CleanupConfig):
        self.endpoint = config.s3_endpoint
        self.bucket = config.bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=config.s3_endpoint,
            region_name=config.s3_region,
            aws_access_key_id=config.s3_access_key,
            aws_secret_access_key=config.s3_secret_key,
            config=Config(
                connect_timeout=3,
                read_timeout=15,
                retries={"mode": "standard", "total_max_attempts": 2},
                max_pool_connections=2,
            ),
        )

    def verify_target(
        self, output_id: UUID, target: NativePyramidRetirementTarget
    ) -> None:
        if target.endpoint_url != self.endpoint:
            raise NativeTargetRefused("native_target_endpoint_mismatch")
        if target.bucket != self.bucket:
            raise NativeTargetRefused("native_target_bucket_mismatch")
        if target.schema_ != 2 or target.purpose != "media-cache":
            raise NativeTargetRefused("native_target_kind_mismatch")
        expected = (
            target.key_prefix + "image-pyramids/objects/" + str(output_id) + "/"
        )
        if target.prefix != expected:
            raise NativeTargetRefused("native_target_prefix_mismatch")

    def close(self) -> None:
        self.client.close()

    @staticmethod
    def _member_key(
        prefix: str, member: NativePyramidRetirementMember
    ) -> str:
        relative = member.relative_key
        if (
            not isinstance(prefix, str)
            or not prefix.endswith("/")
            or not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or "\\" in relative
            or "\x00" in relative
        ):
            raise NativeStorageError("native_member_key_invalid")
        key = prefix + relative
        if len(key) > _MAX_KEY_BYTES:
            raise NativeStorageError("native_member_key_invalid")
        return key

    @staticmethod
    def _member_etag(member: NativePyramidRetirementMember) -> str:
        etag = member.etag
        if (
            not isinstance(etag, str)
            or len(etag) < 3
            or not etag.startswith('"')
            or not etag.endswith('"')
            or '"' in etag[1:-1]
            or "\r" in etag
            or "\n" in etag
        ):
            raise NativeStorageError("native_member_etag_invalid")
        return etag

    @staticmethod
    def _member_version(member: NativePyramidRetirementMember) -> str | None:
        version = member.version_id
        if version is None:
            return None
        if (
            not isinstance(version, str)
            or not version
            or len(version) > 1024
            or "\x00" in version
            or "\r" in version
            or "\n" in version
        ):
            raise NativeStorageError("native_member_version_invalid")
        return version

    @staticmethod
    def _member_bytes(member: NativePyramidRetirementMember) -> None:
        sha = member.sha256
        size = member.size_bytes
        if (
            not isinstance(sha, str)
            or len(sha) != 64
            or any(char not in "0123456789abcdef" for char in sha)
            or type(size) is not int
            or size < 0
        ):
            raise NativeStorageError("native_member_receipt_invalid")

    @staticmethod
    def _check_identity(
        response: dict,
        *,
        etag: str,
        size: int,
        version: str | None,
    ) -> None:
        if response.get("ETag") != etag or response.get("ContentLength") != size:
            raise NativeStorageError("native_member_identity_mismatch")
        if version is not None and response.get("VersionId") != version:
            raise NativeStorageError("native_member_identity_mismatch")

    @staticmethod
    def _missing(exc: ClientError) -> bool:
        error = exc.response.get("Error", {})
        code = str(error.get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"NoSuchKey", "NoSuchVersion", "NotFound", "404", "Not Found"}:
            return True
        return status == 404 and not code

    @staticmethod
    def _client_error(
        exc: ClientError, *, mutation: bool
    ) -> NativeStorageError:
        error = exc.response.get("Error", {})
        code = str(error.get("Code", "") or "s3_request_failed")[:64]
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        unsettled = mutation and (
            status is None or status in {408, 409} or status >= 500
        )
        return NativeStorageError(code, unsettled=unsettled)

    def _head(self, stop, key: str, version: str | None):
        _check_stop(stop)
        parameters: dict[str, object] = {"Bucket": self.bucket, "Key": key}
        if version is not None:
            parameters["VersionId"] = version
        try:
            return self.client.head_object(**parameters)
        except ClientError as exc:
            if self._missing(exc):
                return None
            raise self._client_error(exc, mutation=False) from exc
        except Exception as exc:
            raise NativeStorageError("native_head_failed") from exc

    def _read(
        self,
        stop,
        key: str,
        member: NativePyramidRetirementMember,
        version: str | None,
        etag: str,
    ) -> bool | None:
        _check_stop(stop)
        parameters: dict[str, object] = {
            "Bucket": self.bucket,
            "Key": key,
            "IfMatch": etag,
        }
        if version is not None:
            parameters["VersionId"] = version
        try:
            response = self.client.get_object(**parameters)
        except ClientError as exc:
            if self._missing(exc):
                return None
            error = exc.response.get("Error", {})
            code = str(error.get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"PreconditionFailed", "412"} or status == 412:
                raise NativeStorageError(
                    "native_member_identity_changed"
                ) from exc
            raise self._client_error(exc, mutation=False) from exc
        except Exception as exc:
            raise NativeStorageError("native_get_failed") from exc
        body = response.get("Body")
        try:
            if body is None:
                raise NativeStorageError("native_get_failed")
            self._check_identity(
                response,
                etag=etag,
                size=member.size_bytes,
                version=version,
            )
            digest = hashlib.sha256()
            written = 0
            while True:
                _check_stop(stop)
                chunk = body.read(_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > member.size_bytes:
                    raise NativeStorageError("native_member_identity_mismatch")
                digest.update(chunk)
            if (
                written != member.size_bytes
                or digest.hexdigest() != member.sha256
            ):
                raise NativeStorageError("native_member_identity_mismatch")
        except (NativeStorageError, CleanupStopped):
            raise
        except Exception as exc:
            raise NativeStorageError("native_get_failed") from exc
        finally:
            if body is not None:
                with suppress(Exception):
                    body.close()
        return True

    def _delete(
        self,
        stop,
        key: str,
        version: str | None,
        etag: str,
    ) -> None:
        _check_stop(stop)
        parameters: dict[str, object] = {
            "Bucket": self.bucket,
            "Key": key,
            "IfMatch": etag,
        }
        if version is not None:
            parameters["VersionId"] = version
        try:
            response = self.client.delete_object(**parameters)
        except ClientError as exc:
            if self._missing(exc):
                return
            error = exc.response.get("Error", {})
            code = str(error.get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"PreconditionFailed", "412"} or status == 412:
                raise NativeStorageError(
                    "native_member_identity_changed"
                ) from exc
            raise self._client_error(exc, mutation=True) from exc
        except Exception as exc:
            raise NativeStorageError(
                "native_member_delete_unsettled", unsettled=True
            ) from exc
        deleted_version = response.get("VersionId")
        if version is not None and (
            response.get("DeleteMarker") is True
            or (deleted_version is not None and deleted_version != version)
        ):
            raise NativeStorageError(
                "native_member_version_unknown", unsettled=True
            )

    def _verify_absent(
        self, stop, key: str, version: str | None
    ) -> None:
        if self._head(stop, key, version) is not None:
            raise NativeStorageError("native_member_present_after_delete")

    def delete_member(
        self,
        prefix: str,
        member: NativePyramidRetirementMember,
        stop: threading.Event,
    ) -> None:
        key = self._member_key(prefix, member)
        etag = self._member_etag(member)
        version = self._member_version(member)
        self._member_bytes(member)
        head = self._head(stop, key, version)
        if head is None:
            return
        self._check_identity(
            head,
            etag=etag,
            size=member.size_bytes,
            version=version,
        )
        if self._read(stop, key, member, version, etag) is None:
            return
        self._delete(stop, key, version, etag)
        self._verify_absent(stop, key, version)

    def verify_prefix_absent(self, prefix: str, stop: threading.Event) -> None:
        if (
            not isinstance(prefix, str)
            or not prefix
            or not prefix.endswith("/")
            or "\x00" in prefix
        ):
            raise NativeStorageError("native_prefix_invalid")
        self._versions_absent(stop, prefix)
        self._uploads_absent(stop, prefix)
        self._objects_absent(stop, prefix)

    def _versions_absent(self, stop, prefix: str) -> None:
        _check_stop(stop)
        try:
            page = self.client.list_object_versions(
                Bucket=self.bucket, Prefix=prefix, MaxKeys=1
            )
        except ClientError as exc:
            raise self._client_error(exc, mutation=False) from exc
        except Exception as exc:
            raise NativeStorageError("native_prefix_probe_failed") from exc
        if not isinstance(page, dict):
            raise NativeStorageError("native_prefix_scan_incomplete")
        versions = page.get("Versions") or []
        markers = page.get("DeleteMarkers") or []
        if not isinstance(versions, list) or not isinstance(markers, list):
            raise NativeStorageError("native_prefix_scan_incomplete")
        entries = versions + markers
        for entry in entries:
            if not isinstance(entry, dict):
                raise NativeStorageError("native_prefix_scan_incomplete")
            key = entry.get("Key")
            if not isinstance(key, str) or not key.startswith(prefix):
                raise NativeStorageError("native_prefix_foreign_material")
            version = entry.get("VersionId")
            if not isinstance(version, str) or not version:
                raise NativeStorageError("native_prefix_scan_incomplete")
        if entries:
            raise NativeStorageError("native_prefix_not_empty")
        if page.get("IsTruncated") is True:
            raise NativeStorageError("native_prefix_scan_incomplete")

    def _uploads_absent(self, stop, prefix: str) -> None:
        _check_stop(stop)
        try:
            page = self.client.list_multipart_uploads(
                Bucket=self.bucket, Prefix=prefix, MaxUploads=1
            )
        except ClientError as exc:
            raise self._client_error(exc, mutation=False) from exc
        except Exception as exc:
            raise NativeStorageError("native_prefix_probe_failed") from exc
        if not isinstance(page, dict):
            raise NativeStorageError("native_prefix_scan_incomplete")
        uploads = page.get("Uploads") or []
        if not isinstance(uploads, list):
            raise NativeStorageError("native_prefix_scan_incomplete")
        for upload in uploads:
            if not isinstance(upload, dict):
                raise NativeStorageError("native_prefix_scan_incomplete")
            key = upload.get("Key")
            upload_id = upload.get("UploadId")
            if not isinstance(key, str) or not key.startswith(prefix):
                raise NativeStorageError("native_prefix_foreign_material")
            if not isinstance(upload_id, str) or not upload_id:
                raise NativeStorageError("native_prefix_scan_incomplete")
        if uploads:
            raise NativeStorageError("native_prefix_not_empty")
        if page.get("IsTruncated") is True:
            raise NativeStorageError("native_prefix_scan_incomplete")

    def _objects_absent(self, stop, prefix: str) -> None:
        _check_stop(stop)
        try:
            page = self.client.list_objects_v2(
                Bucket=self.bucket, Prefix=prefix, MaxKeys=1
            )
        except ClientError as exc:
            raise self._client_error(exc, mutation=False) from exc
        except Exception as exc:
            raise NativeStorageError("native_prefix_probe_failed") from exc
        if not isinstance(page, dict):
            raise NativeStorageError("native_prefix_scan_incomplete")
        contents = page.get("Contents") or []
        if not isinstance(contents, list):
            raise NativeStorageError("native_prefix_scan_incomplete")
        for entry in contents:
            if not isinstance(entry, dict):
                raise NativeStorageError("native_prefix_scan_incomplete")
            key = entry.get("Key")
            if not isinstance(key, str) or not key.startswith(prefix):
                raise NativeStorageError("native_prefix_foreign_material")
        if contents:
            raise NativeStorageError("native_prefix_not_empty")
        if page.get("IsTruncated") is True:
            raise NativeStorageError("native_prefix_scan_incomplete")


async def _run_native_io(function, *args, stop: threading.Event):
    """Finish the bounded S3 call on cancellation before releasing any page."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, stop))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if task.done() and not task.cancelled():
            error = task.exception()
            if error is not None:
                log.warning(
                    "Native retirement I/O closed after cancellation code=%s",
                    _code(error),
                )
        raise


def _lease_expired(expires_at: datetime) -> bool:
    return datetime.now(timezone.utc) >= expires_at


async def _release_page(
    data: NativeRetirementDataPort,
    page: NativePyramidRetirementPage,
    cursor: UUID | None,
) -> None:
    """Settle drained work without claiming absence or moving the cursor."""
    request = CompleteNativePyramidRetirementPageRequest(
        output_id=page.output_id,
        lease_token=page.lease_token,
        page_id=page.page_id,
        outcome="closed_without_ack",
        operation_ids=[],
    )
    try:
        ack = await data.complete_native_pyramid_retirement_page(request)
    except Exception as exc:  # noqa: BLE001 - the durable page stays authoritative.
        log.warning(
            "Native retirement closure acknowledgement failed output_id=%s code=%s",
            page.output_id,
            _code(exc),
        )
        return
    if ack.output_id != page.output_id:
        log.warning(
            "Native retirement closure acknowledgement named another output"
        )
    elif ack.after_operation_id != cursor:
        log.info(
            "Native retirement closure found an advanced cursor output_id=%s",
            page.output_id,
        )
    elif ack.completed:
        log.info(
            "Native retirement closure found a closed output output_id=%s",
            page.output_id,
        )


@dataclass(frozen=True)
class _RetirementOutcome:
    completed: int
    failed: int
    pages: int
    halted: bool


async def _retire_output(
    data: NativeRetirementDataPort,
    storage: NativeRetirementStoragePort,
    claim: NativePyramidRetirementClaim,
    *,
    page_size: int,
    stop: asyncio.Event,
    storage_stop: threading.Event,
    health: HealthState | None,
) -> _RetirementOutcome:
    previous_cursor = claim.after_operation_id
    lease_expires_at = claim.lease_expires_at
    pages = 0
    while not stop.is_set():
        if _lease_expired(lease_expires_at):
            log.warning(
                "Native retirement lease expired output_id=%s", claim.output_id
            )
            return _RetirementOutcome(0, 0, pages, True)
        try:
            page = await data.prepare_native_pyramid_retirement_page(
                PrepareNativePyramidRetirementPageRequest(
                    output_id=claim.output_id,
                    lease_token=claim.lease_token,
                    page_size=page_size,
                )
            )
        except Exception as exc:  # noqa: BLE001 - an unknown page stays unsettled.
            log.warning(
                "Native retirement page preparation failed output_id=%s code=%s",
                claim.output_id,
                _code(exc),
            )
            if stop.is_set():
                return _RetirementOutcome(0, 1, pages, True)
            try:
                page = await data.prepare_native_pyramid_retirement_page(
                    PrepareNativePyramidRetirementPageRequest(
                        output_id=claim.output_id,
                        lease_token=claim.lease_token,
                        page_size=page_size,
                    )
                )
            except Exception as retry_exc:  # noqa: BLE001 - no page to settle.
                log.warning(
                    "Native retirement page replay failed output_id=%s code=%s",
                    claim.output_id,
                    _code(retry_exc),
                )
                return _RetirementOutcome(0, 1, pages, True)
        pages += 1
        if health is not None:
            health.heartbeat()
        if (
            page.output_id != claim.output_id
            or page.lease_token != claim.lease_token
        ):
            raise DataContractError(
                "Assets returned a retirement page for another lease"
            )
        lease_expires_at = page.lease_expires_at
        if _lease_expired(lease_expires_at):
            log.warning(
                "Native retirement lease expired output_id=%s",
                claim.output_id,
            )
            await _release_page(data, page, previous_cursor)
            return _RetirementOutcome(0, 0, pages, True)
        if page.items:
            if page.after_operation_id is None:
                raise DataContractError(
                    "Assets returned a member page without an advanced cursor"
                )
            if (
                previous_cursor is not None
                and page.after_operation_id.int <= previous_cursor.int
            ):
                raise DataContractError(
                    "Assets returned a non-advancing retirement page"
                )
            if (
                previous_cursor is not None
                and page.items[0].operation_id.int <= previous_cursor.int
            ):
                raise DataContractError(
                    "Assets returned a retirement page behind its cursor"
                )
            for member in page.items:
                if stop.is_set():
                    await _release_page(data, page, previous_cursor)
                    return _RetirementOutcome(0, 0, pages, True)
                if _lease_expired(lease_expires_at):
                    log.warning(
                        "Native retirement lease expired output_id=%s",
                        claim.output_id,
                    )
                    await _release_page(data, page, previous_cursor)
                    return _RetirementOutcome(0, 0, pages, True)
                try:
                    await _run_native_io(
                        storage.delete_member,
                        claim.storage_ref.prefix,
                        member,
                        stop=storage_stop,
                    )
                except CleanupStopped:
                    await _release_page(data, page, previous_cursor)
                    return _RetirementOutcome(0, 0, pages, True)
                except NativeStorageError as exc:
                    log.warning(
                        "Native retirement member failed output_id=%s "
                        "operation_id=%s code=%s unsettled=%s",
                        claim.output_id,
                        member.operation_id,
                        exc.code,
                        exc.unsettled,
                    )
                    if not exc.unsettled:
                        await _release_page(data, page, previous_cursor)
                    return _RetirementOutcome(0, 1, pages, True)
                except Exception as exc:  # noqa: BLE001 - conservative barrier.
                    log.warning(
                        "Native retirement member failed output_id=%s "
                        "operation_id=%s code=%s",
                        claim.output_id,
                        member.operation_id,
                        _code(exc),
                    )
                    return _RetirementOutcome(0, 1, pages, True)
                if health is not None:
                    health.heartbeat()
            request = CompleteNativePyramidRetirementPageRequest(
                output_id=claim.output_id,
                lease_token=claim.lease_token,
                page_id=page.page_id,
                outcome="members_absent",
                operation_ids=[item.operation_id for item in page.items],
            )
            try:
                ack = await data.complete_native_pyramid_retirement_page(request)
            except Exception as exc:  # noqa: BLE001 - never repost the mutation.
                log.warning(
                    "Native retirement page acknowledgement failed "
                    "output_id=%s code=%s",
                    claim.output_id,
                    _code(exc),
                )
                await _release_page(data, page, previous_cursor)
                return _RetirementOutcome(0, 1, pages, True)
            if (
                ack.output_id != page.output_id
                or ack.completed
                or ack.after_operation_id != page.after_operation_id
            ):
                raise DataContractError(
                    "Assets returned an invalid Native retirement page ack"
                )
            previous_cursor = ack.after_operation_id
            continue
        if stop.is_set():
            await _release_page(data, page, previous_cursor)
            return _RetirementOutcome(0, 0, pages, True)
        if previous_cursor is not None and (
            page.after_operation_id is None
            or page.after_operation_id.int < previous_cursor.int
        ):
            raise DataContractError(
                "Assets returned a rewound terminal retirement page"
            )
        try:
            await _run_native_io(
                storage.verify_prefix_absent,
                claim.storage_ref.prefix,
                stop=storage_stop,
            )
        except CleanupStopped:
            await _release_page(data, page, previous_cursor)
            return _RetirementOutcome(0, 0, pages, True)
        except NativeStorageError as exc:
            log.warning(
                "Native retirement prefix check failed output_id=%s code=%s",
                claim.output_id,
                exc.code,
            )
            await _release_page(data, page, previous_cursor)
            return _RetirementOutcome(0, 1, pages, True)
        except Exception as exc:  # noqa: BLE001 - absence remains unproven.
            log.warning(
                "Native retirement prefix check failed output_id=%s code=%s",
                claim.output_id,
                _code(exc),
            )
            await _release_page(data, page, previous_cursor)
            return _RetirementOutcome(0, 1, pages, True)
        request = CompleteNativePyramidRetirementPageRequest(
            output_id=claim.output_id,
            lease_token=claim.lease_token,
            page_id=page.page_id,
            outcome="prefix_absent",
            operation_ids=[],
        )
        try:
            ack = await data.complete_native_pyramid_retirement_page(request)
        except Exception as exc:  # noqa: BLE001 - terminal state stays durable.
            log.warning(
                "Native retirement terminal acknowledgement failed "
                "output_id=%s code=%s",
                claim.output_id,
                _code(exc),
            )
            await _release_page(data, page, previous_cursor)
            return _RetirementOutcome(0, 1, pages, True)
        if (
            ack.output_id != page.output_id
            or not ack.completed
            or ack.after_operation_id != page.after_operation_id
        ):
            raise DataContractError(
                "Assets did not close the Native retirement output"
            )
        return _RetirementOutcome(1, 0, pages, False)
    return _RetirementOutcome(0, 0, pages, True)


async def drain_native_pyramid_retirement(
    data: NativeRetirementDataPort,
    storage: NativeRetirementStoragePort,
    *,
    worker_id: UUID,
    store: NativePyramidRetirementStore,
    page_size: int,
    stop: asyncio.Event,
    storage_stop: threading.Event,
    lease_seconds: int = NATIVE_RETIREMENT_LEASE_SECONDS,
    health: HealthState | None = None,
) -> dict[str, int]:
    """Retire every currently claimable saved output without touching legacy keys."""
    if type(page_size) is not int or not 1 <= page_size <= _MAX_PAGE_SIZE:
        raise ValueError("Native retirement page size must be 1-100")
    if type(lease_seconds) is not int or not 30 <= lease_seconds <= 3600:
        raise ValueError("Native retirement lease must be 30-3600 seconds")
    completed = failed = pages = 0
    while not stop.is_set():
        request = ClaimNativePyramidRetirementRequest(
            worker_id=worker_id,
            store=store,
            lease_seconds=lease_seconds,
        )
        try:
            claimed = await data.claim_native_pyramid_retirement(request)
        except Exception as exc:  # noqa: BLE001 - a lost claim is never replayed.
            log.warning("Native retirement claim failed code=%s", _code(exc))
            failed += 1
            break
        if claimed.claim is None:
            break
        claim = claimed.claim
        storage.verify_target(claim.output_id, claim.storage_ref)
        if health is not None:
            health.heartbeat()
        outcome = await _retire_output(
            data,
            storage,
            claim,
            page_size=page_size,
            stop=stop,
            storage_stop=storage_stop,
            health=health,
        )
        completed += outcome.completed
        failed += outcome.failed
        pages += outcome.pages
        if outcome.halted:
            break
    return {"completed": completed, "failed": failed, "pages": pages}
