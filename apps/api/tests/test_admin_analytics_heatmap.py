"""/admin/analytics/activity_heatmap 端点测试.

路由挂在 prefix=/admin/analytics（见 router.py）。
测试环境通常没有 DuckDB 文件，端点会 503；不依赖真实 DuckDB 数据，保持轻量健壮：
  - super_admin: 200（已就绪）或 503（未就绪）皆视为合法。
  - 非 super_admin: 被拒（401/403）。
"""

from __future__ import annotations

import pytest

URL = "/api/v1/admin/analytics/activity_heatmap"


@pytest.mark.asyncio
async def test_heatmap_super_admin_ok_or_not_ready(httpx_client, super_admin):
    _, token = super_admin
    res = await httpx_client.get(URL, headers={"Authorization": f"Bearer {token}"})
    assert res.status_code in (200, 503)
    if res.status_code == 200:
        body = res.json()
        assert body["panel"] == "activity_heatmap"
        assert isinstance(body["data"], list)


@pytest.mark.asyncio
async def test_heatmap_rejects_non_super_admin(httpx_client, annotator):
    _, token = annotator
    res = await httpx_client.get(URL, headers={"Authorization": f"Bearer {token}"})
    assert res.status_code in (401, 403)


@pytest.mark.asyncio
async def test_heatmap_rejects_anonymous(httpx_client):
    res = await httpx_client.get(URL)
    assert res.status_code in (401, 403)
