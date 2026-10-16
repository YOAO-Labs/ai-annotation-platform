"""echo 示例 contract tests — 保活, 防示例与 ml-backend-protocol 脱节。

只测协议 shape (health / setup / predict), 无需起服务。
"""

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_health_returns_200() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_setup_declares_protocol_v21() -> None:
    resp = client.get("/setup")
    assert resp.status_code == 200
    body = resp.json()
    # 必填三元组 + 协议版本声明 (协议 §4 / §4.1.1)。
    assert body["name"] == "echo-backend"
    assert body["version"]
    assert body["model_version"]
    assert body["protocol_version"] == "2.1"
    assert body["compat_protocol_versions"] == ["2.0"]
    assert body["is_interactive"] is False


def test_setup_models_catalog_minimal_entry() -> None:
    models = client.get("/setup").json()["models"]
    assert len(models) == 1
    m = models[0]
    assert m["id"] == "echo-detect"
    assert m["display_name"]
    assert m["task"] == "detect"
    assert m["supported_prompts"] == ["none"]
    assert m["supported_geometric_outputs"] == ["bbox"]


def test_versions_shape() -> None:
    resp = client.get("/versions")
    assert resp.status_code == 200
    assert isinstance(resp.json()["versions"], list)


def test_predict_echoes_each_task() -> None:
    resp = client.post(
        "/predict",
        json={
            "tasks": [
                {"id": "t1", "file_path": "s3://bucket/a.jpg"},
                {"id": "t2", "file_path": "s3://bucket/b.jpg"},
            ]
        },
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["task"] for r in results] == ["t1", "t2"]
    for r in results:
        shape = r["result"][0]
        assert shape["type"] == "rectanglelabels"
        assert shape["value"]["rectanglelabels"] == ["demo"]
        assert r["model_version"]
        # 运行时观测字段 (协议 §4.2) 演示值。
        assert r["cache_hit"] is True
        assert r["model_load_ms"] == 0
