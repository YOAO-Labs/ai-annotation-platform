"""Finite Native pyramid S3 signing inside the Assets source process.

The source owns registered connection credentials, the builtin Native
identity, host policy and the three capture capabilities. It never performs
publication, cleanup, listing or object I/O: every admitted request is a
short-lived signed capability for the requesting executor.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from typing import Literal, Protocol
from urllib.parse import parse_qsl, quote, urlsplit
from uuid import UUID

from botocore.auth import S3SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.exceptions import BotoCoreError
from yoao_assets_images.contract import ImageFailure
from yoao_assets_images.native_storage_contract import (
    NATIVE_URL_EXPIRY_MAX,
    NativePyramidIoContext,
    NativePyramidIoReferences,
    NativePyramidPrefixIoReference,
    NativePyramidReadIoRequest,
    NativePyramidRetirementIoRequest,
    NativePyramidRetirementPrefixIoRequest,
    NativePyramidSignedMember,
    NativePyramidSignedRequest,
    NativePyramidWriteIoRequest,
    member_content_type,
    prefix_listing_query,
    require_read_reference_scope,
    require_retirement_prefix_reference_scope,
    require_retirement_reference_scope,
    require_write_reference_scope,
)
from yoao_assets_source.connectors.crypto import (
    ConnectorCryptoError,
    ConnectorCryptoNotConfigured,
    decrypt_secret,
)
from yoao_assets_source.connectors.host_guard import (
    ConnectorAllowlistInvalid,
    ConnectorHostDenied,
    assert_host_allowed,
    normalize_allowlist,
)
from yoao_assets_source.connectors.s3 import validate_s3_endpoint

from .config import NativeSignerSettings, SourceConfig

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class NativeStoragePort(Protocol):
    """The three Source-only capture capabilities, one transaction each."""

    async def capture_native_pyramid_write_io(
        self,
        operation_id: UUID,
        job_id: UUID,
        job_generation: int,
        lease_token: UUID,
        mode: str,
    ) -> NativePyramidIoContext: ...

    async def capture_native_pyramid_read_io(
        self,
        operation_id: UUID,
        token: UUID,
        relative_keys: list[str],
    ) -> NativePyramidIoContext: ...

    async def capture_native_pyramid_retirement_io(
        self,
        output_id: UUID,
        lease_token: UUID,
        page_id: UUID,
    ) -> NativePyramidIoContext: ...


@dataclass(frozen=True, slots=True)
class _TargetCredentials:
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    region: str
    endpoint: str
    public_endpoint: str


def _presign(
    *,
    method: str,
    credentials: "_TargetCredentials",
    endpoint: str,
    path: str,
    query: list[tuple[str, str]],
    headers: dict[str, str],
    expires_in: int,
) -> tuple[str, datetime]:
    """Presign one exact S3 request with botocore's SigV4 query signer."""
    parsed_endpoint = urlsplit(endpoint)
    # Botocore lowercases the signed host; keep the request URL identical so
    # the executor's automatic Host header matches the signed value.
    origin = parsed_endpoint.scheme + "://" + parsed_endpoint.netloc.lower()
    request = AWSRequest(
        method=method,
        url=origin + path,
        params=dict(query) if query else None,
        headers=dict(headers),
    )
    try:
        S3SigV4QueryAuth(
            Credentials(credentials.access_key, credentials.secret_key),
            "s3",
            credentials.region,
            expires=expires_in,
        ).add_auth(request)
        signed = dict(
            parse_qsl(urlsplit(request.url).query, keep_blank_values=True)
        )
        issued_at = datetime.strptime(
            signed["X-Amz-Date"], "%Y%m%dT%H%M%SZ"
        ).replace(tzinfo=timezone.utc)
        expires_at = issued_at + timedelta(
            seconds=int(signed["X-Amz-Expires"])
        )
    except BotoCoreError:
        raise ImageFailure("storage_unavailable") from None
    except (KeyError, TypeError, ValueError):
        raise ImageFailure("invalid_response") from None
    return request.url, expires_at


def _require_within_lease(expires_at: datetime, valid_until: datetime | None) -> None:
    if valid_until is None:
        raise ImageFailure("invalid_response")
    if expires_at > valid_until:
        raise ImageFailure("lease_lost")


def _member_path(bucket: str, key: str) -> str:
    return "/" + quote(bucket, safe="-_.~") + "/" + quote(key, safe="/-_.~")


def _bucket_path(bucket: str) -> str:
    return "/" + quote(bucket, safe="-_.~")


def _version_query(version_id: str | None) -> list[tuple[str, str]]:
    return [] if version_id is None else [("versionId", version_id)]


def _lease_expiry(valid_until: datetime | None, now: datetime) -> int:
    if valid_until is None:
        raise ImageFailure("invalid_response")
    remaining = int((valid_until - now).total_seconds())
    # Keep one second of slack so the later botocore signing timestamp cannot
    # push the issued expiry past the retained lease deadline.
    if remaining < 2:
        raise ImageFailure("lease_lost")
    return min(remaining - 1, NATIVE_URL_EXPIRY_MAX)


def _native_key_prefix(base_prefix: object) -> str:
    if base_prefix is None:
        base = ""
    elif isinstance(base_prefix, str):
        base = base_prefix
    else:
        raise ImageFailure("dependency_changed")
    if (
        base != base.strip()
        or "\\" in base
        or _CONTROL_CHARACTERS.search(base)
    ):
        raise ImageFailure("dependency_changed")
    base = base.rstrip("/")
    if base.startswith("/"):
        raise ImageFailure("dependency_changed")
    if base and any(part in {"", ".", ".."} for part in base.split("/")):
        raise ImageFailure("dependency_changed")
    return (base + "/" if base else "") + "_aap_managed/media-cache/"


def _require_stable_capture(
    first: NativePyramidIoContext, second: NativePyramidIoContext
) -> None:
    """Allow a heartbeat extension but refuse any changed durable fact."""
    if (
        second.mode != first.mode
        or second.scope_id != first.scope_id
        or second.scope_token != first.scope_token
        or second.output_id != first.output_id
        or second.storage_ref != first.storage_ref
        or second.url_expiry_seconds != first.url_expiry_seconds
        or second.members != first.members
        or second.destination != first.destination
        or second.host_allowlist != first.host_allowlist
    ):
        raise ImageFailure("dependency_changed")
    if (first.valid_until is None) != (second.valid_until is None):
        raise ImageFailure("dependency_changed")
    if (
        first.valid_until is not None
        and second.valid_until < first.valid_until
    ):
        raise ImageFailure("dependency_changed")


class NativeStorageSigner:
    """Admit and sign only the finite Native pyramid storage scopes."""

    def __init__(
        self,
        config: SourceConfig,
        settings: NativeSignerSettings,
        data: NativeStoragePort,
    ) -> None:
        self._config = config
        self._settings = settings
        self._data = data

    async def write_reference(
        self, request: NativePyramidWriteIoRequest
    ) -> NativePyramidIoReferences:
        return await self._write_reference(request, "put")

    async def verify_reference(
        self, request: NativePyramidWriteIoRequest
    ) -> NativePyramidIoReferences:
        return await self._write_reference(request, "verify")

    async def _write_reference(
        self, request: NativePyramidWriteIoRequest, mode: Literal["put", "verify"]
    ) -> NativePyramidIoReferences:
        async def capture() -> NativePyramidIoContext:
            return await self._data.capture_native_pyramid_write_io(
                request.operation_id,
                request.job_id,
                request.job_generation,
                request.lease_token,
                mode,
            )

        async def require_scope(context: NativePyramidIoContext) -> None:
            if (
                context.scope_id != request.operation_id
                or context.scope_token != request.lease_token
                or len(context.members) != 1
                or context.members[0].operation_id != request.operation_id
            ):
                raise ImageFailure("invalid_response")

        result = await self._sign_write(mode, capture, require_scope)
        self._self_check(
            lambda: require_write_reference_scope(request, result, mode)
        )
        return result

    async def read_references(
        self, request: NativePyramidReadIoRequest
    ) -> NativePyramidIoReferences:
        async def capture() -> NativePyramidIoContext:
            return await self._data.capture_native_pyramid_read_io(
                request.operation_id,
                request.token,
                list(request.relative_keys),
            )

        async def require_scope(context: NativePyramidIoContext) -> None:
            actual = [member.relative_key for member in context.members]
            if (
                context.scope_id != request.operation_id
                or context.scope_token != request.token
                or len(actual) != len(request.relative_keys)
                or set(actual) != set(request.relative_keys)
            ):
                raise ImageFailure("invalid_response")

        context, credentials = await self._stable_context(
            "read", capture, require_scope
        )
        issued_cap = min(context.url_expiry_seconds, NATIVE_URL_EXPIRY_MAX)
        expires_in: int | None = None
        if request.purpose == "inspect":
            expires_in = None
            ttl = issued_cap
        else:
            ttl = (
                request.expires_in
                if request.expires_in is not None
                else issued_cap
            )
            if ttl > issued_cap:
                raise ImageFailure("invalid_request")
            expires_in = ttl
        members = [
            self._sign_read_member(
                member,
                context,
                credentials,
                purpose=request.purpose,
                ttl=ttl,
            )
            for member in context.members
        ]
        result = NativePyramidIoReferences(
            mode="read",
            scope_id=context.scope_id,
            scope_token=context.scope_token,
            output_id=context.output_id,
            storage_ref=context.storage_ref,
            members=members,
            expires_in=expires_in,
        )
        self._self_check(
            lambda: require_read_reference_scope(request, result)
        )
        return result

    async def retirement_references(
        self, request: NativePyramidRetirementIoRequest
    ) -> NativePyramidIoReferences:
        async def capture() -> NativePyramidIoContext:
            return await self._data.capture_native_pyramid_retirement_io(
                request.output_id,
                request.lease_token,
                request.page_id,
            )

        async def require_scope(context: NativePyramidIoContext) -> None:
            if (
                context.mode != "retirement_members"
                or context.scope_id != request.page_id
                or context.scope_token != request.lease_token
                or context.output_id != request.output_id
            ):
                raise ImageFailure("invalid_response")

        context, credentials = await self._stable_context(
            "retirement_members", capture, require_scope
        )
        ttl = _lease_expiry(context.valid_until, datetime.now(timezone.utc))
        members = [
            self._sign_retirement_member(member, context, credentials, ttl)
            for member in context.members
        ]
        result = NativePyramidIoReferences(
            mode="retirement_members",
            scope_id=context.scope_id,
            scope_token=context.scope_token,
            output_id=context.output_id,
            storage_ref=context.storage_ref,
            members=members,
            expires_in=None,
        )
        self._self_check(
            lambda: require_retirement_reference_scope(request, result)
        )
        return result

    async def retirement_prefix_reference(
        self, request: NativePyramidRetirementPrefixIoRequest
    ) -> NativePyramidPrefixIoReference:
        async def capture() -> NativePyramidIoContext:
            return await self._data.capture_native_pyramid_retirement_io(
                request.output_id,
                request.lease_token,
                request.page_id,
            )

        async def require_scope(context: NativePyramidIoContext) -> None:
            if (
                context.mode != "retirement_prefix"
                or context.members
                or context.scope_id != request.page_id
                or context.scope_token != request.lease_token
                or context.output_id != request.output_id
            ):
                raise ImageFailure("invalid_response")

        context, credentials = await self._stable_context(
            "retirement_prefix", capture, require_scope
        )
        ttl = _lease_expiry(context.valid_until, datetime.now(timezone.utc))
        query = prefix_listing_query(
            context.storage_ref.prefix, request.kind,
            key_marker=request.key_marker, upload_id_marker=request.upload_id_marker,
        )
        url, expires_at = _presign(
            method="GET",
            credentials=credentials,
            endpoint=credentials.endpoint,
            path=_bucket_path(context.storage_ref.bucket),
            query=query,
            headers={},
            expires_in=ttl,
        )
        _require_within_lease(expires_at, context.valid_until)
        result = NativePyramidPrefixIoReference(
            scope_id=context.scope_id,
            scope_token=context.scope_token,
            output_id=context.output_id,
            storage_ref=context.storage_ref,
            kind=request.kind,
            request=NativePyramidSignedRequest(
                method="GET", url=url, headers={}, expires_at=expires_at
            ),
        )
        self._self_check(
            lambda: require_retirement_prefix_reference_scope(request, result)
        )
        return result

    async def _sign_write(
        self,
        mode: Literal["put", "verify"],
        capture,
        require_scope,
    ) -> NativePyramidIoReferences:
        context, credentials = await self._stable_context(
            mode, capture, require_scope
        )
        ttl = _lease_expiry(context.valid_until, datetime.now(timezone.utc))
        member = context.members[0]
        if mode == "put":
            signed = self._sign_put(member, context, credentials, ttl)
        else:
            signed = self._sign_verify(member, context, credentials, ttl)
        result = NativePyramidIoReferences(
            mode=mode,
            scope_id=context.scope_id,
            scope_token=context.scope_token,
            output_id=context.output_id,
            storage_ref=context.storage_ref,
            members=[signed],
            expires_in=None,
        )
        return result

    def _sign_put(
        self,
        member,
        context: NativePyramidIoContext,
        credentials: _TargetCredentials,
        ttl: int,
    ) -> NativePyramidSignedMember:
        key = context.storage_ref.prefix + member.relative_key
        headers = {
            "content-length": str(member.size_bytes),
            "content-type": member_content_type(member.relative_key),
            "if-none-match": "*",
            "x-amz-meta-sha256": member.sha256,
        }
        url, expires_at = _presign(
            method="PUT",
            credentials=credentials,
            endpoint=credentials.endpoint,
            path=_member_path(context.storage_ref.bucket, key),
            query=[],
            headers=headers,
            expires_in=ttl,
        )
        _require_within_lease(expires_at, context.valid_until)
        return NativePyramidSignedMember(
            member=member,
            put=NativePyramidSignedRequest(
                method="PUT",
                url=url,
                headers=headers,
                expires_at=expires_at,
            ),
        )

    def _sign_verify(
        self,
        member,
        context: NativePyramidIoContext,
        credentials: _TargetCredentials,
        ttl: int,
    ) -> NativePyramidSignedMember:
        if member.etag is None:
            raise ImageFailure("invalid_response")
        key = context.storage_ref.prefix + member.relative_key
        headers = {"if-match": member.etag}
        url, expires_at = _presign(
            method="GET",
            credentials=credentials,
            endpoint=credentials.endpoint,
            path=_member_path(context.storage_ref.bucket, key),
            query=_version_query(member.version_id),
            headers=headers,
            expires_in=ttl,
        )
        _require_within_lease(expires_at, context.valid_until)
        return NativePyramidSignedMember(
            member=member,
            get=NativePyramidSignedRequest(
                method="GET",
                url=url,
                headers=headers,
                expires_at=expires_at,
            ),
        )

    def _sign_read_member(
        self,
        member,
        context: NativePyramidIoContext,
        credentials: _TargetCredentials,
        *,
        purpose: Literal["inspect", "publish"],
        ttl: int,
    ) -> NativePyramidSignedMember:
        if member.etag is None:
            raise ImageFailure("invalid_response")
        key = context.storage_ref.prefix + member.relative_key
        query = _version_query(member.version_id)
        if purpose == "publish":
            url, expires_at = _presign(
                method="GET",
                credentials=credentials,
                endpoint=credentials.public_endpoint,
                path=_member_path(context.storage_ref.bucket, key),
                query=query,
                headers={},
                expires_in=ttl,
            )
            return NativePyramidSignedMember(
                member=member,
                public_url=url,
                public_expires_at=expires_at,
            )
        headers = {"if-match": member.etag}
        signed = {}
        for name, method in (("head", "HEAD"), ("get", "GET")):
            url, expires_at = _presign(
                method=method,
                credentials=credentials,
                endpoint=credentials.endpoint,
                path=_member_path(context.storage_ref.bucket, key),
                query=query,
                headers=headers,
                expires_in=ttl,
            )
            signed[name] = NativePyramidSignedRequest(
                method=method,
                url=url,
                headers=headers,
                expires_at=expires_at,
            )
        return NativePyramidSignedMember(member=member, **signed)

    def _sign_retirement_member(
        self,
        member,
        context: NativePyramidIoContext,
        credentials: _TargetCredentials,
        ttl: int,
    ) -> NativePyramidSignedMember:
        if member.etag is None:
            raise ImageFailure("invalid_response")
        key = context.storage_ref.prefix + member.relative_key
        query = _version_query(member.version_id)
        headers = {"if-match": member.etag}
        signed = {}
        for name, method in (("head", "HEAD"), ("get", "GET"), ("delete", "DELETE")):
            url, expires_at = _presign(
                method=method,
                credentials=credentials,
                endpoint=credentials.endpoint,
                path=_member_path(context.storage_ref.bucket, key),
                query=query,
                headers=headers,
                expires_in=ttl,
            )
            _require_within_lease(expires_at, context.valid_until)
            signed[name] = NativePyramidSignedRequest(
                method=method,
                url=url,
                headers=headers,
                expires_at=expires_at,
            )
        return NativePyramidSignedMember(member=member, **signed)

    async def _stable_context(
        self,
        mode: str,
        capture,
        require_scope,
    ) -> tuple[NativePyramidIoContext, _TargetCredentials]:
        """Capture, release SQL, resolve the target, then recheck exact scope."""
        first = await capture()
        if not isinstance(first, NativePyramidIoContext) or first.mode != mode:
            raise ImageFailure("invalid_response")
        await require_scope(first)
        credentials = await self._resolve(first)
        second = await capture()
        if not isinstance(second, NativePyramidIoContext) or second.mode != mode:
            raise ImageFailure("invalid_response")
        _require_stable_capture(first, second)
        # A heartbeat may extend the live lease. Keep the earlier deadline so
        # every issued capability remains bounded by the first admitted lease.
        return first, credentials

    async def _resolve(
        self, context: NativePyramidIoContext
    ) -> _TargetCredentials:
        reference = context.storage_ref
        if context.destination is None:
            endpoint = self._config.s3_endpoint.rstrip("/")
            if (
                reference.endpoint_url.rstrip("/") != endpoint
                or reference.bucket != self._config.media_cache_bucket
                or reference.key_prefix != ""
            ):
                raise ImageFailure("dependency_changed")
            return _TargetCredentials(
                access_key=self._settings.s3_access_key,
                secret_key=self._settings.s3_secret_key,
                region=self._config.s3_region,
                endpoint=endpoint,
                public_endpoint=self._config.s3_public_endpoint.rstrip("/"),
            )
        destination = context.destination
        if not isinstance(destination.config, dict):
            raise ImageFailure("dependency_changed")
        try:
            endpoint = validate_s3_endpoint(destination.config)
        except (TypeError, ValueError):
            raise ImageFailure("dependency_changed") from None
        # The saved bucket comes from the media-cache binding; the registered
        # connection owns the endpoint identity and the managed namespace.
        if (
            endpoint.rstrip("/") != reference.endpoint_url.rstrip("/")
            or _native_key_prefix(destination.config.get("base_prefix"))
            != reference.key_prefix
        ):
            raise ImageFailure("dependency_changed")
        if context.host_allowlist is None:
            allowlist = list(self._config.connector_host_allowlist)
        else:
            try:
                allowlist = normalize_allowlist(list(context.host_allowlist))
            except ConnectorAllowlistInvalid:
                raise ImageFailure("storage_unavailable") from None
        try:
            await asyncio.to_thread(
                assert_host_allowed, reference.endpoint_url, allowlist
            )
        except ConnectorHostDenied:
            raise ImageFailure("storage_unavailable") from None
        try:
            token = base64.b64decode(
                destination.secret_enc.encode("ascii"), validate=True
            )
            secret = decrypt_secret(token, self._config.connector_encryption_key)
        except (
            binascii.Error,
            UnicodeEncodeError,
            ValueError,
            ConnectorCryptoError,
            ConnectorCryptoNotConfigured,
        ):
            raise ImageFailure("storage_unavailable") from None
        access_key = secret.get("access_key")
        secret_key = secret.get("secret_key")
        if (
            not isinstance(access_key, str)
            or not access_key
            or not isinstance(secret_key, str)
            or not secret_key
        ):
            raise ImageFailure("storage_unavailable")
        region = destination.config.get("region")
        if not isinstance(region, str) or not region:
            region = self._config.s3_region
        return _TargetCredentials(
            access_key=access_key,
            secret_key=secret_key,
            region=region,
            endpoint=endpoint,
            public_endpoint=endpoint,
        )

    @staticmethod
    def _self_check(validator) -> None:
        try:
            validator()
        except (TypeError, ValueError):
            raise ImageFailure("invalid_response") from None
