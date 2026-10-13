"""头像图片的服务端规范化（纯函数，不碰 IO）。

上传的头像一律经此收敛为「256×256、方形、无 EXIF 方向、RGB、WebP」的确定性产物，
因此下游（对象存储、``GET /api/v1/avatars/{token}``、前端）永远只面对一种形状。

安全约束都在这里：
- 不信任客户端 ``content_type``，以 Pillow 解出的真实格式为准；
- 显式像素上限挡解压炸弹（Pillow 自带的 bomb 检查按全局阈值，这里再按业务阈值收一次）；
- 透明通道按中性底色合成，避免 PNG→RGB 直接转出黑底。
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps, UnidentifiedImageError

# 请求体上限（与 middleware/upload_body_limits.MAX_AVATAR_FILE_BYTES 保持一致）。
MAX_AVATAR_FILE_BYTES = 2 * 1024 * 1024
# 解码前的像素上限：2MB 的恶意 PNG 可以解出上亿像素。
MAX_AVATAR_PIXELS = 40_000_000
# 低于该边长的图放大会糊，直接拒绝并让用户换图。
MIN_AVATAR_EDGE = 32

AVATAR_OUTPUT_SIZE = 256
AVATAR_WEBP_QUALITY = 90

ALLOWED_IMAGE_FORMATS = {"PNG", "JPEG", "WEBP"}
# 客户端声明的 MIME 只作早退；空值 / octet-stream 交给 Pillow 判定。
ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
NEUTRAL_BACKDROP = (244, 244, 245)  # 与浅色主题 --sc-muted 一致


class AvatarImageError(ValueError):
    """图片不合法或不符合头像要求；消息面向终端用户。"""


def _flatten_transparency(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA", "P"}:
        rgba = image.convert("RGBA")
        backdrop = Image.new("RGB", rgba.size, NEUTRAL_BACKDROP)
        backdrop.paste(rgba, mask=rgba.split()[-1])
        return backdrop
    return image.convert("RGB")


def normalize_avatar(raw: bytes) -> bytes:
    """把任意受支持的上传图片规范化为方形 WebP 字节。

    失败抛 :class:`AvatarImageError`，调用方转成 4xx；成功时保证值确定性，便于单测。
    """
    if not raw:
        raise AvatarImageError("图片内容为空")
    if len(raw) > MAX_AVATAR_FILE_BYTES:
        raise AvatarImageError("图片不能超过 2 MB")

    try:
        image = Image.open(io.BytesIO(raw))
    except UnidentifiedImageError as exc:
        raise AvatarImageError("不是有效的图片文件") from exc
    except Image.DecompressionBombError as exc:
        raise AvatarImageError("图片像素过多，请先压缩后重试") from exc

    with image:
        if image.format not in ALLOWED_IMAGE_FORMATS:
            raise AvatarImageError("仅支持 PNG / JPEG / WebP 格式")
        width, height = image.size
        if width * height > MAX_AVATAR_PIXELS:
            raise AvatarImageError("图片像素过多，请先压缩后重试")
        if min(width, height) < MIN_AVATAR_EDGE:
            raise AvatarImageError(f"图片边长不能小于 {MIN_AVATAR_EDGE} 像素")
        try:
            image.load()
        except Image.DecompressionBombError as exc:
            raise AvatarImageError("图片像素过多，请先压缩后重试") from exc
        except OSError as exc:
            raise AvatarImageError("图片已损坏或不完整") from exc

        upright = ImageOps.exif_transpose(image) or image
        flattened = _flatten_transparency(upright)

        edge = min(flattened.size)
        left = (flattened.size[0] - edge) // 2
        top = (flattened.size[1] - edge) // 2
        square = flattened.crop((left, top, left + edge, top + edge))
        resized = square.resize(
            (AVATAR_OUTPUT_SIZE, AVATAR_OUTPUT_SIZE), Image.Resampling.LANCZOS
        )

        buffer = io.BytesIO()
        resized.save(
            buffer,
            format="WEBP",
            quality=AVATAR_WEBP_QUALITY,
            method=6,
        )
        return buffer.getvalue()
