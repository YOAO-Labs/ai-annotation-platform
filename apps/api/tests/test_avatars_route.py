"""``GET /api/v1/avatars/{token}`` —— 免鉴权、强缓存的头像图片读取。

刻意不依赖任何用户 fixture:该端点没有鉴权依赖,因此这些用例同时验证「匿名可读」这一
有意设计(见 app/api/v1/avatars.py 的模块注释)。
"""

from __future__ import annotations

from app.services import avatar as avatar_service
from tests._avatar_storage import FakeAvatarStorage

TOKEN = "0123456789abcdef0123456789abcdef"
WEBP_BYTES = b"RIFF____WEBPVP8 "


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _fake(monkeypatch) -> FakeAvatarStorage:
    fake = FakeAvatarStorage()
    monkeypatch.setattr(avatar_service, "storage_service", fake)
    return fake


async def test_get_avatar_returns_bytes_with_cache_headers(httpx_client, monkeypatch):
    fake = _fake(monkeypatch)
    fake.seed(avatar_service.upload_object_key(TOKEN), WEBP_BYTES)

    response = await httpx_client.get(f"/api/v1/avatars/{TOKEN}")
    assert response.status_code == 200
    assert response.content == WEBP_BYTES
    assert response.headers["content-type"] == "image/webp"
    assert "max-age=86400" in response.headers["cache-control"]
    assert response.headers["etag"]
    assert response.headers["content-length"] == str(len(WEBP_BYTES))


async def test_get_avatar_anonymous_access_is_allowed(httpx_client, monkeypatch):
    """未携带 Authorization 头也必须能取到图片(<img src> 不会带 Bearer)。"""
    fake = _fake(monkeypatch)
    fake.seed(avatar_service.upload_object_key(TOKEN), WEBP_BYTES)
    response = await httpx_client.get(f"/api/v1/avatars/{TOKEN}")
    assert response.status_code == 200


async def test_get_avatar_returns_304_on_matching_etag(httpx_client, monkeypatch):
    fake = _fake(monkeypatch)
    fake.seed(avatar_service.upload_object_key(TOKEN), WEBP_BYTES)
    etag = fake.etags[(fake.avatars_bucket, avatar_service.upload_object_key(TOKEN))]

    response = await httpx_client.get(
        f"/api/v1/avatars/{TOKEN}", headers={"If-None-Match": etag}
    )
    assert response.status_code == 304
    assert response.content == b""


async def test_get_avatar_404_when_object_missing(httpx_client, monkeypatch):
    _fake(monkeypatch)
    response = await httpx_client.get(f"/api/v1/avatars/{TOKEN}")
    assert response.status_code == 404


async def test_get_avatar_404_for_malformed_token_without_touching_storage(
    httpx_client, monkeypatch
):
    fake = _fake(monkeypatch)
    fake.seed(avatar_service.upload_object_key(TOKEN), WEBP_BYTES)

    for bad in (
        "..",
        "../../etc/passwd",
        "0123456789ABCDEF0123456789ABCDEF",  # 大写非 hex
        "0123456789abcdef",  # 太短
        "0123456789abcdef0123456789abcdef0",  # 太长
        "0123456789abcdef0123456789abcdeg",  # 非 hex 字符
    ):
        response = await httpx_client.get(f"/api/v1/avatars/{bad}")
        assert response.status_code in {404, 400}, bad
