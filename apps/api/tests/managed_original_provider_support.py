"""In-memory S3 protocol support for managed original tests."""

from datetime import datetime, timezone
import hashlib
import io

from botocore.exceptions import ClientError


def error(code, status=None):
    metadata = {} if status is None else {"HTTPStatusCode": status}
    return ClientError(
        {
            "Error": {"Code": code, "Message": "private provider detail"},
            "ResponseMetadata": metadata,
        },
        "ObjectOperation",
    )


def response(**values):
    return {
        "ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": "request"},
        **values,
    }


class Body(io.BytesIO):
    def __init__(self, value, hook=None):
        super().__init__(value)
        self.hook, self.close_count, self.requests = hook, 0, []
        self.close_failure = False

    def read(self, size):
        self.requests.append(size)
        chunk = super().read(size)
        if self.hook:
            self.hook(self, chunk)
        return chunk

    def close(self):
        self.close_count += 1
        super().close()
        if self.close_failure:
            raise OSError("private close detail")


class MemoryS3:
    def __init__(self):
        self.objects, self.uploads, self.versions = {}, {}, {}
        self.calls, self.bodies = [], []
        self.versioned = False
        self.hook, self.body_hook = None, None
        self.ignore_get_condition = False

    def record(self, operation, params):
        self.calls.append((operation, params.copy()))
        if self.hook:
            self.hook(operation, params)

    def store(
        self, bucket, key, data, *, etag=None, content_type="application/octet-stream"
    ):
        version = f"version-{len(self.versions)}" if self.versioned else None
        value = {
            "data": data,
            "ETag": etag or f'"{hashlib.md5(data).hexdigest()}"',
            "VersionId": version,
            "ContentType": content_type,
        }
        self.objects[bucket, key] = value
        if version:
            self.versions[bucket, key, version] = value
        return value

    def current(self, params):
        if params.get("VersionId") is not None:
            value = self.versions.get(
                (params["Bucket"], params["Key"], params["VersionId"])
            )
        else:
            value = self.objects.get((params["Bucket"], params["Key"]))
        if value is None:
            raise error("NoSuchKey", 404)
        return value

    def headers(self, value):
        return response(
            ETag=value["ETag"],
            VersionId=value["VersionId"],
            ContentLength=len(value["data"]),
            ContentType=value["ContentType"],
            LastModified=datetime(2026, 10, 24, tzinfo=timezone.utc),
        )

    def create_multipart_upload(self, **params):
        self.record("create", params)
        upload_id = f"upload-{len(self.calls)}"
        self.uploads[upload_id] = {"parts": {}, "params": params}
        return response(UploadId=upload_id)

    def upload_part(self, **params):
        self.record("part", params)
        self.uploads[params["UploadId"]]["parts"][params["PartNumber"]] = params["Body"]
        return response(ETag=f'"part-{params["PartNumber"]}"')

    def upload_part_copy(self, **params):
        self.record("copy", params)
        value = self.current(params["CopySource"])
        if params["CopySourceIfMatch"] != value["ETag"]:
            raise error("PreconditionFailed", 412)
        self.uploads[params["UploadId"]]["parts"][params["PartNumber"]] = value["data"]
        return response(
            CopyPartResult={"ETag": '"copy-part"'},
            CopySourceVersionId=value["VersionId"],
        )

    def abort_multipart_upload(self, **params):
        self.record("abort", params)
        self.uploads.pop(params["UploadId"], None)
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}

    def complete_multipart_upload(self, **params):
        self.record("complete", params)
        assert params["IfNoneMatch"] == "*"
        if (params["Bucket"], params["Key"]) in self.objects:
            raise error("PreconditionFailed", 412)
        upload = self.uploads.pop(params["UploadId"])
        data = b"".join(
            upload["parts"][p["PartNumber"]] for p in params["MultipartUpload"]["Parts"]
        )
        value = self.store(
            params["Bucket"],
            params["Key"],
            data,
            content_type=upload["params"]["ContentType"],
        )
        return response(ETag=value["ETag"], VersionId=value["VersionId"])

    def put_object(self, **params):
        self.record("put", params)
        assert params["IfNoneMatch"] == "*" and params["Body"] == b""
        if (params["Bucket"], params["Key"]) in self.objects:
            raise error("PreconditionFailed", 412)
        value = self.store(
            params["Bucket"], params["Key"], b"", content_type=params["ContentType"]
        )
        return response(ETag=value["ETag"], VersionId=value["VersionId"])

    def head_object(self, **params):
        self.record("head", params)
        return self.headers(self.current(params))

    def get_object(self, **params):
        self.record("get", params)
        value = self.current(params)
        if not self.ignore_get_condition and params.get("IfMatch") != value["ETag"]:
            raise error("PreconditionFailed", 412)
        body = Body(value["data"], self.body_hook)
        self.bodies.append(body)
        return {**self.headers(value), "Body": body}


def check_error(caught, outcome="failed", code=None):
    assert caught.value.outcome == outcome
    assert bool(getattr(caught.value, "storage_operation_unsettled", False)) == (
        outcome == "unknown"
    )
    assert "private" not in str(caught.value)
    if code:
        assert caught.value.code == code
