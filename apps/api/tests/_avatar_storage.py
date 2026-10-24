"""Saved avatar receipts and fake object storage with independent byte evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
from types import SimpleNamespace
import uuid

from botocore.exceptions import ClientError

from app.config import settings
from app.services import avatar, avatar_storage


def _missing(key: str) -> ClientError:
    return ClientError({"Error": {"Code": "NoSuchKey", "Message": key}}, "GetObject")


class _FakeS3Client:
    def __init__(self, owner: "FakeAvatarStorage") -> None:
        self._owner = owner
        scheme = "https" if settings.minio_use_ssl else "http"
        self.meta = SimpleNamespace(
            endpoint_url=f"{scheme}://{settings.minio_endpoint}"
        )

    def get_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803
        self._owner.reads.append((Bucket, Key))
        try:
            data = self._owner.objects[(Bucket, Key)]
        except KeyError as exc:
            raise _missing(Key) from exc
        body = self._owner.body_factory(data)
        self._owner.bodies.append(body)
        return {"Body": body, "ETag": self._owner.etags[(Bucket, Key)]}


class FakeAvatarStorage:
    def __init__(self, *, avatars_bucket: str | None = None) -> None:
        self.bucket = settings.minio_bucket
        self.avatars_bucket = avatars_bucket or settings.minio_avatars_bucket
        self.objects: dict[tuple[str, str], bytes] = {}
        self.etags: dict[tuple[str, str], str] = {}
        self.puts: list[dict] = []
        self.reads: list[tuple[str, str]] = []
        self.bodies: list[io.BytesIO] = []
        self.deleted: list[tuple[str, str]] = []
        self.client = _FakeS3Client(self)
        self.fail_deletes = False
        self.fail_puts = False
        self.fail_after_put = False
        self.body_factory = io.BytesIO

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
        bucket: str | None = None,
    ) -> None:
        if self.fail_puts:
            raise RuntimeError("storage unavailable with secret signature")
        target = bucket or self.bucket
        self.seed(key, data, bucket=target)
        self.puts.append(
            {
                "bucket": target,
                "key": key,
                "content_type": content_type,
                "cache_control": cache_control,
                "size": len(data),
            }
        )
        if self.fail_after_put:
            raise RuntimeError("unknown PUT response with secret signature")

    def delete_object(self, key: str, bucket: str | None = None) -> None:
        if self.fail_deletes:
            raise RuntimeError("storage unavailable with secret signature")
        target = bucket or self.bucket
        self.objects.pop((target, key), None)
        self.deleted.append((target, key))

    def seed(self, key: str, data: bytes, *, bucket: str | None = None) -> None:
        target = bucket or self.avatars_bucket
        self.objects[(target, key)] = data
        self.etags[(target, key)] = f'"{hashlib.sha256(data).hexdigest()}"'


def patch_storage(monkeypatch, fake=None):
    fake = fake or FakeAvatarStorage()
    monkeypatch.setattr(avatar, "storage_service", fake)
    monkeypatch.setattr(avatar_storage, "storage_service", fake)
    return fake


async def seed_upload(
    db, fake, token, data, *, owner_id=None, state="ready", store=True
):
    from app.db.models.avatar_upload import AvatarUpload

    row = AvatarUpload(
        token=token,
        owner_id=owner_id or uuid.uuid4(),
        storage_ref=await avatar_storage.allocate(db, token),
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        state=state,
        retired_at=datetime.now(timezone.utc) if state == "retired" else None,
    )
    db.add(row)
    await db.flush()
    if store:
        fake.seed(row.storage_ref["object_key"], data, bucket=row.storage_ref["bucket"])
    return row
