import hashlib
from uuid import uuid4

import pytest

from aap_assets_inputs.contract import InputFailure
from aap_assets_video_inputs import values as c
from aap_assets_video_inputs_runtime.storage import S3VideoInputStorage
from test_input_storage import VersionedStore, make_storage


def storage(store):
    config = make_storage(store).config
    return S3VideoInputStorage(
        config, source_storage=object(), client=store, signer=store
    )


def prefix():
    return f"accepted-video-inputs/{uuid4()}/g1/a{uuid4()}/"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix", ["source", "frames/0/w1280-jpeg", "frames/5/w512-webp"]
)
@pytest.mark.parametrize("multipart", [False, True])
async def test_video_and_frames_keep_original_versions_after_latest_key_replacement(
    tmp_path, monkeypatch, suffix, multipart
):
    from aap_assets_inputs_runtime import storage as shared

    if multipart:
        monkeypatch.setattr(shared, "_PART", 4)
    store = VersionedStore()
    adapter = storage(store)
    body = b"retained video or frame bytes"
    source = tmp_path / "source"
    source.write_bytes(body)
    key = prefix() + suffix
    blob = await adapter.upload_blob(
        source, key, sha256=hashlib.sha256(body).hexdigest(), byte_size=len(body)
    )
    assert isinstance(blob, c.VideoBlob if suffix == "source" else c.FrameBlob)
    store.put_object(
        Bucket=adapter.config.inputs_bucket, Key=key, Body=b"replacement", Metadata={}
    )
    await adapter.download_blob(blob, tmp_path / "download")
    assert (tmp_path / "download").read_bytes() == body
    assert "versionId=" + blob.version_id in await adapter.sign_blob(blob)
    assert all(
        values["VersionId"] == blob.version_id
        for op, values in store.calls
        if op in {"head", "get", "sign"}
    )


@pytest.mark.asyncio
async def test_missing_original_video_does_not_fall_back_to_latest(tmp_path):
    store = VersionedStore()
    adapter = storage(store)
    source = tmp_path / "source"
    source.write_bytes(b"original")
    blob = await adapter.upload_blob(
        source,
        prefix() + "source",
        sha256=hashlib.sha256(b"original").hexdigest(),
        byte_size=8,
    )
    store.put_object(
        Bucket=adapter.config.inputs_bucket,
        Key=blob.key,
        Body=b"replacement",
        Metadata={},
    )
    del store.versions[(blob.key, blob.version_id)]
    with pytest.raises(InputFailure, match="input_corrupt"):
        await adapter.download_blob(blob, tmp_path / "download")
    assert not (tmp_path / "download").exists()


@pytest.mark.asyncio
async def test_video_cleanup_removes_only_exact_attempt_versions_and_uploads():
    store = VersionedStore()
    target = prefix()
    other = prefix()
    for key in [target + "source", target + "frames/0/w1280-jpeg", other + "source"]:
        store.put_object(Bucket="isolated-inputs", Key=key, Body=b"bytes", Metadata={})
    upload = store.create_multipart_upload(
        Bucket="isolated-inputs", Key=target + "source"
    )
    assert await storage(store).delete_attempt_versions(target) == 2
    assert upload["UploadId"] not in store.uploads
    assert (
        len(store.versions) == 1 and next(iter(store.versions))[0] == other + "source"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    ["accepted-inputs/", "accepted-video-inputs/", "dataset/video.mp4", "../source"],
)
async def test_video_store_rejects_cross_domain_or_unscoped_keys_before_io(
    tmp_path, bad
):
    store = VersionedStore()
    source = tmp_path / "source"
    source.write_bytes(b"x")
    with pytest.raises(InputFailure, match="invalid_request"):
        await storage(store).upload_blob(
            source, bad, sha256=hashlib.sha256(b"x").hexdigest(), byte_size=1
        )
    assert store.calls == []
    with pytest.raises(InputFailure, match="invalid_request"):
        await storage(store).delete_attempt_versions(bad)
    assert store.calls == []


@pytest.mark.asyncio
async def test_existing_image_store_does_not_gain_video_write_or_cleanup_scope(
    tmp_path,
):
    store = VersionedStore()
    source = tmp_path / "source"
    source.write_bytes(b"x")
    with pytest.raises(InputFailure, match="invalid_request"):
        await make_storage(store).upload_blob(
            source,
            prefix() + "source",
            sha256=hashlib.sha256(b"x").hexdigest(),
            byte_size=1,
        )
    with pytest.raises(InputFailure, match="invalid_request"):
        await make_storage(store).delete_attempt_versions(prefix())
    assert store.calls == []
