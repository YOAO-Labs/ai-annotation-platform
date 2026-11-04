"""Synchronous S3 adapter for the fixed content-addressed Mask namespace."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TypeVar

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel

from . import codec
from .contract import (
    MAX_COMPRESSED_BYTES,
    MAX_LIST_PAGE,
    MAX_RLE_OBJECT_BYTES,
    MASK_PREFIX,
    Candidate,
    CandidatePage,
    DeleteRequest,
    DeleteResult,
    GetRequest,
    GetResult,
    ListRequest,
    MaskContentFailure,
    MaskReference,
    PutRequest,
    PutResult,
)

_NOT_FOUND = {"404", "NoSuchKey", "NotFound"}
_PUT_CONFLICT = {"409", "412", "ConditionalRequestConflict", "PreconditionFailed"}
RequestModel = TypeVar("RequestModel", bound=BaseModel)


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _plain(value.__dict__)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_plain(item) for item in value)
    return value


def _validated_request(model: type[RequestModel], request: object) -> RequestModel:
    if type(request) is not model:
        raise MaskContentFailure("invalid_request")
    try:
        # Revalidate from plain values so even model_construct-created objects
        # cannot bypass the fixed key, size, or RLE constraints.
        return model.model_validate(_plain(request))
    except (TypeError, ValueError):
        raise MaskContentFailure("invalid_request") from None


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _content_error(exc: ValueError) -> MaskContentFailure:
    message = str(exc)
    if any(
        token in message
        for token in (
            "4 MiB",
            "8 MiB",
            "MAX_COMPRESSED_BYTES",
            "MAX_UNCOMPRESSED_BYTES",
            "expansion ratio",
        )
    ):
        return MaskContentFailure("resource_limit")
    return MaskContentFailure("invalid_request")


class S3MaskContentStore:
    """Store immutable RLE objects in one bucket and one fixed key prefix."""

    def __init__(self, client: Any, bucket: str) -> None:
        if not isinstance(bucket, str) or not bucket.strip():
            raise ValueError("Mask content store requires a bucket")
        self.client = client
        self.bucket = bucket

    def check_ready(self) -> None:
        """Check bucket access without requiring ListBucket permission."""
        try:
            self.client.get_bucket_location(Bucket=self.bucket)
        except Exception:
            raise MaskContentFailure("storage_unavailable") from None

    def put(self, request: PutRequest) -> PutResult:
        request = _validated_request(PutRequest, request)
        try:
            canonical = codec.canonical_rle_bytes(request.content)
            if request.storage_encoding == "gzip":
                try:
                    reference = codec.build_rle_gzip_reference(request.content)
                    _canonical, body = codec.canonical_rle_bytes_gzip(request.content)
                    content_type = "application/gzip"
                except ValueError as exc:
                    if "expansion ratio" not in str(exc):
                        raise
                    reference = codec.build_rle_reference(request.content)
                    body = canonical
                    content_type = "application/json"
            else:
                reference = codec.build_rle_reference(request.content)
                body = canonical
                content_type = "application/json"
        except ValueError as exc:
            raise _content_error(exc) from None

        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=reference.object_key,
                Body=body,
                ContentType=content_type,
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if _error_code(exc) not in _PUT_CONFLICT:
                raise MaskContentFailure("storage_unavailable") from None
            existing = self._get_raw(reference)
            try:
                codec.decode_reference(reference, existing)
            except ValueError:
                raise MaskContentFailure("object_corrupt") from None
        except BotoCoreError:
            raise MaskContentFailure("storage_unavailable") from None
        return PutResult(reference=reference)

    def get(self, request: GetRequest) -> GetResult:
        if type(request) is not GetRequest:
            raise MaskContentFailure("invalid_request")
        raw_reference = request.reference
        try:
            reference = MaskReference.model_validate(_plain(raw_reference))
        except (TypeError, ValueError):
            raise MaskContentFailure("invalid_reference") from None
        request = GetRequest(reference=reference)
        try:
            raw = self._get_raw(reference)
            content = codec.decode_reference(reference, raw)
        except MaskContentFailure:
            raise
        except ValueError:
            raise MaskContentFailure("object_corrupt") from None
        return GetResult(reference=reference, content=content)

    def list(self, request: ListRequest) -> CandidatePage:
        request = _validated_request(ListRequest, request)
        if request.limit > MAX_LIST_PAGE:
            raise MaskContentFailure("invalid_request")
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket,
            "Prefix": MASK_PREFIX + "/",
            "MaxKeys": request.limit,
        }
        if request.cursor is not None:
            kwargs["ContinuationToken"] = request.cursor
        try:
            response = self.client.list_objects_v2(**kwargs)
            items = []
            for obj in response.get("Contents", []):
                key = obj.get("Key")
                etag = str(obj.get("ETag") or "").strip('"')
                last_modified = obj.get("LastModified")
                size = obj.get("Size")
                try:
                    reference = _reference_from_key(key)
                    candidate = Candidate(
                        key=reference.object_key,
                        etag=etag,
                        byte_size=size,
                        last_modified=last_modified,
                    )
                except (ValueError, TypeError):
                    # Old or unrelated keys in the bucket are not Mask objects.
                    continue
                items.append(candidate)
            next_cursor = (
                response.get("NextContinuationToken")
                if response.get("IsTruncated") is True
                else None
            )
            if response.get("IsTruncated") is True and (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor == request.cursor
            ):
                raise MaskContentFailure("invalid_response")
            return CandidatePage(items=tuple(items), cursor=next_cursor)
        except MaskContentFailure:
            raise
        except (BotoCoreError, ClientError):
            raise MaskContentFailure("storage_unavailable") from None
        except (ValueError, TypeError, KeyError):
            raise MaskContentFailure("invalid_response") from None

    def delete(self, request: DeleteRequest) -> DeleteResult:
        request = _validated_request(DeleteRequest, request)
        key = request.object_key
        try:
            current = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND:
                return DeleteResult(object_key=key, deleted=False)
            raise MaskContentFailure("storage_unavailable") from None
        except BotoCoreError:
            raise MaskContentFailure("storage_unavailable") from None

        current_etag = str(current.get("ETag") or "").strip('"')
        current_modified = current.get("LastModified")
        if (
            current_etag != request.etag
            or not isinstance(current_modified, datetime)
            # HEAD uses HTTP-date seconds; LIST can include fractional seconds.
            or _utc(current_modified).replace(microsecond=0)
            != _utc(request.last_modified).replace(microsecond=0)
        ):
            raise MaskContentFailure("content_changed")

        try:
            self.client.delete_object(
                Bucket=self.bucket,
                Key=key,
                IfMatch=request.etag,
            )
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND:
                return DeleteResult(object_key=key, deleted=False)
            if _error_code(exc) in _PUT_CONFLICT:
                raise MaskContentFailure("content_changed") from None
            raise MaskContentFailure("storage_unavailable") from None
        except BotoCoreError:
            raise MaskContentFailure("storage_unavailable") from None
        return DeleteResult(object_key=key, deleted=True)

    def _get_raw(self, reference: MaskReference) -> bytes:
        # Validate the complete content address before any S3 request.
        try:
            ref = MaskReference.model_validate(_plain(reference))
        except (ValueError, TypeError):
            raise MaskContentFailure("invalid_reference") from None
        max_bytes = (
            MAX_COMPRESSED_BYTES
            if ref.object_key.endswith(".json.gz")
            else MAX_RLE_OBJECT_BYTES
        )
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=ref.object_key)
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND:
                raise MaskContentFailure("object_missing") from None
            raise MaskContentFailure("storage_unavailable") from None
        except BotoCoreError:
            raise MaskContentFailure("storage_unavailable") from None
        body = response.get("Body")
        if body is None:
            raise MaskContentFailure("object_corrupt")
        try:
            raw = body.read(max_bytes + 1)
        except Exception:
            raise MaskContentFailure("storage_unavailable") from None
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        if not isinstance(raw, (bytes, bytearray)) or len(raw) > max_bytes:
            raise MaskContentFailure("object_corrupt")
        return bytes(raw)


def _reference_from_key(key: str) -> MaskReference:
    if not isinstance(key, str):
        raise ValueError("invalid object key")
    filename = key.rsplit("/", 1)[-1]
    if filename.endswith(".json.gz"):
        digest = filename[:-8]
        encoding = "gzip"
    elif filename.endswith(".json"):
        digest = filename[:-5]
        encoding = "identity"
    else:
        raise ValueError("unsupported Mask key")
    # A list item need not have full reference metadata. Validate the exact key
    # pattern and digest before it can reach the cleanup caller.
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("invalid Mask digest")
    expected = (
        codec.rle_gzip_object_key(digest)
        if encoding == "gzip"
        else codec.rle_object_key(digest)
    )
    if key != expected:
        raise ValueError("Mask key does not match the content digest")
    # The minimal placeholder fields are never returned; Candidate validates
    # the canonical key again on the way out.
    return MaskReference(
        encoding="coco_rle_ref",
        storage_encoding=encoding,
        size=(1, 1),
        object_key=key,
        sha256=digest,
        runs=1,
        bytes=1,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)
