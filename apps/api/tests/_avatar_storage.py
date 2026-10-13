"""Shared fake object storage for avatar tests.

``services/avatar.py`` reaches object storage through the ``storage_service`` singleton, so
the tests here swap that symbol for this fake. It records writes/deletes so tests can assert
both the normalized output and the cleanup of replaced avatars.
"""

from __future__ import annotations

import io
from botocore.exceptions import ClientError


def _missing(key: str) -> ClientError:
    return ClientError({"Error": {"Code": "NoSuchKey", "Message": key}}, "HeadObject")


class _FakeS3Client:
    def __init__(self, owner: "FakeAvatarStorage") -> None:
        self._owner = owner

    def head_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803 - boto3 kwargs
        try:
            data = self._owner.objects[(Bucket, Key)]
        except KeyError as exc:
            raise _missing(Key) from exc
        return {"ETag": self._owner.etags[(Bucket, Key)], "ContentLength": len(data)}

    def get_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803
        try:
            data = self._owner.objects[(Bucket, Key)]
        except KeyError as exc:
            raise _missing(Key) from exc
        return {"Body": io.BytesIO(data), "ETag": self._owner.etags[(Bucket, Key)]}


class FakeAvatarStorage:
    def __init__(self, *, avatars_bucket: str = "test-avatars") -> None:
        self.bucket = "test-annotations"
        self.avatars_bucket = avatars_bucket
        self.objects: dict[tuple[str, str], bytes] = {}
        self.etags: dict[tuple[str, str], str] = {}
        self.puts: list[dict] = []
        self.deleted: list[tuple[str, str]] = []
        self.client = _FakeS3Client(self)
        self.fail_deletes = False

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
        bucket: str | None = None,
    ) -> None:
        target = bucket or self.bucket
        self.objects[(target, key)] = data
        self.etags[(target, key)] = f'"{key}"'
        self.puts.append(
            {
                "bucket": target,
                "key": key,
                "content_type": content_type,
                "cache_control": cache_control,
                "size": len(data),
            }
        )

    def delete_object(self, key: str, bucket: str | None = None) -> None:
        if self.fail_deletes:
            raise RuntimeError("storage unavailable")
        target = bucket or self.bucket
        self.objects.pop((target, key), None)
        self.deleted.append((target, key))

    def seed(self, key: str, data: bytes, *, bucket: str | None = None) -> None:
        """Insert an object as if a previous upload had stored it."""
        target = bucket or self.avatars_bucket
        self.objects[(target, key)] = data
        self.etags[(target, key)] = f'"{key}"'
