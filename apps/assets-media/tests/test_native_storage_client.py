from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from yoao_assets_images import contract as c
from yoao_assets_images.native_storage_client import NativePyramidStorageClient
from yoao_assets_images.native_storage_contract import (
    NATIVE_READ_REFERENCES_PATH,
    NATIVE_RETIREMENT_PREFIX_REFERENCE_PATH,
    NATIVE_RETIREMENT_REFERENCES_PATH,
    NATIVE_VERIFY_REFERENCE_PATH,
    NATIVE_WRITE_REFERENCE_PATH,
    NativePyramidIoMember,
    NativePyramidIoReferences,
    NativePyramidPrefixIoReference,
    NativePyramidReadIoRequest,
    NativePyramidRetirementIoRequest,
    NativePyramidRetirementPrefixIoRequest,
    NativePyramidSignedMember,
    NativePyramidSignedRequest,
    NativePyramidWriteIoRequest,
)

ENDPOINT = "http://minio.internal:9000"
BUCKET = "media-cache"
TOKEN = "w" * 40


def saved_target(output_id, *, key_prefix=""):
    return c.NativePyramidSavedTarget.model_validate(
        {
            "schema": 2,
            "purpose": "media-cache",
            "location_id": uuid4(),
            "config_revision": 1,
            "binding_generation": 1,
            "endpoint_url": ENDPOINT,
            "bucket": BUCKET,
            "key_prefix": key_prefix,
            "prefix": key_prefix
            + "image-pyramids/objects/"
            + str(output_id)
            + "/",
            "storage_identity": "a" * 64,
        }
    )


def member(
    target,
    relative_key="overview.webp",
    *,
    etag='"etag"',
    version_id="v1",
    operation_id=None,
):
    return NativePyramidIoMember(
        operation_id=operation_id or uuid4(),
        relative_key=relative_key,
        sha256="b" * 64,
        size_bytes=12,
        etag=etag,
        version_id=version_id,
    )


def issued(expires_in=120, ago=10):
    return (
        datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=ago),
        expires_in,
    )


def signed_url(
    *,
    method,
    key,
    version_id=None,
    headers=None,
    expires_in=120,
    issued_at=None,
    resource_query=(),
    path=None,
):
    headers = headers or {}
    if issued_at is None:
        issued_at, expires_in = issued(expires_in)
    names = ";".join(sorted({"host", *headers}))
    date = issued_at.strftime("%Y%m%dT%H%M%SZ")
    params = list(resource_query)
    if version_id is not None:
        params.append(("versionId", version_id))
    params.extend(
        [
            ("X-Amz-Algorithm", "AWS4-HMAC-SHA256"),
            (
                "X-Amz-Credential",
                "AKIA/" + issued_at.strftime("%Y%m%d") + "/us-east-1/s3/aws4_request",
            ),
            ("X-Amz-Date", date),
            ("X-Amz-Expires", str(expires_in)),
            ("X-Amz-SignedHeaders", names),
            ("X-Amz-Signature", "c" * 64),
        ]
    )
    encoded = sorted(
        (quote(name, safe="-_.~"), quote(value, safe="-_.~"))
        for name, value in params
    )
    if path is None:
        path = "/" + BUCKET + "/" + quote(key, safe="/-_.~")
    url = ENDPOINT + path + "?" + "&".join(f"{name}={value}" for name, value in encoded)
    return NativePyramidSignedRequest(
        method=method,
        url=url,
        headers=headers,
        expires_at=issued_at + timedelta(seconds=expires_in),
    )


def put_member(
    target, *, relative_key="overview.webp", expires_in=120, operation_id=None
):
    row = member(
        target,
        relative_key,
        etag=None,
        version_id=None,
        operation_id=operation_id,
    )
    headers = {
        "content-length": str(row.size_bytes),
        "content-type": "image/webp",
        "if-none-match": "*",
        "x-amz-meta-sha256": row.sha256,
    }
    return NativePyramidSignedMember(
        member=row,
        put=signed_url(
            method="PUT",
            key=target.prefix + relative_key,
            headers=headers,
            expires_in=expires_in,
        ),
    )


def verify_member(target, *, relative_key="overview.webp", operation_id=None):
    row = member(target, relative_key, operation_id=operation_id)
    headers = {"if-match": row.etag}
    return NativePyramidSignedMember(
        member=row,
        get=signed_url(
            method="GET",
            key=target.prefix + relative_key,
            version_id=row.version_id,
            headers=headers,
        ),
    )


def _target_output_id(target):
    return UUID(target.prefix.rstrip("/").rsplit("/", 1)[-1])


def references(mode, target, rows, *, scope_id, scope_token, expires_in=None):
    return NativePyramidIoReferences(
        mode=mode,
        scope_id=scope_id,
        scope_token=scope_token,
        output_id=_target_output_id(target),
        storage_ref=target,
        members=rows,
        expires_in=expires_in,
    )


def write_request(operation_id, lease_token):
    return NativePyramidWriteIoRequest(
        operation_id=operation_id,
        job_id=uuid4(),
        job_generation=1,
        lease_token=lease_token,
    )


def client_for(result, *, status=200, token=TOKEN, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(
                status,
                json={
                    "schema": c.IMAGE_SCHEMA,
                    "ok": False,
                    "error": {"code": "lease_lost"},
                },
            )
        return httpx.Response(
            200,
            json={
                "schema": c.IMAGE_SCHEMA,
                "ok": True,
                "data": result.model_dump(mode="json", by_alias=True),
            },
        )

    return NativePyramidStorageClient(
        "http://source.internal:8767", token, http=httpx.MockTransport(handler)
    )


def test_client_rejects_unsafe_configuration():
    with pytest.raises(c.ImageFailure):
        NativePyramidStorageClient("http://source.internal:8767?x=1", TOKEN)
    with pytest.raises(c.ImageFailure):
        NativePyramidStorageClient("http://source.internal:8767", "short")
    with pytest.raises(c.ImageFailure):
        NativePyramidStorageClient("http://source.internal:8767", TOKEN, http=object())


async def test_write_reference_accepts_the_exact_reserved_put():
    output_id, operation_id, lease_token = uuid4(), uuid4(), uuid4()
    target = saved_target(output_id)
    row = put_member(target, operation_id=operation_id)
    result = references(
        "put", target, [row], scope_id=operation_id, scope_token=lease_token
    )
    seen: list[httpx.Request] = []
    client = client_for(result, seen=seen)
    try:
        actual = await client.write_reference(write_request(operation_id, lease_token))
    finally:
        await client.aclose()
    assert actual == result
    assert seen[0].url.path == NATIVE_WRITE_REFERENCE_PATH
    assert seen[0].headers["authorization"] == "Bearer " + TOKEN


async def test_verify_reference_uses_the_recorded_version():
    output_id, operation_id, lease_token = uuid4(), uuid4(), uuid4()
    target = saved_target(output_id, key_prefix="tenant/")
    row = verify_member(target, operation_id=operation_id)
    result = references(
        "verify", target, [row], scope_id=operation_id, scope_token=lease_token
    )
    seen: list[httpx.Request] = []
    client = client_for(result, seen=seen)
    try:
        await client.verify_reference(write_request(operation_id, lease_token))
    finally:
        await client.aclose()
    assert seen[0].url.path == NATIVE_VERIFY_REFERENCE_PATH


@pytest.mark.parametrize("purpose", ["inspect", "publish"])
async def test_read_references_accepts_exact_inspection_and_publication(purpose):
    output_id, operation_id, token = uuid4(), uuid4(), uuid4()
    target = saved_target(output_id)
    inspected = member(target, version_id=None)
    if purpose == "inspect":
        headers = {"if-match": inspected.etag}
        row = NativePyramidSignedMember(
            member=inspected,
            head=signed_url(
                method="HEAD",
                key=target.prefix + inspected.relative_key,
                headers=headers,
            ),
            get=signed_url(
                method="GET",
                key=target.prefix + inspected.relative_key,
                headers=headers,
            ),
        )
        result = references(
            "read", target, [row], scope_id=operation_id, scope_token=token
        )
        request = NativePyramidReadIoRequest(
            operation_id=operation_id,
            token=token,
            relative_keys=[inspected.relative_key],
            purpose="inspect",
        )
    else:
        expires_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
            seconds=90
        )
        issued_at = expires_at - timedelta(seconds=90)
        row = NativePyramidSignedMember(
            member=inspected,
            public_url=signed_url(
                method="GET",
                key=target.prefix + inspected.relative_key,
                issued_at=issued_at,
                expires_in=90,
            ).url,
            public_expires_at=expires_at,
        )
        result = references(
            "read",
            target,
            [row],
            scope_id=operation_id,
            scope_token=token,
            expires_in=90,
        )
        request = NativePyramidReadIoRequest(
            operation_id=operation_id,
            token=token,
            relative_keys=[inspected.relative_key],
            purpose="publish",
            expires_in=90,
        )
    seen: list[httpx.Request] = []
    client = client_for(result, seen=seen)
    try:
        actual = await client.read_references(request)
    finally:
        await client.aclose()
    assert actual == result
    assert seen[0].url.path == NATIVE_READ_REFERENCES_PATH


async def test_retirement_references_require_exact_conditional_slots():
    output_id, page_id, lease_token = uuid4(), uuid4(), uuid4()
    target = saved_target(output_id)
    row = member(target)
    headers = {"if-match": row.etag}
    signed = NativePyramidSignedMember(
        member=row,
        head=signed_url(
            method="HEAD",
            key=target.prefix + row.relative_key,
            version_id=row.version_id,
            headers=headers,
        ),
        get=signed_url(
            method="GET",
            key=target.prefix + row.relative_key,
            version_id=row.version_id,
            headers=headers,
        ),
        delete=signed_url(
            method="DELETE",
            key=target.prefix + row.relative_key,
            version_id=row.version_id,
            headers=headers,
        ),
    )
    result = references(
        "retirement_members",
        target,
        [signed],
        scope_id=page_id,
        scope_token=lease_token,
    )
    seen: list[httpx.Request] = []
    client = client_for(result, seen=seen)
    try:
        await client.retirement_references(
            NativePyramidRetirementIoRequest(
                output_id=result.output_id, lease_token=lease_token, page_id=page_id
            )
        )
    finally:
        await client.aclose()
    assert seen[0].url.path == NATIVE_RETIREMENT_REFERENCES_PATH


async def test_retirement_prefix_signs_only_the_exact_absence_probe():
    output_id, page_id, lease_token = uuid4(), uuid4(), uuid4()
    target = saved_target(output_id)
    request = NativePyramidRetirementPrefixIoRequest(
        output_id=output_id, lease_token=lease_token, page_id=page_id, kind="versions"
    )
    listing = signed_url(
        method="GET",
        key="",
        path="/" + BUCKET,
        resource_query=(
            ("versions", ""),
            ("max-keys", "1"),
            ("prefix", target.prefix),
        ),
    )
    result = NativePyramidPrefixIoReference(
        scope_id=page_id,
        scope_token=lease_token,
        output_id=output_id,
        storage_ref=target,
        kind="versions",
        request=listing,
    )
    seen: list[httpx.Request] = []
    client = client_for(result, seen=seen)
    try:
        await client.retirement_prefix_reference(request)
    finally:
        await client.aclose()
    assert seen[0].url.path == NATIVE_RETIREMENT_PREFIX_REFERENCE_PATH


@pytest.mark.parametrize(
    "mutate",
    [
        "scope_token",
        "member_key",
        "method",
        "bucket",
        "missing_header",
        "unsigned_header",
        "guessed_version",
        "expired",
    ],
)
async def test_write_scope_rejects_every_scope_escape(mutate):
    output_id, operation_id, lease_token = uuid4(), uuid4(), uuid4()
    target = saved_target(output_id)
    row = put_member(target, operation_id=operation_id)
    scope_token = lease_token
    if mutate == "scope_token":
        scope_token = uuid4()
    elif mutate == "member_key":
        row = put_member(
            target, relative_key="manifest.json", operation_id=operation_id
        )
    elif mutate == "method":
        row = row.model_copy(
            update={
                "put": signed_url(
                    method="GET",
                    key=target.prefix + row.member.relative_key,
                    headers=dict(row.put.headers),
                )
            }
        )
    elif mutate == "bucket":
        row = row.model_copy(
            update={
                "put": signed_url(
                    method="PUT",
                    key=target.prefix + row.member.relative_key,
                    headers=dict(row.put.headers),
                    path="/other-bucket/"
                    + quote(target.prefix + row.member.relative_key, safe="/-_.~"),
                )
            }
        )
    elif mutate == "missing_header":
        headers = dict(row.put.headers)
        headers.pop("if-none-match")
        row = row.model_copy(
            update={
                "put": row.put.model_copy(update={"headers": headers})
            }
        )
    elif mutate == "unsigned_header":
        signed = row.put
        signed_names = (
            "content-length;content-type;host;if-none-match;x-amz-meta-sha256"
        )
        url = signed.url.replace(
            quote(signed_names, safe=""),
            quote("content-type;host;x-amz-meta-sha256", safe=""),
        )
        row = row.model_copy(update={"put": signed.model_copy(update={"url": url})})
    elif mutate == "guessed_version":
        row = verify_member(target, operation_id=operation_id)
        signed = row.get
        row = row.model_copy(
            update={
                "get": signed.model_copy(
                    update={
                        "url": signed.url
                        + "&versionId=invented"
                    }
                )
            }
        )
    elif mutate == "expired":
        row = put_member(target, operation_id=operation_id)
        row = row.model_copy(
            update={
                "put": signed_url(
                    method="PUT",
                    key=target.prefix + row.member.relative_key,
                    headers=dict(row.put.headers),
                    issued_at=datetime.now(timezone.utc).replace(microsecond=0)
                    - timedelta(seconds=300),
                    expires_in=60,
                )
            }
        )
    result = references(
        "verify" if mutate == "guessed_version" else "put",
        target,
        [row],
        scope_id=operation_id,
        scope_token=scope_token,
    )
    request = write_request(operation_id, lease_token)
    client = client_for(result)
    try:
        with pytest.raises(c.ImageFailure) as caught:
            if mutate == "guessed_version":
                await client.verify_reference(request)
            else:
                await client.write_reference(request)
    finally:
        await client.aclose()
    assert caught.value.code == "invalid_response"


async def test_read_reference_rejects_unrequested_members_and_wrong_lifetime():
    output_id, operation_id, token = uuid4(), uuid4(), uuid4()
    target = saved_target(output_id)
    row = member(target, version_id=None)
    headers = {"if-match": row.etag}
    signed = NativePyramidSignedMember(
        member=row,
        head=signed_url(
            method="HEAD",
            key=target.prefix + row.relative_key,
            headers=headers,
        ),
        get=signed_url(
            method="GET",
            key=target.prefix + row.relative_key,
            headers=headers,
        ),
    )
    result = references(
        "read", target, [signed], scope_id=operation_id, scope_token=token
    )
    request = NativePyramidReadIoRequest(
        operation_id=operation_id,
        token=token,
        relative_keys=["manifest.json"],
        purpose="inspect",
    )
    client = client_for(result)
    try:
        with pytest.raises(c.ImageFailure):
            await client.read_references(request)
    finally:
        await client.aclose()


async def test_publish_rejects_a_changed_explicit_lifetime():
    output_id, operation_id, token = uuid4(), uuid4(), uuid4()
    target = saved_target(output_id)
    row = member(target, version_id=None)
    expires_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
        seconds=60
    )
    issued_at = expires_at - timedelta(seconds=60)
    signed = NativePyramidSignedMember(
        member=row,
        public_url=signed_url(
            method="GET",
            key=target.prefix + row.relative_key,
            issued_at=issued_at,
            expires_in=60,
        ).url,
        public_expires_at=expires_at,
    )
    result = references(
        "read",
        target,
        [signed],
        scope_id=operation_id,
        scope_token=token,
        expires_in=60,
    )
    request = NativePyramidReadIoRequest(
        operation_id=operation_id,
        token=token,
        relative_keys=[row.relative_key],
        purpose="publish",
        expires_in=120,
    )
    client = client_for(result)
    try:
        with pytest.raises(c.ImageFailure):
            await client.read_references(request)
    finally:
        await client.aclose()


async def test_prefix_reference_rejects_an_unadmitted_query():
    output_id, page_id, lease_token = uuid4(), uuid4(), uuid4()
    target = saved_target(output_id)
    request = NativePyramidRetirementPrefixIoRequest(
        output_id=output_id, lease_token=lease_token, page_id=page_id, kind="objects"
    )
    listing = signed_url(
        method="GET",
        key="",
        path="/" + BUCKET,
        resource_query=(
            ("continuation-token", "foreign"),
            ("list-type", "2"),
            ("max-keys", "1"),
            ("prefix", target.prefix),
        ),
    )
    result = NativePyramidPrefixIoReference(
        scope_id=page_id,
        scope_token=lease_token,
        output_id=output_id,
        storage_ref=target,
        kind="objects",
        request=listing,
    )
    client = client_for(result)
    try:
        with pytest.raises(c.ImageFailure):
            await client.retirement_prefix_reference(request)
    finally:
        await client.aclose()


async def test_failure_envelope_preserves_its_typed_code():
    output_id, operation_id, lease_token = uuid4(), uuid4(), uuid4()
    target = saved_target(output_id)
    result = references(
        "put",
        target,
        [put_member(target, operation_id=operation_id)],
        scope_id=operation_id,
        scope_token=lease_token,
    )
    client = client_for(result, status=409)
    try:
        with pytest.raises(c.ImageFailure) as caught:
            await client.write_reference(write_request(operation_id, lease_token))
    finally:
        await client.aclose()
    assert caught.value.code == "lease_lost"


def test_contract_rejects_unrecorded_verify_and_terminal_members():
    output_id, operation_id, lease_token = uuid4(), uuid4(), uuid4()
    target = saved_target(output_id)
    unrecorded = NativePyramidSignedMember(
        member=member(
            target, etag=None, version_id=None, operation_id=operation_id
        ),
        get=signed_url(
            method="GET", key=target.prefix + "overview.webp", headers={}
        ),
    )
    with pytest.raises(ValidationError):
        references(
            "verify",
            target,
            [unrecorded],
            scope_id=operation_id,
            scope_token=lease_token,
        )
    with pytest.raises(ValidationError):
        NativePyramidReadIoRequest(
            operation_id=operation_id,
            token=lease_token,
            relative_keys=[],
            purpose="inspect",
        )
    with pytest.raises(ValidationError):
        NativePyramidRetirementPrefixIoRequest(
            output_id=output_id,
            lease_token=lease_token,
            page_id=uuid4(),
            kind="objects",
            markers={},
        )
