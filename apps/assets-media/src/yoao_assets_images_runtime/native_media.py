"""Source-issued signed S3 requests for one saved native pyramid output.

The Source signer owns the portable HTTP models and the client that admits
them.  This module proves the parts the physical executor owns: the exact saved
target and member scope, the conditional verb bound to the recorded receipt,
the raw HTTP ETag/VersionId that becomes the durable receipt, and the full
saved-version readback of the exact bytes that receipt describes.

Only an admitted Source context may be executed here.  No configured cache
bucket, ambient credential or reconstructed object key is ever used.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import hashlib
import re
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit

import httpx

from yoao_assets_images.contract import (
    ImageFailure,
    NativePyramidSavedTarget,
    PyramidMemberReservation,
)
from yoao_assets_images.native_storage_contract import (
    NATIVE_URL_MAX_LENGTH,
    NativePyramidIoMember,
    NativePyramidIoReferences,
    NativePyramidReadIoRequest,
    NativePyramidSignedMember,
    NativePyramidSignedRequest,
    NativePyramidWriteIoRequest,
)

_QUOTED_ETAG = re.compile(r'^"[^"]+"$')
_MAX_HEADER_LENGTH = 4096
# Raw HTTP receipts share the portable ``NativePyramidIoMember`` bounds.
_MAX_RECEIPT = 1024
_READ_CHUNK = 64 * 1024


class NativeWriteRefused(ImageFailure):
    """The signed PUT was refused before it could write the reserved key."""

    def __init__(self) -> None:
        super().__init__("upload_verification_failed")


class NativeMemberMissing(ImageFailure):
    """An inspected saved member no longer exists at its recorded coordinates."""

    def __init__(self, relative_key: str) -> None:
        super().__init__("object_missing")
        self.relative_key = relative_key


@dataclass(frozen=True, slots=True)
class MemberReceipt:
    """The actual raw HTTP receipt of one saved member write or read.

    ``etag`` stays exactly as quoted on the wire and ``version_id`` keeps a
    literal ``"null"`` distinct from an absent header.  The durable
    ``PyramidMemberReceipt`` is built from these values without normalizing or
    re-quoting them.
    """

    etag: str
    version_id: str | None
    byte_size: int


def content_type_for(relative_key: str) -> str:
    if relative_key == "manifest.json" or relative_key.endswith("/manifest.json"):
        return "application/json"
    return "image/webp"


def _invalid() -> ImageFailure:
    return ImageFailure("invalid_response")


def _unavailable() -> ImageFailure:
    return ImageFailure("storage_unavailable")


def _require_saved_target(actual: Any, expected: NativePyramidSavedTarget) -> None:
    if not isinstance(actual, NativePyramidSavedTarget) or actual != expected:
        raise _invalid()


def _aware(value: Any, code: str = "invalid_response") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ImageFailure(code)
    if value.utcoffset() is None:
        raise ImageFailure(code)
    return value


def _http_url(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= NATIVE_URL_MAX_LENGTH:
        raise _invalid()
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError:
        raise _invalid() from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise _invalid()
    return value


def _fold_headers(headers: Any) -> tuple[dict[str, str], dict[str, str]]:
    if not isinstance(headers, Mapping):
        raise _invalid()
    folded: dict[str, str] = {}
    raw: dict[str, str] = {}
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or not 1 <= len(name) <= 256
            or len(value) > _MAX_HEADER_LENGTH
            or "\r" in value
            or "\n" in value
        ):
            raise _invalid()
        key = name.strip().lower()
        if not key or key in folded:
            raise _invalid()
        folded[key] = value
        raw[name] = value
    return raw, folded


def _signed(
    request: Any, method: str
) -> tuple[str, dict[str, str], dict[str, str], datetime]:
    if not isinstance(request, NativePyramidSignedRequest):
        raise _invalid()
    if request.method != method or request.method not in {
        "GET",
        "HEAD",
        "PUT",
        "DELETE",
    }:
        raise _invalid()
    url = _http_url(request.url)
    raw, folded = _fold_headers(request.headers)
    expires_at = _aware(request.expires_at)
    return url, raw, folded, expires_at


def _receipt_member(result: Any, mode: str) -> NativePyramidSignedMember:
    if not isinstance(result, NativePyramidIoReferences):
        raise _invalid()
    if result.mode != mode or result.expires_in is not None:
        raise _invalid()
    if not isinstance(result.members, list) or len(result.members) != 1:
        raise _invalid()
    signed = result.members[0]
    if not isinstance(signed, NativePyramidSignedMember):
        raise _invalid()
    member = signed.member
    if not isinstance(member, NativePyramidIoMember):
        raise _invalid()
    return signed


def _same_member_scope(
    member: NativePyramidIoMember,
    reservation: PyramidMemberReservation,
) -> None:
    if (
        member.operation_id != reservation.operation_id
        or member.relative_key != reservation.relative_key
        or member.sha256 != reservation.sha256
        or member.size_bytes != reservation.size_bytes
    ):
        raise _invalid()


def _require_recorded_etag(member: NativePyramidIoMember) -> None:
    if not isinstance(member.etag, str) or _QUOTED_ETAG.fullmatch(member.etag) is None:
        raise _invalid()
    if member.version_id is not None and (
        not isinstance(member.version_id, str)
        or not 0 < len(member.version_id) <= _MAX_RECEIPT
    ):
        raise _invalid()


def write_member(
    result: Any,
    *,
    request: NativePyramidWriteIoRequest,
    output_id,
    storage_ref: NativePyramidSavedTarget,
    reservation: PyramidMemberReservation,
) -> NativePyramidSignedMember:
    """Validate the Source PUT context for exactly one reserved saved member."""
    signed = _receipt_member(result, "put")
    if (
        result.scope_id != request.operation_id
        or result.scope_token != request.lease_token
        or result.output_id != output_id
    ):
        raise _invalid()
    _require_saved_target(result.storage_ref, storage_ref)
    member = signed.member
    _same_member_scope(member, reservation)
    if member.etag is not None or member.version_id is not None:
        raise _invalid()
    if signed.put is None:
        raise _invalid()
    return signed


def verify_member(
    result: Any,
    *,
    request: NativePyramidWriteIoRequest,
    output_id,
    storage_ref: NativePyramidSavedTarget,
    reservation: PyramidMemberReservation,
    etag: str,
    version_id: str | None,
) -> NativePyramidSignedMember:
    """Validate the Source context of the durable recorded write receipt."""
    signed = _receipt_member(result, "verify")
    if (
        result.scope_id != request.operation_id
        or result.scope_token != request.lease_token
        or result.output_id != output_id
    ):
        raise _invalid()
    _require_saved_target(result.storage_ref, storage_ref)
    member = signed.member
    _same_member_scope(member, reservation)
    _require_recorded_etag(member)
    if member.etag != etag or member.version_id != version_id:
        raise _invalid()
    if signed.get is None:
        raise _invalid()
    return signed


def read_page(
    result: Any,
    *,
    request: NativePyramidReadIoRequest,
    permit,
    purpose: str,
) -> list[NativePyramidSignedMember]:
    """Validate one Source read page for the exact admitted permit and keys."""
    if not isinstance(result, NativePyramidIoReferences) or result.mode != "read":
        raise _invalid()
    if (
        result.scope_id != request.operation_id
        or result.scope_token != request.token
        or result.output_id != permit.output_id
    ):
        raise _invalid()
    _require_saved_target(result.storage_ref, permit.storage_ref)
    requested = list(request.relative_keys)
    if len(requested) != len(set(requested)) or not 1 <= len(requested) <= 100:
        raise _invalid()
    if request.purpose != purpose:
        raise _invalid()
    if purpose == "inspect":
        if request.expires_in is not None or result.expires_in is not None:
            raise _invalid()
    else:
        if request.expires_in is None or result.expires_in != request.expires_in:
            raise _invalid()
    if not isinstance(result.members, list):
        raise _invalid()
    keys = [signed.member.relative_key for signed in result.members]
    if len(keys) != len(set(keys)) or set(keys) != set(requested):
        raise _invalid()
    for signed in result.members:
        if not isinstance(signed, NativePyramidSignedMember):
            raise _invalid()
        member = signed.member
        if not isinstance(member, NativePyramidIoMember):
            raise _invalid()
        _require_recorded_etag(member)
        if purpose == "inspect":
            if signed.get is None and signed.head is None:
                raise _invalid()
            if signed.public_url is not None or signed.public_expires_at is not None:
                raise _invalid()
        else:
            if signed.public_url is None or signed.public_expires_at is None:
                raise _invalid()
            _http_url(signed.public_url)
            _aware(signed.public_expires_at)
    return list(result.members)


def same_receipt(left: NativePyramidIoMember, right: NativePyramidIoMember) -> bool:
    """Compare two saved-member receipts without accepting a substituted object."""
    return (
        isinstance(left, NativePyramidIoMember)
        and isinstance(right, NativePyramidIoMember)
        and left.operation_id == right.operation_id
        and left.relative_key == right.relative_key
        and left.sha256 == right.sha256
        and left.size_bytes == right.size_bytes
        and left.etag == right.etag
        and left.version_id == right.version_id
    )


def _raw_etag(headers: Mapping[str, str]) -> str:
    value = headers.get("etag")
    if (
        not isinstance(value, str)
        or len(value.strip()) > _MAX_RECEIPT
        or _QUOTED_ETAG.fullmatch(value.strip()) is None
    ):
        raise ImageFailure("upload_verification_failed")
    return value.strip()


def _raw_version(headers: Mapping[str, str]) -> str | None:
    """Preserve the actual returned version, including a literal ``null``.

    ``None`` means the response returned no version header at all; it never
    authorizes adopting a later current version of the saved key.  A present
    header must be the exact opaque token the target reported, so an empty
    value, whitespace, a NUL/DEL or any other non-printable byte is refused
    instead of being silently folded into "absent".
    """
    value = headers.get("x-amz-version-id")
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= _MAX_RECEIPT
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        raise ImageFailure("upload_verification_failed")
    return value


def same_version(recorded: str | None, observed: str | None) -> bool:
    """Compare a saved version receipt with an observed response version.

    The only admitted equivalence is a saved literal ``"null"`` against a
    response that omits the version header entirely, which is how an
    unversioned target reports the same object.  A saved ``None`` still
    requires an omitted header and never accepts a later current version.
    """
    if recorded == observed:
        return True
    return recorded == "null" and observed is None


def _observed_identity(headers: Mapping[str, str]) -> MemberReceipt:
    try:
        etag = _raw_etag(headers)
        version = _raw_version(headers)
    except ImageFailure:
        # A malformed observation is an invalid response to this read, never a
        # write-verification verdict.
        raise _invalid() from None
    length = headers.get("content-length")
    try:
        size = int(length)
    except (TypeError, ValueError):
        raise _invalid() from None
    if size < 0:
        raise _invalid()
    return MemberReceipt(etag=etag, version_id=version, byte_size=size)


def require_exact_member_request(
    url: str,
    headers: Mapping[str, str],
    member: NativePyramidIoMember,
) -> None:
    """Require the conditional verb bound to this exact recorded member."""
    if headers.get("if-match") != member.etag:
        raise _invalid()
    query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    versions = [value for name, value in query if name.lower() == "versionid"]
    if member.version_id is None:
        if versions:
            raise _invalid()
    elif versions != [member.version_id]:
        raise _invalid()


async def put_member(
    http: httpx.AsyncClient,
    signed: NativePyramidSignedRequest,
    *,
    content: Any,
    relative_key: str,
    size_bytes: int,
    sha256: str,
) -> MemberReceipt:
    """Execute the Source-issued put and return its actual raw receipt."""
    url, raw, folded, _expires_at = _signed(signed, "PUT")
    if folded.get("if-none-match") != "*":
        raise _invalid()
    if folded.get("content-length") != str(size_bytes):
        raise _invalid()
    if folded.get("content-type") != content_type_for(relative_key):
        raise _invalid()
    if folded.get("x-amz-meta-sha256") != sha256:
        raise _invalid()
    try:
        response = await http.put(url, headers=raw, content=content)
    except asyncio.CancelledError:
        raise
    except ImageFailure:
        raise
    except httpx.HTTPError:
        raise _unavailable() from None
    if response.status_code == 412:
        raise NativeWriteRefused()
    if not 200 <= response.status_code < 300:
        raise _unavailable()
    if not response.is_closed:
        # A confirmed write whose response was never drained and closed has no
        # proven settlement; the caller must keep the member unknown.
        raise _unavailable()
    return MemberReceipt(
        etag=_raw_etag(response.headers),
        version_id=_raw_version(response.headers),
        byte_size=size_bytes,
    )


async def _close_abandoned(response: httpx.Response) -> bool:
    """Return closure proof, not httpx's preemptive ``is_closed`` marker."""
    if response.is_closed:
        return False
    try:
        await response.aclose()
    except BaseException:
        return False
    return True


async def get_member(
    http: httpx.AsyncClient,
    signed: NativePyramidSignedRequest,
    *,
    member: NativePyramidIoMember,
    limit: int,
) -> bytes:
    """Fully read and prove exactly the saved member version this receipt names."""
    url, raw, folded, _expires_at = _signed(signed, "GET")
    require_exact_member_request(url, folded, member)
    data = bytearray()
    response = None
    try:
        response = await http.send(
            http.build_request("GET", url, headers=raw), stream=True
        )
        if response.status_code == 404:
            raise NativeMemberMissing(member.relative_key)
        if response.status_code != 200:
            raise _unavailable()
        observed = _observed_identity(response.headers)
        if (
            observed.etag != member.etag
            or not same_version(member.version_id, observed.version_id)
            or observed.byte_size != member.size_bytes
        ):
            raise _invalid()
        if observed.byte_size > limit:
            raise ImageFailure("resource_budget_exceeded")
        async for chunk in response.aiter_raw(chunk_size=_READ_CHUNK):
            if len(data) + len(chunk) > limit:
                raise ImageFailure("resource_budget_exceeded")
            data.extend(chunk)
    except BaseException as exc:
        closed = response is not None and await _close_abandoned(response)
        failure = _unavailable() if isinstance(exc, httpx.HTTPError) else exc
        failure.storage_operation_unsettled = not closed
        raise failure from None
    if len(data) != member.size_bytes:
        raise _invalid()
    if hashlib.sha256(data).hexdigest() != member.sha256:
        raise _invalid()
    return bytes(data)


async def head_member(
    http: httpx.AsyncClient,
    signed: NativePyramidSignedRequest,
    *,
    member: NativePyramidIoMember,
) -> MemberReceipt:
    """Confirm the saved member receipt without transferring immutable bytes."""
    url, raw, folded, _expires_at = _signed(signed, "HEAD")
    require_exact_member_request(url, folded, member)
    try:
        response = await http.head(url, headers=raw)
    except BaseException as exc:
        failure = _unavailable() if isinstance(exc, httpx.HTTPError) else exc
        failure.storage_operation_unsettled = True
        raise failure from None
    if response.status_code == 404:
        raise NativeMemberMissing(member.relative_key)
    if response.status_code != 200:
        raise _unavailable()
    if not response.is_closed:
        failure = _unavailable()
        failure.storage_operation_unsettled = True
        raise failure
    observed = _observed_identity(response.headers)
    if (
        observed.etag != member.etag
        or not same_version(member.version_id, observed.version_id)
        or observed.byte_size != member.size_bytes
    ):
        raise _invalid()
    return observed
