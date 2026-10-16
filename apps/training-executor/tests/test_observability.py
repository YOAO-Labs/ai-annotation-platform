import json
import os
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

import psutil
import httpx
import pytest
from training_contract.observability import (
    LOG_BYTES,
    METRICS,
    REQUEST_BYTES,
    encoded,
    measurement,
    utc_now,
)

from training_executor.log_capture import LogCapture
from training_executor.observability import (
    NativeProgress,
    ObservationSpool,
    ObservationSession,
    ProcessSampler,
)


def sample(boot):
    return {
        "producer_boot_id": boot,
        "captured_at": utc_now(),
        "phase": "training",
        "phase_elapsed_seconds": 0,
        "progress": {},
        "metrics": {key: measurement(0) for key in METRICS},
    }


def test_spool_reopens_exact_pending_receipt_after_lost_response_and_producer_restart(
    tmp_path,
):
    path = tmp_path / "observations.sqlite"
    spool = ObservationSpool(path)
    boot = str(uuid.uuid4())
    spool.append("telemetry", sample(boot))
    spool.append(
        "logs", {"producer_boot_id": boot, "captured_at": utc_now(), "text": "safe\n"}
    )
    body = spool.seal(
        {"lease_token": "credential"},
        boot,
        {"protocol": 1, "peaks": {}, "latest": None},
    )
    spool.close()
    resumed = ObservationSpool(path)
    assert resumed.seal({}, str(uuid.uuid4()), {}) == body
    resumed.acknowledge(body, {**body, "telemetry_cursor": 1, "logs_cursor": 1})
    assert resumed.seal({}, boot, {}) is None
    assert resumed.append("telemetry", sample(boot))["sequence"] == 2
    resumed.close()


def test_spool_retention_keeps_watermarks_explicit_gaps_and_bounded_real_disk(tmp_path):
    path = tmp_path / "observations.sqlite"
    spool = ObservationSpool(path)
    boot = str(uuid.uuid4())
    for index in range(1000):
        spool.append("telemetry", sample(boot))
        spool.append(
            "logs",
            {"producer_boot_id": boot, "captured_at": utc_now(), "text": "界" * 682},
        )
    body = spool.seal({}, boot, {"protocol": 1, "peaks": {}, "latest": None})
    assert body["skipped"] == {
        "telemetry": {"after": 0, "through": 280},
        "logs": {"after": 0, "through": 488},
    }
    assert len(spool.recent("telemetry", 1000)) == 720
    assert len(spool.recent("logs", 1000)) == 512
    assert path.stat().st_size <= 8 * 1024 * 1024
    assert spool.db.execute("PRAGMA max_page_count").fetchone()[0] == 2048
    assert not path.with_name(path.name + "-wal").exists()
    spool.close()


def test_json_escape_expansion_reduces_batch_without_stalling(tmp_path):
    spool = ObservationSpool(tmp_path / "observations.sqlite")
    boot = str(uuid.uuid4())
    for _ in range(32):
        spool.append(
            "logs",
            {"producer_boot_id": boot, "captured_at": utc_now(), "text": "\t" * 2048},
        )
    body = spool.seal({}, boot, {"protocol": 1, "peaks": {}, "latest": None})
    assert 0 < len(body["logs"]) < 32 and len(encoded(body)) < REQUEST_BYTES
    spool.acknowledge(
        body,
        {**body, "telemetry_cursor": 0, "logs_cursor": body["logs"][-1]["sequence"]},
    )
    assert (
        spool.seal({}, boot, {})["logs"][0]["sequence"]
        == body["logs"][-1]["sequence"] + 1
    )
    spool.close()


def test_quota_pressure_shortens_window_and_exposes_gap_without_resetting_cursor(
    tmp_path,
):
    spool = ObservationSpool(tmp_path / "observations.sqlite")
    spool.db.execute("PRAGMA max_page_count=80")
    boot = str(uuid.uuid4())
    for index in range(200):
        spool.append(
            "logs",
            {
                "producer_boot_id": boot,
                "captured_at": utc_now(),
                "text": str(index).zfill(4) + "x" * 2044,
            },
        )
    body = spool.seal({}, boot, {"protocol": 1, "peaks": {}, "latest": None})
    assert body["skipped"]["logs"]["through"] > 0
    assert (
        spool.get("logs_sequence") == 200
        and spool.db.execute("PRAGMA page_count").fetchone()[0] <= 80
    )
    spool.close()


def test_final_summary_includes_last_native_progress_and_remains_immutable_during_upload(
    tmp_path,
):
    supervisor = SimpleNamespace(
        boot=str(uuid.uuid4()), runtime={"gpu_uuid": "GPU-owned"}
    )
    journal = {
        "claim": {"runtime": {}},
        "assignment": {
            "run_id": "run",
            "attempt_id": "attempt",
            "config_digest": "a" * 64,
        },
    }
    session = ObservationSession(supervisor, journal, tmp_path)
    session.sampler.attach(os.getpid(), psutil.Process().create_time())
    progress = {
        "phase": "evaluating",
        "progress": {"completed_epoch": 6},
        "captured_at": utc_now(),
    }
    (tmp_path / "progress.json").write_text(json.dumps(progress))
    session.freeze({"revision": 1})
    artifact = (tmp_path / "observability.json").read_bytes()
    assert json.loads(artifact)["native_progress"]["progress"]["completed_epoch"] == 6
    session.phase("uploading")
    (tmp_path / "progress.json").write_text("{}")
    session.freeze({"revision": 2})
    assert (tmp_path / "observability.json").read_bytes() == artifact
    session.stop()


def test_failure_classification_uses_exception_types_without_forcing_gpu_oom():
    import torch
    from training_contract.observability import (
        InputMismatchError,
        NativeRuntimeMismatch,
    )
    from training_executor.train import native_failure

    spec, progress = {"frozen_config": {}}, SimpleNamespace(phase_name="training")
    for error, expected in (
        (torch.cuda.OutOfMemoryError("password=private"), "cuda_oom"),
        (RuntimeError("CUDA out of memory"), "native_exception"),
        (InputMismatchError("bad input"), "input_mismatch"),
        (NativeRuntimeMismatch("bad runtime"), "runtime_mismatch"),
    ):
        receipt = native_failure(spec, progress, error)
        assert receipt["code"] == expected and receipt["source"] == "native"
        assert "private" not in receipt["diagnostic"]


def test_mismatched_ack_cannot_discard_pending_request(tmp_path):
    spool = ObservationSpool(tmp_path / "observations.sqlite")
    boot = str(uuid.uuid4())
    spool.append("telemetry", sample(boot))
    body = spool.seal({}, boot, {})
    with pytest.raises(ValueError, match="ACK identity"):
        spool.acknowledge(
            body,
            {
                "stream_id": str(uuid.uuid4()),
                "request_id": body["request_id"],
                "telemetry_cursor": 1,
                "logs_cursor": 0,
            },
        )
    assert spool.get("pending") == body and spool.get("telemetry_ack", 0) == 0
    spool.close()


def test_transient_authority_lock_conflict_retries_the_same_observation_receipt(
    tmp_path, monkeypatch
):
    supervisor = SimpleNamespace(
        boot=str(uuid.uuid4()),
        runtime={"gpu_uuid": "GPU-owned"},
        client=SimpleNamespace(base_url="http://127.0.0.1/", headers={}),
        fence=lambda journal: {},
        endpoint=lambda journal, suffix: suffix,
    )
    session = ObservationSession(supervisor, {"claim": {}, "assignment": {}}, tmp_path)
    session.log("safe\n")
    bodies = []

    class Transport:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, endpoint, json):
            bodies.append(json)
            if len(bodies) == 1:
                return httpx.Response(
                    409,
                    json={"detail": {"code": "project_busy"}},
                    request=httpx.Request("POST", "http://127.0.0.1/"),
                )
            session.done.set()
            return httpx.Response(
                200,
                json={
                    "stream_id": json["stream_id"],
                    "request_id": json["request_id"],
                    "telemetry_cursor": 0,
                    "logs_cursor": 1,
                },
                request=httpx.Request("POST", "http://127.0.0.1/"),
            )

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: Transport())
    monkeypatch.setattr(session.done, "wait", lambda seconds: None)
    session.transport()
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert session.spool.get("logs_ack") == 1
    session.stop()


def test_log_drain_redacts_a_real_pipe_and_recovers_segments_without_rewriting_source(
    tmp_path,
):
    capture = LogCapture(tmp_path)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os; os.write(1, b'password=private https://store/file?sig=private /state/host\\n')",
        ],
        stdout=subprocess.PIPE,
    )
    capture.start(process.stdout)
    process.wait(timeout=10)
    evidence = capture.finish()
    assert not evidence["drain_error"]
    original = (tmp_path / "train.log").read_bytes()
    assert b"private" not in original and b"/state" not in original
    (tmp_path / "train.log").unlink()
    recovered = LogCapture(tmp_path)
    assert recovered.finish()["recovered_segments"]
    assert (tmp_path / "train.log").read_bytes() == original


def test_head_tail_order_and_artifact_bound_with_rotations(tmp_path):
    capture = LogCapture(tmp_path)
    capture.half, capture.segment = 200, 100
    for index in range(200):
        capture.write((str(index).zfill(4) + " " + "x" * 39 + "\n").encode())
    evidence = capture.finish()
    text = (tmp_path / "train.log").read_text()
    assert text.startswith("0000 ") and text.endswith("0199 " + "x" * 39 + "\n")
    assert evidence["truncated"] and evidence["artifact_bytes"] <= LOG_BYTES
    indexes = [int(line[:4]) for line in text.splitlines() if line[:4].isdigit()]
    assert indexes == sorted(indexes)


def test_native_progress_uses_actual_resolved_closing_period_and_comparable_cycles(
    tmp_path,
):
    clock = [0.0]
    progress = NativeProgress(
        {"parameters": {"epochs": 8}}, tmp_path, clock=lambda: clock[0]
    )
    progress.started(closing_epochs=2)
    for epoch in range(1, 5):
        clock[0] += 10
        progress.epoch(epoch, raw_epoch_seconds=8)
    receipt = json.loads((tmp_path / "progress.json").read_text())
    assert not receipt["progress"]["cycles"][0]["comparable"]
    assert receipt["progress"]["eta"]["reason"] == "unobserved_augmentation_regime"
    assert receipt["progress"]["native_raw"]["epoch_seconds"] == 8
    assert receipt["allocator"]["cuda_allocated_bytes"]["value"] is None
    progress.started(closing_epochs=0)
    clock[0] += 10
    progress.epoch(5)
    assert json.loads((tmp_path / "progress.json").read_text())["progress"]["eta"][
        "available"
    ]


def test_native_progress_io_failure_does_not_interrupt_training_callback(
    tmp_path, monkeypatch
):
    progress = NativeProgress({"parameters": {"epochs": 8}}, tmp_path)
    monkeypatch.setattr(
        "training_executor.observability.atomic_json",
        lambda *args: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    progress.started(closing_epochs=0)
    progress.batch(2)
    progress.epoch(1)


def test_sampler_pid_reuse_and_unavailable_device_never_look_like_zero(
    tmp_path, monkeypatch
):
    sampler = ProcessSampler("GPU-owned")
    sampler.attach(os.getpid(), psutil.Process().create_time() - 10)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("nvidia-smi", 2)
        ),
    )
    values = sampler.sample()
    assert values["rss_bytes"]["reason"] == "pid_reused"
    assert values["gpu_utilization"]["reason"] == "device_unavailable"
    assert values["process_gpu_utilization"]["reason"] == "pid_namespace_unresolved"


def test_sampler_real_process_group_excludes_parallel_process_and_preserves_device_zero(
    monkeypatch,
):
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    other = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; x=bytearray(50*1024*1024); time.sleep(30)",
        ],
        start_new_session=True,
    )
    try:
        sampler = ProcessSampler("GPU-owned")
        sampler.attach(process.pid, psutil.Process(process.pid).create_time())
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(
                stdout="GPU-owned, 0, 100, 24576\n"
            ),
        )
        first = sampler.sample()
        time.sleep(0.05)
        second = sampler.sample()
        assert first["cpu_cores"]["reason"] == "baseline_pending"
        assert second["cpu_cores"]["value"] is not None
        assert (
            second["rss_bytes"]["value"]
            == psutil.Process(process.pid).memory_info().rss
        )
        assert second["gpu_utilization"] == measurement(0)
    finally:
        for child in (process, other):
            child.terminate()
            child.wait(timeout=10)
