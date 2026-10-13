"""头像上传 / 选择 / 清除 与图片规范化。

覆盖:
1. ``services/avatar_image.normalize_avatar`` 纯函数(方裁 / EXIF 转正 / 透明合成 / 各类拒绝)
2. ``POST|PATCH|DELETE /api/v1/auth/me/avatar`` 契约与旧对象清理
3. ``UserBrief.avatar_ref`` 随用户身份 payload 下发
"""

from __future__ import annotations

import io
import re

import pytest
from PIL import Image

from app.services import avatar as avatar_service
from app.services.avatar_image import (
    MAX_AVATAR_FILE_BYTES,
    AvatarImageError,
    normalize_avatar,
)
from tests._avatar_storage import FakeAvatarStorage

AVATAR_URL = "/api/v1/auth/me/avatar"
UPLOAD_REF_RE = re.compile(r"^upload:[0-9a-f]{32}$")

RED = (220, 30, 30)
BLUE = (30, 30, 220)
NEUTRAL = (244, 244, 245)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _png_bytes(size: tuple[int, int] = (600, 300), color=RED) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_bytes_with_orientation(orientation: int) -> bytes:
    """左半红 / 右半蓝的 300×100 JPEG,附 EXIF 方向标记。"""
    image = Image.new("RGB", (300, 100), BLUE)
    image.paste(RED, (0, 0, 150, 100))
    exif = image.getexif()
    exif[274] = orientation
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def _pixel(webp: bytes, x: int, y: int) -> tuple[int, int, int]:
    with Image.open(io.BytesIO(webp)) as image:
        return image.convert("RGB").getpixel((x, y))


@pytest.fixture
def fake_storage(monkeypatch) -> FakeAvatarStorage:
    fake = FakeAvatarStorage()
    monkeypatch.setattr(avatar_service, "storage_service", fake)
    return fake


# ── 1. normalize_avatar 纯函数 ────────────────────────────────────────


def test_normalize_produces_square_webp():
    out = normalize_avatar(_png_bytes((600, 300)))
    with Image.open(io.BytesIO(out)) as image:
        assert image.format == "WEBP"
        assert image.size == (256, 256)
    # 600×300 居中取 300×300,故左右两侧的纯色块取样都应是红色。
    assert _pixel(out, 4, 128) == pytest.approx(RED, abs=12)
    assert _pixel(out, 251, 128) == pytest.approx(RED, abs=12)


def test_normalize_applies_exif_orientation():
    """orientation=6 (顺时针 90°) 必须被转正;不转正时方裁结果会完全不同。"""
    raw = _jpeg_bytes_with_orientation(6)
    # 解码前先确认源图确实是横图,否则本用例失去意义。
    with Image.open(io.BytesIO(raw)) as src:
        assert src.size == (300, 100)

    out = normalize_avatar(raw)
    top = _pixel(out, 128, 40)
    bottom = _pixel(out, 128, 216)
    assert top[0] > top[2], f"转正后上半应为红,实际 {top}"
    assert bottom[2] > bottom[0], f"转正后下半应为蓝,实际 {bottom}"


def test_normalize_flattens_transparency_onto_neutral_backdrop():
    buffer = io.BytesIO()
    Image.new("RGBA", (128, 128), (0, 0, 0, 0)).save(buffer, format="PNG")
    out = normalize_avatar(buffer.getvalue())
    # 直接 RGBA→RGB 会得到黑底;这里必须是中性底色。
    assert _pixel(out, 64, 64) == pytest.approx(NEUTRAL, abs=6)


def test_normalize_rejects_invalid_inputs():
    with pytest.raises(AvatarImageError):
        normalize_avatar(b"")
    with pytest.raises(AvatarImageError):
        normalize_avatar(b"definitely not an image")
    with pytest.raises(AvatarImageError):
        normalize_avatar(_png_bytes((16, 16)))
    with pytest.raises(AvatarImageError):
        normalize_avatar(b"x" * (MAX_AVATAR_FILE_BYTES + 1))


def test_normalize_rejects_unsupported_format():
    buffer = io.BytesIO()
    Image.new("P", (128, 128)).save(buffer, format="GIF")
    with pytest.raises(AvatarImageError, match="PNG / JPEG / WebP"):
        normalize_avatar(buffer.getvalue())


def test_normalize_rejects_truncated_image():
    raw = _png_bytes((128, 128))
    with pytest.raises(AvatarImageError):
        normalize_avatar(raw[: len(raw) // 2])


# ── 2. 上传 / 选择 / 清除 ────────────────────────────────────────────


async def test_upload_avatar_stores_normalized_webp(
    httpx_client, annotator, fake_storage
):
    user, token = annotator
    response = await httpx_client.post(
        AVATAR_URL,
        files={"file": ("me.png", _png_bytes((600, 300)), "image/png")},
        headers=_bearer(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert UPLOAD_REF_RE.match(body["avatar_ref"])

    upload_token = body["avatar_ref"].split(":", 1)[1]
    stored = fake_storage.objects[(fake_storage.avatars_bucket, f"{upload_token}.webp")]
    with Image.open(io.BytesIO(stored)) as image:
        assert image.format == "WEBP"
        assert image.size == (256, 256)
    assert fake_storage.puts[-1]["bucket"] == fake_storage.avatars_bucket
    assert fake_storage.puts[-1]["content_type"] == "image/webp"
    assert "max-age" in fake_storage.puts[-1]["cache_control"]


async def test_upload_avatar_replaces_and_deletes_previous_object(
    httpx_client, annotator, fake_storage
):
    _, token = annotator
    first = await httpx_client.post(
        AVATAR_URL,
        files={"file": ("a.png", _png_bytes((300, 300)), "image/png")},
        headers=_bearer(token),
    )
    first_token = first.json()["avatar_ref"].split(":", 1)[1]

    second = await httpx_client.post(
        AVATAR_URL,
        files={"file": ("b.png", _png_bytes((300, 300), BLUE), "image/png")},
        headers=_bearer(token),
    )
    assert second.status_code == 200
    assert (fake_storage.avatars_bucket, f"{first_token}.webp") in fake_storage.deleted
    assert (
        fake_storage.avatars_bucket,
        f"{first_token}.webp",
    ) not in fake_storage.objects


async def test_upload_avatar_rejects_bad_input_without_writing(
    httpx_client, annotator, fake_storage
):
    _, token = annotator
    not_an_image = await httpx_client.post(
        AVATAR_URL,
        files={"file": ("x.png", b"plain text", "image/png")},
        headers=_bearer(token),
    )
    assert not_an_image.status_code == 400

    wrong_type = await httpx_client.post(
        AVATAR_URL,
        files={"file": ("x.pdf", _png_bytes(), "application/pdf")},
        headers=_bearer(token),
    )
    assert wrong_type.status_code == 415

    tiny = await httpx_client.post(
        AVATAR_URL,
        files={"file": ("x.png", _png_bytes((16, 16)), "image/png")},
        headers=_bearer(token),
    )
    assert tiny.status_code == 400

    oversized = await httpx_client.post(
        AVATAR_URL,
        files={
            "file": ("x.png", b"x" * (MAX_AVATAR_FILE_BYTES + 1), "image/png"),
        },
        headers=_bearer(token),
    )
    assert oversized.status_code == 413

    assert fake_storage.puts == []

    me = await httpx_client.get("/api/v1/auth/me", headers=_bearer(token))
    assert me.json()["avatar_ref"] is None


async def test_patch_avatar_selects_preset_and_clears_upload(
    httpx_client, annotator, fake_storage
):
    _, token = annotator
    uploaded = await httpx_client.post(
        AVATAR_URL,
        files={"file": ("a.png", _png_bytes((300, 300)), "image/png")},
        headers=_bearer(token),
    )
    upload_token = uploaded.json()["avatar_ref"].split(":", 1)[1]

    preset = await httpx_client.patch(
        AVATAR_URL, json={"avatar_ref": "preset:pixel-07"}, headers=_bearer(token)
    )
    assert preset.status_code == 200
    assert preset.json()["avatar_ref"] == "preset:pixel-07"
    assert (fake_storage.avatars_bucket, f"{upload_token}.webp") in fake_storage.deleted

    cleared = await httpx_client.patch(
        AVATAR_URL, json={"avatar_ref": None}, headers=_bearer(token)
    )
    assert cleared.status_code == 200
    assert cleared.json()["avatar_ref"] is None


async def test_delete_avatar_clears_reference(httpx_client, annotator, fake_storage):
    _, token = annotator
    await httpx_client.patch(
        AVATAR_URL, json={"avatar_ref": "preset:pixel-01"}, headers=_bearer(token)
    )
    response = await httpx_client.delete(AVATAR_URL, headers=_bearer(token))
    assert response.status_code == 200
    assert response.json()["avatar_ref"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"avatar_ref": "upload:" + "a" * 32},
        {"avatar_ref": "preset:../../etc/passwd"},
        {"avatar_ref": "preset:PIXEL-01"},
        {"avatar_ref": "preset:" + "a" * 41},
        {"avatar_ref": "https://evil.test/x.svg"},
    ],
)
async def test_patch_avatar_rejects_client_supplied_ref(
    httpx_client, annotator, fake_storage, payload
):
    _, token = annotator
    response = await httpx_client.patch(
        AVATAR_URL, json=payload, headers=_bearer(token)
    )
    assert response.status_code == 400
    me = await httpx_client.get("/api/v1/auth/me", headers=_bearer(token))
    assert me.json()["avatar_ref"] is None


async def test_patch_avatar_rejects_unknown_fields(httpx_client, annotator):
    _, token = annotator
    response = await httpx_client.patch(
        AVATAR_URL,
        json={"avatar_ref": "preset:pixel-01", "avatar_url": "http://evil.test"},
        headers=_bearer(token),
    )
    assert response.status_code == 422


async def test_upload_survives_failed_old_object_cleanup(
    httpx_client, annotator, fake_storage
):
    """清理旧对象失败不得让本次上传失败(孤儿对象可运维清理)。"""
    _, token = annotator
    first = await httpx_client.post(
        AVATAR_URL,
        files={"file": ("a.png", _png_bytes((300, 300)), "image/png")},
        headers=_bearer(token),
    )
    assert first.status_code == 200

    fake_storage.fail_deletes = True
    second = await httpx_client.post(
        AVATAR_URL,
        files={"file": ("b.png", _png_bytes((300, 300), BLUE), "image/png")},
        headers=_bearer(token),
    )
    assert second.status_code == 200
    assert UPLOAD_REF_RE.match(second.json()["avatar_ref"])


async def test_me_and_brief_expose_avatar_ref(
    httpx_client, annotator, db_session, fake_storage
):
    """UserOut(/auth/me) 与 UserBrief(任务/批次责任人) 都必须带上 avatar_ref。"""
    from sqlalchemy import select

    from app.db.models.user import User
    from app.services.user_brief import resolve_briefs

    user, token = annotator
    response = await httpx_client.patch(
        AVATAR_URL, json={"avatar_ref": "preset:pixel-12"}, headers=_bearer(token)
    )
    assert response.status_code == 200

    me = await httpx_client.get("/api/v1/auth/me", headers=_bearer(token))
    assert me.json()["avatar_ref"] == "preset:pixel-12"

    refreshed = (
        await db_session.execute(select(User).where(User.id == user.id))
    ).scalar_one()
    briefs = await resolve_briefs(db_session, [refreshed.id])
    assert briefs[str(refreshed.id)].avatar_ref == "preset:pixel-12"
