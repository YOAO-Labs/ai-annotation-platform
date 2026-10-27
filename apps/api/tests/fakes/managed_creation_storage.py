"""Conditional S3 fake for application creation contracts, without network I/O."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import io
from types import SimpleNamespace
import uuid

from botocore.exceptions import ClientError

from app.config import settings


def client_error(code, status, operation):
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation,
    )


class CreationStorage:
    def __init__(self, *, keyed_by_bucket=False):
        self.objects = {}
        self.keyed_by_bucket = keyed_by_bucket
        self.content_types = {}
        self.uploads = {}
        self.calls = []
        self.closed = 0
        self.presigned = []
        scheme = "https" if settings.minio_use_ssl else "http"
        self.meta = SimpleNamespace(
            endpoint_url=f"{scheme}://{settings.minio_endpoint}"
        )

    def _key(self, bucket, key):
        return (bucket, key) if self.keyed_by_bucket else key

    @staticmethod
    def response(**fields):
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": str(uuid.uuid4())},
            **fields,
        }

    @staticmethod
    def etag(data):
        return '"' + hashlib.md5(data).hexdigest() + '"'

    def head_bucket(self, **kwargs):
        return self.response()

    def put_object(self, *, Bucket, Key, Body, ContentType=None, **kwargs):
        self.calls.append(("put", Bucket, Key, kwargs))
        if kwargs.get("IfNoneMatch") == "*" and self._key(Bucket, Key) in self.objects:
            raise client_error("PreconditionFailed", 412, "PutObject")
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[self._key(Bucket, Key)] = data
        self.content_types[self._key(Bucket, Key)] = (
            ContentType or "application/octet-stream"
        )
        return self.response(ETag=self.etag(data))

    def head_object(self, *, Bucket, Key, **kwargs):
        if self._key(Bucket, Key) not in self.objects:
            raise client_error("NoSuchKey", 404, "HeadObject")
        return self.response(
            LastModified=datetime(2026, 10, 24, tzinfo=timezone.utc),
            ContentLength=len(self.objects[self._key(Bucket, Key)]),
            ETag=self.etag(self.objects[self._key(Bucket, Key)]),
            ContentType=self.content_types.get(
                self._key(Bucket, Key), "application/octet-stream"
            ),
        )

    def get_object(self, *, Bucket, Key, **kwargs):
        meta = self.head_object(Bucket=Bucket, Key=Key)
        if kwargs.get("IfMatch") and kwargs["IfMatch"] != meta["ETag"]:
            raise client_error("PreconditionFailed", 412, "GetObject")
        return {**meta, "Body": io.BytesIO(self.objects[self._key(Bucket, Key)])}

    def create_multipart_upload(self, *, Bucket, Key, **kwargs):
        uid = str(uuid.uuid4())
        self.uploads[uid] = {
            "bucket": Bucket,
            "key": Key,
            "parts": {},
            "type": kwargs.get("ContentType"),
        }
        self.calls.append(("create", Bucket, Key, kwargs))
        return self.response(UploadId=uid)

    def upload_part(self, *, Bucket, Key, UploadId, PartNumber, Body, **kwargs):
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.uploads[UploadId]["parts"][PartNumber] = data
        return self.response(ETag=self.etag(data))

    def upload_part_copy(
        self, *, Bucket, Key, UploadId, PartNumber, CopySource, **kwargs
    ):
        data = self.objects[self._key(CopySource["Bucket"], CopySource["Key"])]
        if kwargs.get("CopySourceIfMatch") != self.etag(data):
            raise client_error("PreconditionFailed", 412, "UploadPartCopy")
        self.uploads[UploadId]["parts"][PartNumber] = data
        return self.response(CopyPartResult={"ETag": self.etag(data)})

    def complete_multipart_upload(
        self, *, Bucket, Key, UploadId, MultipartUpload, **kwargs
    ):
        self.calls.append(("complete", Bucket, Key, kwargs))
        if kwargs.get("IfNoneMatch") == "*" and self._key(Bucket, Key) in self.objects:
            raise client_error("PreconditionFailed", 412, "CompleteMultipartUpload")
        upload = self.uploads.pop(UploadId)
        data = b"".join(
            upload["parts"][p["PartNumber"]] for p in MultipartUpload["Parts"]
        )
        self.objects[self._key(Bucket, Key)] = data
        self.content_types[self._key(Bucket, Key)] = (
            upload["type"] or "application/octet-stream"
        )
        return self.response(ETag=self.etag(data))

    def abort_multipart_upload(self, *, UploadId, **kwargs):
        if UploadId not in self.uploads:
            raise client_error("NoSuchUpload", 404, "AbortMultipartUpload")
        del self.uploads[UploadId]
        return self.response()

    def delete_object(self, *, Bucket, Key, **kwargs):
        self.calls.append(("delete", Bucket, Key, kwargs))
        self.objects.pop(self._key(Bucket, Key), None)
        return self.response()

    def generate_presigned_url(self, operation, **kwargs):
        self.presigned.append((operation, kwargs["Params"]))
        return "http://storage.test/object"

    def close(self):
        self.closed += 1
