"""``interactive-annotating-frame`` 端点契约（视频当前帧的交互式 SAM 提示）。

视频 task 的 ``file_path`` 是整段 mp4，SAM 吃不到帧；故前端把当前帧解成 JPEG 随 multipart
传入，服务端上传对象存储换 presigned URL 后投给 ``predict_interactive``。本文件锁住三件事：

1. 送给 backend 的 ``task_data.file_path`` 是**上传后的帧 URL**，不是 task 的 mp4 路径。
2. 兼容 ``context`` 原样透传；显式原生 Mask 请求按目标 model 预检并校验 JPEG 尺寸。
3. ``mask_input_next`` 经平台签名封装后回前端（支撑同帧多次点击的 logits 回灌精修）。

候选**瞬态返回、不落 Prediction**（区别于走批量 ``/predict`` 协议的 ``predict-frame``）。
"""

from __future__ import annotations

import io
import json
import uuid
from unittest.mock import patch

import numpy as np
import pytest
from aap_protocol_v2 import (
    CocoRlePayload,
    NativeMaskCandidate,
    NativeMaskCandidateValue,
    encode_low_res_mask,
    native_mask_candidate_id,
)
from PIL import Image

from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.ml_client import PredictionResult
from tests.conftest import create_registry_with_pool

FRAME_URL = "http://minio/import/frame-interactive/x/7.jpg"
VIDEO_PATH = "http://example/clip.mp4"


def _jpeg_bytes(width: int = 32, height: int = 24) -> bytes:
    """Minimal valid JPEG — v0.23.5 WS-D D2 requires PIL-decodable frames."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (8, 16, 32)).save(buf, format="JPEG")
    return buf.getvalue()


JPEG_BYTES = _jpeg_bytes()
RAW_MASK_INPUT = encode_low_res_mask(np.zeros((256, 256), dtype=np.float32))


async def _seed(db, owner_id, *, is_interactive=True):
    suffix = uuid.uuid4().hex[:8]
    proj = Project(
        id=uuid.uuid4(),
        display_id=f"P-IAF-{suffix}",
        name=f"iaf-{suffix}",
        type_label="video-track",
        type_key="video-track",
        owner_id=owner_id,
    )
    db.add(proj)
    await db.flush()

    backend, pool = await create_registry_with_pool(
        db,
        name="sam3",
        url=f"http://example-{suffix}/",
        is_interactive=is_interactive,
        state="connected",
        enabled_pool=True,
    )
    db.add(ProjectMLBackendPool(project_id=proj.id, pool_id=pool.id, enabled=True))
    await db.flush()

    task = Task(
        id=uuid.uuid4(),
        project_id=proj.id,
        display_id=f"T-IAF-{suffix}",
        file_name="clip.mp4",
        file_path=VIDEO_PATH,
        status="pending",
    )
    db.add(task)
    await db.flush()
    return proj, backend, task


@pytest.fixture
def patched(monkeypatch):
    """抓取 predict_interactive 的调用参数；把对象存储上传打桩成固定 URL。"""
    captured: dict = {}

    async def fake_setup(self):
        return {
            "models": [
                {
                    "id": "frame-interactive",
                    "task": "interactive_seg",
                    "is_interactive": True,
                    "supported_prompts": ["point", "interactive_box"],
                    "supported_inputs": [
                        "full_image",
                        "point_prompt",
                        "bbox_prompt",
                    ],
                    "supported_geometric_outputs": ["polygon", "mask"],
                }
            ]
        }

    async def fake_predict_interactive(self, task_data, context, **kwargs):
        captured["task_data"] = task_data
        captured["context"] = context
        captured["predict_kwargs"] = kwargs
        if context.get("output_geometry") == "mask":
            height, width = captured.get("native_size", (24, 32))
            rle = CocoRlePayload(
                encoding="coco_rle",
                size=[height, width],
                counts=[0, 1, height * width - 1],
            )
            candidate = NativeMaskCandidate(
                value=NativeMaskCandidateValue(rle=rle, masklabels=["object"]),
                score=0.91,
                candidate_id=native_mask_candidate_id(
                    rle,
                    prompt_revision=context["prompt_revision"],
                    candidate_index=0,
                ),
            )
            return PredictionResult(
                task_id=task_data["id"],
                result=[candidate.model_dump(mode="json")],
                model_version="test-frame-model",
            )
        return PredictionResult(
            task_id=task_data["id"],
            result=[{"type": "polygonlabels", "points": [[0.1, 0.1]]}],
            score=0.9,
            inference_time_ms=7,
            cache_hit=False,
            model_load_ms=42,
            mask_input_next=RAW_MASK_INPUT,
        )

    def fake_upload(self, data, key):
        captured["upload_key"] = key
        captured["upload_size"] = len(data)
        return FRAME_URL

    with (
        patch(
            "app.services.ml_client.MLBackendClient.setup",
            new=fake_setup,
        ),
        patch(
            "app.services.ml_client.MLBackendClient.predict_interactive",
            new=fake_predict_interactive,
        ),
    ):
        monkeypatch.setattr(
            "app.services.storage.StorageService.upload_crop_bytes", fake_upload
        )
        yield captured


def _url(proj, backend) -> str:
    return (
        f"/api/v1/projects/{proj.id}/ml-backends/{backend.id}"
        "/interactive-annotating-frame"
    )


async def test_frame_bytes_become_backend_file_path(
    httpx_client, super_admin, db_session, patched
):
    """帧 JPEG 上传换 URL 后投给 backend；task 的 mp4 路径不得出现在 task_data。"""
    user, token = super_admin
    proj, backend, task = await _seed(db_session, user.id)
    await db_session.commit()

    resp = await httpx_client.post(
        _url(proj, backend),
        files={"frame": ("f.jpg", JPEG_BYTES, "image/jpeg")},
        data={
            "task_id": str(task.id),
            "frame_index": "7",
            "context": '{"type": "point", "points": [[0.5, 0.5]], "labels": [1]}',
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    assert patched["task_data"]["file_path"] == FRAME_URL
    assert patched["task_data"]["file_path"] != VIDEO_PATH
    assert patched["task_data"]["id"] == str(task.id)
    # 上传键按 task/frame 分桶，便于同帧连击命中同一对象。
    assert patched["upload_key"] == f"frame-interactive/{task.id}/7.jpg"
    assert patched["upload_size"] == len(JPEG_BYTES)


async def test_context_passthrough_without_threshold_injection(
    httpx_client, super_admin, db_session, patched
):
    """context 原样透传；平台不注入项目级 DINO 阈值（与 interactive-annotating 一致）。"""
    user, token = super_admin
    proj, backend, task = await _seed(db_session, user.id)
    await db_session.commit()

    resp = await httpx_client.post(
        _url(proj, backend),
        files={"frame": ("f.jpg", JPEG_BYTES, "image/jpeg")},
        data={
            "task_id": str(task.id),
            "frame_index": "0",
            "context": '{"type": "interactive_box", "bbox": [0.1, 0.1, 0.4, 0.4]}',
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    ctx = patched["context"]
    assert ctx["type"] == "interactive_box"
    assert ctx["bbox"] == [0.1, 0.1, 0.4, 0.4]
    assert "box_threshold" not in ctx
    assert "text_threshold" not in ctx
    assert set(ctx) == {"type", "bbox", "model_id"}


async def test_mask_input_next_passthrough(
    httpx_client, super_admin, db_session, patched
):
    """low-res logits 由平台绑定帧与模型后签名，供同帧下一次点击精修。"""
    user, token = super_admin
    proj, backend, task = await _seed(db_session, user.id)
    await db_session.commit()

    resp = await httpx_client.post(
        _url(proj, backend),
        files={"frame": ("f.jpg", JPEG_BYTES, "image/jpeg")},
        data={
            "task_id": str(task.id),
            "frame_index": "3",
            "context": '{"type": "point"}',
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    from app.services.ai_mask_session import verify_ai_mask_session

    session = verify_ai_mask_session(body["mask_input_next"])
    assert session["raw"] == RAW_MASK_INPUT
    assert session["task_id"] == str(task.id)
    assert session["frame_index"] == 3
    assert session["model_id"] == "frame-interactive"
    assert session["model_variants"] == {}
    assert body["model_load_ms"] == 42
    assert body["cache_hit"] is False
    assert body["result"][0]["type"] == "polygonlabels"
    # 候选瞬态返回, 不落 Prediction (对齐图片侧交互链路)。
    assert "prediction_id" not in body
    # frame_index 进上传键 → 同 task 不同帧不会互相覆盖。
    assert patched["upload_key"].endswith("/3.jpg")


async def test_native_mask_frame_validates_jpeg_size_and_returns_lineage(
    httpx_client, super_admin, db_session, patched
):
    user, token = super_admin
    proj, backend, task = await _seed(db_session, user.id)
    await db_session.commit()

    resp = await httpx_client.post(
        _url(proj, backend),
        files={"frame": ("f.jpg", JPEG_BYTES, "image/jpeg")},
        data={
            "task_id": str(task.id),
            "frame_index": "4",
            "context": '{"type":"point","points":[[0.5,0.5]],"output_geometry":"mask"}',
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"][0]["value"]["rle"]["size"] == [24, 32]
    assert body["routing"]["model_id"] == "frame-interactive"
    assert body["model_version"] == "test-frame-model"
    assert body["prompt_revision"]
    candidate_id = body["result"][0]["candidate_id"]
    from app.services.ai_mask_receipt import verify_ai_mask_receipt

    receipt = verify_ai_mask_receipt(body["accept_receipts"][candidate_id])
    assert receipt["task_id"] == str(task.id)
    assert receipt["candidate_id"] == candidate_id
    assert receipt["routing"] == body["routing"]
    assert receipt["accept_target"] == {
        "mode": "create",
        "source_annotation_id": None,
        "source_version": None,
        "frame_index": 4,
    }
    assert patched["predict_kwargs"]["max_response_bytes"] == 16 * 1024 * 1024


async def test_native_mask_frame_rejects_backend_size_mismatch(
    httpx_client, super_admin, db_session, patched
):
    user, token = super_admin
    proj, backend, task = await _seed(db_session, user.id)
    await db_session.commit()
    patched["native_size"] = (23, 32)

    resp = await httpx_client.post(
        _url(proj, backend),
        files={"frame": ("f.jpg", JPEG_BYTES, "image/jpeg")},
        data={
            "task_id": str(task.id),
            "frame_index": "5",
            "context": '{"type":"point","points":[[0.5,0.5]],"output_geometry":"mask"}',
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 502
    assert resp.json()["detail"]["reason"] == "invalid_mask_payload"


async def test_non_interactive_backend_rejected(
    httpx_client, super_admin, db_session, patched
):
    user, token = super_admin
    proj, backend, task = await _seed(db_session, user.id, is_interactive=False)
    await db_session.commit()

    resp = await httpx_client.post(
        _url(proj, backend),
        files={"frame": ("f.jpg", JPEG_BYTES, "image/jpeg")},
        data={"task_id": str(task.id), "frame_index": "0", "context": "{}"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "interactive" in resp.json()["detail"]
    # 拒绝发生在读帧之前: 不上传、不打 backend。
    assert "upload_key" not in patched
    assert "task_data" not in patched


async def test_interactive_frame_rejects_context_over_one_mib_before_upload(
    httpx_client, super_admin, db_session, patched
):
    user, token = super_admin
    proj, backend, task = await _seed(db_session, user.id)
    await db_session.commit()
    oversized = json.dumps(
        {"type": "point", "padding": "界" * 400_000},
        ensure_ascii=False,
    )

    resp = await httpx_client.post(
        _url(proj, backend),
        files={"frame": ("f.jpg", JPEG_BYTES, "image/jpeg")},
        data={
            "task_id": str(task.id),
            "frame_index": "0",
            "context": oversized,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 413
    assert resp.json()["detail"]["reason"] == "interactive_context_too_large"
    assert "upload_key" not in patched
    assert "task_data" not in patched


async def test_empty_frame_rejected(httpx_client, super_admin, db_session, patched):
    """空帧不该白跑一次 GPU 推理。"""
    user, token = super_admin
    proj, backend, task = await _seed(db_session, user.id)
    await db_session.commit()

    resp = await httpx_client.post(
        _url(proj, backend),
        files={"frame": ("f.jpg", b"", "image/jpeg")},
        data={"task_id": str(task.id), "frame_index": "0", "context": "{}"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Empty frame image"
    assert "upload_key" not in patched
    assert "task_data" not in patched


async def test_invalid_context_json_rejected(
    httpx_client, super_admin, db_session, patched
):
    user, token = super_admin
    proj, backend, task = await _seed(db_session, user.id)
    await db_session.commit()

    resp = await httpx_client.post(
        _url(proj, backend),
        files={"frame": ("f.jpg", JPEG_BYTES, "image/jpeg")},
        data={"task_id": str(task.id), "frame_index": "0", "context": "{not json"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid context JSON"
    # context 解析先于读帧: 坏 JSON 不该白传一张图上对象存储。
    assert "upload_key" not in patched
    assert "task_data" not in patched


async def test_unknown_task_rejected(httpx_client, super_admin, db_session, patched):
    user, token = super_admin
    proj, backend, _ = await _seed(db_session, user.id)
    await db_session.commit()

    resp = await httpx_client.post(
        _url(proj, backend),
        files={"frame": ("f.jpg", JPEG_BYTES, "image/jpeg")},
        data={"task_id": str(uuid.uuid4()), "frame_index": "0", "context": "{}"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
    assert "upload_key" not in patched
    assert "task_data" not in patched


async def test_cross_project_task_rejected(
    httpx_client, super_admin, db_session, patched
):
    """跨项目越权: 用 A 项目的 backend + B 项目的 task_id → 404, 不落对象存储。"""
    user, token = super_admin
    proj_a, backend_a, _ = await _seed(db_session, user.id)
    _, _, task_b = await _seed(db_session, user.id)
    await db_session.commit()

    resp = await httpx_client.post(
        _url(proj_a, backend_a),
        files={"frame": ("f.jpg", JPEG_BYTES, "image/jpeg")},
        data={"task_id": str(task_b.id), "frame_index": "0", "context": "{}"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
    assert "upload_key" not in patched
    assert "task_data" not in patched


async def test_ai_interactive_disabled_rejected(
    httpx_client, super_admin, db_session, patched
):
    """项目关掉「交互式 AI 工具」总开关后, 直接调 API 也应 403 (开关不再是装饰)。"""
    user, token = super_admin
    proj, backend, task = await _seed(db_session, user.id)
    proj.ai_interactive_enabled = False
    await db_session.commit()

    resp = await httpx_client.post(
        _url(proj, backend),
        files={"frame": ("f.jpg", JPEG_BYTES, "image/jpeg")},
        data={
            "task_id": str(task.id),
            "frame_index": "0",
            "context": '{"type": "point"}',
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert "upload_key" not in patched
    assert "task_data" not in patched
