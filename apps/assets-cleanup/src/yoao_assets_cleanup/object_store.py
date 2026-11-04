"""Delete only UUID-scoped image prefixes using explicit credentials."""

from __future__ import annotations

import re
import threading

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .config import CleanupConfig

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ASSET_PREFIX = re.compile(
    rf"^(?:image-pyramids/{_UUID}/|"
    rf"image-assets/{_UUID}/g[1-9][0-9]*/a{_UUID}/|"
    rf"image-pyramids/{_UUID}/g[1-9][0-9]*/|"
    rf"image-pyramids/{_UUID}/g[1-9][0-9]*/a{_UUID}/)$"
)
_LIST_PREFIXES = ("image-assets/", "image-pyramids/")


class StorageFailure(RuntimeError):
    """Object storage could not complete the exact requested operation."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class CleanupStopped(RuntimeError):
    """A shutdown interrupted a multi-page prefix delete before acknowledgement."""


class S3PrefixDeleter:
    def __init__(self, config: CleanupConfig):
        self.bucket = config.bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=config.s3_endpoint,
            region_name=config.s3_region,
            aws_access_key_id=config.s3_access_key,
            aws_secret_access_key=config.s3_secret_key,
            config=Config(
                connect_timeout=3,
                read_timeout=15,
                retries={"mode": "standard", "total_max_attempts": 2},
                max_pool_connections=2,
            ),
        )

    def probe(self) -> None:
        try:
            for prefix in _LIST_PREFIXES:
                self.client.list_objects_v2(
                    Bucket=self.bucket,
                    Prefix=prefix,
                    MaxKeys=1,
                )
        except Exception as exc:
            raise StorageFailure("s3_scope_probe_failed") from exc

    def delete_prefix(self, prefix: str, stop: threading.Event) -> int:
        if _ASSET_PREFIX.fullmatch(prefix) is None:
            raise StorageFailure("invalid_asset_prefix")

        deleted = 0
        continuation_token: str | None = None
        while True:
            if stop.is_set():
                raise CleanupStopped("shutdown before the prefix was fully deleted")
            request: dict[str, object] = {
                "Bucket": self.bucket,
                "Prefix": prefix,
                "MaxKeys": 1000,
            }
            if continuation_token is not None:
                request["ContinuationToken"] = continuation_token
            try:
                page = self.client.list_objects_v2(**request)
                objects = [
                    {"Key": item["Key"]}
                    for item in page.get("Contents", [])
                    if isinstance(item.get("Key"), str)
                    and item["Key"].startswith(prefix)
                ]
                if len(objects) != len(page.get("Contents", [])):
                    raise StorageFailure("s3_list_returned_out_of_scope_key")
                if objects:
                    result = self.client.delete_objects(
                        Bucket=self.bucket,
                        Delete={"Objects": objects, "Quiet": True},
                    )
                    errors = result.get("Errors", [])
                    if errors:
                        raise StorageFailure(f"s3_delete_rejected_{len(errors)}")
                    deleted += len(objects)
            except StorageFailure:
                raise
            except ClientError as exc:
                error = exc.response.get("Error", {})
                code = str(error.get("Code", "s3_request_failed"))
                raise StorageFailure(code[:64]) from exc
            except Exception as exc:
                raise StorageFailure("s3_request_failed") from exc

            if not page.get("IsTruncated"):
                return deleted
            continuation_token = page.get("NextContinuationToken")
            if not isinstance(continuation_token, str) or not continuation_token:
                raise StorageFailure("s3_continuation_missing")
            if stop.is_set():
                raise CleanupStopped("shutdown before the prefix was fully deleted")
