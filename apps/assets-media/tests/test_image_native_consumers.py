"""Consumer-visible regression coverage for dynamic saved native targets.

These sources exercise the Images producer and control consumers against the
exact Source-issued request/response shapes: actual raw HTTP receipts, full
saved-version readback, durable read permits, receipt comparison before URL
publication, explicit lifetime equality and the fixture/production split.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from yoao_assets_images import contract as c
from yoao_assets_images.geometry import (
    build_manifest,
    canonical_manifest_bytes,
    sha256_digest,
)
from yoao_assets_images.native_storage_contract import (
    NativePyramidIoMember,
    NativePyramidIoReferences,
    NativePyramidSignedMember,
    NativePyramidSignedRequest,
    NativePyramidWriteIoRequest,
)
from yoao_assets_images.policy import ImagePolicy
from yoao_assets_images.transport import (
    ImageAssetReferencesRequest,
    ImageGenerationReferenceRequest,
)
from yoao_assets_images_runtime import native_media
from yoao_assets_images_runtime.config import (
    ConfigurationError,
    ImageStorageConfig,
    load_config,
    load_native_storage_config,
)
from yoao_assets_images_runtime.control import ImageControl as NativeImageControl
from yoao_assets_images_runtime.storage import S3ImageStorage
from yoao_assets_images_runtime.worker import _require_native_allocation
from yoao_assets_media_runtime.fixture_native_outputs import (
    native_execution,
    output_scope,
)

_SAVED_ORIGIN = "http://saved.local"
_POLICY = ImagePolicy(url_expiry_seconds=900)


class OrdinarySourceDatabase:
    """Answer the original-read admission the way the ordinary owner does."""

    def __init__(self, delegate):
        self.delegate = delegate

    async def call(self, name, request):
        if name in {"begin_image_original_actor", "begin_image_original_system"}:
            return c.OriginalRead(ownership="ordinary", object_key=request.object_key)
        return await self.delegate.call(name, request)


class ImageControl(NativeImageControl):
    def __init__(self, database, *args, **kwargs):
        super().__init__(
            OrdinarySourceDatabase(database) if database is not None else None,
            *args,
            **kwargs,
        )


def source(*, owner=None, thumbnail_path=None, thumbnail_current=False):
    owner = owner or c.ImageOwner(kind="dataset_item", id=uuid4())
    return c.ImageSource(
        owner=owner,
        task_id=None,
        project_id=uuid4(),
        dataset_id=uuid4(),
        item_id=owner.id,
        source_generation=1,
        source_snapshot="a" * 64,
        object_identity={"etag": "source-etag", "byte_size": 128},
        source_drifted=False,
        upload_ready=True,
        file_path="source/image.png",
        file_name="image.png",
        file_type="image",
        bucket_kind="datasets",
        file_size=128,
        content_hash=None,
        width=2,
        height=1,
        thumbnail_path=thumbnail_path,
        blurhash="LEHV6nWB2yk8pyo0adR*.7kCMdnj" if thumbnail_path else None,
        thumbnail_source_generation=1 if thumbnail_current else None,
        thumbnail_error=None,
    )


def saved_target(output_id, *, key_prefix="_aap_managed/media-cache/"):
    return c.NativePyramidSavedTarget(
        schema_=2,
        purpose="media-cache",
        location_id=uuid4(),
        config_revision=3,
        binding_generation=7,
        endpoint_url=_SAVED_ORIGIN,
        bucket="saved-bucket",
        key_prefix=key_prefix,
        prefix=f"{key_prefix}image-pyramids/objects/{output_id}/",
        storage_identity="e" * 64,
    )


def native_projection(current, *, output=None, fixture_owned=False, status="ready"):
    output_id = uuid4()
    target = saved_target(output_id)
    if output is None:
        output = c.NativePyramidOutputReference(
            output_id=output_id,
            storage_ref=target,
            url_expiry_seconds=300,
            fixture_owned=fixture_owned,
        )
    manifest_data = build_manifest(
        generation=3,
        source_fingerprint="sha256:" + "b" * 64,
        width=2,
        height=1,
        overview_width=2,
        overview_height=1,
        overview_digest="sha256:" + "c" * 64,
        policy=_POLICY,
    )
    raw = canonical_manifest_bytes(manifest_data)
    manifest = c.ImagePyramidManifest.model_validate(manifest_data)
    tile_count = sum(level.rows * level.columns for level in manifest.levels)
    pyramid = c.ImagePyramidGeneration(
        asset_id=uuid4(),
        native=True,
        native_output=output,
        profile_version=_POLICY.profile_version,
        status=status,
        required=False,
        generation=3 if status == "ready" else None,
        building_generation=None,
        building_status=None,
        source_generation=current.source_generation,
        source_snapshot=current.source_snapshot,
        width=2,
        height=1,
        format="webp",
        manifest=manifest if status == "ready" else None,
        manifest_key=(
            output.storage_ref.prefix + "manifest.json" if status == "ready" else None
        ),
        manifest_digest=sha256_digest(raw) if status == "ready" else None,
        overview_key=(
            output.storage_ref.prefix + "overview.webp" if status == "ready" else None
        ),
        overview_digest=(
            manifest.overview.content_digest if status == "ready" else None
        ),
        tile_count=tile_count if status == "ready" else 0,
        retained_bytes=len(raw) + 8 if status == "ready" else 0,
    )
    return (
        c.ImageGenerationRead(source=current, pyramid=pyramid),
        raw,
        manifest,
        output,
    )


def saved_member(relative_key, raw, *, etag=None, version_id="v1"):
    return NativePyramidIoMember(
        operation_id=uuid4(),
        relative_key=relative_key,
        sha256=sha256(raw).hexdigest(),
        size_bytes=len(raw),
        etag=etag or f'"etag-{relative_key.replace("/", "-")}"',
        version_id=version_id,
    )


def signed_request(method, url, *, headers=None):
    return NativePyramidSignedRequest(
        method=method,
        url=url,
        headers={key.lower(): value for key, value in (headers or {}).items()},
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
    )


def member_url(member, *, relative=None):
    key = relative if relative is not None else member.relative_key
    suffix = f"?versionId={member.version_id}" if member.version_id else ""
    return f"{_SAVED_ORIGIN}/saved-bucket/{key}{suffix}"


def conditional(member, *, relative=None, method="GET"):
    return signed_request(
        method,
        member_url(member, relative=relative),
        headers={"If-Match": member.etag},
    )


def read_references(mode, scope_id, scope_token, output, members, *, expires_in=None):
    return NativePyramidIoReferences(
        mode=mode,
        scope_id=scope_id,
        scope_token=scope_token,
        output_id=output.output_id,
        storage_ref=output.storage_ref,
        members=members,
        expires_in=expires_in,
    )


class _MemberBody(httpx.AsyncByteStream):
    """A genuinely streamed member body, the way a real saved target answers."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.closed = False

    async def __aiter__(self):
        yield self.body

    async def aclose(self) -> None:
        self.closed = True


def streamed(status_code: int = 200, *, headers=None, body: bytes = b""):
    return httpx.Response(status_code, headers=headers, stream=_MemberBody(body))


def saved_http_client(bodies):
    def handler(request):
        key = request.url.path.split("/saved-bucket/", 1)[1]
        if key not in bodies:
            return streamed(404)
        body = bodies[key]
        return streamed(
            200,
            headers={
                "etag": f'"etag-{key.replace("/", "-")}"',
                "x-amz-version-id": "v1",
                "content-length": str(len(body)),
            },
            body=body,
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class FakeNativeReader:
    def __init__(self, inspection, publication=None):
        self.inspection = inspection
        self.publication = publication
        self.calls = []

    async def read_references(self, request):
        self.calls.append(request)
        if request.purpose == "inspect":
            return self.inspection(request)
        assert self.publication is not None
        return self.publication(request)

    async def aclose(self):
        return None


class FakeStorage:
    def __init__(self, *, fixture_values=None):
        self.fixture_values = dict(fixture_values or {})
        self.head_source_calls = []
        self.head_cache_calls = []
        self.read_cache_calls = []
        self.signed_cache = []
        self.signed_source = []
        self.fixture_head_calls = []
        self.fixture_read_calls = []
        self.fixture_sign_calls = []

    def bind_original(self, permit):
        assert permit.ownership == "ordinary"
        return self

    async def head_source(self, current):
        self.head_source_calls.append(current)
        return current.object_identity

    async def head_cache(self, key):
        self.head_cache_calls.append(key)
        return None

    async def read_cache(self, key, limit):
        self.read_cache_calls.append((key, limit))
        raise AssertionError("configured-cache read used for native data")

    async def sign_cache(self, key, expires_in=3600):
        self.signed_cache.append((key, expires_in))
        raise AssertionError("configured-cache signing used for native data")

    async def sign_source(self, current, expires_in=3600):
        self.signed_source.append((current, expires_in))
        return f"https://image.local/{current.bucket_kind}/{current.file_path}"

    async def head_fixture_cache(self, key):
        self.fixture_head_calls.append(key)
        return c.ObjectIdentity(etag="fixture-etag", byte_size=64)

    async def read_fixture_cache(self, key, limit):
        self.fixture_read_calls.append((key, limit))
        value = self.fixture_values.get(key)
        if value is None:
            raise c.ImageFailure("object_missing")
        if len(value) > limit:
            raise c.ImageFailure("resource_budget_exceeded")
        return value

    async def sign_fixture_cache(self, key, expires_in=3600):
        self.fixture_sign_calls.append((key, expires_in))
        return f"https://fixture.local/cache/{key}?expires={expires_in}"


class NativeReadDatabase:
    """Owner database that issues and settles exactly the caller's permit."""

    def __init__(self, current, state):
        self.current = current
        self.state = state
        self.begun = []
        self.closed = []
        self.operations = []

    async def call(self, operation, request):
        self.operations.append(operation)
        if operation in {"read_image_source", "observe_image_source"}:
            return self.current
        if operation == "read_image_generation":
            return self.state
        if operation == "begin_native_pyramid_read":
            self.begun.append(request)
            output = self.state.pyramid.native_output
            return c.NativePyramidReadPermit(
                operation_id=request.operation_id,
                token=request.token,
                output_id=output.output_id,
                storage_ref=output.storage_ref,
                pyramid=self.state.pyramid,
                url_expiry_seconds=output.url_expiry_seconds,
                fixture_owned=False,
            )
        if operation == "complete_native_pyramid_read":
            self.closed.append(request)
            return c.NativePyramidReadClosure(
                operation_id=request.operation_id,
                token=request.token,
                closed=True,
            )
        raise AssertionError(f"unexpected image operation {operation}")


def native_read_reader(
    state, raw, *, tile_bodies=None, public_origin="https://cdn.local"
):
    output = state.pyramid.native_output
    bodies = {"manifest.json": raw, "overview.webp": b"overview-bytes"}
    bodies.update(tile_bodies or {})
    receipts = {}
    for relative, body in bodies.items():
        receipts[relative] = saved_member(relative, body)

    def inspection(request):
        members = []
        for relative in request.relative_keys:
            member = receipts[relative]
            members.append(
                NativePyramidSignedMember(
                    member=member,
                    put=None,
                    head=conditional(member, method="HEAD"),
                    get=conditional(member),
                    delete=None,
                    public_url=None,
                    public_expires_at=None,
                )
            )
        return read_references(
            "read",
            request.operation_id,
            request.token,
            output,
            members,
            expires_in=None,
        )

    def publication(request):
        members = []
        for relative in request.relative_keys:
            member = receipts[relative]
            members.append(
                NativePyramidSignedMember(
                    member=member,
                    put=None,
                    head=None,
                    get=None,
                    delete=None,
                    public_url=(
                        f"{public_origin}/{output.storage_ref.prefix}{relative}"
                        f"?expires={request.expires_in}"
                    ),
                    public_expires_at=datetime.now(timezone.utc)
                    + timedelta(seconds=request.expires_in),
                )
            )
        return read_references(
            "read",
            request.operation_id,
            request.token,
            output,
            members,
            expires_in=request.expires_in,
        )

    return FakeNativeReader(inspection, publication), bodies


def storage_config():
    return ImageStorageConfig(
        endpoint="http://s3.internal",
        public_endpoint="http://s3.public",
        region="us-east-1",
        datasets_bucket="datasets",
        legacy_bucket="annotations",
        cache_bucket="derived-cache",
        inputs_bucket="accepted-inputs",
        access_key="access",
        secret_key="secret",
    )


def test_native_media_write_binds_the_reserved_member_and_saved_target():
    output_id = uuid4()
    target = saved_target(output_id)
    allocation = c.PyramidOutputAllocation(
        output_id=output_id,
        storage_ref=target,
        job_id=uuid4(),
        job_generation=4,
        lease_token=uuid4(),
        asset_id=uuid4(),
        generation=3,
    )
    reservation = c.PyramidMemberReservation(
        operation_id=uuid4(),
        output_id=output_id,
        relative_key="manifest.json",
        sha256="d" * 64,
        size_bytes=12,
    )
    request = NativePyramidWriteIoRequest(
        operation_id=reservation.operation_id,
        job_id=allocation.job_id,
        job_generation=allocation.job_generation,
        lease_token=allocation.lease_token,
    )
    member = NativePyramidIoMember(
        operation_id=reservation.operation_id,
        relative_key="manifest.json",
        sha256=reservation.sha256,
        size_bytes=reservation.size_bytes,
        etag=None,
        version_id=None,
    )
    output = c.NativePyramidOutputReference(
        output_id=output_id,
        storage_ref=target,
        url_expiry_seconds=300,
        fixture_owned=False,
    )
    result = read_references(
        "put",
        request.operation_id,
        request.lease_token,
        output,
        [
            NativePyramidSignedMember(
                member=member,
                put=signed_request(
                    "PUT",
                    f"{_SAVED_ORIGIN}/saved-bucket/{target.prefix}manifest.json",
                    headers={
                        "If-None-Match": "*",
                        "Content-Length": "12",
                        "Content-Type": "application/json",
                        "x-amz-meta-sha256": reservation.sha256,
                    },
                ),
                head=None,
                get=None,
                delete=None,
                public_url=None,
                public_expires_at=None,
            )
        ],
    )
    signed = native_media.write_member(
        result,
        request=request,
        output_id=allocation.output_id,
        storage_ref=allocation.storage_ref,
        reservation=reservation,
    )
    assert signed.put is not None

    with pytest.raises(c.ImageFailure, match="invalid_response"):
        native_media.write_member(
            result,
            request=request.model_copy(update={"lease_token": uuid4()}),
            output_id=allocation.output_id,
            storage_ref=allocation.storage_ref,
            reservation=reservation,
        )
    with pytest.raises(c.ImageFailure, match="invalid_response"):
        native_media.write_member(
            result,
            request=request,
            output_id=allocation.output_id,
            storage_ref=saved_target(output_id),
            reservation=reservation,
        )


@pytest.mark.asyncio
async def test_native_media_put_returns_the_actual_raw_receipt_only():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(
            200, headers={"etag": '"put-etag"', "x-amz-version-id": "v9"}
        )

    signed = signed_request(
        "PUT",
        f"{_SAVED_ORIGIN}/saved-bucket/_aap_managed/media-cache/"
        "image-pyramids/objects/1/manifest.json",
        headers={
            "If-None-Match": "*",
            "Content-Length": "3",
            "Content-Type": "application/json",
            "x-amz-meta-sha256": "f" * 64,
        },
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        receipt = await native_media.put_member(
            http,
            signed,
            content=b"abc",
            relative_key="manifest.json",
            size_bytes=3,
            sha256="f" * 64,
        )
    assert seen["method"] == "PUT"
    assert seen["headers"]["if-none-match"] == "*"
    assert receipt.etag == '"put-etag"'
    assert receipt.version_id == "v9"
    assert native_media.same_version("null", None)
    assert native_media.same_version(None, None)
    assert not native_media.same_version(None, "v-current")
    assert not native_media.same_version("null", "v-current")
    assert not native_media.same_version("v1", None)

    def precondition(_request):
        return httpx.Response(412, headers={"etag": '"other"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(precondition)) as http:
        with pytest.raises(native_media.NativeWriteRefused):
            await native_media.put_member(
                http,
                signed,
                content=b"abc",
                relative_key="manifest.json",
                size_bytes=3,
                sha256="f" * 64,
            )

    def literal_null(_request):
        return httpx.Response(
            200, headers={"etag": '"put-etag"', "x-amz-version-id": "null"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(literal_null)) as http:
        null_receipt = await native_media.put_member(
            http,
            signed,
            content=b"abc",
            relative_key="manifest.json",
            size_bytes=3,
            sha256="f" * 64,
        )
    assert null_receipt.version_id == "null"

    def no_version(_request):
        return httpx.Response(200, headers={"etag": '"put-etag"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_version)) as http:
        absent_receipt = await native_media.put_member(
            http,
            signed,
            content=b"abc",
            relative_key="manifest.json",
            size_bytes=3,
            sha256="f" * 64,
        )
    assert absent_receipt.version_id is None

    def unquoted(_request):
        return httpx.Response(200, headers={"etag": "weak", "x-amz-version-id": "v9"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(unquoted)) as http:
        with pytest.raises(c.ImageFailure, match="upload_verification_failed"):
            await native_media.put_member(
                http,
                signed,
                content=b"abc",
                relative_key="manifest.json",
                size_bytes=3,
                sha256="f" * 64,
            )


@pytest.mark.asyncio
async def test_native_media_saved_version_readback_proves_exact_bytes():
    body = b"exact saved member bytes"
    member = saved_member("tiles/0/0/0.webp", body)
    signed = conditional(member)

    async with saved_http_client({member.relative_key: body}) as http:
        assert (
            await native_media.get_member(http, signed, member=member, limit=len(body))
            == body
        )

    async with saved_http_client({member.relative_key: body + b"x"}) as http:
        with pytest.raises(c.ImageFailure, match="invalid_response"):
            await native_media.get_member(
                http, signed, member=member, limit=len(body) + 1
            )

    async def wrong_version(request):
        return streamed(
            200,
            headers={
                "etag": member.etag,
                "x-amz-version-id": "other",
                "content-length": str(len(body)),
            },
            body=body,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(wrong_version)) as http:
        with pytest.raises(c.ImageFailure, match="invalid_response"):
            await native_media.get_member(http, signed, member=member, limit=len(body))


@pytest.mark.asyncio
async def test_native_media_unversioned_receipt_admits_only_an_omitted_header():
    body = b"unversioned saved member"
    unversioned = saved_member("overview.webp", body, version_id="null")
    signed = conditional(unversioned)

    def without_header(_request):
        return streamed(
            200,
            headers={"etag": unversioned.etag, "content-length": str(len(body))},
            body=body,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(without_header)) as http:
        assert (
            await native_media.get_member(
                http, signed, member=unversioned, limit=len(body)
            )
            == body
        )

    def substituted_current(_request):
        return streamed(
            200,
            headers={
                "etag": unversioned.etag,
                "x-amz-version-id": str(uuid4()),
                "content-length": str(len(body)),
            },
            body=body,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(substituted_current)
    ) as http:
        with pytest.raises(c.ImageFailure, match="invalid_response"):
            await native_media.get_member(
                http, signed, member=unversioned, limit=len(body)
            )

    absent = saved_member("overview.webp", body, version_id=None)
    signed_absent = conditional(absent)

    def current_version(_request):
        return streamed(
            200,
            headers={
                "etag": absent.etag,
                "x-amz-version-id": "current-version",
                "content-length": str(len(body)),
            },
            body=body,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(current_version)
    ) as http:
        with pytest.raises(c.ImageFailure, match="invalid_response"):
            await native_media.get_member(
                http, signed_absent, member=absent, limit=len(body)
            )


class FakeNativeWriter:
    def __init__(self, *, put_result, verify_result):
        self.put_result = put_result
        self.verify_result = verify_result
        self.calls = []
        self.closed = False

    async def write_reference(self, request):
        self.calls.append(("put", request))
        return self.put_result

    async def verify_reference(self, request):
        self.calls.append(("verify", request))
        return self.verify_result

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_ordinary_member_write_executes_source_put_then_reads_back(tmp_path):
    output_id = uuid4()
    target = saved_target(output_id)
    allocation = c.PyramidOutputAllocation(
        output_id=output_id,
        storage_ref=target,
        job_id=uuid4(),
        job_generation=2,
        lease_token=uuid4(),
        asset_id=uuid4(),
        generation=3,
    )
    body = b"exact saved member bytes"
    path = tmp_path / "manifest.json"
    path.write_bytes(body)
    reservation = c.PyramidMemberReservation(
        operation_id=uuid4(),
        output_id=output_id,
        relative_key="manifest.json",
        sha256=sha256(body).hexdigest(),
        size_bytes=len(body),
    )
    signed = NativePyramidIoMember(
        operation_id=reservation.operation_id,
        relative_key=reservation.relative_key,
        sha256=reservation.sha256,
        size_bytes=reservation.size_bytes,
        etag='"put-etag"',
        version_id="v1",
    )
    output = c.NativePyramidOutputReference(
        output_id=output_id,
        storage_ref=target,
        url_expiry_seconds=300,
        fixture_owned=False,
    )
    write_result = read_references(
        "put",
        reservation.operation_id,
        allocation.lease_token,
        output,
        [
            NativePyramidSignedMember(
                member=signed.model_copy(update={"etag": None, "version_id": None}),
                put=signed_request(
                    "PUT",
                    f"{_SAVED_ORIGIN}/saved-bucket/{target.prefix}manifest.json",
                    headers={
                        "If-None-Match": "*",
                        "Content-Length": str(len(body)),
                        "Content-Type": "application/json",
                        "x-amz-meta-sha256": reservation.sha256,
                    },
                ),
                head=None,
                get=None,
                delete=None,
                public_url=None,
                public_expires_at=None,
            )
        ],
    )
    verify_result = read_references(
        "verify",
        reservation.operation_id,
        allocation.lease_token,
        output,
        [
            NativePyramidSignedMember(
                member=signed,
                put=None,
                head=conditional(signed, method="HEAD"),
                get=conditional(signed),
                delete=None,
                public_url=None,
                public_expires_at=None,
            )
        ],
    )
    writer = FakeNativeWriter(put_result=write_result, verify_result=verify_result)
    requests = []

    def handler(request):
        requests.append((request.method, str(request.url)))
        if request.method == "PUT":
            assert request.headers["if-none-match"] == "*"
            assert request.headers["content-length"] == str(len(body))
            assert request.headers["x-amz-meta-sha256"] == reservation.sha256
            return httpx.Response(
                200, headers={"etag": '"put-etag"', "x-amz-version-id": "v1"}
            )
        assert request.method == "GET"
        assert request.headers["if-match"] == '"put-etag"'
        return streamed(
            200,
            headers={
                "etag": '"put-etag"',
                "x-amz-version-id": "v1",
                "content-length": str(len(body)),
            },
            body=body,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    storage = S3ImageStorage(
        storage_config(),
        client=object(),
        signer=object(),
        http_client=http,
        native_writer=writer,
    )
    claim = SimpleNamespace(job_id=allocation.job_id)
    try:
        async with native_execution(None, "image", claim.job_id):
            async with output_scope(None, storage, claim, "image", None):
                receipt = await storage.upload_pyramid_member(
                    path, allocation, reservation
                )
                assert receipt == c.PyramidMemberReceipt(
                    operation_id=reservation.operation_id,
                    etag='"put-etag"',
                    version_id="v1",
                )
                assert (
                    await storage.verify_pyramid_member(
                        allocation, reservation, receipt
                    )
                    is None
                )
    finally:
        await http.aclose()
        await storage.close()
    assert [method for method, _url in requests] == ["PUT", "GET"]
    assert [call[0] for call in writer.calls] == ["put", "verify"]
    assert writer.closed


def test_native_allocation_rejects_a_substituted_saved_scope():
    output_id = uuid4()
    target = saved_target(output_id)
    allocation = c.PyramidOutputAllocation(
        output_id=output_id,
        storage_ref=target,
        job_id=uuid4(),
        job_generation=2,
        lease_token=uuid4(),
        asset_id=uuid4(),
        generation=3,
    )
    claim = c.ImageClaim.model_construct(
        job_id=allocation.job_id,
        generation=allocation.job_generation,
        lease_token=allocation.lease_token,
        pyramid_asset_id=allocation.asset_id,
        pyramid_generation=allocation.generation,
    )
    assert _require_native_allocation(claim, allocation) == target

    wrong_asset = allocation.model_copy(update={"asset_id": uuid4()})
    with pytest.raises(c.ImageFailure, match="invalid_response"):
        _require_native_allocation(claim, wrong_asset)
    wrong_prefix = allocation.model_copy(
        update={
            "storage_ref": target.model_copy(update={"prefix": target.prefix + "x/"})
        }
    )
    with pytest.raises(c.ImageFailure, match="invalid_response"):
        _require_native_allocation(claim, wrong_prefix)


@pytest.mark.asyncio
async def test_native_generation_reference_settles_its_permit_and_uses_receipts():
    current = source()
    state, raw, manifest, output = native_projection(current)
    database = NativeReadDatabase(current, state)
    storage = FakeStorage()
    reader, bodies = native_read_reader(state, raw)
    http = saved_http_client(bodies)
    control = ImageControl(
        database,
        storage,
        _POLICY,
        native_storage=reader,
        native_http=http,
    )
    try:
        result = await control.generation_reference(
            ImageGenerationReferenceRequest(
                actor_id=uuid4(),
                dataset_item_id=current.owner.id,
                expires_in=60,
            )
        )
    finally:
        await http.aclose()
        await control.close()
    assert result.expires_in == 60
    assert result.manifest == manifest
    assert result.overview_url.endswith("overview.webp?expires=60")
    assert [request.purpose for request in reader.calls] == ["inspect", "publish"]
    assert len(database.begun) == 1
    assert len(database.closed) == 1
    assert database.closed[0].operation_id == database.begun[0].operation_id
    assert database.closed[0].token == database.begun[0].token
    assert storage.head_cache_calls == []
    assert storage.read_cache_calls == []
    assert storage.signed_cache == []


@pytest.mark.asyncio
async def test_native_generation_reference_defaults_and_refuses_too_long_requests():
    current = source()
    state, raw, _manifest, _output = native_projection(current)
    database = NativeReadDatabase(current, state)
    reader, bodies = native_read_reader(state, raw)
    http = saved_http_client(bodies)
    control = ImageControl(
        database,
        FakeStorage(),
        _POLICY,
        native_storage=reader,
        native_http=http,
    )
    try:
        default = await control.generation_reference(
            ImageGenerationReferenceRequest(
                actor_id=uuid4(), dataset_item_id=current.owner.id
            )
        )
        assert default.expires_in == 300
        with pytest.raises(c.ImageFailure, match="invalid_request"):
            await control.generation_reference(
                ImageGenerationReferenceRequest(
                    actor_id=uuid4(),
                    dataset_item_id=current.owner.id,
                    expires_in=301,
                )
            )
    finally:
        await http.aclose()
        await control.close()
    assert len(database.begun) == 1
    assert len(database.closed) == 1


@pytest.mark.asyncio
async def test_native_generation_read_failure_still_settles_the_permit():
    current = source()
    state, raw, _manifest, output = native_projection(current)
    database = NativeReadDatabase(current, state)

    overview = saved_member("overview.webp", b"overview-bytes")
    rewritten = saved_member("manifest.json", raw).model_copy(
        update={"etag": '"rewritten"'}
    )

    def broken(request):
        members = []
        for relative in request.relative_keys:
            member = rewritten if relative == "manifest.json" else overview
            members.append(
                NativePyramidSignedMember(
                    member=member,
                    put=None,
                    head=conditional(member, method="HEAD"),
                    get=conditional(member),
                    delete=None,
                    public_url=None,
                    public_expires_at=None,
                )
            )
        return read_references(
            "read",
            request.operation_id,
            request.token,
            output,
            members,
            expires_in=None,
        )

    http = saved_http_client({"manifest.json": raw, "overview.webp": b"overview-bytes"})
    control = ImageControl(
        database,
        FakeStorage(),
        _POLICY,
        native_storage=FakeNativeReader(broken),
        native_http=http,
    )
    try:
        with pytest.raises(c.ImageFailure):
            await control.generation_reference(
                ImageGenerationReferenceRequest(
                    actor_id=uuid4(), dataset_item_id=current.owner.id
                )
            )
    finally:
        await http.aclose()
        await control.close()
    assert len(database.closed) == 1


@pytest.mark.asyncio
async def test_unproven_native_response_closure_keeps_the_read_permit(monkeypatch):
    current = source()
    state, raw, _manifest, _output = native_projection(current)
    database = NativeReadDatabase(current, state)
    reader, bodies = native_read_reader(state, raw)
    http = saved_http_client(bodies)

    class UnsettledRead(c.ImageFailure):
        storage_operation_unsettled = True

    async def unsettled(*args, **kwargs):
        raise UnsettledRead("storage_unavailable")

    monkeypatch.setattr(native_media, "get_member", unsettled)
    control = ImageControl(
        database,
        FakeStorage(),
        _POLICY,
        native_storage=reader,
        native_http=http,
    )
    try:
        with pytest.raises(UnsettledRead) as failure:
            await control.generation_reference(
                ImageGenerationReferenceRequest(
                    actor_id=uuid4(), dataset_item_id=current.owner.id
                )
            )
    finally:
        await http.aclose()
        await control.close()

    assert failure.value.storage_operation_unsettled is True
    assert len(database.begun) == 1
    assert database.closed == []


@pytest.mark.asyncio
async def test_missing_native_response_with_failed_close_is_not_repaired():
    current = source()
    state, raw, _manifest, _output = native_projection(current)
    database = NativeReadDatabase(current, state)
    reader, _bodies = native_read_reader(state, raw)

    class UnclosedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b""

        async def aclose(self):
            raise RuntimeError("response closure failed")

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(404, stream=UnclosedBody())
        )
    )
    control = ImageControl(
        database,
        FakeStorage(),
        _POLICY,
        native_storage=reader,
        native_http=http,
    )
    try:
        with pytest.raises(native_media.NativeMemberMissing) as failure:
            await control.generation_reference(
                ImageGenerationReferenceRequest(
                    actor_id=uuid4(), dataset_item_id=current.owner.id
                )
            )
    finally:
        await http.aclose()
        await control.close()
    assert failure.value.storage_operation_unsettled is True
    assert database.closed == []
    assert all(request.purpose == "inspect" for request in reader.calls)
    assert "invalidate_image_media" not in database.operations


@pytest.mark.asyncio
async def test_native_asset_references_publish_only_inspected_tile_receipts():
    current = source()
    state, raw, manifest, output = native_projection(current)
    tile_key = "tiles/0/0/0.webp"
    database = NativeReadDatabase(current, state)
    reader, bodies = native_read_reader(
        state, raw, tile_bodies={tile_key: b"tile-bytes"}
    )
    http = saved_http_client(bodies)
    control = ImageControl(
        database,
        FakeStorage(),
        _POLICY,
        native_storage=reader,
        native_http=http,
    )
    request = ImageAssetReferencesRequest(
        actor_id=uuid4(),
        dataset_item_id=current.owner.id,
        items=[
            {"kind": "overview", "generation": 3},
            {"kind": "tile", "generation": 3, "level": 0, "x": 0, "y": 0},
        ],
    )
    try:
        result = await control.asset_references(request)
    finally:
        await http.aclose()
        await control.close()
    assert result.expires_in == 300
    assert [item.kind for item in result.items] == ["overview", "tile"]
    assert result.items[1].url.endswith(f"{tile_key}?expires=300")
    assert len(database.begun) == 1
    assert len(database.closed) == 1


@pytest.mark.asyncio
async def test_native_fixture_outputs_use_the_fixture_owner_without_a_permit():
    current = source()
    state, raw, _manifest, output = native_projection(current, fixture_owned=True)
    prefix = output.storage_ref.prefix
    database = NativeReadDatabase(current, state)
    storage = FakeStorage(fixture_values={prefix + "manifest.json": raw})
    control = ImageControl(database, storage, _POLICY, native_storage=None)
    try:
        result = await control.generation_reference(
            ImageGenerationReferenceRequest(
                actor_id=uuid4(), dataset_item_id=current.owner.id
            )
        )
    finally:
        await control.close()
    assert result.expires_in == 300
    assert result.overview_url.startswith("https://fixture.local/cache/")
    assert storage.fixture_read_calls == [(prefix + "manifest.json", 1024 * 1024)]
    assert storage.fixture_head_calls == [prefix + "overview.webp"]
    assert storage.fixture_sign_calls == [(prefix + "overview.webp", 300)]
    assert database.begun == []
    assert database.closed == []
    assert storage.read_cache_calls == []
    assert storage.signed_cache == []


@pytest.mark.asyncio
async def test_fixture_native_overview_refuses_a_key_outside_the_saved_prefix():
    current = source()
    state, raw, _manifest, output = native_projection(current, fixture_owned=True)
    legacy_key = f"image-pyramids/{uuid4()}/g3/a{uuid4()}/overview.webp"
    pyramid = state.pyramid.model_copy(update={"overview_key": legacy_key})
    state = state.model_copy(update={"pyramid": pyramid})
    database = NativeReadDatabase(current, state)
    storage = FakeStorage(
        fixture_values={output.storage_ref.prefix + "manifest.json": raw}
    )
    control = ImageControl(database, storage, _POLICY, native_storage=None)
    try:
        with pytest.raises(c.ImageFailure, match="invalid_response"):
            await control.generation_reference(
                ImageGenerationReferenceRequest(
                    actor_id=uuid4(), dataset_item_id=current.owner.id
                )
            )
    finally:
        await control.close()
    assert storage.fixture_read_calls == []
    assert storage.fixture_head_calls == []
    assert storage.fixture_sign_calls == []


@pytest.mark.asyncio
async def test_native_frozen_url_policy_above_the_sigv4_limit_is_capped():
    current = source()
    state, raw, _manifest, output = native_projection(current)
    frozen = output.model_copy(update={"url_expiry_seconds": 2_147_483_647})
    state = state.model_copy(
        update={"pyramid": state.pyramid.model_copy(update={"native_output": frozen})}
    )
    database = NativeReadDatabase(current, state)
    reader, bodies = native_read_reader(state, raw)
    http = saved_http_client(bodies)
    control = ImageControl(
        database,
        FakeStorage(),
        ImagePolicy(url_expiry_seconds=900),
        native_storage=reader,
        native_http=http,
    )
    try:
        default = await control.generation_reference(
            ImageGenerationReferenceRequest(
                actor_id=uuid4(), dataset_item_id=current.owner.id
            )
        )
        explicit = await control.generation_reference(
            ImageGenerationReferenceRequest(
                actor_id=uuid4(),
                dataset_item_id=current.owner.id,
                expires_in=300,
            )
        )
    finally:
        await http.aclose()
        await control.close()
    assert default.expires_in == 900
    assert explicit.expires_in == 300
    assert len(database.begun) == 2
    assert len(database.closed) == 2


def _worker_environment():
    return {
        "ASSETS_IMAGES_DATABASE_URL": "postgresql://image@db/image",
        "ASSETS_MEDIA_S3_ENDPOINT": "http://s3.internal",
        "ASSETS_MEDIA_DATASETS_BUCKET": "datasets",
        "ASSETS_MEDIA_CACHE_BUCKET": "derived-cache",
        "ASSETS_MEDIA_INPUTS_BUCKET": "accepted-inputs",
        "ASSETS_MEDIA_S3_ACCESS_KEY": "access",
        "ASSETS_MEDIA_S3_SECRET_KEY": "secret",
        "MINIO_BUCKET": "annotations",
    }


def test_native_writer_signer_configuration_is_explicit_and_complete():
    values = _worker_environment()
    assert load_config(values).native_storage is None
    with pytest.raises(ConfigurationError, match="ASSETS_IMAGES_NATIVE_STORAGE_URL"):
        load_config({**values, "ASSETS_IMAGES_NATIVE_STORAGE_TOKEN": "t" * 40})
    with pytest.raises(ConfigurationError, match="ASSETS_IMAGES_NATIVE_STORAGE_TOKEN"):
        load_config(
            {
                **values,
                "ASSETS_IMAGES_NATIVE_STORAGE_URL": "http://assets-source:8765",
            }
        )
    configured = load_config(
        {
            **values,
            "ASSETS_IMAGES_NATIVE_STORAGE_URL": "http://assets-source:8765",
            "ASSETS_IMAGES_NATIVE_STORAGE_TOKEN": "w" * 40,
        }
    )
    assert configured.native_storage is not None
    assert configured.native_storage.url == "http://assets-source:8765"
    assert "w" * 40 not in repr(configured)


def test_native_reader_signer_configuration_shares_one_explicit_loader():
    assert (
        load_native_storage_config(
            {},
            "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_URL",
            "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_TOKEN",
        )
        is None
    )
    with pytest.raises(ConfigurationError):
        load_native_storage_config(
            {"ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_TOKEN": "r" * 40},
            "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_URL",
            "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_TOKEN",
        )
    reader = load_native_storage_config(
        {
            "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_URL": "https://assets-source",
            "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_TOKEN": "r" * 40,
        },
        "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_URL",
        "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_TOKEN",
    )
    assert reader is not None
    assert reader.url == "https://assets-source"
    assert "r" * 40 not in repr(reader)


def test_native_signer_configuration_treats_all_blank_settings_as_unconfigured():
    url_name = "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_URL"
    token_name = "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_TOKEN"
    blank = {url_name: "", token_name: "", f"{token_name}_FILE": ""}
    assert load_native_storage_config(blank, url_name, token_name) is None
    with pytest.raises(ConfigurationError, match=token_name):
        load_native_storage_config(
            {**blank, url_name: "https://assets-source"}, url_name, token_name
        )
    with pytest.raises(ConfigurationError, match=url_name):
        load_native_storage_config(
            {**blank, token_name: "t" * 40}, url_name, token_name
        )


def test_native_signer_token_rejects_non_printable_and_whitespace_bytes():
    url_name = "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_URL"
    token_name = "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_TOKEN"
    base = {url_name: "https://assets-source"}
    for token in (
        "a" * 39 + "\x00",
        "a" * 39 + "\x7f",
        "a" * 39 + "\t",
        "a" * 39 + "\n",
        "a" * 31 + " ",
        "é" * 32,
    ):
        with pytest.raises(ConfigurationError, match="printable ASCII"):
            load_native_storage_config(
                {**base, token_name: token}, url_name, token_name
            )
    configured = load_native_storage_config(
        {**base, token_name: "a" * 32}, url_name, token_name
    )
    assert configured is not None
    assert configured.token == "a" * 32
    assert "a" * 32 not in repr(configured)
