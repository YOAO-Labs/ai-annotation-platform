"""v0.14.11 · GET /v1/ml-capabilities/protocol 端点测试.

覆盖:
- 200 返回完整受控词表 + 元数据;
- ETag 二次请求带 If-None-Match 返回 304;
- 未登录 401 (与 /model-market 页面同权限);
- 协议层 fields 与 capability_registry SSOT 一致。
"""

from __future__ import annotations

import pytest

from aap_inference import capability_registry as reg


@pytest.mark.asyncio
async def test_protocol_returns_full_catalog(httpx_client, auth_headers):
    r = await httpx_client.get("/api/v1/ml-capabilities/protocol", headers=auth_headers)
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["version"] == "v2"
    assert len(data["tasks"]) == 9
    assert len(data["infras"]) == 6
    assert len(data["modalities"]) == 3
    assert len(data["geometries"]) == 9
    assert [p["id"] for p in data["prompts"]] == list(reg.PROMPT_VALUES)
    assert [i["id"] for i in data["inputs"]] == list(reg.INPUT_VALUES)

    # task id 与 SSOT 一致, 顺序保留
    assert [t["id"] for t in data["tasks"]] == list(reg.TASK_VALUES)
    assert [i["id"] for i in data["infras"]] == list(reg.INFRA_VALUES)
    assert [m["id"] for m in data["modalities"]] == list(reg.MODALITY_VALUES)
    assert [g["id"] for g in data["geometries"]] == list(reg.GEOMETRY_VALUES)


@pytest.mark.asyncio
async def test_protocol_task_metadata_shape(httpx_client, auth_headers):
    r = await httpx_client.get("/api/v1/ml-capabilities/protocol", headers=auth_headers)
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    detection = next(t for t in tasks if t["id"] == "detect")
    assert detection["label"] == "目标检测"
    assert detection["summary"]
    assert detection["protocol_notes"]
    assert detection["default_geometry"] == ["bbox"]
    assert isinstance(detection["suggested_backends"], list)
    assert detection["suggested_backends"]  # detection 必有推荐
    s0 = detection["suggested_backends"][0]
    assert s0["name"] and s0["repo_url"].startswith("https://")


@pytest.mark.asyncio
async def test_protocol_etag_304(httpx_client, auth_headers):
    r1 = await httpx_client.get(
        "/api/v1/ml-capabilities/protocol", headers=auth_headers
    )
    assert r1.status_code == 200
    etag = r1.headers.get("etag")
    assert etag and etag.startswith('W/"')

    r2 = await httpx_client.get(
        "/api/v1/ml-capabilities/protocol",
        headers={**auth_headers, "If-None-Match": etag},
    )
    assert r2.status_code == 304
    assert r2.headers.get("etag") == etag


@pytest.mark.asyncio
async def test_protocol_requires_auth(httpx_client):
    r = await httpx_client.get("/api/v1/ml-capabilities/protocol")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_protocol_cache_control_header(httpx_client, auth_headers):
    r = await httpx_client.get("/api/v1/ml-capabilities/protocol", headers=auth_headers)
    assert r.status_code == 200
    assert "max-age=300" in r.headers.get("cache-control", "")
