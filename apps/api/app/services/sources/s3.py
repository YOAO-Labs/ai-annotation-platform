from __future__ import annotations

from datetime import datetime
from typing import BinaryIO, Iterable, Iterator

import boto3
from botocore.config import Config as BotoConfig

from app.services.storage_access import close_registered, mark_unsettled
from app.services.sources.base import (
    SourceAdapter,
    SourceObject,
    SourcePathError,
    matches_include_globs,
    normalize_relpath,
)


def _under_prefix(prefix: str, key: str) -> bool:
    return not prefix or key == prefix or key.startswith(f"{prefix}/")


def _join_under_base(base_prefix: str, path: str | None) -> str:
    base = normalize_relpath(base_prefix)
    requested = normalize_relpath(path)
    if not requested:
        return base
    if base and _under_prefix(base, requested):
        return requested
    return f"{base}/{requested}" if base else requested


def validate_s3_source_path(config: dict, path: str | None) -> None:
    _join_under_base(str((config or {}).get("base_prefix") or ""), path)


class S3CompatibleSource(SourceAdapter):
    def __init__(self, config: dict, secret: dict) -> None:
        scheme = "https" if config.get("use_ssl") else "http"
        endpoint = str(config["endpoint"])
        if "://" not in endpoint:
            endpoint = f"{scheme}://{endpoint}"

        self.bucket = str(config["bucket"])
        self.base_prefix = normalize_relpath(str(config.get("base_prefix") or ""))
        try:
            self.client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=secret.get("access_key"),
                aws_secret_access_key=secret.get("secret_key"),
                region_name=config.get("region"),
                config=BotoConfig(
                    connect_timeout=10, read_timeout=60, retries={"max_attempts": 2}
                ),
            )
        except BaseException as exc:
            raise mark_unsettled(exc)

    def _rel_from_key(self, key: str) -> str:
        key = normalize_relpath(key)
        if self.base_prefix and key.startswith(f"{self.base_prefix}/"):
            return key[len(self.base_prefix) + 1 :]
        if self.base_prefix and key == self.base_prefix:
            return ""
        return key

    def _key_for_relpath(self, relpath: str) -> str:
        rel = normalize_relpath(relpath)
        if not rel:
            raise SourcePathError("relpath 不能为空")
        return f"{self.base_prefix}/{rel}" if self.base_prefix else rel

    def list(
        self,
        path: str,
        recursive: bool,
        include_globs: Iterable[str] | None = None,
    ) -> Iterator[SourceObject]:
        prefix = _join_under_base(self.base_prefix, path)
        continuation_token: str | None = None
        while True:
            kwargs: dict = {"Bucket": self.bucket, "Prefix": prefix}
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            resp = self.client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []):
                key = str(obj.get("Key") or "")
                if not key or key.endswith("/"):
                    continue
                normalized_key = normalize_relpath(key)
                if prefix and not _under_prefix(prefix, normalized_key):
                    continue
                if not recursive:
                    below_source = (
                        normalized_key[len(prefix) :].lstrip("/")
                        if prefix
                        else normalized_key
                    )
                    if "/" in below_source:
                        continue

                relpath = self._rel_from_key(normalized_key)
                if not relpath or not matches_include_globs(relpath, include_globs):
                    continue
                mtime = obj.get("LastModified")
                yield SourceObject(
                    relpath=relpath,
                    size=int(obj.get("Size") or 0),
                    mtime=mtime if isinstance(mtime, datetime) else None,
                    etag=(str(obj.get("ETag") or "").strip('"') or None),
                )
            if not resp.get("IsTruncated"):
                break
            continuation_token = resp.get("NextContinuationToken")

    def open(self, relpath: str) -> BinaryIO:
        key = self._key_for_relpath(relpath)
        resp = self.client.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"]

    def close(self) -> None:
        close_registered(self.client)
