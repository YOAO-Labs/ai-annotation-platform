from copy import deepcopy
import hashlib
import io
from types import SimpleNamespace
from uuid import uuid4

from botocore.exceptions import ClientError
import pytest

from aap_assets_inputs import contract as c
from aap_assets_inputs_runtime import storage as module
from aap_assets_inputs_runtime.storage import S3InputStorage
from input_fixtures import BODY


class VersionedStore:
    def __init__(self):
        self.enabled = True
        self.versions = {}
        self.latest = {}
        self.uploads = {}
        self.calls = []
        self.corrupt_reads = False

    def get_bucket_versioning(self, **values):
        self.calls.append(("versioning", values))
        return {"Status": "Enabled" if self.enabled else "Suspended"}

    def put_object(self, **values):
        self.calls.append(("put", {k: v for k, v in values.items() if k != "Body"}))
        if values.get("IfNoneMatch") == "*" and values["Key"] in self.latest:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        body = (
            values["Body"].read() if hasattr(values["Body"], "read") else values["Body"]
        )
        version = str(uuid4())
        response = {
            "VersionId": version,
            "ETag": '"' + hashlib.md5(body, usedforsecurity=False).hexdigest() + '"',
            "ContentLength": len(body),
            "Metadata": values.get("Metadata", {}),
        }
        self.versions[(values["Key"], version)] = (body, response)
        self.latest[values["Key"]] = version
        return deepcopy(response)

    def _value(self, values):
        try:
            return self.versions[(values["Key"], values["VersionId"])]
        except KeyError:
            raise ClientError(
                {"Error": {"Code": "NoSuchVersion"}}, "GetObject"
            ) from None

    def head_object(self, **values):
        self.calls.append(("head", values))
        return deepcopy(self._value(values)[1])

    def get_object(self, **values):
        self.calls.append(("get", values))
        body, metadata = self._value(values)
        if values["IfMatch"] != metadata["ETag"]:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "GetObject")
        result = deepcopy(metadata)
        result["Body"] = io.BytesIO(b"x" * len(body) if self.corrupt_reads else body)
        return result

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.calls.append(
            ("sign", {"operation": operation, **Params, "expiry": ExpiresIn})
        )
        return f"http://s3.test/{Params['Bucket']}/{Params['Key']}?versionId={Params['VersionId']}"

    def create_multipart_upload(self, **values):
        upload_id = str(uuid4())
        self.uploads[upload_id] = {**values, "parts": {}}
        return {"UploadId": upload_id}

    def upload_part(self, **values):
        self.uploads[values["UploadId"]]["parts"][values["PartNumber"]] = values["Body"]
        return {"ETag": '"part"'}

    def complete_multipart_upload(self, **values):
        upload = self.uploads.pop(values["UploadId"])
        body = b"".join(
            upload["parts"][p["PartNumber"]] for p in values["MultipartUpload"]["Parts"]
        )
        return self.put_object(
            Bucket=values["Bucket"],
            Key=values["Key"],
            Body=body,
            Metadata=upload["Metadata"],
            IfNoneMatch=values["IfNoneMatch"],
        )

    def abort_multipart_upload(self, **values):
        self.calls.append(("abort", values))
        self.uploads.pop(values["UploadId"], None)

    def list_multipart_uploads(self, **values):
        self.calls.append(("list-uploads", values))
        return {
            "Uploads": [
                {"Key": item["Key"], "UploadId": upload_id}
                for upload_id, item in self.uploads.items()
                if item["Key"].startswith(values["Prefix"])
            ],
            "IsTruncated": False,
        }

    def list_object_versions(self, **values):
        self.calls.append(("list-versions", values))
        return {
            "Versions": [
                {"Key": key, "VersionId": version}
                for key, version in self.versions
                if key.startswith(values["Prefix"])
            ],
            "IsTruncated": False,
        }

    def delete_object(self, **values):
        self.calls.append(("delete", values))
        self.versions.pop((values["Key"], values["VersionId"]), None)
        return {}


def make_storage(store):
    config = SimpleNamespace(
        inputs_bucket="isolated-inputs",
        datasets_bucket="isolated-datasets",
        legacy_bucket="isolated-legacy",
        cache_bucket="isolated-cache",
    )
    return S3InputStorage(config, source_storage=object(), client=store, signer=store)


def new_key():
    return c.attempt_prefix(uuid4(), 1, uuid4()) + "source"


async def upload(storage, tmp_path, key=None):
    path = tmp_path / "source"
    path.write_bytes(BODY)
    return await storage.upload_blob(
        path,
        key or new_key(),
        sha256=hashlib.sha256(BODY).hexdigest(),
        byte_size=len(BODY),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("multipart", [False, True])
async def test_version_bound_bytes_survive_latest_key_overwrite(
    tmp_path, monkeypatch, multipart
):
    store = VersionedStore()
    storage = make_storage(store)
    if multipart:
        monkeypatch.setattr(module, "_PART", 4)
    blob = await upload(storage, tmp_path)
    replacement = store.put_object(
        Bucket="isolated-inputs",
        Key=blob.key,
        Body=b"new source",
        Metadata={"aap-input-sha256": hashlib.sha256(b"new source").hexdigest()},
    )
    assert replacement["VersionId"] != blob.version_id
    target = tmp_path / "read"
    await storage.download_blob(blob, target)
    assert target.read_bytes() == BODY
    assert "versionId=" + blob.version_id in await storage.sign_blob(blob)
    assert all(
        values["VersionId"] == blob.version_id
        for operation, values in store.calls
        if operation in {"get", "head", "sign"}
    )
    assert all(
        values.get("Bucket") == "isolated-inputs" for _operation, values in store.calls
    )


@pytest.mark.asyncio
async def test_unversioned_bucket_is_rejected_before_publish(tmp_path):
    store = VersionedStore()
    store.enabled = False
    with pytest.raises(c.InputFailure, match="versioning_required"):
        await upload(make_storage(store), tmp_path)
    assert not store.versions
    assert not any(operation == "put" for operation, _ in store.calls)


@pytest.mark.asyncio
async def test_uploaded_version_is_read_back_before_receipt(tmp_path):
    store = VersionedStore()
    store.corrupt_reads = True
    with pytest.raises(c.InputFailure, match="input_corrupt"):
        await upload(make_storage(store), tmp_path)
    assert (
        store.versions
    )  # Unconfirmed version remains discoverable under the lease prefix.


@pytest.mark.asyncio
async def test_missing_original_version_never_reads_latest(tmp_path):
    store = VersionedStore()
    storage = make_storage(store)
    blob = await upload(storage, tmp_path)
    store.put_object(
        Bucket="isolated-inputs", Key=blob.key, Body=b"replacement", Metadata={}
    )
    del store.versions[(blob.key, blob.version_id)]
    store.calls.clear()
    with pytest.raises(c.InputFailure, match="input_corrupt"):
        await storage.download_blob(blob, tmp_path / "read")
    assert len(store.calls) == 1
    assert store.calls[0][1]["VersionId"] == blob.version_id
    assert not (tmp_path / "read").exists()


@pytest.mark.asyncio
async def test_garbage_collects_only_attempt_versions_and_uploads(tmp_path):
    store = VersionedStore()
    storage = make_storage(store)
    blob = await upload(storage, tmp_path)
    store.put_object(
        Bucket="isolated-inputs", Key=blob.key, Body=b"second version", Metadata={}
    )
    other_key = new_key()
    store.put_object(
        Bucket="isolated-inputs", Key=other_key, Body=b"retain", Metadata={}
    )
    store.create_multipart_upload(Bucket="isolated-inputs", Key=blob.key, Metadata={})
    assert await storage.delete_attempt_versions(blob.key.removesuffix("source")) == 2
    assert not store.uploads
    assert {key for key, _ in store.versions} == {other_key}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prefix",
    ["", "accepted-inputs/", "image-assets/x/", "../source/", "accepted-inputs/*/"],
)
async def test_cleanup_rejects_unscoped_prefix_before_io(prefix):
    store = VersionedStore()
    with pytest.raises(c.InputFailure, match="invalid_request"):
        await make_storage(store).delete_attempt_versions(prefix)
    assert not store.calls
