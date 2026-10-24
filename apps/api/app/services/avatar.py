"""头像引用的唯一语法归属。

头像在 ``users.avatar_ref`` 里只存一个可空字符串，取值两种：

- ``preset:<slug>`` —— 内置像素头像，slug 指向 ``apps/web/public/avatars/pixel/``
  下由生成脚本产出的静态 SVG；用户可自助选择。
- ``upload:<32位hex>`` —— 用户上传并已被服务端规范化的 WebP，对象 key 为
  ``{token}.webp``（见对象存储 ``avatars`` 桶）；**只能由上传端点写入**，客户端
  提交该前缀一律拒绝，因此无法引用他人的图片。

``NULL`` 表示回退到首字母圆片（历史行为）。

解析与构造集中在这里，避免语法散落到端点、schema 与前端解析各自实现一套。
"""

from __future__ import annotations

import re
import uuid

import hashlib
from contextlib import nullcontext
from copy import copy

from botocore.exceptions import ClientError

from app.services import avatar_storage, storage_access
from app.services.storage import storage_service
from app.services.avatar_storage_target import object_params, version_id, _prove_absent
from app.services.dataset_assets import LEGACY_LOCATION_ID

PRESET_PREFIX = "preset:"
UPLOAD_PREFIX = "upload:"

# 严格白名单顺带挡掉路径穿越（/ . ? #）与任意字符串进入 <img src>。
_PRESET_SLUG_RE = re.compile(r"[a-z0-9-]{1,40}")
_UPLOAD_TOKEN_RE = re.compile(r"[0-9a-f]{32}")

# 头像按 24h 强缓存；token 每次上传都是新的,故不存在 stale 问题。
AVATAR_CACHE_CONTROL = "public, max-age=86400, stale-while-revalidate=604800"


class InvalidAvatarRef(ValueError):
    """客户端提交了语法不合法,或无权自选(``upload:``)的头像引用。"""


def new_upload_token() -> str:
    return uuid.uuid4().hex


def build_upload_ref(token: str) -> str:
    return f"{UPLOAD_PREFIX}{token}"


def build_preset_ref(slug: str) -> str:
    return f"{PRESET_PREFIX}{slug}"


def parse_ref(ref: str | None) -> tuple[str, str] | None:
    """返回 ``(kind, value)``；``kind`` ∈ {preset, upload}；None = 无头像。"""
    if not ref:
        return None
    if ref.startswith(PRESET_PREFIX):
        slug = ref[len(PRESET_PREFIX) :]
        if _PRESET_SLUG_RE.fullmatch(slug):
            return ("preset", slug)
        return None
    if ref.startswith(UPLOAD_PREFIX):
        token = ref[len(UPLOAD_PREFIX) :]
        if _UPLOAD_TOKEN_RE.fullmatch(token):
            return ("upload", token)
        return None
    return None


def is_upload_token(token: str) -> bool:
    return bool(_UPLOAD_TOKEN_RE.fullmatch(token))


def upload_object_key(token: str) -> str:
    return avatar_storage.object_key_for(token)


def validate_client_ref(ref: str | None) -> str | None:
    """校验用户可自助写入的引用：``preset:<slug>`` 或 None(清空)。

    ``upload:`` 即使语法合法也拒绝——上传对象只能由上传端点产生，这样用户无法把
    别人的 token 挂到自己名下。内置头像是否存在由前端 manifest 决定；这里只保证语法，
    指向已下线 slug 的引用在前端表现为破图回退首字母，不影响数据一致性。
    """
    if ref is None or ref == "":
        return None
    if ref.startswith(UPLOAD_PREFIX):
        raise InvalidAvatarRef("上传头像不可由客户端直接指定")
    slug = ref[len(PRESET_PREFIX) :] if ref.startswith(PRESET_PREFIX) else None
    if slug is None or not _PRESET_SLUG_RE.fullmatch(slug):
        raise InvalidAvatarRef("头像引用格式不正确")
    return build_preset_ref(slug)


def _client_context(owner, target):
    ref = owner.ref
    if target is None:
        if ref["location_id"] != str(LEGACY_LOCATION_ID):
            raise avatar_storage.AvatarStorageError("avatar_storage_binding_unproven")
        return nullcontext(storage_service.client)
    if (
        str(target.location_id) != ref["location_id"]
        or target.endpoint_url != owner.endpoint_url
        or target.bucket != owner.bucket
        or target.key_prefix + owner.object_key.rsplit("/", 1)[-1] != owner.object_key
    ):
        raise avatar_storage.AvatarStorageError("avatar_storage_identity_changed")
    return target.client()


def write_avatar_object(record, data: bytes, *, target=None) -> str | None:
    """Write only the exact saved object, then independently verify its bytes."""
    owner = avatar_storage.resolve(record)
    if (
        len(data) != record.size_bytes
        or hashlib.sha256(data).hexdigest() != record.sha256
    ):
        raise avatar_storage.AvatarStorageError("avatar_storage_invalid_receipt")
    with _client_context(owner, target) as client:
        response = client.put_object(
            Bucket=owner.bucket,
            Key=owner.object_key,
            Body=data,
            IfNoneMatch="*",
            ContentType="image/webp",
            CacheControl=AVATAR_CACHE_CONTROL,
        )
        receipt = copy(record)
        receipt.object_version_id = version_id(response)
        if _read(client, owner, receipt) is None:
            raise avatar_storage.AvatarStorageError("avatar_storage_invalid_receipt")
    return receipt.object_version_id


def read_avatar_object(record, *, target=None) -> tuple[bytes, str] | None:
    """Bound a full read by the independent receipt; always close its body."""
    owner = avatar_storage.resolve(record)
    with _client_context(owner, target) as client:
        return _read(client, owner, record)


def _read(client, owner, record):
    version = getattr(record, "object_version_id", None)
    try:
        response = client.get_object(
            **object_params(owner.bucket, owner.object_key, version)
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {
            "404",
            "NoSuchKey",
            "NoSuchVersion",
            "NotFound",
        }:
            return None
        raise
    with storage_access.closing_registered(response["Body"]) as body:
        if (
            (version is not None and response.get("VersionId") != version)
            or response.get("ContentRange") is not None
            or response.get("DeleteMarker")
        ):
            raise avatar_storage.AvatarStorageError("avatar_storage_invalid_receipt")
        checksum = hashlib.sha256()
        chunks, size = [], 0
        while True:
            chunk = body.read(min(64 * 1024, record.size_bytes - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > record.size_bytes:
                raise avatar_storage.AvatarStorageError(
                    "avatar_storage_invalid_receipt"
                )
            checksum.update(chunk)
            chunks.append(chunk)
        if size != record.size_bytes or checksum.hexdigest() != record.sha256:
            raise avatar_storage.AvatarStorageError("avatar_storage_invalid_receipt")
    return b"".join(chunks), f'"{record.sha256}"'


def delete_avatar_object(record, *, target=None) -> None:
    """The caller owns the committed retirement; never infer its current bucket."""
    owner = avatar_storage.resolve(record)
    version = getattr(record, "object_version_id", None)
    with _client_context(owner, target) as client:
        params = object_params(owner.bucket, owner.object_key, version)
        if (
            target is not None
            and version is None
            and _read(client, owner, record) is None
        ):
            return
        client.delete_object(**params)
        _prove_absent(client, params)
