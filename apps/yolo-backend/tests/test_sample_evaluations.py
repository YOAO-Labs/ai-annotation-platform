"""Request identity, remote completion and cancellation qualification without a GPU."""

import asyncio
import base64
import hashlib
import io
import json
import subprocess
import sys
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
import httpx
from fastapi import HTTPException
from PIL import Image
from pydantic import ValidationError

from managed_models import digest
from sample_evaluations import (
    FIXED_OPTIONS,
    EvaluationRequest,
    EvaluationRequestStore,
    EvaluationSeal,
    qualification_profile,
    request_binding,
    verify_input,
)
from schemas import BatchPredictRequest, Context


def request(store, *, request_id=None):
    issued = int(time.time())
    return EvaluationRequest(
        request_id=request_id or uuid.uuid4(),
        binding_digest="a" * 64,
        profile_digest="b" * 64,
        storage_identity=store.storage_identity,
        input_sha256="c" * 64,
        issued_at=issued,
        expires_at=issued + 86400,
    )


def seal_request(body):
    return EvaluationSeal.model_validate(
        body.model_dump(exclude={"request_id", "input_sha256"})
    )


def response(task_id="task"):
    return {
        "results": [
            {
                "task": task_id,
                "result": [],
                "meta": {"trained_model": {"sha256": "a" * 64}},
            }
        ]
    }


def png(*, mode="RGB", exif=None):
    target = io.BytesIO()
    Image.new(mode, (8, 4)).save(
        target, format="PNG", **({"exif": exif} if exif else {})
    )
    raw = target.getvalue()
    return raw, "data:image/png;base64," + base64.b64encode(raw).decode()


async def test_libre_is_rejected_before_shared_profile_or_receipt_admission(
    tmp_path, monkeypatch
):
    import main

    store = EvaluationRequestStore(tmp_path)
    req, body, _profile = qualified_batch(store)
    monkeypatch.setattr(
        main._managed_models, "receipt", lambda _: {"framework": "libreyolo"}
    )
    monkeypatch.setattr(
        main,
        "qualification_profile",
        lambda *_: pytest.fail("Must reject before profile"),
    )
    with pytest.raises(HTTPException) as error:
        await main._run_sample_evaluation(req, operation=None)
    assert error.value.status_code == 422
    assert error.value.detail["reason"] == "evaluation_framework_unqualified"
    assert (
        store.receipt(
            body.request_id,
            binding_digest=body.binding_digest,
            storage_identity=body.storage_identity,
        )["state"]
        == "missing"
    )


def test_completed_request_survives_restart_and_never_reexecutes_or_changes_response(
    tmp_path,
):
    store = EvaluationRequestStore(tmp_path)
    body = request(store)
    calls = []
    with store.execution(body) as receipt:
        calls.append("native")
        store.publish(body, receipt, response())
    restarted = EvaluationRequestStore(tmp_path)
    assert restarted.storage_identity == body.storage_identity
    with restarted.execution(body) as receipt:
        assert receipt["state"] == "completed" and receipt["response"] == response()
        with pytest.raises(HTTPException) as conflict:
            restarted.publish(body, receipt, response("other-task"))
        assert conflict.value.status_code == 409
    assert calls == ["native"]
    observed = restarted.receipt(
        body.request_id,
        binding_digest=body.binding_digest,
        storage_identity=body.storage_identity,
    )
    assert observed["response_digest"] == digest(response())


@pytest.mark.parametrize(
    "change",
    [
        {"binding_digest": "d" * 64},
        {"profile_digest": "d" * 64},
        {"issued_at": 100, "expires_at": 86500},
    ],
)
def test_identity_is_bound_before_native_response_including_original_expiry(
    tmp_path, change
):
    store = EvaluationRequestStore(tmp_path)
    body = request(store)
    with pytest.raises(RuntimeError, match="worker disappeared"):
        with store.execution(body):
            raise RuntimeError("worker disappeared")
    restarted = EvaluationRequestStore(tmp_path)
    with pytest.raises(HTTPException) as conflict:
        with restarted.execution(body.model_copy(update=change)):
            pytest.fail("Changed frozen identity cannot run")
    assert conflict.value.status_code == 409
    with restarted.execution(body) as receipt:
        assert receipt["state"] == "bound"


def test_busy_execution_blocks_second_process_handle_receipt_and_cancellation_seal(
    tmp_path,
):
    store = EvaluationRequestStore(tmp_path)
    another = EvaluationRequestStore(tmp_path)
    body = request(store)
    with store.execution(body):
        for call in (
            lambda: another.execution(body).__enter__(),
            lambda: another.receipt(
                body.request_id,
                binding_digest=body.binding_digest,
                storage_identity=body.storage_identity,
            ),
            lambda: another.seal(body.request_id, seal_request(body)),
        ):
            with pytest.raises(HTTPException) as busy:
                call()
            assert (
                busy.value.status_code == 409
                and busy.value.detail["reason"] == "evaluation_request_busy"
            )


def test_seal_closes_late_post_window_before_first_request_arrives(tmp_path):
    store = EvaluationRequestStore(tmp_path)
    body = request(store)
    assert store.seal(body.request_id, seal_request(body))["state"] == "sealed"
    assert (
        EvaluationRequestStore(tmp_path).seal(body.request_id, seal_request(body))[
            "state"
        ]
        == "sealed"
    )
    with pytest.raises(HTTPException) as stopped:
        with store.execution(body):
            pytest.fail("A delayed HTTP POST must never start native work")
    assert stopped.value.status_code == 410
    assert (
        store.receipt(
            body.request_id,
            binding_digest=body.binding_digest,
            storage_identity=body.storage_identity,
        )["state"]
        == "sealed"
    )


def test_completed_receipt_can_confirm_exit_without_overwriting_evidence(tmp_path):
    store = EvaluationRequestStore(tmp_path)
    body = request(store)
    with store.execution(body) as receipt:
        store.publish(body, receipt, response())
    assert store.seal(body.request_id, seal_request(body))["state"] == "completed"
    assert (
        store.receipt(
            body.request_id,
            binding_digest=body.binding_digest,
            storage_identity=body.storage_identity,
        )["response"]
        == response()
    )


def test_store_replacement_cannot_claim_old_requests_stopped(tmp_path):
    original = EvaluationRequestStore(tmp_path / "original")
    replacement = EvaluationRequestStore(tmp_path / "replacement")
    body = request(original)
    for call in (
        lambda: replacement.execution(body).__enter__(),
        lambda: replacement.receipt(
            body.request_id,
            binding_digest=body.binding_digest,
            storage_identity=body.storage_identity,
        ),
        lambda: replacement.seal(body.request_id, seal_request(body)),
    ):
        with pytest.raises(HTTPException) as changed:
            call()
        assert changed.value.detail["reason"] == "evaluation_store_changed"


def test_corrupt_or_oversized_receipt_is_never_replaced_by_new_native_work(tmp_path):
    store = EvaluationRequestStore(tmp_path)
    body = request(store)
    with store.execution(body) as receipt:
        store.publish(body, receipt, response())
    path = tmp_path / f"{body.request_id}.json"
    record = json.loads(path.read_bytes())
    record["response"]["results"][0]["task"] = "corrupt-task"
    path.write_text(json.dumps(record))
    with pytest.raises(HTTPException) as corrupt:
        with store.execution(body):
            pytest.fail("Corrupt completed evidence is not recoverable by replacement")
    assert corrupt.value.detail["reason"] == "evaluation_receipt_corrupt"
    path.write_bytes(b" " * (256 * 1024 + 1))
    with pytest.raises(HTTPException):
        store.receipt(
            body.request_id,
            binding_digest=body.binding_digest,
            storage_identity=body.storage_identity,
        )


def test_bounded_publication_leaves_only_bound_manifest_on_failure(tmp_path):
    store = EvaluationRequestStore(tmp_path)
    body = request(store)
    with store.execution(body) as receipt:
        with pytest.raises(HTTPException) as too_large:
            store.publish(body, receipt, {**response(), "payload": "x" * (256 * 1024)})
        assert too_large.value.status_code == 413
        assert receipt["state"] == "bound"
        with pytest.raises(HTTPException) as too_many:
            store.publish(body, receipt, {"results": [{"result": [{}] * 101}]})
        assert too_many.value.status_code == 422
    assert not list(tmp_path.glob(".receipt-*"))


def test_capacity_cleanup_never_unlinks_lock_inode_or_running_native_request(
    tmp_path, monkeypatch
):
    import sample_evaluations as module

    monkeypatch.setattr(module, "MAX_RECEIPTS", 1)
    store = EvaluationRequestStore(tmp_path)
    first = request(store, request_id=uuid.UUID(int=1))
    second = request(store, request_id=uuid.UUID(int=2))
    with store.execution(first):
        lock = tmp_path / "lock-1"
        inode = lock.stat().st_ino
        monkeypatch.setattr(module.time, "time", lambda: first.expires_at + 1)
        second = second.model_copy(
            update={
                "issued_at": first.expires_at + 1,
                "expires_at": first.expires_at + 1 + 86400,
            }
        )
        with pytest.raises(HTTPException) as full:
            with store.execution(second):
                pytest.fail("Running request must not be purged")
        assert full.value.status_code == 429
        assert (tmp_path / f"{first.request_id}.json").exists()
    with store.execution(second):
        pass
    assert not (tmp_path / f"{first.request_id}.json").exists()
    assert lock.stat().st_ino == inode
    with pytest.raises(HTTPException) as expired:
        with store.execution(first):
            pytest.fail("An expired late POST cannot recreate a purged request")
    assert expired.value.status_code == 410


def test_profile_records_actual_pinned_source_and_shared_storage_identity(tmp_path):
    store = EvaluationRequestStore(tmp_path)
    profile = qualification_profile("cpu", store.storage_identity)
    import importlib.metadata

    assert profile["ultralytics_version"] == importlib.metadata.version("ultralytics")
    assert profile["options"] == FIXED_OPTIONS
    assert profile["options"]["quantize"] == 32 and profile["options"]["rect"] is False
    assert len(profile["source_digest"]) == 64
    assert profile["profile_digest"] == digest(
        {k: v for k, v in profile.items() if k != "profile_digest"}
    )
    assert profile == qualification_profile(
        "cpu", EvaluationRequestStore(tmp_path).storage_identity
    )


def test_mask_profile_and_persistent_receipts_have_independent_identity_and_budget(
    tmp_path,
    monkeypatch,
):
    import segmentation_evaluations
    from segmentation_evaluations import qualification_profile as mask_profile

    monkeypatch.setattr(segmentation_evaluations, "verify_runtime", lambda: None)

    detect = EvaluationRequestStore(tmp_path / "detection")
    segment = EvaluationRequestStore(
        tmp_path / "segmentation", response_bytes=17 * 1024 * 1024
    )
    profile = mask_profile("cpu", segment.storage_identity)
    assert profile["implementation"] == "aap-ultralytics-instance-mask"
    assert profile["options"]["retina_masks"] is True
    assert (
        profile["profile_digest"]
        != qualification_profile("cpu", detect.storage_identity)["profile_digest"]
    )
    assert profile["storage_identity"] != str(detect.storage_identity)
    body = request(segment).model_copy(
        update={"profile_digest": profile["profile_digest"]}
    )
    large = response()
    large["results"][0]["meta"]["mask_evidence"] = "a" * (300 * 1024)
    with segment.execution(body) as record:
        segment.publish(body, record, large)
    assert (
        EvaluationRequestStore(segment.root, response_bytes=17 * 1024 * 1024).receipt(
            body.request_id,
            binding_digest=body.binding_digest,
            storage_identity=body.storage_identity,
        )["response"]
        == large
    )
    with pytest.raises(HTTPException):
        detect.receipt(
            body.request_id,
            binding_digest=body.binding_digest,
            storage_identity=body.storage_identity,
        )


def test_input_is_exact_bounded_metadata_free_rgb_png(tmp_path, monkeypatch):
    import sample_evaluations as module

    store = EvaluationRequestStore(tmp_path)
    raw, data_url = png()
    body = request(store).model_copy(
        update={"input_sha256": hashlib.sha256(raw).hexdigest()}
    )
    assert verify_input(data_url, body) == {"width": 8, "height": 4}
    invalid = [
        "http://mutable.test/image",
        data_url + "!",
        data_url.replace("image/png", "image/jpeg"),
    ]
    for mode in ("RGBA", "L"):
        _, url = png(mode=mode)
        invalid.append(url)
    exif = Image.Exif()
    exif[274] = 6
    _, oriented = png(exif=exif)
    invalid.append(oriented)
    for url in invalid:
        with pytest.raises(HTTPException) as bad_input:
            verify_input(url, body)
        assert bad_input.value.status_code == 422
    monkeypatch.setattr(module, "MAX_PIXELS", 31)
    with pytest.raises(HTTPException):
        verify_input(data_url, body)
    monkeypatch.setattr(module, "MAX_PIXELS", 32)
    monkeypatch.setattr(module, "MAX_INPUT_BYTES", len(raw) - 1)
    with pytest.raises(HTTPException):
        verify_input(data_url, body)


def controlled_context(body):
    return {
        "type": "detection",
        "model_id": "trained:00000000-0000-0000-0000-000000000001",
        "frozen_model_ref": {
            "model_version_id": "00000000-0000-0000-0000-000000000001",
            "sha256": "d" * 64,
            "classes_digest": digest(["car"]),
        },
        "params": {"conf": 0.25, "iou": 0.7, "max_det": 100},
        "sample_evaluation": body.model_dump(mode="json"),
    }


def test_evaluation_context_cannot_escape_controlled_single_full_class_contract(
    tmp_path,
):
    body = request(EvaluationRequestStore(tmp_path))
    wire = controlled_context(body)
    Context.model_validate(wire)
    for change in (
        {"classes": []},
        {"classes": [0]},
        {"params": {"max_det": 300}},
        {"frozen_model_ref": None},
        {"type": "keypoint"},
    ):
        with pytest.raises(ValidationError):
            Context.model_validate({**wire, **change})
    assert Context.model_validate({**wire, "type": "segmentation"}).task == "segment"
    with pytest.raises(ValidationError):
        BatchPredictRequest.model_validate(
            {
                "context": wire,
                "tasks": [
                    {"id": "one", "file_path": "x"},
                    {"id": "two", "file_path": "x"},
                ],
            }
        )


def qualified_batch(store):
    raw, data_url = png()
    profile = qualification_profile("cpu", store.storage_identity)
    body = request(store).model_copy(
        update={
            "input_sha256": hashlib.sha256(raw).hexdigest(),
            "profile_digest": profile["profile_digest"],
        }
    )
    wire = controlled_context(body)
    body.binding_digest = request_binding(
        "sample", wire["frozen_model_ref"], wire["params"], body
    )
    wire["sample_evaluation"] = body.model_dump(mode="json")
    return (
        BatchPredictRequest.model_validate(
            {"context": wire, "tasks": [{"id": "sample", "file_path": data_url}]}
        ),
        body,
        profile,
    )


@pytest.mark.asyncio
async def test_profile_receipt_and_seal_require_managed_auth_without_gpu_admission(
    tmp_path, monkeypatch
):
    import main

    store = EvaluationRequestStore(tmp_path)
    body = request(store)
    monkeypatch.setattr(main, "_evaluation_requests", store)
    monkeypatch.setattr(main, "effective_device", lambda _: "cpu")
    monkeypatch.setenv("YOLO_MANAGED_MODELS_TOKEN", "a" * 40)

    async def forbidden(*_args, **_kwargs):
        pytest.fail("Read/seal must not acquire a GPU workload")

    monkeypatch.setattr(main, "_begin_workload", forbidden)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://backend"
    ) as client:
        assert (await client.get("/managed-evaluation/profile")).status_code == 403
        client.headers["Authorization"] = "Bearer " + "a" * 40
        profile = await client.get("/managed-evaluation/profile")
        assert profile.status_code == 200 and profile.json()["storage_identity"] == str(
            body.storage_identity
        )
        receipt = await client.get(
            f"/managed-evaluation/requests/{body.request_id}",
            params={
                "binding_digest": body.binding_digest,
                "storage_identity": str(body.storage_identity),
            },
        )
        assert receipt.status_code == 200 and receipt.json()["state"] == "missing"
        sealed = await client.post(
            f"/managed-evaluation/requests/{body.request_id}/seal",
            json=seal_request(body).model_dump(mode="json"),
        )
        assert sealed.status_code == 200 and sealed.json()["state"] == "sealed"


@pytest.mark.asyncio
async def test_lost_response_is_recovered_from_receipt_without_another_native_call(
    tmp_path, monkeypatch
):
    import main
    from aap_protocol_v2 import PredictionResult

    store = EvaluationRequestStore(tmp_path)
    req, body, profile = qualified_batch(store)
    monkeypatch.setattr(main, "_evaluation_requests", store)
    monkeypatch.setattr(main, "effective_device", lambda _: "cpu")
    calls = []

    monkeypatch.setattr(
        main, "_model_pool", SimpleNamespace(builder_for_now=lambda *_: None)
    )

    async def native(batch, *, operation):
        calls.append("native")
        batch.context._evaluation_meta = {"width": 8, "height": 4}
        return [
            PredictionResult(
                task="sample",
                result=[],
                model_version=batch.context.model_id,
                meta={
                    "trained_model": batch.context.frozen_model_ref.model_dump(
                        mode="json"
                    ),
                    "sample_evaluation": {"profile_digest": profile["profile_digest"]},
                },
            )
        ]

    monkeypatch.setattr(
        main._managed_models, "receipt", lambda _: {"framework": "ultralytics"}
    )
    monkeypatch.setattr(main, "_run_predict", native)
    original = await main._run_sample_evaluation(req, operation=None)
    recovered = await main._run_sample_evaluation(req, operation=None)
    assert original == recovered and calls == ["native"]
    # Receipt reads do not call _begin_workload or need a new GPU admission.
    receipt = store.receipt(
        body.request_id,
        binding_digest=body.binding_digest,
        storage_identity=body.storage_identity,
    )
    assert receipt["state"] == "completed" and receipt["response"] == original


@pytest.mark.asyncio
async def test_transport_cancellation_keeps_native_lock_until_exit_then_seal_blocks_late_post(
    tmp_path, monkeypatch
):
    import main
    from model_pool import ModelPool
    from predictor import YoloPredictor

    store = EvaluationRequestStore(tmp_path)
    req, body, profile = qualified_batch(store)
    started, release = threading.Event(), threading.Event()

    class Model:
        device = "cpu"

        def __init__(self):
            self.predictor = SimpleNamespace(
                args=SimpleNamespace(
                    **{k: v for k, v in FIXED_OPTIONS.items() if k != "stream"}
                ),
                imgsz=[640, 640],
                model=SimpleNamespace(fp16=False, end2end=False),
            )

        def predict(self, *_args, **kwargs):
            assert kwargs["quantize"] == 32 and kwargs["rect"] is False
            started.set()
            assert release.wait(timeout=5)
            return []

    pool = ModelPool(1, lambda *_: Model(), lambda: None, build_timeout=2)
    monkeypatch.setattr(main, "_model_pool", pool)
    monkeypatch.setattr(main, "_predictor", YoloPredictor(pool))
    monkeypatch.setattr(main, "_evaluation_requests", store)
    monkeypatch.setattr(main, "effective_device", lambda _: "cpu")
    monkeypatch.setattr(
        main._managed_models,
        "receipt",
        lambda _: {
            **req.context.frozen_model_ref.model_dump(mode="json"),
            "framework": "ultralytics",
            "framework_version": "8.4.183",
            "model_family": "yolo11",
            "model_size": "n",
            "task": "detection",
        },
    )
    task = asyncio.create_task(main._run_sample_evaluation(req, operation=None))
    assert await asyncio.wait_for(asyncio.to_thread(started.wait, 3), timeout=3)
    task.cancel()
    await asyncio.sleep(0)
    try:
        with pytest.raises(HTTPException) as busy:
            store.seal(body.request_id, seal_request(body))
        assert busy.value.detail["reason"] == "evaluation_request_busy"
        assert (await pool.snapshot())["borrowers"] == 1
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await pool.snapshot())["borrowers"] == 0
    assert store.seal(body.request_id, seal_request(body))["state"] == "sealed"
    with pytest.raises(HTTPException) as stopped:
        await main._run_sample_evaluation(req, operation=None)
    assert stopped.value.status_code == 410


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
async def test_cold_native_loader_keeps_request_lock_after_borrower_leaves(
    tmp_path, monkeypatch, interruption
):
    import main
    from model_pool import ModelPool
    from predictor import YoloPredictor

    store = EvaluationRequestStore(tmp_path)
    req, body, _profile = qualified_batch(store)
    started, release = threading.Event(), threading.Event()
    native_calls = []

    def load(*_args):
        native_calls.append("load")
        started.set()
        assert release.wait(timeout=5)
        return SimpleNamespace(device="cpu")

    pool = ModelPool(1, load, lambda: None, build_timeout=0.02)
    monkeypatch.setattr(main, "_model_pool", pool)
    monkeypatch.setattr(main, "_predictor", YoloPredictor(pool))
    monkeypatch.setattr(main, "_evaluation_requests", store)
    monkeypatch.setattr(main, "effective_device", lambda _: "cpu")
    monkeypatch.setattr(
        main._managed_models,
        "receipt",
        lambda _: {
            **req.context.frozen_model_ref.model_dump(mode="json"),
            "framework": "ultralytics",
            "framework_version": "8.4.183",
            "model_family": "yolo11",
            "model_size": "n",
            "task": "detection",
        },
    )
    task = asyncio.create_task(main._run_sample_evaluation(req, operation=None))
    assert await asyncio.wait_for(asyncio.to_thread(started.wait, 3), timeout=3)
    if interruption == "cancel":
        task.cancel()
    await asyncio.sleep(0.06)
    if interruption == "cancel":
        task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done()
        assert (await pool.snapshot())["builders"] == 1
        with pytest.raises(HTTPException) as busy:
            store.seal(body.request_id, seal_request(body))
        assert busy.value.detail["reason"] == "evaluation_request_busy"
        with pytest.raises(HTTPException) as busy:
            with EvaluationRequestStore(tmp_path).execution(body):
                pytest.fail("A second native loader must not overlap")
        assert busy.value.status_code == 409 and native_calls == ["load"]
    finally:
        release.set()
    expected = (
        asyncio.CancelledError
        if interruption == "cancel"
        else main.ModelUnavailableError
    )
    with pytest.raises(expected):
        await task
    assert (await pool.snapshot())["builders"] == 0
    assert store.seal(body.request_id, seal_request(body))["state"] == "sealed"


def test_request_lock_and_tombstone_are_shared_with_a_real_other_process(tmp_path):
    store = EvaluationRequestStore(tmp_path)
    body = request(store)
    code = """
import json, sys
from pathlib import Path
from fastapi import HTTPException
from sample_evaluations import EvaluationRequest, EvaluationRequestStore, EvaluationSeal
store = EvaluationRequestStore(Path(sys.argv[1]))
body = EvaluationRequest.model_validate_json(sys.argv[2])
seal = EvaluationSeal.model_validate(body.model_dump(exclude={'request_id', 'input_sha256'}))
try:
    print(store.seal(body.request_id, seal)['state'])
except HTTPException as exc:
    print(exc.detail['reason'])
"""

    def other_process():
        return subprocess.run(
            [sys.executable, "-c", code, str(tmp_path), body.model_dump_json()],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()

    with store.execution(body):
        assert other_process() == "evaluation_request_busy"
    assert other_process() == "sealed"
    with pytest.raises(HTTPException) as late:
        with store.execution(body):
            pytest.fail("A tombstone from the other process must block late work")
    assert late.value.status_code == 410


@pytest.mark.asyncio
async def test_old_weights_require_explicit_runtime_pin_and_emit_actual_version(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import main
    from training_contract.inference_runtime import inference_runtime

    req, _body, _profile = qualified_batch(EvaluationRequestStore(tmp_path))
    req.context.sample_evaluation = None
    receipt = {
        **req.context.frozen_model_ref.model_dump(mode="json", exclude_none=True),
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "task": "detection",
    }
    monkeypatch.setattr(main._managed_models, "receipt", lambda _: receipt)
    predictor = SimpleNamespace(predict_one=AsyncMock(return_value=([], False, 0, 1)))
    monkeypatch.setattr(main, "_predictor", predictor)
    monkeypatch.setattr(main, "_model_pool", object())
    with pytest.raises(HTTPException) as error:
        await main._run_predict(req)
    assert error.value.status_code == 409
    predictor.predict_one.assert_not_awaited()
    actual = inference_runtime(receipt, "8.4.183")
    req.context.frozen_model_ref.inference_runtime = actual
    results = await main._run_predict(req)
    assert results[0].meta["inference_runtime"] == actual
    assert set(results[0].meta["trained_model"]) == {
        "model_version_id",
        "sha256",
        "classes_digest",
    }
    req.context.frozen_model_ref.inference_runtime = {**actual, "version": "8.4.184"}
    with pytest.raises(HTTPException) as error:
        await main._run_predict(req)
    assert error.value.status_code == 409
