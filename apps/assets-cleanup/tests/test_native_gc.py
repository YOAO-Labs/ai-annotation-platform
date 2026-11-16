"""Saved-target HTTP deletion, exact receipts and bounded prefix proofs."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import threading
from types import SimpleNamespace
from urllib.parse import parse_qsl, quote, urlsplit
from uuid import uuid4

from botocore.auth import S3SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
import httpx
import pytest

from yoao_assets_images.contract import (
    NativePyramidRetirementClaim,
    NativePyramidRetirementPage,
    NativePyramidSavedMember,
    NativePyramidSavedTarget,
)
from yoao_assets_images.native_storage_contract import (
    NativePyramidIoMember,
    NativePyramidIoReferences,
    NativePyramidPrefixIoReference,
    NativePyramidSignedMember,
    NativePyramidSignedRequest,
    prefix_listing_query,
)
from yoao_assets_cleanup.native_gc import (
    NativeStorageError,
    SignedNativeRetirementStore,
)

_PAYLOAD = b"owned-native-bytes"


def _scope(name="a", *, version="saved-version"):
    output_id = uuid4()
    target = NativePyramidSavedTarget.model_validate({
        "schema": 2,
        "purpose": "media-cache",
        "location_id": str(uuid4()),
        "config_revision": 1,
        "binding_generation": 2,
        "endpoint_url": f"http://store-{name}:9000",
        "bucket": f"cache-{name}",
        "key_prefix": "team/_aap_managed/media-cache/",
        "prefix": f"team/_aap_managed/media-cache/image-pyramids/objects/{output_id}/",
        "storage_identity": "b" * 64,
    })
    claim = NativePyramidRetirementClaim(
        output_id=output_id,
        lease_token=uuid4(),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        storage_ref=target,
        after_operation_id=None,
    )
    member = NativePyramidSavedMember(
        operation_id=uuid4(), relative_key="tiles/0/0/0.webp",
        sha256=hashlib.sha256(_PAYLOAD).hexdigest(), size_bytes=len(_PAYLOAD),
        etag='"saved-etag"', version_id=version,
    )
    page = NativePyramidRetirementPage(
        output_id=output_id, lease_token=claim.lease_token, page_id=uuid4(),
        lease_expires_at=claim.lease_expires_at,
        after_operation_id=member.operation_id, items=[member], exhausted=False,
    )
    return SimpleNamespace(claim=claim, page=page, member=member, target=target)


def _signed(method, target, *, member=None, kind=None):
    path = "/" + quote(target.bucket, safe="-_.~")
    headers = {}
    if member is not None:
        path += "/" + quote(target.prefix + member.relative_key, safe="/-_.~")
        query = {} if member.version_id is None else {"versionId": member.version_id}
        headers["if-match"] = member.etag
    else:
        query = dict(prefix_listing_query(target.prefix, kind))
    request = AWSRequest(
        method=method, url=target.endpoint_url + path,
        params=query, headers=headers,
    )
    S3SigV4QueryAuth(
        Credentials("test-native-key", "test-native-secret"),
        "s3", "us-east-1", expires=120,
    ).add_auth(request)
    query = dict(parse_qsl(urlsplit(request.url).query, keep_blank_values=True))
    issued = datetime.strptime(query["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(
        tzinfo=timezone.utc
    )
    return NativePyramidSignedRequest(
        method=method, url=request.url, headers=headers,
        expires_at=issued + timedelta(seconds=int(query["X-Amz-Expires"])),
    )


class _Signer:
    def __init__(self, scopes):
        self.scopes = {scope.claim.output_id: scope for scope in scopes}
        self.replacement = None

    async def retirement_references(self, request):
        scope = self.scopes[request.output_id]
        assert request.page_id == scope.page.page_id
        assert request.lease_token == scope.claim.lease_token
        return self.replacement or NativePyramidIoReferences(
            mode="retirement_members", scope_id=scope.page.page_id,
            scope_token=scope.claim.lease_token, output_id=scope.claim.output_id,
            storage_ref=scope.target,
            members=[NativePyramidSignedMember(
                member=NativePyramidIoMember.model_validate(scope.member.model_dump()),
                head=_signed("HEAD", scope.target, member=scope.member),
                get=_signed("GET", scope.target, member=scope.member),
                delete=_signed("DELETE", scope.target, member=scope.member),
            )],
        )

    async def retirement_prefix_reference(self, request):
        scope = self.scopes[request.output_id]
        return NativePyramidPrefixIoReference(
            scope_id=request.page_id, scope_token=request.lease_token,
            output_id=request.output_id, storage_ref=scope.target,
            kind=request.kind,
            request=_signed("GET", scope.target, kind=request.kind),
        )


class _Body(httpx.AsyncByteStream):
    def __init__(self, payload, *, close_error=False):
        self.payload = payload
        self.closed = False
        self.close_error = close_error

    async def __aiter__(self):
        if self.payload:
            yield self.payload

    async def aclose(self):
        if self.close_error:
            raise OSError("body closure is unknown")
        self.closed = True


class _S3:
    def __init__(self, scopes):
        self.scopes = {urlsplit(s.target.endpoint_url).hostname: s for s in scopes}
        self.present = {s.claim.output_id: True for s in scopes}
        self.calls = []
        self.bodies = []
        self.payload = _PAYLOAD
        self.headers_override = {}
        self.delete_status = 204
        self.delete_error = None
        self.remove_on_delete = True
        self.close_get_error = False
        self.head_status = None
        self.listing = {}

    def _response(self, status, payload=b"", *, headers=None, close_error=False):
        body = _Body(payload, close_error=close_error)
        self.bodies.append(body)
        return httpx.Response(status, headers=headers, stream=body)

    async def __call__(self, request):
        self.calls.append(request)
        scope = self.scopes[request.url.host]
        if request.url.path == "/" + scope.target.bucket:
            assert request.method == "GET"
            kind = ("versions" if "versions" in request.url.params else
                    "uploads" if "uploads" in request.url.params else "objects")
            assert request.url.params["prefix"] == scope.target.prefix
            limit = "max-uploads" if kind == "uploads" else "max-keys"
            assert request.url.params[limit] == "1"
            return self._response(200, self.listing.get(kind, _xml(scope, kind)))
        assert request.url.path == (
            "/" + scope.target.bucket + "/" + scope.target.prefix + scope.member.relative_key
        )
        assert request.headers["if-match"] == scope.member.etag
        assert request.url.params.get("versionId") == scope.member.version_id
        if request.method == "HEAD" and self.head_status is not None:
            return self._response(self.head_status)
        if not self.present[scope.claim.output_id]:
            return self._response(404)
        if request.method == "DELETE":
            if self.delete_error is not None:
                raise self.delete_error
            if self.delete_status in {200, 204} and self.remove_on_delete:
                self.present[scope.claim.output_id] = False
            return self._response(self.delete_status)
        headers = {"etag": scope.member.etag, "content-length": str(len(_PAYLOAD))}
        if scope.member.version_id is not None:
            headers["x-amz-version-id"] = scope.member.version_id
        for key, value in self.headers_override.items():
            if value is None:
                headers.pop(key, None)
            else:
                headers[key] = value
        return self._response(
            200, self.payload if request.method == "GET" else b"",
            headers=headers, close_error=self.close_get_error and request.method == "GET",
        )


def _xml(scope, kind, *, entries="", truncated="false", bucket=None, prefix=None):
    root, bucket_tag = {
        "versions": ("ListVersionsResult", "Name"),
        "uploads": ("ListMultipartUploadsResult", "Bucket"),
        "objects": ("ListBucketResult", "Name"),
    }[kind]
    return (
        f'<{root} xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<{bucket_tag}>{bucket or scope.target.bucket}</{bucket_tag}>"
        f"<Prefix>{prefix or scope.target.prefix}</Prefix>"
        f"<IsTruncated>{truncated}</IsTruncated>{entries}</{root}>"
    ).encode()


@asynccontextmanager
async def _store(*scopes):
    signer = _Signer(scopes)
    s3 = _S3(scopes)
    async with httpx.AsyncClient(transport=httpx.MockTransport(s3)) as http:
        store = SignedNativeRetirementStore(
            SimpleNamespace(), signer=signer, http=http
        )
        yield store, signer, s3


async def _delete(store, scope):
    references = await store.prepare_page(scope.claim, scope.page)
    await store.delete_member(
        scope.member, references[scope.member.operation_id], threading.Event()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "null", "saved-version"])
async def test_saved_targets_and_versions_drive_every_request_without_a_fixed_store(version):
    a, b = _scope("a", version=version), _scope("b", version=version)
    async with _store(a, b) as (store, _, s3):
        await _delete(store, a)
        await _delete(store, b)
    assert [request.url.host for request in s3.calls] == ["store-a"] * 4 + ["store-b"] * 4
    assert [request.method for request in s3.calls] == ["HEAD", "GET", "DELETE", "HEAD"] * 2
    assert not any(s3.present.values())
    assert all(body.closed for body in s3.bodies)


@pytest.mark.asyncio
@pytest.mark.parametrize("substitution", ["target", "receipt"])
async def test_substituted_saved_scope_is_rejected_before_physical_io(substitution):
    scope = _scope()
    async with _store(scope) as (store, signer, s3):
        original = await signer.retirement_references(SimpleNamespace(
            output_id=scope.claim.output_id, page_id=scope.page.page_id,
            lease_token=scope.claim.lease_token,
        ))
        if substitution == "target":
            signer.replacement = original.model_copy(update={
                "storage_ref": scope.target.model_copy(update={"bucket": "foreign-bucket"})
            })
        else:
            member = original.members[0]
            signer.replacement = original.model_copy(update={"members": [
                member.model_copy(update={"member": member.member.model_copy(
                    update={"sha256": "c" * 64}
                )})
            ]})
        with pytest.raises(NativeStorageError):
            await _delete(store, scope)
        assert s3.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {"etag": '"foreign"'}, {"content-length": "1"},
    {"x-amz-version-id": "foreign-version"},
])
async def test_head_receipt_mismatch_never_reads_or_deletes(headers):
    scope = _scope(version=None)
    async with _store(scope) as (store, _, s3):
        s3.headers_override = headers
        with pytest.raises(NativeStorageError):
            await _delete(store, scope)
        assert [request.method for request in s3.calls] == ["HEAD"]
        assert s3.present[scope.claim.output_id]


@pytest.mark.asyncio
async def test_literal_null_receipt_accepts_an_omitted_observed_version_only():
    scope = _scope(version="null")
    async with _store(scope) as (store, _, s3):
        s3.headers_override = {"x-amz-version-id": None}
        await _delete(store, scope)
        assert not s3.present[scope.claim.output_id]


@pytest.mark.asyncio
async def test_complete_body_digest_mismatch_closes_without_deleting():
    scope = _scope()
    async with _store(scope) as (store, _, s3):
        s3.payload = b"X" + _PAYLOAD[1:]
        with pytest.raises(NativeStorageError):
            await _delete(store, scope)
        assert [request.method for request in s3.calls] == ["HEAD", "GET"]
        assert all(body.closed for body in s3.bodies)
        assert s3.present[scope.claim.output_id]


@pytest.mark.asyncio
async def test_missing_exact_version_is_accounted_without_a_delete():
    scope = _scope()
    async with _store(scope) as (store, _, s3):
        s3.present[scope.claim.output_id] = False
        await _delete(store, scope)
        assert [request.method for request in s3.calls] == ["HEAD"]


@pytest.mark.asyncio
async def test_access_denial_is_not_absence():
    scope = _scope()
    async with _store(scope) as (store, _, s3):
        s3.head_status = 403
        with pytest.raises(NativeStorageError):
            await _delete(store, scope)
        assert [request.method for request in s3.calls] == ["HEAD"]


@pytest.mark.asyncio
async def test_delete_transport_failure_retains_the_unknown_mutation_barrier():
    scope = _scope()
    async with _store(scope) as (store, _, s3):
        s3.delete_error = httpx.ReadTimeout("delete reply lost")
        with pytest.raises(NativeStorageError) as failure:
            await _delete(store, scope)
        assert failure.value.unsettled
        assert [request.method for request in s3.calls] == ["HEAD", "GET", "DELETE"]


@pytest.mark.asyncio
async def test_failed_body_close_is_not_a_settled_read():
    scope = _scope()
    async with _store(scope) as (store, _, s3):
        s3.close_get_error = True
        with pytest.raises(NativeStorageError) as failure:
            await _delete(store, scope)
        assert failure.value.unsettled
        assert [request.method for request in s3.calls] == ["HEAD", "GET"]
        assert s3.present[scope.claim.output_id]


@pytest.mark.asyncio
async def test_delete_precondition_failure_preserves_the_object():
    scope = _scope()
    async with _store(scope) as (store, _, s3):
        s3.delete_status = 412
        with pytest.raises(NativeStorageError) as failure:
            await _delete(store, scope)
        assert not failure.value.unsettled
        assert s3.present[scope.claim.output_id]


@pytest.mark.asyncio
async def test_successful_delete_response_still_requires_observed_absence():
    scope = _scope()
    async with _store(scope) as (store, _, s3):
        s3.remove_on_delete = False
        with pytest.raises(NativeStorageError):
            await _delete(store, scope)
        assert [request.method for request in s3.calls] == ["HEAD", "GET", "DELETE", "HEAD"]
        assert s3.present[scope.claim.output_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "entries"), [
    ("versions", "<Version><Key>foreign</Key></Version>"),
    ("versions", "<DeleteMarker><Key>foreign</Key></DeleteMarker>"),
    ("uploads", "<Upload><Key>foreign</Key></Upload>"),
    ("objects", "<Contents><Key>foreign</Key></Contents>"),
    ("objects", "<CommonPrefixes><Prefix>foreign/</Prefix></CommonPrefixes>"),
])
async def test_terminal_probe_refuses_foreign_material_without_deleting(kind, entries):
    scope = _scope()
    async with _store(scope) as (store, _, s3):
        s3.listing[kind] = _xml(scope, kind, entries=entries)
        with pytest.raises(NativeStorageError):
            await store.verify_prefix_absent(scope.claim, scope.page, threading.Event())
        assert all(request.method == "GET" for request in s3.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["truncated", "bucket", "prefix", "malformed"])
async def test_terminal_probe_requires_complete_exact_scope_xml(corruption):
    scope = _scope()
    options = {"truncated": {"truncated": "true"}, "bucket": {"bucket": "foreign"},
               "prefix": {"prefix": "foreign/"}}
    async with _store(scope) as (store, _, s3):
        s3.listing["versions"] = (
            b"<malformed" if corruption == "malformed" else
            _xml(scope, "versions", **options[corruption])
        )
        with pytest.raises(NativeStorageError):
            await store.verify_prefix_absent(scope.claim, scope.page, threading.Event())
        assert all(request.method == "GET" for request in s3.calls)


@pytest.mark.asyncio
async def test_terminal_absence_requires_all_three_bounded_scans():
    scope = _scope()
    async with _store(scope) as (store, _, s3):
        await store.verify_prefix_absent(scope.claim, scope.page, threading.Event())
        assert len(s3.calls) == 3
        assert "versions" in s3.calls[0].url.params
        assert "uploads" in s3.calls[1].url.params
        assert s3.calls[2].url.params["list-type"] == "2"
        assert all(body.closed for body in s3.bodies)
