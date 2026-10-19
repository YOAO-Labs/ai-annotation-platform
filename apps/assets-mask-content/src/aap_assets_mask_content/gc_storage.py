"""S3 implementation of the immutable Mask garbage-collection journal."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel

from . import gc_contract as gc
from .contract import MASK_PREFIX
from .storage import _error_code, _utc

_NOT_FOUND = {"404", "NoSuchKey", "NotFound"}
_CONDITIONAL_CONFLICT = {
    "409",
    "412",
    "ConditionalRequestConflict",
    "PreconditionFailed",
}
_PRECONDITION_FAILED = {"412", "PreconditionFailed"}
_MASK_PREFIX = MASK_PREFIX + "/"
_MAX_JOURNAL_BYTES = gc.MAX_GC_RESPONSE_BYTES


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _plain(value.__dict__)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_plain(item) for item in value)
    return value


def _validated_request(model, request):
    if type(request) is not model:
        raise gc.MaskGcFailure("invalid_request")
    try:
        return model.model_validate(_plain(request))
    except (TypeError, ValueError):
        raise gc.MaskGcFailure("invalid_request") from None


def _status(
    request: gc.DeleteOperationRequest | None,
    state: gc.GcState,
    outcome: gc.GcOutcome | None = None,
) -> gc.DeleteOperationStatus:
    operation_id = request.operation_id if request is not None else None
    if operation_id is None:
        raise ValueError("operation ID is required for a non-not-found status")
    return gc.DeleteOperationStatus(
        operation_id=operation_id,
        request=request,
        state=state,
        outcome=outcome,
    )


class S3MaskGcStore:
    """Use conditional S3 requests as a durable, non-expiring delete fence."""

    def __init__(self, client: Any, bucket: str) -> None:
        if not isinstance(bucket, str) or not bucket.strip():
            raise ValueError("Mask GC store requires a bucket")
        self.client = client
        self.bucket = bucket

    def check_ready(self) -> None:
        try:
            self.client.get_bucket_location(Bucket=self.bucket)
        except Exception:
            raise gc.MaskGcFailure("storage_unavailable") from None

    def scan(self, request: gc.GcScanRequest) -> gc.GcScanPage:
        request = _validated_request(gc.GcScanRequest, request)
        kwargs = {
            "Bucket": self.bucket,
            "Prefix": _MASK_PREFIX,
            "MaxKeys": request.limit,
        }
        if request.after_key is not None:
            kwargs["StartAfter"] = request.after_key
        try:
            response = self.client.list_objects_v2(**kwargs)
            contents = response.get("Contents", [])
            truncated = response.get("IsTruncated", False)
            if not isinstance(contents, list) or type(truncated) is not bool:
                raise gc.MaskGcFailure("invalid_response")

            physical_keys: list[str] = []
            candidates: list[gc.GcCandidate] = []
            previous = request.after_key
            for obj in contents:
                if not isinstance(obj, dict):
                    raise gc.MaskGcFailure("invalid_response")
                key = obj.get("Key")
                if not isinstance(key, str) or not key.startswith(_MASK_PREFIX):
                    raise gc.MaskGcFailure("invalid_response")
                try:
                    key_bytes = key.encode("utf-8")
                    if len(key_bytes) > gc.MAX_CURSOR_BYTES:
                        # S3 keys are capped at 1024 UTF-8 bytes. An overlong
                        # provider value cannot safely become the next cursor.
                        raise ValueError("physical key exceeds the cursor limit")
                    if previous is not None and key_bytes <= previous.encode("utf-8"):
                        raise ValueError("S3 scan page is not strictly ordered")
                except UnicodeEncodeError:
                    raise gc.MaskGcFailure("invalid_response") from None
                except ValueError:
                    raise gc.MaskGcFailure("invalid_response") from None
                physical_keys.append(key)
                previous = key

                # Legacy or unrelated names are cursor progress, not deletion
                # authority. Invalid metadata also remains safely skipped.
                try:
                    etag = obj.get("ETag")
                    candidate = gc.GcCandidate(
                        object_key=key,
                        etag=etag.strip('"') if isinstance(etag, str) else etag,
                        last_modified=obj.get("LastModified"),
                        byte_size=obj.get("Size"),
                    )
                except (TypeError, ValueError):
                    continue
                candidates.append(candidate)

            after_key = physical_keys[-1] if physical_keys else request.after_key
            if truncated and (
                after_key is None
                or (request.after_key is not None and after_key == request.after_key)
            ):
                raise gc.MaskGcFailure("invalid_response")
            return gc.GcScanPage(
                items=tuple(candidates),
                after_key=after_key,
                complete=not truncated,
            )
        except gc.MaskGcFailure:
            raise
        except (BotoCoreError, ClientError):
            raise gc.MaskGcFailure("storage_unavailable") from None
        except (ValueError, TypeError, KeyError):
            raise gc.MaskGcFailure("invalid_response") from None
        except Exception:
            raise gc.MaskGcFailure("storage_unavailable") from None

    def execute(self, request: gc.DeleteOperationRequest) -> gc.DeleteOperationStatus:
        request = _validated_request(gc.DeleteOperationRequest, request)
        self._require_safe_execute_client()
        request_key, receipt_key = self._journal_keys(request.operation_id)
        request_body = gc.json_bytes(request)
        if len(request_body) > _MAX_JOURNAL_BYTES:
            raise gc.MaskGcFailure("resource_limit")

        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=request_key,
                Body=request_body,
                ContentType="application/json",
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if _error_code(exc) not in _CONDITIONAL_CONFLICT:
                raise gc.MaskGcFailure("storage_unavailable") from None
            existing = self._read_request(request_key)
            if existing is None:
                raise gc.MaskGcFailure("storage_unavailable") from None
            if existing != request:
                raise gc.MaskGcFailure("idempotency_conflict") from None
            # A request marker is a one-time execution claim. A matching replay
            # only reconciles a receipt; it never calls DeleteObject again.
            return self._status_for_claim(request, receipt_key)
        except gc.MaskGcFailure:
            raise
        except BotoCoreError:
            # The conditional request PUT may have succeeded before its reply
            # was lost. Leave it as a fence and never guess whether we own it.
            raise gc.MaskGcFailure("storage_unavailable") from None
        except Exception:
            raise gc.MaskGcFailure("storage_unavailable") from None

        outcome = self._inspect_payload(request)
        if outcome == "same":
            try:
                self.client.delete_object(
                    Bucket=self.bucket,
                    Key=request.object_key,
                    IfMatch=request.etag,
                )
            except ClientError as exc:
                code = _error_code(exc)
                if code in _NOT_FOUND:
                    outcome = "missing"
                elif code in _PRECONDITION_FAILED or _status_code(exc) == 412:
                    outcome = "content_changed"
                else:
                    # A timeout or unrecognized result can follow a successful
                    # delete. Keep the immutable request and leave it unresolved.
                    raise gc.MaskGcFailure("storage_unavailable") from None
            except BotoCoreError:
                raise gc.MaskGcFailure("storage_unavailable") from None
            except Exception:
                raise gc.MaskGcFailure("storage_unavailable") from None
            else:
                outcome = "deleted"

        terminal = _status(request, "completed", outcome)
        return self._put_or_read_receipt(receipt_key, request, terminal)

    def status(self, request: gc.DeleteStatusRequest) -> gc.DeleteOperationStatus:
        request = _validated_request(gc.DeleteStatusRequest, request)
        request_key, receipt_key = self._journal_keys(request.operation_id)
        operation = self._read_request(request_key)
        if operation is None:
            return gc.DeleteOperationStatus(
                operation_id=request.operation_id,
                request=None,
                state="not_found",
                outcome=None,
            )
        return self._status_for_claim(operation, receipt_key)

    def _status_for_claim(
        self, request: gc.DeleteOperationRequest, receipt_key: str
    ) -> gc.DeleteOperationStatus:
        receipt = self._read_receipt(receipt_key)
        if receipt is not None:
            if receipt.request != request:
                raise gc.MaskGcFailure("invalid_response")
            return receipt

        # No receipt means DeleteObject must never be retried. A fresh HEAD can
        # safely settle an already-missing or changed object; a still-matching
        # object is indeterminate because the first process may have been in
        # flight when its reply was lost.
        outcome = self._inspect_payload(request)
        if outcome == "same":
            return _status(request, "indeterminate")
        terminal = _status(request, "completed", outcome)
        return self._put_or_read_receipt(receipt_key, request, terminal)

    def _inspect_payload(self, request: gc.DeleteOperationRequest) -> str:
        try:
            current = self.client.head_object(
                Bucket=self.bucket,
                Key=request.object_key,
            )
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND:
                return "missing"
            raise gc.MaskGcFailure("storage_unavailable") from None
        except BotoCoreError:
            raise gc.MaskGcFailure("storage_unavailable") from None
        except Exception:
            raise gc.MaskGcFailure("storage_unavailable") from None

        if not isinstance(current, dict):
            raise gc.MaskGcFailure("invalid_response")
        raw_etag = current.get("ETag")
        modified = current.get("LastModified")
        if (
            not isinstance(raw_etag, str)
            or not isinstance(modified, datetime)
            or modified.tzinfo is None
        ):
            return "content_changed"
        etag = raw_etag.strip('"')
        if etag != request.etag or _utc(modified).replace(microsecond=0) != _utc(
            request.last_modified
        ).replace(microsecond=0):
            return "content_changed"
        return "same"

    def _read_request(self, key: str) -> gc.DeleteOperationRequest | None:
        raw = self._read_journal_object(key)
        if raw is None:
            return None
        try:
            value = gc.parse_json(raw)
            request = gc.DeleteOperationRequest.model_validate(value)
            if self._journal_keys(request.operation_id)[0] != key:
                raise ValueError("request journal key does not match operation ID")
            return request
        except (TypeError, ValueError, RecursionError):
            raise gc.MaskGcFailure("invalid_response") from None

    def _read_receipt(self, key: str) -> gc.DeleteOperationStatus | None:
        raw = self._read_journal_object(key)
        if raw is None:
            return None
        try:
            value = gc.parse_json(raw)
            receipt = gc.DeleteOperationStatus.model_validate(value)
            if receipt.state != "completed" or receipt.request is None:
                raise ValueError("journal receipt is not terminal")
            if self._journal_keys(receipt.operation_id)[1] != key:
                raise ValueError("receipt journal key does not match operation ID")
            return receipt
        except (TypeError, ValueError, RecursionError):
            raise gc.MaskGcFailure("invalid_response") from None

    def _read_journal_object(self, key: str) -> bytes | None:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND:
                return None
            raise gc.MaskGcFailure("storage_unavailable") from None
        except BotoCoreError:
            raise gc.MaskGcFailure("storage_unavailable") from None
        except Exception:
            raise gc.MaskGcFailure("storage_unavailable") from None

        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND:
                # S3 is strongly consistent; HEAD then GET disagreement is a
                # provider/protocol failure, not a new absence result.
                raise gc.MaskGcFailure("storage_unavailable") from None
            raise gc.MaskGcFailure("storage_unavailable") from None
        except BotoCoreError:
            raise gc.MaskGcFailure("storage_unavailable") from None
        except Exception:
            raise gc.MaskGcFailure("storage_unavailable") from None
        if not isinstance(response, dict):
            raise gc.MaskGcFailure("invalid_response")
        body = response.get("Body")
        if body is None:
            raise gc.MaskGcFailure("invalid_response")
        try:
            raw = body.read(_MAX_JOURNAL_BYTES + 1)
        except Exception:
            raise gc.MaskGcFailure("storage_unavailable") from None
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        if not isinstance(raw, (bytes, bytearray)) or len(raw) > _MAX_JOURNAL_BYTES:
            raise gc.MaskGcFailure("invalid_response")
        return bytes(raw)

    def _put_or_read_receipt(
        self,
        key: str,
        request: gc.DeleteOperationRequest,
        receipt: gc.DeleteOperationStatus,
    ) -> gc.DeleteOperationStatus:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=gc.json_bytes(receipt),
                ContentType="application/json",
                IfNoneMatch="*",
            )
            return receipt
        except ClientError as exc:
            if _error_code(exc) not in _CONDITIONAL_CONFLICT:
                raise gc.MaskGcFailure("storage_unavailable") from None
            existing = self._read_receipt(key)
            if existing is None or existing.request != request:
                raise gc.MaskGcFailure("invalid_response") from None
            return existing
        except gc.MaskGcFailure:
            raise
        except BotoCoreError:
            # Never claim the terminal status was durable when receipt PUT may
            # have failed after the provider accepted it.
            raise gc.MaskGcFailure("storage_unavailable") from None
        except Exception:
            raise gc.MaskGcFailure("storage_unavailable") from None

    def _require_safe_execute_client(self) -> None:
        meta = getattr(self.client, "meta", None)
        config = getattr(meta, "config", None)
        retries = getattr(config, "retries", None)
        if (
            getattr(config, "connect_timeout", None) != 3
            or getattr(config, "read_timeout", None) != 8
            or not isinstance(retries, dict)
            or retries.get("total_max_attempts") != 1
        ):
            raise gc.MaskGcFailure("storage_unavailable")

    def _journal_keys(self, operation_id) -> tuple[str, str]:
        prefix = f"{gc.GC_JOURNAL_PREFIX}/{operation_id}"
        return f"{prefix}/request.json", f"{prefix}/receipt.json"


def _status_code(exc: ClientError) -> int | None:
    value = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return value if type(value) is int else None
