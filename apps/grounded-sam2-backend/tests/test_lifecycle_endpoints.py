"""Admission ordering and legacy workload wire compatibility."""

from __future__ import annotations

import asyncio
import sys
import threading
from types import ModuleType
from unittest.mock import MagicMock

import httpx
import pytest
from aap_protocol_v2.errors import LifecycleErrorCode, LifecycleHTTPError
from aap_protocol_v2.lifecycle import (
    GPU_ADMISSION_TOKEN_HEADER,
    GPU_GENERATION_HEADER,
    GPU_HEALTH_CHALLENGE_HEADER,
    GPU_HEALTH_CHALLENGE_QUERY_PARAM,
)


def _load_main():
    sys.modules.setdefault(
        "torch",
        MagicMock(cuda=MagicMock(is_available=MagicMock(return_value=False))),
    )
    sys.modules.setdefault("cv2", MagicMock())
    if "mask_utils" not in sys.modules:
        mask_utils = ModuleType("mask_utils")
        mask_utils.MultiPolygonRing = dict
        mask_utils.PromptAdapterError = ValueError
        mask_utils.decode_coco_rle = MagicMock(return_value=[])
        mask_utils.encode_coco_rle = MagicMock(return_value={})
        mask_utils.mask_prompt_to_low_res_logits = MagicMock()
        mask_utils.mask_to_multi_polygon = MagicMock(return_value=[])
        mask_utils.mask_to_preview_polygon = MagicMock(return_value=[])
        mask_utils.scribbles_to_point_prompts = MagicMock(return_value=([], []))
        polygon = ModuleType("mask_utils.polygon")
        polygon.mask_to_polygon = MagicMock(return_value=[])
        rle = ModuleType("mask_utils.rle")
        rle.encode_coco_rle = MagicMock(return_value={})
        sys.modules["mask_utils"] = mask_utils
        sys.modules["mask_utils.polygon"] = polygon
        sys.modules["mask_utils.rle"] = rle
    import main

    return main


class _Operation:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.futures = []

    def track_future(self, future) -> None:
        if future is not None:
            self.futures.append(future)

    async def close(self) -> None:
        self.events.append("close")


class _Lifecycle:
    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.events: list[str] = []

    async def begin_workload(self, scope, **_headers):
        self.events.append(f"begin:{scope.value}")
        if self.deny:
            raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED)
        return _Operation(self.events)

    async def legacy_unload(self):
        return {"ok": True, "unloaded": True, "loaded": False}


class _ImagePool:
    async def warmup(self, _sv, _dv):
        return False, 12, None

    def builder_for_now(self, _sv, _dv):
        return None


class _VideoPool:
    async def warmup(self, _sv):
        return False, 13, None

    def builder_for_now(self, _sv):
        return None


async def _request(method: str, path: str, **kwargs):
    main = _load_main()
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        return await client.request(method, path, **kwargs)


def _run(coro):
    return asyncio.run(coro)


def test_health_echoes_only_exact_gpu_challenge() -> None:
    challenge = "ab" * 32
    response = _run(
        _request(
            "GET",
            "/health",
            headers={GPU_HEALTH_CHALLENGE_HEADER: challenge},
            params={GPU_HEALTH_CHALLENGE_QUERY_PARAM: challenge},
        )
    )
    assert response.status_code == 200
    assert response.headers[GPU_HEALTH_CHALLENGE_HEADER] == challenge
    assert response.headers["cache-control"] == "no-store"

    ordinary = _run(_request("GET", "/health"))
    assert GPU_HEALTH_CHALLENGE_HEADER not in ordinary.headers
    mismatch = _run(
        _request(
            "GET",
            "/health",
            headers={GPU_HEALTH_CHALLENGE_HEADER: challenge},
            params={GPU_HEALTH_CHALLENGE_QUERY_PARAM: "cd" * 32},
        )
    )
    assert mismatch.status_code == 200
    assert GPU_HEALTH_CHALLENGE_HEADER not in mismatch.headers


@pytest.mark.parametrize("path", ["/warmup", "/reload"])
def test_typed_workload_admits_before_malformed_json(
    monkeypatch,
    path: str,
) -> None:
    main = _load_main()
    lifecycle = _Lifecycle()
    monkeypatch.setattr(main, "_gpu_lifecycle", lifecycle)
    response = _run(
        _request(
            "POST",
            path,
            content=b"{",
            headers={"content-type": "application/json"},
        )
    )
    assert response.status_code == 422
    assert lifecycle.events == [f"begin:{path[1:]}", "close"]


def test_admission_denial_wins_over_invalid_body(monkeypatch) -> None:
    main = _load_main()
    lifecycle = _Lifecycle(deny=True)
    monkeypatch.setattr(main, "_gpu_lifecycle", lifecycle)
    response = _run(
        _request(
            "POST",
            "/warmup",
            content=b"{",
            headers={"content-type": "application/json"},
        )
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error_code"] == "gpu_admission_denied"
    assert lifecycle.events == ["begin:warmup"]


def test_legacy_wire_rejects_partial_duplicate_and_bodyless_managed_headers(
    monkeypatch,
) -> None:
    main = _load_main()
    lifecycle = _Lifecycle()
    monkeypatch.setattr(main, "_gpu_lifecycle", lifecycle)
    workload_headers = [
        [(GPU_GENERATION_HEADER, "1")],
        [(GPU_ADMISSION_TOKEN_HEADER, "signed-token")],
        [
            (GPU_GENERATION_HEADER, "1"),
            (GPU_GENERATION_HEADER, "1"),
            (GPU_ADMISSION_TOKEN_HEADER, "signed-token"),
        ],
        [
            (GPU_GENERATION_HEADER, "1"),
            (GPU_ADMISSION_TOKEN_HEADER, "signed-token"),
            (GPU_ADMISSION_TOKEN_HEADER, "signed-token"),
        ],
    ]

    for headers in workload_headers:
        response = _run(_request("POST", "/warmup", headers=headers, content=b"{"))
        assert response.status_code == 403
        assert response.json()["detail"]["error_code"] == "gpu_admission_denied"

    unload = _run(
        _request(
            "POST",
            "/unload",
            headers={
                GPU_GENERATION_HEADER: "1",
                GPU_ADMISSION_TOKEN_HEADER: "signed-token",
            },
        )
    )
    assert unload.status_code == 403
    assert unload.json()["detail"]["error_code"] == "gpu_admission_denied"


def test_predict_keeps_legacy_malformed_json_400(monkeypatch) -> None:
    main = _load_main()
    lifecycle = _Lifecycle()
    monkeypatch.setattr(main, "_gpu_lifecycle", lifecycle)
    response = _run(
        _request(
            "POST",
            "/predict",
            content=b"{",
            headers={"content-type": "application/json"},
        )
    )
    assert response.status_code == 400
    assert lifecycle.events == ["begin:predict", "close"]


def test_predict_rejects_oversized_body_before_gpu_admission(monkeypatch) -> None:
    main = _load_main()
    lifecycle = _Lifecycle()
    monkeypatch.setattr(main, "_gpu_lifecycle", lifecycle)

    response = _run(
        _request(
            "POST",
            "/predict",
            content=b"",
            headers={
                "content-type": "application/json",
                "content-length": str(main.MAX_PREDICT_REQUEST_BYTES + 1),
            },
        )
    )

    assert response.status_code == 413
    assert lifecycle.events == []


@pytest.mark.parametrize(
    ("body", "expected_task"),
    [(None, "image"), ({"task_type": "video"}, "video")],
)
def test_reload_accepts_legacy_input_and_returns_modality(
    monkeypatch,
    body,
    expected_task: str,
) -> None:
    main = _load_main()
    lifecycle = _Lifecycle()
    monkeypatch.setattr(main, "_gpu_lifecycle", lifecycle)
    monkeypatch.setattr(main, "_pool", _ImagePool())
    monkeypatch.setattr(main, "_video_pool", _VideoPool())
    kwargs = {} if body is None else {"json": body}
    response = _run(_request("POST", "/reload", **kwargs))
    assert response.status_code == 200
    payload = response.json()
    assert payload["modality"] == expected_task
    assert payload["loaded"] is True
    assert payload["reloaded"] is True
    if expected_task == "image":
        assert set(payload) == {
            "ok",
            "loaded",
            "reloaded",
            "sam_variant",
            "dino_variant",
            "modality",
        }
    else:
        assert set(payload) == {
            "ok",
            "loaded",
            "reloaded",
            "sam_variant",
            "modality",
        }


def test_bodyless_unload_keeps_image_only_legacy_wire(monkeypatch) -> None:
    main = _load_main()
    lifecycle = _Lifecycle()
    monkeypatch.setattr(main, "_gpu_lifecycle", lifecycle)
    response = _run(_request("POST", "/unload"))
    assert response.status_code == 200
    assert response.json() == {"ok": True, "unloaded": True, "loaded": False}


def test_cancelled_image_inference_holds_lease_until_thread_finishes(
    monkeypatch,
) -> None:
    main = _load_main()
    from model_pool import ModelPool, ModelPoolBusyError

    started = threading.Event()
    release = threading.Event()

    class _Predictor:
        device = "cpu"
        cleanup_uncertain = False

        def predict_point(self, *_args, **_kwargs):
            started.set()
            release.wait(timeout=5)
            return [], False, None

    async def scenario() -> None:
        pool = ModelPool(
            1,
            lambda _sv, _dv, _cache: _Predictor(),
            lambda: None,
        )
        monkeypatch.setattr(main, "_pool", pool)
        monkeypatch.setattr(main, "fetch_image", lambda *_args, **_kwargs: object())
        operation = _Operation([])
        task = asyncio.create_task(
            main._run_prompt(
                "image.jpg",
                {"type": "point", "points": [[0.5, 0.5]]},
                operation,
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        with pytest.raises(ModelPoolBusyError):
            await pool.unload_all(reason="manual")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(future.done() for future in operation.futures)
        assert await pool.unload_all(reason="manual") == 1
        await pool.shutdown()

    _run(scenario())


def test_deployment_gate_controls_declaration_and_rejects_invalid_value(
    monkeypatch,
) -> None:
    try:
        monkeypatch.setenv("GROUNDED_SAM2_MANAGED_LIFECYCLE_VERIFIED", "0")
        sys.modules.pop("main", None)
        assert "managed_lifecycle" not in _load_main().setup()

        monkeypatch.setenv("GROUNDED_SAM2_MANAGED_LIFECYCLE_VERIFIED", "1")
        sys.modules.pop("main", None)
        assert "managed_lifecycle" in _load_main().setup()

        monkeypatch.setenv("GROUNDED_SAM2_MANAGED_LIFECYCLE_VERIFIED", "true")
        sys.modules.pop("main", None)
        with pytest.raises(ValueError, match="exactly 0 or 1"):
            _load_main()
    finally:
        sys.modules.pop("main", None)
