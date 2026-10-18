from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest

from aap_assets_media.contract import MediaFailure, MediaSource, ObjectIdentity
from aap_assets_media_runtime.storage import S3MediaStorage


class _ClientError(Exception):
    def __init__(self, code: str, status: int):
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class _FakeS3:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
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
        return self._identity(body)

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

    identity = asyncio.run(storage.head_source("legacy/clip.mp4"))
    destination = tmp_path / "source.mp4"
    asyncio.run(
        storage.download_source(
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


def test_conditional_source_read_rejects_a_changed_object(tmp_path: Path):
    client = _FakeS3()
    client.objects[("datasets", "clip.mp4")] = b"current"
    storage, _ = _storage(client)
    stale = ObjectIdentity(etag="stale", byte_size=7, version_id="v0")

    with pytest.raises(MediaFailure, match="source_changed"):
        asyncio.run(
            storage.download_source(_source("clip.mp4", stale, 7), tmp_path / "src")
        )
    assert not (tmp_path / "src").exists()


def test_upload_head_sign_and_delete_stay_in_fixed_cache_bucket(tmp_path: Path):
    client = _FakeS3()
    storage, _ = _storage(client)
    key = f"media-assets/{uuid4()}/g1/a{uuid4()}/poster.webp"
    path = tmp_path / "poster.webp"
    path.write_bytes(b"image")

    artifact = asyncio.run(storage.upload_cache(path, key))
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
