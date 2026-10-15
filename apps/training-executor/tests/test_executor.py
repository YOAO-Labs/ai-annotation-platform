import fcntl
import json
import os
import subprocess
import sys
import threading
from types import SimpleNamespace
from zipfile import ZipFile

import httpx
import pytest

from training_executor.files import extract_dataset, immutable_copy
from training_executor.worker import Supervisor, live_group, stop_process
from training_executor.train import ultralytics_report


def test_report_keeps_observed_class_identity_and_validation_scope():
    result = SimpleNamespace(
        box=SimpleNamespace(ap_class_index=[1]),
        class_result=lambda position: (0.7, 0.6, 0.5, 0.4),
        results_dict={"metrics/mAP50(B)": 0.5},
    )
    report = ultralytics_report(
        result,
        {
            "frozen_config": {
                "classes": ["car", "person"],
                "validation_digest": "fixed",
            },
            "parameters": {"imgsz": 128},
        },
    )
    assert report["per_class"] == [
        {
            "index": 1,
            "name": "person",
            "precision": 0.7,
            "recall": 0.6,
            "mAP50": 0.5,
            "mAP50-95": 0.4,
        }
    ]
    assert report["validation_digest"] == "fixed"
    assert report["evaluation"]["independent_test_set"] is False


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "a/../../escape", "a\\escape"]
)
def test_dataset_rejects_paths_outside_materialization(tmp_path, name):
    archive = tmp_path / "dataset.zip"
    with ZipFile(archive, "w") as package:
        package.writestr(name, b"unexpected")
        package.writestr("data.yaml", "train: images/train")
    with pytest.raises(ValueError, match="Unsafe"):
        extract_dataset(archive, tmp_path / "dataset")
    assert not (tmp_path / "escape").exists()


def test_dataset_and_immutable_checkpoint_integrity(tmp_path):
    archive = tmp_path / "dataset.zip"
    with ZipFile(archive, "w") as package:
        package.writestr("data.yaml", "train: images/train")
        package.writestr("images/train/sample.png", b"image")
    yaml = extract_dataset(archive, tmp_path / "dataset")
    assert yaml.is_absolute()
    source, target = tmp_path / "source.pt", tmp_path / "checkpoint.pt"
    source.write_bytes(b"first checkpoint")
    initial = immutable_copy(source, target)
    assert immutable_copy(source, target) == initial
    source.write_bytes(b"changed checkpoint")
    with pytest.raises(ValueError, match="different content"):
        immutable_copy(source, target)
    assert target.read_bytes() == b"first checkpoint"


def test_child_inherits_resource_lock_until_process_group_exits(tmp_path):
    with (tmp_path / "gpu.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            pass_fds=(lock.fileno(),),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            lock.close()
            with (tmp_path / "gpu.lock").open("a+") as competitor:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                assert live_group(process.pid)
                stop_process(process)
                assert not live_group(process.pid)
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, 9)
                process.wait()


def test_final_events_are_drained_before_artifact_finish(tmp_path, monkeypatch):
    supervisor = object.__new__(Supervisor)
    supervisor.shutdown = threading.Event()
    supervisor.runtime = {"torch": "verified"}
    supervisor.journal_path = tmp_path / "journal.json"
    journal = {
        "claim": {
            "executor_id": "owned",
            "boot_id": "boot",
            "runtime": {"torch": "training-runtime"},
        },
        "assignment": {
            "attempt_id": "attempt",
            "run_id": "run",
            "lease_token": "secret",
            "config_digest": "digest",
            "config": {},
        },
        "outcome": "succeeded",
    }
    (tmp_path / "train.log").write_text("finished")
    (tmp_path / "events.jsonl").write_text(
        "".join(
            json.dumps({"kind": "epoch", "epoch": index + 1, "metrics": {}}) + "\n"
            for index in range(101)
        )
        + '{"unfinished":'
    )
    calls = []

    def api(endpoint, payload):
        calls.append((endpoint.rsplit("/", 1)[-1], payload))
        if endpoint.endswith("uploads"):
            return {
                item["name"]: {
                    "url": "https://storage.test/" + item["name"],
                    "headers": {},
                }
                for item in payload["artifacts"]
            }
        return {"stop": False}

    supervisor.api = api
    monkeypatch.setattr(
        httpx,
        "put",
        lambda url, **kwargs: httpx.Response(200, request=httpx.Request("PUT", url)),
    )
    assert supervisor.upload_and_finish(journal, tmp_path)
    assert [kind for kind, _ in calls] == [
        "heartbeat",
        "heartbeat",
        "uploads",
        "finish",
    ]
    assert calls[1][1]["events"][0]["sequence"] == 101
    assert "secret" not in (tmp_path / "manifest.json").read_text()
    assert (
        json.loads((tmp_path / "manifest.json").read_text())["runtime"]["torch"]
        == "training-runtime"
    )


def test_claim_request_is_replayable_after_lost_http_response(tmp_path):
    supervisor = object.__new__(Supervisor)
    supervisor.journal_path = tmp_path / "journal.json"
    journal = {"claim": {"claim_id": "same-id"}, "phase": "claiming"}
    supervisor.save(journal)
    supervisor.api = lambda endpoint, payload: (_ for _ in ()).throw(
        httpx.ReadTimeout("lost response")
    )
    with pytest.raises(httpx.ReadTimeout):
        supervisor.claim(journal)
    recovered = json.loads(supervisor.journal_path.read_text())
    assert recovered["claim"]["claim_id"] == "same-id"
    supervisor.api = lambda endpoint, payload: {
        "assignment": {"attempt_id": "same-attempt"}
    }
    supervisor.claim(recovered)
    assert (
        json.loads(supervisor.journal_path.read_text())["assignment"]["attempt_id"]
        == "same-attempt"
    )
