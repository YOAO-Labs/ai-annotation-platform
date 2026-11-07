from __future__ import annotations

import asyncio
import builtins
from io import BytesIO
from hashlib import sha256
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from yoao_assets_media.contract import (
    MediaFailure,
    MediaSource,
    ObjectIdentity,
    OriginalRead,
)
from yoao_assets_media_runtime.additional_original import (
    AdditionalOriginalConfig,
    physical_identity,
)
from yoao_assets_media_runtime.fixture_native_outputs import native_execution, output_scope
from yoao_assets_media_runtime.storage import S3MediaStorage


class _ClientError(Exception):
    def __init__(self, code: str, status: int):
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class _FakeS3:
    def __init__(self):
        self.meta = SimpleNamespace(endpoint_url="http://s3.internal")
        self.objects: dict[tuple[str, str], bytes] = {}
        self.metadata: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.continuations = 0

    @staticmethod
    def _identity(body: bytes):
        import hashlib

        return {
            "ETag": f'"{hashlib.md5(body).hexdigest()}"',
            "ContentLength": len(body),
            "VersionId": "v1",
        }

    def head_object(self, **params):
        self.calls.append(("head_object", params))
        try:
            body = self.objects[(params["Bucket"], params["Key"])]
        except KeyError as exc:
            raise _ClientError("NoSuchKey", 404) from exc
        return {
            **self._identity(body),
            **(
                {"Metadata": self.metadata[(params["Bucket"], params["Key"])]}
                if (params["Bucket"], params["Key"]) in self.metadata
                else {}
            ),
        }

    def get_object(self, **params):
        self.calls.append(("get_object", params))
        try:
            body = self.objects[(params["Bucket"], params["Key"])]
        except KeyError as exc:
            raise _ClientError("NoSuchKey", 404) from exc
        identity = self._identity(body)
        if params.get("IfMatch") != identity["ETag"]:
            raise _ClientError("PreconditionFailed", 412)
        if params.get("VersionId") != identity["VersionId"]:
            raise _ClientError("NoSuchVersion", 404)
        return {**identity, "Body": BytesIO(body)}

    def upload_file(self, filename, bucket, key, ExtraArgs):
        self.calls.append(("upload_file", {"Bucket": bucket, "Key": key, **ExtraArgs}))
        self.objects[(bucket, key)] = Path(filename).read_bytes()
        self.metadata[(bucket, key)] = ExtraArgs.get("Metadata", {})

    def delete_objects(self, **params):
        self.calls.append(("delete_objects", params))
        for item in params["Delete"]["Objects"]:
            self.objects.pop((params["Bucket"], item["Key"]), None)
        return {"Deleted": params["Delete"]["Objects"]}

    def list_objects_v2(self, **params):
        self.calls.append(("list_objects_v2", params))
        keys = sorted(
            key
            for bucket, key in self.objects
            if bucket == params["Bucket"] and key.startswith(params["Prefix"])
        )
        continuation = params.get("ContinuationToken")
        candidates = [key for key in keys if continuation is None or key > continuation]
        page = candidates[: params["MaxKeys"]]
        truncated = len(candidates) > len(page)
        if truncated:
            self.continuations += 1
        return {
            "Contents": [{"Key": key} for key in page],
            "IsTruncated": truncated,
            **({"NextContinuationToken": page[-1]} if truncated else {}),
        }

    def generate_presigned_url(self, method, *, Params, ExpiresIn):
        self.calls.append(
            ("sign", {"Method": method, **Params, "ExpiresIn": ExpiresIn})
        )
        return f"https://signed.test/{Params['Bucket']}/{Params['Key']}?expires={ExpiresIn}"


def _storage(client: _FakeS3 | None = None):
    client = client or _FakeS3()
    return S3MediaStorage(
        endpoint="http://s3.internal",
        public_endpoint="https://s3.public",
        region="us-east-1",
        access_key="worker",
        secret_key="secret",
        datasets_bucket="datasets",
        cache_bucket="media-cache",
        client=client,
        signer=client,
    ), client


async def _ordinary_upload(storage, path, key):
    claim = SimpleNamespace(job_id=uuid4())
    async with native_execution(None, "media", claim.job_id):
        async with output_scope(None, storage, claim, "media", None):
            return await storage.upload_cache(path, key)


def _permit(key: str, body: bytes, *, identity: ObjectIdentity | None = None):
    facts = identity or ObjectIdentity(
        etag=_FakeS3._identity(body)["ETag"].strip('"'),
        byte_size=len(body),
        version_id="v1",
    )
    return OriginalRead(
        ownership="fixture",
        creation_id=uuid4(),
        operation_id=uuid4(),
        location_id=uuid4(),
        endpoint_url="http://s3.internal",
        storage_identity=sha256(
            json.dumps(["http://s3.internal", "datasets"]).encode()
        ).hexdigest(),
        bucket="datasets",
        object_key=key,
        version_id=facts.version_id,
        etag=facts.etag,
        size_bytes=facts.byte_size,
        sha256=sha256(body).hexdigest(),
    )


def _source(key: str, identity: ObjectIdentity, size: int):
    return MediaSource(
        dataset_item_id=uuid4(),
        dataset_id=uuid4(),
        source_generation=1,
        source_snapshot="a" * 64,
        timetable_revision=0,
        materialized_generation=0,
        object_identity=identity,
        source_drifted=False,
        file_name="clip.mp4",
        file_path=key,
        file_type="video",
        file_size=size,
        content_hash=None,
        width=640,
        height=480,
        thumbnail_path=None,
        blurhash=None,
        video_metadata={},
    )


def test_head_and_conditional_download_use_only_datasets_bucket(tmp_path: Path):
    client = _FakeS3()
    client.objects[("datasets", "legacy/clip.mp4")] = b"video bytes"
    storage, _ = _storage(client)

    admitted = storage.bind_original(_permit("legacy/clip.mp4", b"video bytes"))
    identity = asyncio.run(admitted.head_source("legacy/clip.mp4"))
    destination = tmp_path / "source.mp4"
    asyncio.run(
        admitted.download_source(
            _source("legacy/clip.mp4", identity, len(b"video bytes")), destination
        )
    )

    assert identity.version_id == "v1"
    assert destination.read_bytes() == b"video bytes"
    assert {
        call[1]["Bucket"]
        for call in client.calls
        if call[0] in {"head_object", "get_object"}
    } == {"datasets"}
    get = next(call[1] for call in client.calls if call[0] == "get_object")
    assert get["IfMatch"] == f'"{identity.etag}"'
    assert get["VersionId"] == identity.version_id


@pytest.mark.asyncio
async def test_fixture_original_uses_only_explicitly_admitted_ordinary_media_output(
    tmp_path, monkeypatch
):
    from yoao_assets_media_runtime.original_read import admitted_original

    monkeypatch.delenv("AAP_PRIVATE_FIXTURE_MEDIA_CONTROL_DSN_FILE", raising=False)
    client = _FakeS3()
    source_key = "fixture/clip.mp4"
    source_bytes = b"fixture original"
    client.objects[("datasets", source_key)] = source_bytes
    storage, _ = _storage(client)
    permit = _permit(source_key, source_bytes)

    class Reader:
        def __init__(self):
            self.finished = []

        async def call(self, operation, request):
            if operation == "begin_media_original_worker":
                return permit
            assert operation == "finish_media_original_worker"
            self.finished.append((request.creation_id, request.operation_id))

    reader = Reader()
    claim = SimpleNamespace(job_id=uuid4())
    key = f"media-assets/{claim.job_id}/g1/a{uuid4()}/poster.webp"
    artifact = tmp_path / "poster.webp"
    artifact.write_bytes(b"ordinary output")
    async with native_execution(reader, "media", claim.job_id):
        async with admitted_original(
            reader, storage, "begin_media_original_worker", None,
            "finish_media_original_worker",
        ) as original:
            with pytest.raises(RuntimeError, match="output authority"):
                await storage.upload_cache(artifact, key)
            assert ("media-cache", key) not in client.objects
            with pytest.raises(RuntimeError, match="output execution mismatch"):
                async with output_scope(
                    reader, storage, SimpleNamespace(job_id=uuid4()), "media", permit
                ):
                    pass
            async with output_scope(reader, storage, claim, "media", permit):
                identity = await original.head_source(source_key)
                destination = tmp_path / "original.mp4"
                await original.download_source(
                    _source(source_key, identity, len(source_bytes)), destination
                )
                result = await storage.upload_cache(artifact, key)
            assert destination.read_bytes() == source_bytes
            assert result.sha256 == sha256(b"ordinary output").hexdigest()
    assert client.objects[("media-cache", key)] == b"ordinary output"
    assert any(
        call[0] == "get_object" and call[1]["VersionId"] == "v1"
        for call in client.calls
    )
    assert reader.finished == [(permit.creation_id, permit.operation_id)]


def test_ordinary_source_keeps_identity_and_url_while_fixture_url_is_not_signed(
    tmp_path: Path,
):
    client = _FakeS3()
    client.objects[("datasets", "ordinary/clip.mp4")] = b"ordinary bytes"
    storage, _ = _storage(client)
    ordinary = storage.bind_original(
        OriginalRead(ownership="ordinary", object_key="ordinary/clip.mp4")
    )
    identity = asyncio.run(ordinary.head_source("ordinary/clip.mp4"))
    path = tmp_path / "ordinary.mp4"
    asyncio.run(
        ordinary.download_source(
            _source("ordinary/clip.mp4", identity, len(b"ordinary bytes")), path
        )
    )
    assert path.read_bytes() == b"ordinary bytes"
    assert asyncio.run(ordinary.sign_source("ordinary/clip.mp4", 60)) == (
        "https://signed.test/datasets/ordinary/clip.mp4?expires=60"
    )
    fixture = storage.bind_original(_permit("ordinary/clip.mp4", b"ordinary bytes"))
    assert asyncio.run(fixture.sign_source("ordinary/clip.mp4", 60)) is None
    with pytest.raises(MediaFailure, match="source_changed"):
        asyncio.run(ordinary.sign_source("another/clip.mp4", 60))


def test_ordinary_collision_blocks_only_the_owned_physical_store():
    key = "shared/path.mp4"
    client = _FakeS3()
    client.objects[("datasets", key)] = b"ordinary bytes"
    storage, _ = _storage(client)
    other = sha256(
        json.dumps(["http://s3.internal", "other-bucket"]).encode()
    ).hexdigest()
    owned = sha256(json.dumps(["http://s3.internal", "datasets"]).encode()).hexdigest()
    admitted = storage.bind_original(
        OriginalRead(
            ownership="ordinary",
            object_key=key,
            blocked_storage_identities=(other,),
            source_storage_identity=owned,
        )
    )
    assert asyncio.run(admitted.head_source(key)).byte_size == len(b"ordinary bytes")
    assert asyncio.run(admitted.sign_source(key, 60))
    with pytest.raises(MediaFailure, match="source_changed"):
        storage.bind_original(
            OriginalRead(
                ownership="ordinary",
                object_key=key,
                blocked_storage_identities=(owned,),
            )
        )
    with pytest.raises(MediaFailure, match="source_changed"):
        storage.bind_original(
            OriginalRead(
                ownership="ordinary",
                object_key=key,
                source_storage_identity=other,
            )
        )


def test_unversioned_ordinary_source_uses_existing_conditional_identity(tmp_path: Path):
    class Unversioned(_FakeS3):
        @staticmethod
        def _identity(body):
            return {"ETag": f'"{sha256(body).hexdigest()}"', "ContentLength": len(body)}

        def get_object(self, **params):
            self.calls.append(("get_object", params))
            body = self.objects[params["Bucket"], params["Key"]]
            identity = self._identity(body)
            if params.get("IfMatch") != identity["ETag"] or "VersionId" in params:
                raise _ClientError("PreconditionFailed", 412)
            return {**identity, "Body": BytesIO(body)}

    client = Unversioned()
    client.objects["datasets", "legacy/unversioned.mp4"] = b"unchanged"
    storage, _ = _storage(client)
    admitted = storage.bind_original(
        OriginalRead(ownership="ordinary", object_key="legacy/unversioned.mp4")
    )
    identity = asyncio.run(admitted.head_source("legacy/unversioned.mp4"))
    assert identity.version_id is None
    path = tmp_path / "unversioned.mp4"
    asyncio.run(
        admitted.download_source(
            _source("legacy/unversioned.mp4", identity, len(b"unchanged")), path
        )
    )
    assert path.read_bytes() == b"unchanged"


def test_conditional_source_read_rejects_a_changed_object(tmp_path: Path):
    client = _FakeS3()
    client.objects[("datasets", "clip.mp4")] = b"current"
    storage, _ = _storage(client)
    stale = ObjectIdentity(etag="stale", byte_size=7, version_id="v0")
    admitted = storage.bind_original(_permit("clip.mp4", b"current", identity=stale))

    with pytest.raises(MediaFailure, match="source_changed"):
        asyncio.run(
            admitted.download_source(_source("clip.mp4", stale, 7), tmp_path / "src")
        )
    assert not (tmp_path / "src").exists()


def test_source_read_preserves_first_failure_when_stream_close_is_unknown(
    tmp_path: Path,
):
    class BrokenBody:
        def read(self, _size):
            raise ValueError("source read failed")

        def close(self):
            raise OSError("source close failed")

    class BrokenStream(_FakeS3):
        def get_object(self, **params):
            result = super().get_object(**params)
            result["Body"].close()
            result["Body"] = BrokenBody()
            return result

    client = BrokenStream()
    client.objects[("datasets", "clip.mp4")] = b"source"
    storage, _ = _storage(client)
    admitted = storage.bind_original(_permit("clip.mp4", b"source"))
    identity = asyncio.run(admitted.head_source("clip.mp4"))
    with pytest.raises(builtins.ExceptionGroup) as caught:
        asyncio.run(
            admitted.download_source(_source("clip.mp4", identity, 6), tmp_path / "src")
        )
    assert [type(error) for error in caught.value.exceptions] == [ValueError, OSError]
    assert getattr(caught.value, "storage_operation_unsettled", False)
    assert not (tmp_path / "src").exists()


def test_upload_head_sign_and_delete_stay_in_fixed_cache_bucket(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AAP_PRIVATE_FIXTURE_MEDIA_CONTROL_DSN_FILE", raising=False)
    client = _FakeS3()
    storage, _ = _storage(client)
    key = f"media-assets/{uuid4()}/g1/a{uuid4()}/poster.webp"
    path = tmp_path / "poster.webp"
    path.write_bytes(b"image")

    artifact = asyncio.run(_ordinary_upload(storage, path, key))
    identity = asyncio.run(storage.head_cache(key))
    url = asyncio.run(storage.sign_cache(key, 60))
    deleted = asyncio.run(storage.delete_cache_keys([key]))

    assert artifact.key == key and artifact.byte_size == 5
    assert identity is not None and identity.byte_size == 5
    assert "/media-cache/" in url
    assert deleted == 1
    assert all(
        call[1].get("Bucket") == "media-cache"
        for call in client.calls
        if call[0] in {"upload_file", "head_object", "delete_objects"}
        and call[1].get("Key", key) == key
    )
    with pytest.raises(MediaFailure, match="invalid_request"):
        asyncio.run(storage.upload_cache(path, "videos/legacy.mp4"))


def test_jpeg_output_upload_uses_jpeg_media_type(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AAP_PRIVATE_FIXTURE_MEDIA_CONTROL_DSN_FILE", raising=False)
    client = _FakeS3()
    storage, _ = _storage(client)
    key = f"media-assets/{uuid4()}/g1/a{uuid4()}/frame.jpeg"
    path = tmp_path / "frame.jpeg"
    path.write_bytes(b"jpeg")

    asyncio.run(_ordinary_upload(storage, path, key))

    call = next(call for call in client.calls if call[0] == "upload_file")
    assert call[1]["ContentType"] == "image/jpeg"


@pytest.mark.asyncio
async def test_cancelled_upload_waits_for_boto_thread_before_returning(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AAP_PRIVATE_FIXTURE_MEDIA_CONTROL_DSN_FILE", raising=False)
    class _SlowUpload(_FakeS3):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def upload_file(self, filename, bucket, key, ExtraArgs):
            self.started.set()
            if not self.release.wait(timeout=2):
                raise TimeoutError("test upload was not released")
            super().upload_file(filename, bucket, key, ExtraArgs)

    client = _SlowUpload()
    storage, _ = _storage(client)
    key = f"media-assets/{uuid4()}/g1/a{uuid4()}/poster.webp"
    path = tmp_path / "poster.webp"
    path.write_bytes(b"image")

    task = asyncio.create_task(_ordinary_upload(storage, path, key))
    assert await asyncio.to_thread(client.started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    client.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ("media-cache", key) in client.objects


@pytest.mark.asyncio
async def test_cancelled_source_head_preserves_late_physical_failure():
    class _FailingHead(_FakeS3):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def head_object(self, **params):
            self.started.set()
            if not self.release.wait(timeout=2):
                raise TimeoutError("test head was not released")
            raise RuntimeError("physical head failed")

    client = _FailingHead()
    storage, _ = _storage(client)
    admitted = storage.bind_original(_permit("fixture/source.mp4", b""))
    task = asyncio.create_task(admitted.head_source("fixture/source.mp4"))
    assert await asyncio.to_thread(client.started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    client.release.set()
    with pytest.raises(builtins.BaseExceptionGroup) as caught:
        await task
    assert isinstance(caught.value.exceptions[0], asyncio.CancelledError)
    assert str(caught.value.exceptions[1]) == "physical head failed"


def test_missing_bucket_is_not_misreported_as_a_missing_cache_object():
    client = _FakeS3()
    storage, _ = _storage(client)

    def missing_bucket(**params):
        raise _ClientError("NoSuchBucket", 404)

    client.head_object = missing_bucket
    key = f"media-assets/{uuid4()}/g1/a{uuid4()}/poster.webp"
    with pytest.raises(MediaFailure, match="storage_unavailable"):
        asyncio.run(storage.head_cache(key))


def test_cache_signing_retains_its_own_limit():
    storage, _ = _storage()
    cache_key = f"media-assets/{uuid4()}/g1/a{uuid4()}/poster.webp"
    with pytest.raises(MediaFailure, match="invalid_request"):
        asyncio.run(storage.sign_cache(cache_key, 3601))


def test_legacy_frame_chunk_reads_and_keyset_deletion_are_exact():
    client = _FakeS3()
    storage, _ = _storage(client)
    dataset_item_id = uuid4()
    frame_key = f"videos/{dataset_item_id}/frames/12_512.webp"
    chunk_key = f"videos/{dataset_item_id}/chunks/3.mp4"
    playback_key = f"playback/{dataset_item_id}.mp4"
    client.objects[("media-cache", frame_key)] = b"frame"
    client.objects[("media-cache", chunk_key)] = b"chunk"
    client.objects[("media-cache", playback_key)] = b"playback"

    assert asyncio.run(storage.head_cache(frame_key)).byte_size == 5
    assert "/media-cache/" in asyncio.run(storage.sign_cache(playback_key))
    assert asyncio.run(storage.delete_cache_keys([frame_key, chunk_key])) == 2
    with pytest.raises(MediaFailure, match="invalid_request"):
        asyncio.run(storage.delete_cache_keys([playback_key]))
    with pytest.raises(MediaFailure, match="invalid_request"):
        asyncio.run(storage.delete_cache_prefix(f"videos/{dataset_item_id}/"))
    assert ("media-cache", playback_key) in client.objects


def test_missing_cache_object_is_none_and_prefix_delete_pages(tmp_path: Path):
    client = _FakeS3()
    storage, _ = _storage(client)
    asset_id, token = uuid4(), uuid4()
    prefix = f"media-assets/{asset_id}/g1/a{token}/"
    assert asyncio.run(storage.head_cache(prefix + "poster.webp")) is None
    for index in range(1001):
        client.objects[("media-cache", prefix + f"frame-{index}.webp")] = b"x"
    deleted = asyncio.run(storage.delete_cache_prefix(prefix))
    assert deleted == 1001
    assert client.continuations == 1
    assert not any(key.startswith(prefix) for bucket, key in client.objects)


def test_prefix_delete_refuses_non_attempt_scope():
    storage, client = _storage()
    with pytest.raises(MediaFailure, match="invalid_request"):
        asyncio.run(storage.delete_cache_prefix("media-assets/"))
    assert not client.calls


@pytest.mark.asyncio
async def test_media_reads_only_owner_selected_exact_additional_version(tmp_path):
    primary, extra = _FakeS3(), _FakeS3()
    extra.meta = SimpleNamespace(endpoint_url="http://fixture.internal")
    config = AdditionalOriginalConfig(
        "http://fixture.internal", "fixture-originals", "reader", "secret"
    )
    storage = S3MediaStorage(
        endpoint="http://s3.internal",
        public_endpoint="http://s3.public",
        region="us-east-1",
        access_key="platform",
        secret_key="platform-secret",
        datasets_bucket="datasets",
        cache_bucket="media-cache",
        client=primary,
        signer=primary,
        additional_original=config,
        additional_original_client=extra,
    )
    body, key = b"versioned video source", "originals/video.mp4"
    extra.objects[(config.bucket, key)] = body
    identity = _FakeS3._identity(body)
    permit = OriginalRead(
        ownership="fixture",
        creation_id=uuid4(),
        operation_id=uuid4(),
        location_id=uuid4(),
        endpoint_url=config.endpoint,
        storage_identity=physical_identity(config.endpoint, config.bucket),
        bucket=config.bucket,
        object_key=key,
        version_id="v1",
        etag=identity["ETag"].strip('"'),
        size_bytes=len(body),
        sha256=sha256(body).hexdigest(),
    )
    bound = storage.bind_original(permit)
    observed = await bound.head_source(key)
    target = tmp_path / "source.mp4"
    await bound.download_source(_source(key, observed, len(body)), target)
    assert target.read_bytes() == body
    assert await bound.sign_source(key, 60) is None
    assert primary.calls == []
    assert all(call[1].get("VersionId") == "v1" for call in extra.calls)
    with pytest.raises(MediaFailure, match="source_changed"):
        storage.bind_original(permit.model_copy(update={"storage_identity": "f" * 64}))
    with pytest.raises(MediaFailure, match="source_changed"):
        storage.bind_original(
            permit.model_copy(update={"endpoint_url": "http://other.internal"})
        )
    with pytest.raises(MediaFailure, match="source_changed"):
        await storage.bind_original(
            permit.model_copy(update={"version_id": "v2"})
        ).head_source(key)
