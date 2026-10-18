from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import gzip
from hashlib import sha256
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from aap_assets_images.contract import ImageFailure, ImageOwner, ImageSource
from aap_assets_images_runtime.config import ImageStorageConfig
from aap_assets_images_runtime.storage import S3ImageStorage


class _Body(BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.was_closed = False

    def close(self):
        self.was_closed = True
        super().close()


class _S3Error(Exception):
    def __init__(self, code, status):
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }
        super().__init__(code)


class _Signer:
    def __init__(self):
        self.calls = []

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.calls.append((operation, Params, ExpiresIn))
        return f"https://objects.invalid/{Params['Bucket']}/{Params['Key']}"


class _AsyncBody(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        while chunk := self.body.read(64 * 1024):
            yield chunk

    async def aclose(self):
        self.body.close()


class _S3:
    def __init__(self):
        self.objects = {}
        self.metadata = {}
        self.calls = []
        self.get_bodies = []
        self.pages = []
        self.mutate_after_head = None
        self.presigned_urls = {}
        self.next_presigned_id = 0

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        token = str(self.next_presigned_id)
        self.next_presigned_id += 1
        self.presigned_urls[token] = (operation, dict(Params))
        self.calls.append(("presign", operation, dict(Params), ExpiresIn))
        return f"http://s3.internal/signed/{token}"

    def set_object(self, bucket, key, data, *, etag="source-etag", version_id="v1"):
        self.objects[(bucket, key)] = (data, etag, version_id)

    def head_object(self, *, Bucket, Key):
        self.calls.append(("head", Bucket, Key))
        value = self.objects.get((Bucket, Key))
        if value is None:
            raise _S3Error("NoSuchKey", 404)
        data, etag, version_id = value
        if self.mutate_after_head == (
            Bucket,
            Key,
            len([c for c in self.calls if c[0] == "head"]),
        ):
            self.set_object(Bucket, Key, data, etag="replacement", version_id="v2")
        return {
            "ETag": f'"{etag}"',
            "ContentLength": len(data),
            "VersionId": version_id,
            "Metadata": self.metadata.get((Bucket, Key), {}),
        }

    def get_object(self, *, Bucket, Key, IfMatch=None, VersionId=None, Range=None):
        self.calls.append(("get", Bucket, Key, IfMatch, VersionId, Range))
        value = self.objects.get((Bucket, Key))
        if value is None:
            raise _S3Error("NoSuchKey", 404)
        data, etag, version_id = value
        if IfMatch is not None and IfMatch != f'"{etag}"':
            raise _S3Error("PreconditionFailed", 412)
        if VersionId is not None and VersionId != version_id:
            raise _S3Error("NoSuchVersion", 404)
        if Range is None:
            body = _Body(data)
            self.get_bodies.append(body)
            return {
                "ETag": f'"{etag}"',
                "ContentLength": len(data),
                "VersionId": version_id,
                "Body": body,
                "ResponseMetadata": {"HTTPStatusCode": 200},
            }
        start, end = map(int, Range.removeprefix("bytes=").split("-"))
        selected = data[start : end + 1]
        body = _Body(selected)
        self.get_bodies.append(body)
        return {
            "ETag": f'"{etag}"',
            "ContentLength": len(selected),
            "ContentRange": f"bytes {start}-{start + len(selected) - 1}/{len(data)}",
            "VersionId": version_id,
            "Body": body,
            "ResponseMetadata": {"HTTPStatusCode": 206},
        }

    def put_object_from_http(self, bucket, key, data, *, content_type, metadata):
        self.set_object(bucket, key, data, etag="uploaded-etag", version_id=None)
        self.metadata[(bucket, key)] = metadata
        self.calls.append(
            (
                "upload",
                bucket,
                key,
                {"ContentType": content_type, "Metadata": metadata},
            )
        )

    def list_objects_v2(self, **request):
        self.calls.append(("list", request["Bucket"], request.get("ContinuationToken")))
        if not self.pages:
            return {"IsTruncated": False, "Contents": []}
        return self.pages.pop(0)


class _S3Transport(httpx.AsyncBaseTransport):
    def __init__(self, s3):
        self.s3 = s3

    async def handle_async_request(self, request):
        token = request.url.path.rsplit("/", 1)[-1]
        operation, params = self.s3.presigned_urls[token]
        bucket, key = params["Bucket"], params["Key"]
        if operation == "head_object":
            try:
                response = self.s3.head_object(Bucket=bucket, Key=key)
            except _S3Error as exc:
                return self._error(exc)
            return httpx.Response(200, headers=self._headers(response))
        if operation == "get_object":
            if params.get("IfMatch") != request.headers.get("if-match"):
                return httpx.Response(403)
            if params.get("Range") != request.headers.get("range"):
                return httpx.Response(403)
            try:
                response = self.s3.get_object(
                    Bucket=bucket,
                    Key=key,
                    IfMatch=request.headers.get("if-match"),
                    VersionId=params.get("VersionId"),
                    Range=request.headers.get("range"),
                )
            except _S3Error as exc:
                return self._error(exc)
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode", 200)
            return httpx.Response(
                status,
                headers=self._headers(response),
                stream=_AsyncBody(response["Body"]),
            )
        if operation == "put_object":
            data = bytearray()
            async for chunk in request.stream:
                data.extend(chunk)
            try:
                expected = int(request.headers["content-length"])
            except (KeyError, ValueError):
                return httpx.Response(400)
            if len(data) != expected or expected != params["ContentLength"]:
                return httpx.Response(400)
            if request.headers.get("content-type") != params["ContentType"]:
                return httpx.Response(403)
            metadata = {"sha256": request.headers.get("x-amz-meta-sha256")}
            if metadata != params["Metadata"]:
                return httpx.Response(403)
            self.s3.put_object_from_http(
                bucket,
                key,
                bytes(data),
                content_type=params["ContentType"],
                metadata=metadata,
            )
            return httpx.Response(200)
        return httpx.Response(400)

    @staticmethod
    def _headers(response):
        headers = {
            "ETag": response["ETag"],
            "Content-Length": str(response["ContentLength"]),
        }
        if response.get("VersionId") is not None:
            headers["x-amz-version-id"] = response["VersionId"]
        if isinstance(response.get("Metadata"), dict):
            headers.update(
                {
                    f"x-amz-meta-{name}": value
                    for name, value in response["Metadata"].items()
                }
            )
        if response.get("ContentRange") is not None:
            headers["Content-Range"] = response["ContentRange"]
        return headers

    @staticmethod
    def _error(error):
        response = error.response
        status = response["ResponseMetadata"]["HTTPStatusCode"]
        code = response["Error"]["Code"]
        return httpx.Response(status, headers={"x-amz-error-code": code})


class _BlockingBody(httpx.AsyncByteStream):
    def __init__(self):
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield b"partial"
        self.started.set()
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class _BlockingGetTransport(_S3Transport):
    def __init__(self, s3, body):
        super().__init__(s3)
        self.body = body

    async def handle_async_request(self, request):
        token = request.url.path.rsplit("/", 1)[-1]
        operation, params = self.s3.presigned_urls[token]
        if operation == "get_object" and params.get("Range") is None:
            value = self.s3.objects[(params["Bucket"], params["Key"])]
            data, etag, version_id = value
            return httpx.Response(
                200,
                headers={
                    "ETag": f'"{etag}"',
                    "Content-Length": str(len(data)),
                    "x-amz-version-id": version_id,
                },
                stream=self.body,
            )
        return await super().handle_async_request(request)


class _BlockingPutTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.started = asyncio.Event()

    async def handle_async_request(self, request):
        async for _chunk in request.stream:
            self.started.set()
            await asyncio.Event().wait()
        return httpx.Response(200)


class _TransientGetTransport(_S3Transport):
    def __init__(self, s3):
        super().__init__(s3)
        self.get_requests = 0

    async def handle_async_request(self, request):
        token = request.url.path.rsplit("/", 1)[-1]
        operation, _params = self.s3.presigned_urls[token]
        if operation == "get_object":
            self.get_requests += 1
            return httpx.Response(503, headers={"x-amz-error-code": "SlowDown"})
        return await super().handle_async_request(request)


class _GzipContentEncodingTransport(_S3Transport):
    async def handle_async_request(self, request):
        response = await super().handle_async_request(request)
        if request.method == "GET" and response.status_code == 200:
            response.headers["content-encoding"] = "gzip"
        return response


_HTTP_CLIENTS = []


@pytest_asyncio.fixture(autouse=True)
async def _close_test_http_clients():
    start = len(_HTTP_CLIENTS)
    yield
    for client in _HTTP_CLIENTS[start:]:
        await client.aclose()
    del _HTTP_CLIENTS[start:]


def _storage(client=None, signer=None, http_client=None):
    config = ImageStorageConfig(
        endpoint="http://s3.internal",
        public_endpoint="http://s3.public",
        region="us-east-1",
        datasets_bucket="datasets",
        legacy_bucket="annotations",
        cache_bucket="derived-cache",
        access_key="access",
        secret_key="secret",
    )
    client = client or _S3()
    signer = signer or _Signer()
    if http_client is None:
        http_client = httpx.AsyncClient(transport=_S3Transport(client))
        _HTTP_CLIENTS.append(http_client)
    return (
        S3ImageStorage(
            config,
            client=client,
            signer=signer,
            http_client=http_client,
        ),
        config,
    )


def _source(data, *, legacy=False, content_hash=None, etag="source-etag"):
    owner_id = uuid4()
    owner = (
        ImageOwner(kind="task", id=owner_id)
        if legacy
        else ImageOwner(kind="dataset_item", id=owner_id)
    )
    return ImageSource(
        owner=owner,
        task_id=owner_id if legacy else None,
        project_id=None,
        dataset_id=None if legacy else uuid4(),
        item_id=None if legacy else owner_id,
        source_generation=1,
        source_snapshot="a" * 64,
        object_identity={"etag": etag, "byte_size": len(data), "version_id": "v1"},
        source_drifted=False,
        upload_ready=True,
        file_path="tasks/original.png" if legacy else "dataset/original.png",
        file_name="original.png",
        file_type="image",
        bucket_kind="legacy" if legacy else "datasets",
        file_size=len(data),
        content_hash=content_hash,
        width=None,
        height=None,
        thumbnail_path=None,
        blurhash=None,
        thumbnail_source_generation=None,
        thumbnail_error=None,
    )


@pytest.mark.asyncio
async def test_header_read_is_conditional_bounded_and_rechecks_full_identity():
    source_bytes = b"0123456789"
    source = _source(source_bytes)
    client = _S3()
    client.set_object("datasets", source.file_path, source_bytes)
    storage, _config = _storage(client, _Signer())

    header = await storage.read_source_header(source)

    assert header.data == source_bytes
    assert header.identity == source.object_identity
    range_call = next(call for call in client.calls if call[0] == "get")
    assert range_call[1:5] == (
        "datasets",
        source.file_path,
        '"source-etag"',
        "v1",
    )
    assert range_call[5] == "bytes=0-262143"
    assert [call[0] for call in client.calls].count("head") == 2
    assert client.get_bodies[-1].was_closed


@pytest.mark.asyncio
async def test_header_reader_never_materializes_more_than_256_kibibytes():
    source_bytes = b"x" * (400 * 1024)
    source = _source(source_bytes)
    client = _S3()
    client.set_object("datasets", source.file_path, source_bytes)
    storage, _config = _storage(client, _Signer())

    header = await storage.read_source_header(source)

    assert len(header.data) == 256 * 1024
    assert client.get_bodies[-1].was_closed


@pytest.mark.asyncio
async def test_signed_source_url_pins_captured_version():
    source = _source(b"versioned source")
    signer = _Signer()
    storage, _config = _storage(_S3(), signer)

    await storage.sign_source(source, expires_in=90)

    assert signer.calls[-1][0] == "get_object"
    assert signer.calls[-1][1] == {
        "Bucket": "datasets",
        "Key": source.file_path,
        "VersionId": "v1",
    }
    unversioned = source.model_copy(
        update={
            "object_identity": source.object_identity.model_copy(
                update={"version_id": None}
            )
        }
    )
    await storage.sign_source(unversioned)
    assert "VersionId" not in signer.calls[-1][1]


@pytest.mark.asyncio
async def test_storage_close_releases_its_reusable_async_client():
    config = ImageStorageConfig(
        endpoint="http://s3.internal",
        public_endpoint="http://s3.public",
        region="us-east-1",
        datasets_bucket="datasets",
        legacy_bucket="annotations",
        cache_bucket="derived-cache",
        access_key="access",
        secret_key="secret",
    )
    storage = S3ImageStorage(config, client=_S3(), signer=_Signer())
    client = storage._get_http_client()

    await storage.close()
    await storage.close()

    assert client.is_closed


@pytest.mark.asyncio
async def test_legacy_task_source_uses_only_the_configured_annotations_bucket():
    source_bytes = b"task source"
    source = _source(source_bytes, legacy=True)
    client = _S3()
    client.set_object("annotations", source.file_path, source_bytes)
    storage, _config = _storage(client, _Signer())

    identity = await storage.head_source(source)

    assert identity == source.object_identity
    assert next(call for call in client.calls if call[0] == "head") == (
        "head",
        "annotations",
        source.file_path,
    )


@pytest.mark.asyncio
async def test_if_match_rejects_replacement_between_head_and_range_get():
    data = b"before replacement"
    source = _source(data)
    client = _S3()
    client.set_object("datasets", source.file_path, data)
    client.mutate_after_head = ("datasets", source.file_path, 1)
    storage, _config = _storage(client, _Signer())

    with pytest.raises(ImageFailure, match="source_changed"):
        await storage.read_source_header(source)

    get = next(call for call in client.calls if call[0] == "get")
    assert get[3] == '"source-etag"'
    assert get[4] == "v1"
    assert not client.get_bodies


@pytest.mark.asyncio
async def test_download_is_if_match_bound_hash_checked_and_deleted_on_hash_mismatch(
    tmp_path,
):
    source_bytes = b"verified image source"
    expected_hash = sha256(source_bytes).hexdigest()
    source = _source(source_bytes, content_hash=f"sha256:{expected_hash}")
    client = _S3()
    client.set_object("datasets", source.file_path, source_bytes)
    storage, _config = _storage(client, _Signer())
    destination = tmp_path / "source.png"

    downloaded = await storage.download_source(source, destination)

    assert downloaded.identity == source.object_identity
    assert downloaded.byte_size == len(source_bytes)
    assert downloaded.sha256 == expected_hash
    assert destination.read_bytes() == source_bytes
    assert destination.stat().st_mode & 0o777 == 0o400
    assert client.get_bodies[-1].was_closed
    assert [call[0] for call in client.calls].count("head") == 2

    bad_source = source.model_copy(update={"content_hash": "sha256:" + "0" * 64})
    failed_path = tmp_path / "bad.png"
    with pytest.raises(ImageFailure, match="source_changed"):
        await storage.download_source(bad_source, failed_path)
    assert not failed_path.exists()


@pytest.mark.asyncio
async def test_download_cancellation_closes_active_body_and_removes_partial_file(
    tmp_path,
):
    source_bytes = b"source bytes"
    source = _source(source_bytes)
    client = _S3()
    client.set_object("datasets", source.file_path, source_bytes)
    body = _BlockingBody()
    http_client = httpx.AsyncClient(transport=_BlockingGetTransport(client, body))
    _HTTP_CLIENTS.append(http_client)
    storage, _config = _storage(client, _Signer(), http_client)
    destination = tmp_path / "cancelled.png"
    transfer = asyncio.create_task(storage.download_source(source, destination))

    await asyncio.wait_for(body.started.wait(), timeout=1)
    assert destination.exists()
    transfer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(transfer, timeout=1)

    assert body.closed
    assert not destination.exists()


@pytest.mark.asyncio
async def test_download_rejects_changed_response_identity_and_removes_partial_file(
    tmp_path,
):
    source_bytes = b"source bytes"
    source = _source(source_bytes)
    client = _S3()
    client.set_object("datasets", source.file_path, source_bytes)
    original_get = client.get_object

    def changed_get(**request):
        response = original_get(**request)
        response["ETag"] = '"replacement-etag"'
        return response

    client.get_object = changed_get
    storage, _config = _storage(client, _Signer())
    destination = tmp_path / "changed.png"

    with pytest.raises(ImageFailure, match="source_changed"):
        await storage.download_source(source, destination)

    assert not destination.exists()
    assert client.get_bodies[-1].was_closed


@pytest.mark.asyncio
async def test_download_rejects_streamed_bytes_over_captured_size(tmp_path):
    source_bytes = b"source bytes"
    source = _source(source_bytes)
    client = _S3()
    client.set_object("datasets", source.file_path, source_bytes)
    original_get = client.get_object

    def oversized_get(**request):
        response = original_get(**request)
        body = _Body(source_bytes + b"extra")
        client.get_bodies[-1] = body
        response["Body"] = body
        return response

    client.get_object = oversized_get
    storage, _config = _storage(client, _Signer())
    destination = tmp_path / "oversized.png"

    with pytest.raises(ImageFailure, match="source_changed"):
        await storage.download_source(source, destination)

    assert not destination.exists()
    assert client.get_bodies[-1].was_closed


@pytest.mark.asyncio
async def test_download_hashes_stored_bytes_without_http_content_decoding(tmp_path):
    source_bytes = gzip.compress(b"source bytes before HTTP content decoding")
    source_digest = sha256(source_bytes).hexdigest()
    source = _source(source_bytes, content_hash=f"sha256:{source_digest}")
    client = _S3()
    client.set_object("datasets", source.file_path, source_bytes)
    http_client = httpx.AsyncClient(transport=_GzipContentEncodingTransport(client))
    _HTTP_CLIENTS.append(http_client)
    storage, _config = _storage(client, _Signer(), http_client)
    destination = tmp_path / "encoded-source.bin"

    downloaded = await storage.download_source(source, destination)

    assert downloaded.byte_size == len(source_bytes)
    assert downloaded.sha256 == source_digest
    assert destination.read_bytes() == source_bytes


@pytest.mark.asyncio
async def test_download_failure_preserves_a_destination_it_did_not_create(tmp_path):
    source_bytes = b"registered source"
    source = _source(source_bytes)
    client = _S3()
    client.set_object("datasets", source.file_path, source_bytes)
    storage, _config = _storage(client, _Signer())
    destination = tmp_path / "caller-owned.png"
    destination.write_bytes(b"leave this file alone")

    with pytest.raises(ImageFailure, match="storage_unavailable"):
        await storage.download_source(source, destination)

    assert destination.read_bytes() == b"leave this file alone"
    assert client.get_bodies[-1].was_closed


@pytest.mark.asyncio
async def test_transient_s3_failure_is_retryable_and_not_retried_inside_storage(
    tmp_path,
):
    source_bytes = b"source bytes"
    source = _source(source_bytes)
    client = _S3()
    client.set_object("datasets", source.file_path, source_bytes)
    transport = _TransientGetTransport(client)
    http_client = httpx.AsyncClient(transport=transport)
    _HTTP_CLIENTS.append(http_client)
    storage, _config = _storage(client, _Signer(), http_client)
    destination = tmp_path / "retry-later.png"

    with pytest.raises(ImageFailure, match="storage_unavailable"):
        await storage.download_source(source, destination)

    assert transport.get_requests == 1
    assert not destination.exists()


@pytest.mark.asyncio
async def test_cache_put_cancellation_closes_local_stream_and_returns_promptly(
    monkeypatch, tmp_path
):
    import aap_assets_images_runtime.storage as storage_module

    client = _S3()
    transport = _BlockingPutTransport()
    http_client = httpx.AsyncClient(transport=transport)
    _HTTP_CLIENTS.append(http_client)
    storage, _config = _storage(client, _Signer(), http_client)
    path = tmp_path / "thumbnail.webp"
    path.write_bytes(b"artifact bytes")
    job, attempt = uuid4(), uuid4()
    key = f"image-assets/{job}/g1/a{attempt}/thumbnail.webp"
    body_closed = asyncio.Event()
    original_close = storage_module._FileBody.aclose

    async def watched_close(body):
        await original_close(body)
        body_closed.set()

    monkeypatch.setattr(storage_module._FileBody, "aclose", watched_close)
    upload = asyncio.create_task(storage.upload_cache(path, key))

    await asyncio.wait_for(transport.started.wait(), timeout=1)
    upload.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(upload, timeout=1)

    assert body_closed.is_set()
    assert path.read_bytes() == b"artifact bytes"


@pytest.mark.asyncio
async def test_range_identity_mismatch_closes_body_and_rejects_stale_bytes():
    source_bytes = b"range bytes"
    source = _source(source_bytes)
    client = _S3()
    client.set_object("datasets", source.file_path, source_bytes)
    original_get = client.get_object

    def changed_get(**request):
        response = original_get(**request)
        response["ETag"] = '"wrong-etag"'
        return response

    client.get_object = changed_get
    storage, _config = _storage(client, _Signer())

    with pytest.raises(ImageFailure, match="source_changed"):
        await storage.read_source_header(source)

    assert client.get_bodies[-1].was_closed


@pytest.mark.asyncio
async def test_upload_and_sign_reject_keys_outside_image_attempt_scope(tmp_path):
    client = _S3()
    signer = _Signer()
    storage, config = _storage(client, signer)
    path = tmp_path / "thumbnail.webp"
    path.write_bytes(b"webp artifact")
    job, attempt = uuid4(), uuid4()
    key = f"image-assets/{job}/g1/a{attempt}/thumbnail.webp"

    artifact = await storage.upload_cache(path, key)
    url = await storage.sign_cache(key, expires_in=90)

    assert artifact.key == key
    assert artifact.byte_size == len(b"webp artifact")
    assert artifact.sha256 == sha256(b"webp artifact").hexdigest()
    assert url.endswith("/" + key)
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"schema":"aap-image-pyramid/v1"}', encoding="utf-8")
    manifest_key = f"image-pyramids/{uuid4()}/g1/a{attempt}/manifest.json"
    manifest_artifact = await storage.upload_cache(manifest, manifest_key)
    assert manifest_artifact.key == manifest_key
    uploads = [call for call in client.calls if call[0] == "upload"]
    assert uploads[0][1:3] == (config.cache_bucket, key)
    assert uploads[0][3]["ContentType"] == "image/webp"
    assert uploads[1][1:3] == (config.cache_bucket, manifest_key)
    assert uploads[1][3]["ContentType"] == "application/json"
    assert signer.calls[-1][1] == {"Bucket": config.cache_bucket, "Key": key}
    with pytest.raises(ImageFailure, match="invalid_request"):
        await storage.sign_cache("../foreign.webp")
    with pytest.raises(ImageFailure, match="invalid_request"):
        await storage.upload_cache(path, "image-assets/foreign/thumbnail.webp")
    legacy_generation_key = f"image-pyramids/{uuid4()}/g1/overview.webp"
    await storage.sign_cache(legacy_generation_key)
    with pytest.raises(ImageFailure, match="invalid_request"):
        await storage.upload_cache(path, legacy_generation_key)


@pytest.mark.asyncio
async def test_cache_reads_have_an_explicit_byte_limit_and_close_the_body():
    asset, attempt = uuid4(), uuid4()
    key = f"image-pyramids/{asset}/g1/a{attempt}/overview.webp"
    client = _S3()
    client.set_object("derived-cache", key, b"overview")
    storage, _config = _storage(client, _Signer())

    assert await storage.read_cache(key, 100) == b"overview"
    assert client.get_bodies[-1].was_closed
    with pytest.raises(ImageFailure, match="resource_budget_exceeded"):
        await storage.read_cache(key, 2)
    assert client.get_bodies[-1].was_closed


@pytest.mark.asyncio
async def test_orphan_listing_aggregates_last_modified_across_pages_and_checks_order():
    client = _S3()
    storage, _config = _storage(client, _Signer())
    asset_id = uuid4()
    asset = str(asset_id)
    attempt = str(uuid4())
    earlier = datetime(2026, 10, 18, tzinfo=timezone.utc)
    later = datetime(2026, 10, 19, tzinfo=timezone.utc)
    first_key = f"image-pyramids/{asset}/g1/a{attempt}/manifest.json"
    second_key = f"image-pyramids/{asset}/g1/a{attempt}/tiles/0/0/0.webp"
    third_key = f"image-pyramids/{asset}/g2/overview.webp"
    client.pages = [
        {
            "Contents": [{"Key": first_key, "LastModified": earlier}],
            "IsTruncated": True,
            "NextContinuationToken": "page-2",
        },
        {
            "Contents": [
                {"Key": second_key, "LastModified": later},
                {"Key": third_key, "LastModified": earlier},
            ],
            "IsTruncated": False,
        },
    ]

    rows = [row async for row in storage.iter_orphan_generations()]

    assert [(row.asset_id, row.generation, row.last_modified) for row in rows] == [
        (asset_id, 1, later),
        (asset_id, 2, earlier),
    ]
    assert [call[0] for call in client.calls] == ["list", "list"]
    assert client.calls[1][2] == "page-2"


@pytest.mark.asyncio
async def test_orphan_listing_rejects_repeated_continuation_tokens():
    client = _S3()
    storage, _config = _storage(client, _Signer())
    client.pages = [
        {"Contents": [], "IsTruncated": True, "NextContinuationToken": "same"},
        {"Contents": [], "IsTruncated": True, "NextContinuationToken": "same"},
    ]

    with pytest.raises(ImageFailure, match="invalid_response"):
        _ = [row async for row in storage.iter_orphan_generations()]
