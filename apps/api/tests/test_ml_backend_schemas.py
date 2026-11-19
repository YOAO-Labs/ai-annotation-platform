"""协议 context.type 扩 text。

InteractiveRequest.context 仍是开放 dict（与 backend 协商空间不锁死），
但 schema docstring 与协议文档 §2.2 列出的 4 种 type 要随版本同步。
"""

from __future__ import annotations

import uuid

from app.schemas.ml_backend import InteractiveRequest


def test_interactive_request_accepts_point_type():
    req = InteractiveRequest(
        task_id=uuid.uuid4(),
        context={"type": "point", "points": [[0.5, 0.5]], "labels": [1]},
    )
    assert req.context["type"] == "point"


def test_interactive_request_accepts_bbox_type():
    req = InteractiveRequest(
        task_id=uuid.uuid4(),
        context={"type": "bbox", "bbox": [0.1, 0.1, 0.5, 0.5]},
    )
    assert req.context["type"] == "bbox"


def test_interactive_request_accepts_polygon_type():
    req = InteractiveRequest(
        task_id=uuid.uuid4(),
        context={"type": "polygon", "points": [[0.1, 0.1], [0.2, 0.2]]},
    )
    assert req.context["type"] == "polygon"


def test_interactive_request_accepts_text_type():
    """Grounded-SAM-2 文本批量入口 — schema 必须不拒绝。"""
    req = InteractiveRequest(
        task_id=uuid.uuid4(),
        context={"type": "text", "text": "ripe apples"},
    )
    assert req.context["type"] == "text"
    assert req.context["text"] == "ripe apples"


def test_interactive_request_accepts_unknown_type_no_strict_schema():
    """context 是开放 dict — 平台不校验，留给 backend 协商。"""
    req = InteractiveRequest(
        task_id=uuid.uuid4(),
        context={"type": "exemplar", "bbox": [0.1, 0.1, 0.5, 0.5]},
    )
    assert req.context["type"] == "exemplar"


def test_interactive_request_default_context_empty_dict():
    req = InteractiveRequest(task_id=uuid.uuid4())
    assert req.context == {}


# ─── MLBackendCreate / MLBackendUpdate URL host blacklist ──────────


import pytest  # noqa: E402

from app.schemas.ml_backend import ComputeInfo, MLBackendCreate, MLBackendUpdate  # noqa: E402


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://localhost:8001",
        "http://127.0.0.1:8001/predict",
        "http://0.0.0.0/",
        "https://[::1]:443/path",
        "http://LOCALHOST:9000",  # 大小写不敏感
    ],
)
def test_ml_backend_create_rejects_loopback_url(bad_url: str):
    with pytest.raises(ValueError) as exc:
        MLBackendCreate(name="b", url=bad_url)
    assert "loopback" in str(exc.value)


@pytest.mark.parametrize(
    "good_url",
    [
        "http://172.17.0.1:8001",
        "http://sam-backend:8001",
        "https://gpu.internal/predict",
        "http://192.168.1.10:8001",
    ],
)
def test_ml_backend_create_accepts_non_loopback(good_url: str):
    m = MLBackendCreate(name="b", url=good_url)
    assert m.url == good_url


def test_ml_backend_update_rejects_loopback_when_url_provided():
    with pytest.raises(ValueError) as exc:
        MLBackendUpdate(url="http://localhost:8001")
    assert "loopback" in str(exc.value)


def test_ml_backend_update_allows_url_none():
    m = MLBackendUpdate(name="rename")
    assert m.url is None
    assert m.name == "rename"


def test_compute_info_preserves_gpu_only_capability():
    compute = ComputeInfo(
        configured_device="cuda",
        effective_device=None,
        cpu_fallback_supported=False,
    )

    assert compute.model_dump()["cpu_fallback_supported"] is False
