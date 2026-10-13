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

import logging
import re
import uuid

from botocore.exceptions import ClientError

from app.services.storage import storage_service

logger = logging.getLogger(__name__)

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
    return f"{token}.webp"


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


def write_avatar_object(token: str, data: bytes) -> None:
    storage_service.put_bytes(
        upload_object_key(token),
        data,
        content_type="image/webp",
        cache_control=AVATAR_CACHE_CONTROL,
        bucket=storage_service.avatars_bucket,
    )


def read_avatar_object(token: str) -> tuple[bytes, str] | None:
    """读取头像字节与 ETag；对象不存在返回 None。"""
    key = upload_object_key(token)
    try:
        head = storage_service.client.head_object(
            Bucket=storage_service.avatars_bucket, Key=key
        )
    except ClientError:
        return None
    response = storage_service.client.get_object(
        Bucket=storage_service.avatars_bucket, Key=key
    )
    body = response["Body"]
    try:
        data = body.read()
    finally:
        body.close()
    return data, str(head.get("ETag") or "")


def delete_ref_object(ref: str | None) -> None:
    """删除某个引用指向的上传对象(若存在)。

    尽力而为：清理失败只记日志，不影响调用方已完成的头像变更；遗留的是一个几十 KB
    的无引用孤儿对象，运维可按「桶内 key 不在 users.avatar_ref 中」比对清理。
    """
    parsed = parse_ref(ref)
    if parsed is None or parsed[0] != "upload":
        return
    try:
        storage_service.delete_object(
            upload_object_key(parsed[1]), bucket=storage_service.avatars_bucket
        )
    except Exception as exc:  # noqa: BLE001 - 清理失败不得影响主流程
        logger.warning("Failed to delete avatar object for %s: %s", ref, exc)
