"""Retire saved Native pyramid outputs through scoped storage capabilities."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
import threading
from typing import Protocol
from uuid import UUID
from xml.etree import ElementTree

import httpx

from yoao_assets_images.contract import (
    ClaimNativePyramidRetirementRequest,
    ClaimedNativePyramidRetirement,
    CompleteNativePyramidRetirementPageRequest,
    NativePyramidRetirementAck,
    NativePyramidRetirementClaim,
    NativePyramidSavedMember,
    NativePyramidRetirementPage,
    PrepareNativePyramidRetirementPageRequest,
)
from yoao_assets_images.native_storage_client import NativePyramidStorageClient
from yoao_assets_images.native_storage_contract import (
    NativePyramidRetirementIoRequest,
    NativePyramidRetirementPrefixIoRequest,
    NativePyramidSignedMember,
    NativePyramidSignedRequest,
)

from .config import CleanupConfig
from .data import DataContractError
from .health import HealthState
from .object_store import CleanupStopped

log = logging.getLogger("aap.assets_cleanup")

_CHUNK = 1024 * 1024
_MAX_XML_BYTES = 1024 * 1024
_S3_NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"
_MAX_PAGE_SIZE = 100
NATIVE_RETIREMENT_LEASE_SECONDS = 300




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
    async def prepare_page(
        self,
        claim: NativePyramidRetirementClaim,
        page: NativePyramidRetirementPage,
    ) -> dict[UUID, NativePyramidSignedMember]: ...

    async def delete_member(
        self,
        member: NativePyramidSavedMember,
        reference: NativePyramidSignedMember,
        stop: threading.Event,
    ) -> None: ...

    async def verify_prefix_absent(
        self,
        claim: NativePyramidRetirementClaim,
        page: NativePyramidRetirementPage,
        stop: threading.Event,
    ) -> None: ...

    async def aclose(self) -> None: ...


def _code(exc: BaseException) -> str:
    return str(getattr(exc, "code", type(exc).__name__))[:64]


def _check_stop(stop: threading.Event) -> None:
    if stop.is_set():
        raise CleanupStopped("shutdown before the exact native scope was settled")


class SignedNativeRetirementStore:
    """Execute only Source-issued requests for the prepared immutable page."""

    def __init__(self, config: CleanupConfig, *, signer=None, http=None):
        self._owns_signer = signer is None
        self._owns_http = http is None
        self.signer = signer or NativePyramidStorageClient(
            config.native_storage_url, config.native_storage_token
        )
        self.http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=3),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
            headers={"Accept-Encoding": "identity"},
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        try:
            if self._owns_http:
                await self.http.aclose()
        finally:
            if self._owns_signer:
                await self.signer.aclose()

    @staticmethod
    def _scope_matches(reference, claim, page) -> bool:
        return (
            reference.scope_id == page.page_id
            and reference.scope_token == page.lease_token
            and reference.output_id == claim.output_id
            and reference.storage_ref == claim.storage_ref
        )

    async def prepare_page(self, claim, page):
        result = await self.signer.retirement_references(
            NativePyramidRetirementIoRequest(
                output_id=claim.output_id,
                lease_token=page.lease_token,
                page_id=page.page_id,
            )
        )
        if (
            not self._scope_matches(result, claim, page)
            or result.mode != "retirement_members"
            or len(result.members) != len(page.items)
        ):
            raise NativeStorageError("native_retirement_reference_mismatch")
        references = {}
        for expected, reference in zip(page.items, result.members, strict=True):
            if (
                reference.member.model_dump() != expected.model_dump()
                or reference.head is None
                or reference.get is None
                or reference.delete is None
            ):
                raise NativeStorageError("native_member_reference_mismatch")
            references[expected.operation_id] = reference
        return references

    @asynccontextmanager
    async def _response(
        self,
        call: NativePyramidSignedRequest,
        stop: threading.Event,
        *,
        mutation: bool = False,
    ):
        _check_stop(stop)
        if _lease_expired(call.expires_at):
            raise NativeStorageError("native_storage_capability_expired")
        try:
            request = self.http.build_request(
                call.method, call.url, headers=call.headers
            )
            response = await self.http.send(request, stream=True)
        except asyncio.CancelledError as exc:
            exc.storage_operation_unsettled = True
            raise
        except Exception:
            raise NativeStorageError(
                "native_storage_request_failed", unsettled=mutation
            ) from None
        try:
            yield response
        finally:
            try:
                await response.aclose()
            except asyncio.CancelledError as exc:
                exc.storage_operation_unsettled = True
                raise
            except Exception:
                raise NativeStorageError(
                    "native_response_close_unproven", unsettled=True
                ) from None

    @staticmethod
    async def _bounded_body(response, stop) -> bytes:
        body = bytearray()
        try:
            async for chunk in response.aiter_raw():
                _check_stop(stop)
                if len(body) + len(chunk) > _MAX_XML_BYTES:
                    raise NativeStorageError("native_storage_reply_too_large")
                body.extend(chunk)
        except (NativeStorageError, CleanupStopped):
            raise
        except Exception:
            # httpx may mark a response closed before its stream close fails.
            raise NativeStorageError(
                "native_response_body_unsettled", unsettled=True
            ) from None
        return bytes(body)

    @staticmethod
    def _xml(body: bytes):
        if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
            raise NativeStorageError("native_storage_reply_invalid")
        try:
            return ElementTree.fromstring(body.decode("utf-8"))
        except (UnicodeDecodeError, ElementTree.ParseError):
            raise NativeStorageError("native_storage_reply_invalid") from None

    @classmethod
    async def _missing(cls, response, stop) -> bool:
        if response.status_code != 404:
            return False
        if response.headers.get("x-amz-delete-marker", "").lower() == "true":
            raise NativeStorageError("native_member_version_unknown")
        code = response.headers.get("x-amz-error-code")
        body = await cls._bounded_body(response, stop)
        if body:
            root = cls._xml(body)
            if root.tag == "Error":
                code = root.findtext("Code")
            elif root.tag == f"{{{_S3_NAMESPACE}}}Error":
                code = root.findtext(f"{{{_S3_NAMESPACE}}}Code")
            else:
                raise NativeStorageError("native_storage_reply_invalid")
        return code in {None, "", "NoSuchKey", "NoSuchVersion", "NotFound", "404"}

    @staticmethod
    def _status_error(response, *, mutation=False):
        status = response.status_code
        raise NativeStorageError(
            "native_member_identity_changed"
            if status == 412
            else f"native_storage_http_{status}",
            unsettled=mutation and (status in {408, 409} or status >= 500),
        )

    @staticmethod
    def _check_identity(response, member):
        length = response.headers.get("content-length", "")
        try:
            size = int(length) if length.isascii() and length.isdecimal() else -1
        except ValueError:
            size = -1
        actual_version = response.headers.get("x-amz-version-id")
        version_matches = actual_version == member.version_id or (
            member.version_id == "null" and actual_version is None
        )
        if (
            response.headers.get("etag") != member.etag
            or size != member.size_bytes
            or not version_matches
        ):
            raise NativeStorageError("native_member_identity_mismatch")

    async def _head(self, call, member, stop) -> bool:
        async with self._response(call, stop) as response:
            if await self._missing(response, stop):
                return False
            if response.status_code != 200:
                self._status_error(response)
            self._check_identity(response, member)
            return True

    async def _read(self, call, member, stop) -> bool:
        async with self._response(call, stop) as response:
            if await self._missing(response, stop):
                return False
            if response.status_code != 200:
                self._status_error(response)
            self._check_identity(response, member)
            digest = hashlib.sha256()
            size = 0
            try:
                async for chunk in response.aiter_raw(chunk_size=_CHUNK):
                    _check_stop(stop)
                    size += len(chunk)
                    if size > member.size_bytes:
                        raise NativeStorageError("native_member_identity_mismatch")
                    digest.update(chunk)
            except (NativeStorageError, CleanupStopped):
                raise
            except Exception:
                raise NativeStorageError(
                    "native_member_read_failed", unsettled=True
                ) from None
            if size != member.size_bytes or digest.hexdigest() != member.sha256:
                raise NativeStorageError("native_member_identity_mismatch")
            return True

    async def _delete(self, call, member, stop) -> None:
        async with self._response(call, stop, mutation=True) as response:
            if await self._missing(response, stop):
                return
            if response.status_code not in {200, 204}:
                if 200 <= response.status_code < 300:
                    raise NativeStorageError(
                        "native_delete_ack_unproven", unsettled=True
                    )
                self._status_error(response, mutation=True)
            await self._bounded_body(response, stop)
            actual_version = response.headers.get("x-amz-version-id")
            if (
                response.headers.get("x-amz-delete-marker", "").lower() == "true"
                or (
                    actual_version is not None
                    and actual_version != member.version_id
                )
            ):
                raise NativeStorageError(
                    "native_member_version_unknown", unsettled=True
                )

    async def delete_member(self, member, reference, stop) -> None:
        if not await self._head(reference.head, member, stop):
            return
        if not await self._read(reference.get, member, stop):
            return
        await self._delete(reference.delete, member, stop)
        if await self._head(reference.head, member, stop):
            raise NativeStorageError("native_member_present_after_delete")

    async def _verify_multipart_absent(self, claim, page, stop) -> None:
        # Prefix-filtered multipart listing is not reliable on every supported
        # S3 peer. Scan bounded metadata pages; never abort a foreign upload.
        marker = None
        seen = set()
        while True:
            _check_stop(stop)
            request = NativePyramidRetirementPrefixIoRequest(
                output_id=claim.output_id,
                lease_token=page.lease_token,
                page_id=page.page_id,
                kind="uploads",
                key_marker=None if marker is None else marker[0],
                upload_id_marker=None if marker is None else marker[1],
            )
            reference = await self.signer.retirement_prefix_reference(request)
            if (
                not self._scope_matches(reference, claim, page)
                or reference.kind != "uploads"
            ):
                raise NativeStorageError("native_retirement_reference_mismatch")
            async with self._response(reference.request, stop) as response:
                if response.status_code != 200:
                    self._status_error(response)
                root = self._xml(await self._bounded_body(response, stop))
            namespace = (
                f"{{{_S3_NAMESPACE}}}"
                if root.tag == f"{{{_S3_NAMESPACE}}}ListMultipartUploadsResult"
                else ""
            )
            allowed = {
                namespace + name for name in (
                    "Bucket", "KeyMarker", "UploadIdMarker", "NextKeyMarker",
                    "NextUploadIdMarker", "Prefix", "Delimiter", "MaxUploads",
                    "IsTruncated", "Upload", "CommonPrefixes", "EncodingType",
                )
            }
            if (
                root.tag != namespace + "ListMultipartUploadsResult"
                or any(child.tag not in allowed for child in root)
                or root.findtext(namespace + "Bucket") != claim.storage_ref.bucket
                or root.findtext(namespace + "Prefix") not in {None, ""}
                or root.findtext(namespace + "Delimiter") not in {None, ""}
                or root.findtext(namespace + "EncodingType") not in {None, ""}
                or (root.findtext(namespace + "KeyMarker") or "") != (request.key_marker or "")
                or (root.findtext(namespace + "UploadIdMarker") or "") != (request.upload_id_marker or "")
                or root.findall(namespace + "CommonPrefixes")
            ):
                raise NativeStorageError("native_prefix_scan_scope_mismatch")
            uploads = root.findall(namespace + "Upload")
            if len(uploads) > 100:
                raise NativeStorageError("native_prefix_scan_incomplete")
            for upload in uploads:
                key = upload.findtext(namespace + "Key")
                upload_id = upload.findtext(namespace + "UploadId")
                if not key or not upload_id:
                    raise NativeStorageError("native_prefix_scan_incomplete")
                if key.startswith(claim.storage_ref.prefix):
                    raise NativeStorageError("native_prefix_not_empty")
            truncated = root.findtext(namespace + "IsTruncated")
            if truncated == "false":
                return
            next_marker = (
                root.findtext(namespace + "NextKeyMarker"),
                root.findtext(namespace + "NextUploadIdMarker"),
            )
            if (
                truncated != "true"
                or not uploads
                or not all(isinstance(value, str) and value for value in next_marker)
                or next_marker != (key, upload_id)
                or next_marker in seen
                or (marker is not None and next_marker[0] < marker[0])
            ):
                raise NativeStorageError("native_prefix_scan_incomplete")
            try:
                NativePyramidRetirementPrefixIoRequest(
                    output_id=claim.output_id, lease_token=page.lease_token,
                    page_id=page.page_id, kind="uploads",
                    key_marker=next_marker[0], upload_id_marker=next_marker[1],
                )
            except ValueError:
                raise NativeStorageError("native_prefix_scan_incomplete") from None
            seen.add(next_marker)
            marker = next_marker

    async def verify_prefix_absent(self, claim, page, stop) -> None:
        shapes = (
            ("versions", "ListVersionsResult", "Name", ("Version", "DeleteMarker")),
            ("uploads", "ListMultipartUploadsResult", "Bucket", ("Upload",)),
            ("objects", "ListBucketResult", "Name", ("Contents",)),
        )
        for kind, root_name, bucket_name, entries in shapes:
            if kind == "uploads":
                await self._verify_multipart_absent(claim, page, stop)
                continue
            _check_stop(stop)
            reference = await self.signer.retirement_prefix_reference(
                NativePyramidRetirementPrefixIoRequest(
                    output_id=claim.output_id,
                    lease_token=page.lease_token,
                    page_id=page.page_id,
                    kind=kind,
                )
            )
            if (
                not self._scope_matches(reference, claim, page)
                or reference.kind != kind
            ):
                raise NativeStorageError("native_retirement_reference_mismatch")
            async with self._response(reference.request, stop) as response:
                if response.status_code != 200:
                    self._status_error(response)
                root = self._xml(await self._bounded_body(response, stop))
            namespace = (
                f"{{{_S3_NAMESPACE}}}"
                if root.tag == f"{{{_S3_NAMESPACE}}}{root_name}"
                else ""
            )
            if (
                root.tag != namespace + root_name
                or root.findtext(namespace + bucket_name) != claim.storage_ref.bucket
                or root.findtext(namespace + "Prefix") != claim.storage_ref.prefix
            ):
                raise NativeStorageError("native_prefix_scan_scope_mismatch")
            if any(root.findall(namespace + name) for name in entries) or root.findall(
                namespace + "CommonPrefixes"
            ):
                raise NativeStorageError("native_prefix_not_empty")
            if root.findtext(namespace + "IsTruncated") != "false":
                raise NativeStorageError("native_prefix_scan_incomplete")


async def _run_native_io(function, *args, stop: threading.Event):
    """Drain the physical request before reporting cancellation to the owner."""
    task = asyncio.create_task(function(*args, stop))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        stop.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        cancelled.storage_operation_unsettled = True
        if task.done() and not task.cancelled():
            error = task.exception()
            cancelled.storage_operation_unsettled = (
                False
                if error is None or isinstance(error, CleanupStopped)
                else getattr(error, "unsettled", True)
            )
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
            try:
                references = await storage.prepare_page(claim, page)
            except asyncio.CancelledError:
                # Signing performs no object I/O; this prepared page is unstarted.
                await asyncio.shield(_release_page(data, page, previous_cursor))
                raise
            except Exception as exc:
                log.warning(
                    "Native retirement signing failed output_id=%s code=%s",
                    claim.output_id,
                    _code(exc),
                )
                await _release_page(data, page, previous_cursor)
                return _RetirementOutcome(0, 1, pages, True)
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
                        member,
                        references[member.operation_id],
                        stop=storage_stop,
                    )
                except asyncio.CancelledError as exc:
                    if not getattr(exc, "storage_operation_unsettled", True):
                        await asyncio.shield(
                            _release_page(data, page, previous_cursor)
                        )
                    raise
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
                claim,
                page,
                stop=storage_stop,
            )
        except asyncio.CancelledError as exc:
            if not getattr(exc, "storage_operation_unsettled", True):
                await asyncio.shield(_release_page(data, page, previous_cursor))
            raise
        except CleanupStopped:
            await _release_page(data, page, previous_cursor)
            return _RetirementOutcome(0, 0, pages, True)
        except NativeStorageError as exc:
            log.warning(
                "Native retirement prefix check failed output_id=%s code=%s",
                claim.output_id,
                exc.code,
            )
            if not exc.unsettled:
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
