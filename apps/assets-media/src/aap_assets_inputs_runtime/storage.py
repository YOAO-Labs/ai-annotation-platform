"""Exact-version input Blobs in a dedicated versioned S3 bucket."""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
import hashlib
from pathlib import Path
import re
import threading

from aap_assets_inputs import contract as c

_CHUNK = 1024 * 1024
_PART = 16 * 1024 * 1024


def _check_key(key: str) -> None:
    if not isinstance(key, str) or re.fullmatch(c.BLOB_KEY_PATTERN, key) is None:
        raise c.InputFailure("invalid_request")


def _check_prefix(prefix: str) -> None:
    if (
        not isinstance(prefix, str)
        or re.fullmatch(c.ATTEMPT_PREFIX_PATTERN, prefix) is None
    ):
        raise c.InputFailure("invalid_request")


def _error(exc, *, blob: bool = False):
    code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
    if code in {
        "NoSuchKey",
        "NoSuchVersion",
        "NotFound",
        "404",
        "PreconditionFailed",
        "412",
    }:
        return c.InputFailure("input_corrupt" if blob else "object_missing")
    return c.InputFailure("storage_unavailable")


def _version(value):
    if not isinstance(value, str) or not value.strip() or value == "null":
        raise c.InputFailure("versioning_required")
    return value


def _etag(value):
    if not isinstance(value, str) or not value:
        raise c.InputFailure("input_corrupt")
    return value.strip('"')


def _if_match(value):
    return '"' + value.strip('"') + '"'


async def _run(function, *args):
    """Stop between requests and finish the current bounded S3 call on cancellation."""
    stop = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(function, stop, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        with suppress(BaseException):
            await task
        raise


def _check_stop(stop):
    if stop.is_set():
        raise c.InputFailure("timeout")


class S3InputStorage:
    def __init__(self, config, *, source_storage, client=None, signer=None):
        if config.inputs_bucket in {
            config.datasets_bucket,
            config.legacy_bucket,
            config.cache_bucket,
        }:
            raise ValueError("accepted inputs require their own versioned bucket")
        self.config = config
        self.source_storage = source_storage
        self._owned_client = client is None
        self._owned_signer = signer is None
        if client is None or signer is None:
            import boto3
            from botocore.config import Config

            credentials = {
                "aws_access_key_id": config.access_key,
                "aws_secret_access_key": config.secret_key,
                "region_name": config.region,
                "config": Config(
                    signature_version="s3v4",
                    connect_timeout=config.connect_timeout,
                    read_timeout=config.read_timeout,
                    retries={
                        "mode": "standard",
                        "total_max_attempts": config.max_attempts,
                    },
                    s3={"addressing_style": "path"},
                ),
            }
            if client is None:
                client = boto3.client("s3", endpoint_url=config.endpoint, **credentials)
            if signer is None:
                signer = boto3.client(
                    "s3", endpoint_url=config.public_endpoint, **credentials
                )
        self._client, self._signer = client, signer

    async def close(self):
        if self._owned_client:
            self._client.close()
        if self._owned_signer and self._signer is not self._client:
            self._signer.close()

    def _assert_versioning(self, stop):
        _check_stop(stop)
        try:
            if (
                self._client.get_bucket_versioning(
                    Bucket=self.config.inputs_bucket
                ).get("Status")
                != "Enabled"
            ):
                raise c.InputFailure("versioning_required")
        except c.InputFailure:
            raise
        except Exception as exc:
            raise _error(exc) from None

    async def assert_versioning(self):
        await _run(self._assert_versioning)

    async def download_source(self, source, destination: Path):
        return await self.source_storage.download_source(source, destination)

    async def head_source(self, source):
        return await self.source_storage.head_source(source)

    def _head_blob(self, stop, blob):
        _check_key(blob.key)
        _check_stop(stop)
        try:
            value = self._client.head_object(
                Bucket=self.config.inputs_bucket,
                Key=blob.key,
                VersionId=blob.version_id,
            )
            if (
                value.get("VersionId") != blob.version_id
                or value.get("ContentLength") != blob.byte_size
                or _etag(value.get("ETag")) != blob.etag
                or value.get("Metadata", {}).get("aap-input-sha256") != blob.sha256
            ):
                raise c.InputFailure("input_corrupt")
        except c.InputFailure:
            raise
        except Exception as exc:
            raise _error(exc, blob=True) from None

    async def verify_blob(self, blob: c.InputBlob):
        await _run(self._head_blob, blob)

    def _read_blob(self, stop, blob, destination):
        _check_key(blob.key)
        body, output = None, None
        created = False
        try:
            _check_stop(stop)
            value = self._client.get_object(
                Bucket=self.config.inputs_bucket,
                Key=blob.key,
                VersionId=blob.version_id,
                IfMatch=_if_match(blob.etag),
            )
            if (
                value.get("VersionId") != blob.version_id
                or value.get("ContentLength") != blob.byte_size
                or _etag(value.get("ETag")) != blob.etag
            ):
                raise c.InputFailure("input_corrupt")
            body = value.get("Body")
            if body is None:
                raise c.InputFailure("input_corrupt")
            if destination is not None:
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                output = destination.open("xb")
                created = True
            written, digest = 0, hashlib.sha256()
            while True:
                _check_stop(stop)
                chunk = body.read(_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > blob.byte_size:
                    raise c.InputFailure("input_corrupt")
                digest.update(chunk)
                if output is not None:
                    output.write(chunk)
            if written != blob.byte_size or digest.hexdigest() != blob.sha256:
                raise c.InputFailure("input_corrupt")
            if output is not None:
                output.close()
                output = None
                destination.chmod(0o400)
        except c.InputFailure:
            if created:
                destination.unlink(missing_ok=True)
            raise
        except Exception as exc:
            if created:
                destination.unlink(missing_ok=True)
            raise _error(exc, blob=True) from None
        finally:
            if output is not None:
                output.close()
            if body is not None:
                body.close()

    async def download_blob(self, blob: c.InputBlob, destination: Path):
        await _run(self._read_blob, blob, destination)

    def _upload_blob(self, stop, path, key, sha256, byte_size):
        _check_key(key)
        self._assert_versioning(stop)
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(_CHUNK):
                _check_stop(stop)
                digest.update(chunk)
        if path.stat().st_size != byte_size or digest.hexdigest() != sha256:
            raise c.InputFailure("input_corrupt")
        upload_id = None
        try:
            _check_stop(stop)
            if byte_size <= _PART:
                with path.open("rb") as source:
                    response = self._client.put_object(
                        Bucket=self.config.inputs_bucket,
                        Key=key,
                        Body=source,
                        ContentLength=byte_size,
                        ContentType="application/octet-stream",
                        Metadata={"aap-input-sha256": sha256},
                        IfNoneMatch="*",
                        ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode(
                            "ascii"
                        ),
                    )
            else:
                upload_id = self._client.create_multipart_upload(
                    Bucket=self.config.inputs_bucket,
                    Key=key,
                    ContentType="application/octet-stream",
                    Metadata={"aap-input-sha256": sha256},
                )["UploadId"]
                parts = []
                with path.open("rb") as source:
                    while chunk := source.read(_PART):
                        _check_stop(stop)
                        number = len(parts) + 1
                        if number > 10000:
                            raise c.InputFailure("resource_limit")
                        part = self._client.upload_part(
                            Bucket=self.config.inputs_bucket,
                            Key=key,
                            UploadId=upload_id,
                            PartNumber=number,
                            Body=chunk,
                            ContentMD5=base64.b64encode(
                                hashlib.md5(chunk, usedforsecurity=False).digest()
                            ).decode("ascii"),
                        )
                        parts.append({"PartNumber": number, "ETag": part["ETag"]})
                _check_stop(stop)
                response = self._client.complete_multipart_upload(
                    Bucket=self.config.inputs_bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                    IfNoneMatch="*",
                )
                upload_id = None
            blob = c.InputBlob(
                key=key,
                version_id=_version(response.get("VersionId")),
                etag=_etag(response.get("ETag")),
                byte_size=byte_size,
                sha256=sha256,
            )
            self._head_blob(stop, blob)
            self._read_blob(stop, blob, None)
            return blob
        except c.InputFailure:
            raise
        except Exception as exc:
            raise _error(exc, blob=True) from None
        finally:
            if upload_id is not None:
                with suppress(Exception):
                    self._client.abort_multipart_upload(
                        Bucket=self.config.inputs_bucket, Key=key, UploadId=upload_id
                    )

    async def upload_blob(
        self, path: Path, key: str, *, sha256: str, byte_size: int
    ) -> c.InputBlob:
        return await _run(self._upload_blob, path, key, sha256, byte_size)

    async def sign_blob(self, blob: c.InputBlob, *, expires_in: int = 900):
        _check_key(blob.key)
        if type(expires_in) is not int or not 1 <= expires_in <= 3600:
            raise c.InputFailure("invalid_request")
        try:
            return self._signer.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.config.inputs_bucket,
                    "Key": blob.key,
                    "VersionId": blob.version_id,
                },
                ExpiresIn=expires_in,
            )
        except Exception:
            raise c.InputFailure("storage_unavailable") from None

    def _delete_prefix(self, stop, prefix):
        _check_prefix(prefix)
        deleted = 0
        try:
            while True:
                _check_stop(stop)
                parameters = {"Bucket": self.config.inputs_bucket, "Prefix": prefix}
                page = self._client.list_multipart_uploads(**parameters)
                uploads = page.get("Uploads", [])
                for upload in uploads:
                    _check_stop(stop)
                    if not upload["Key"].startswith(prefix):
                        raise c.InputFailure("invalid_response")
                    self._client.abort_multipart_upload(
                        Bucket=self.config.inputs_bucket,
                        Key=upload["Key"],
                        UploadId=upload["UploadId"],
                    )
                if not page.get("IsTruncated"):
                    break
                if not uploads:
                    raise c.InputFailure("invalid_response")
                # Re-list the first page: aborting invalidates upload markers on
                # some S3 peers, and this exact attempt prefix is already retired.
            while True:
                _check_stop(stop)
                parameters = {"Bucket": self.config.inputs_bucket, "Prefix": prefix}
                page = self._client.list_object_versions(**parameters)
                objects = []
                for obj in page.get("Versions", []) + page.get("DeleteMarkers", []):
                    if not obj["Key"].startswith(prefix) or not isinstance(
                        obj.get("VersionId"), str
                    ):
                        raise c.InputFailure("invalid_response")
                    _version(obj["VersionId"])
                    objects.append({"Key": obj["Key"], "VersionId": obj["VersionId"]})
                for obj in objects:
                    _check_stop(stop)
                    # Per-version requests retain MinIO's version-scoped IAM condition.
                    self._client.delete_object(
                        Bucket=self.config.inputs_bucket,
                        Key=obj["Key"],
                        VersionId=obj["VersionId"],
                    )
                    deleted += 1
                if not page.get("IsTruncated"):
                    break
                if not objects:
                    raise c.InputFailure("invalid_response")
            return deleted
        except c.InputFailure:
            raise
        except Exception as exc:
            raise _error(exc) from None

    async def delete_attempt_versions(self, prefix: str) -> int:
        return await _run(self._delete_prefix, prefix)
