import fcntl
import copy
import json
import os
import subprocess
import sys
import threading
import uuid
from types import SimpleNamespace
from zipfile import ZipFile

import httpx
import pytest

from training_executor.files import digest_file, extract_dataset, immutable_copy
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
                "framework_version": "8.4.183",
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
    assert report["evaluation"]["version"] == "8.4.183"


def test_segmentation_report_keeps_box_mask_metrics_and_native_fitness_separate():
    from training_contract.dataset import SEGMENTATION

    result = SimpleNamespace(
        box=SimpleNamespace(ap_class_index=[0]),
        class_result=lambda position: (0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2),
        results_dict={
            "metrics/mAP50-95(B)": 0.6,
            "metrics/mAP50-95(M)": 0.2,
            "fitness": 0.8,
        },
    )
    report = ultralytics_report(
        result,
        {
            "frozen_config": {
                "framework_version": "8.4.183",
                "classes": ["shape"],
                "validation_digest": "frozen",
                "configuration": {"identity": {"task": SEGMENTATION}},
            },
            "parameters": {"imgsz": 128},
        },
    )
    row = report["per_class"][0]
    assert row["mAP50-95(B)"] == 0.6 and row["mAP50-95(M)"] == 0.2
    assert row["precision(B)"] == 0.9 and row["precision(M)"] == 0.5
    assert "mAP50-95" not in row and report["summary"]["fitness"] == 0.8
    assert report["evaluation"]["task"] == "instance-segmentation"


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
        return {"stop": False, "reason": None}

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
    assignment = {
        "run_id": str(uuid.uuid4()),
        "attempt_id": str(uuid.uuid4()),
        "lease_token": "a" * 64,
        "config": {"parameters": {"epochs": 2}},
        "config_digest": "b" * 64,
        "dataset_url": "https://storage.test/dataset.zip",
        "resume": None,
    }
    supervisor.api = lambda endpoint, payload: {"assignment": assignment}
    supervisor.claim(recovered)
    saved = json.loads(supervisor.journal_path.read_text())["assignment"]
    assert saved == assignment
    assert saved["resume"] is None
    assert "native_budget" not in saved and "initialization" not in saved


def test_cancel_after_resume_download_preserves_input_before_child_starts(tmp_path):
    supervisor = object.__new__(Supervisor)
    supervisor.state = tmp_path
    supervisor.models = tmp_path / "models"
    supervisor.models.mkdir()
    base = supervisor.models / "base.pt"
    base.write_bytes(b"controlled base")
    supervisor.journal_path = tmp_path / "journal.json"
    supervisor.stop_training = threading.Event()
    supervisor.shutdown = threading.Event()
    supervisor.heartbeat_done = threading.Event()
    supervisor.heartbeat = lambda *args: None
    archive = tmp_path / "source.zip"
    with ZipFile(archive, "w") as package:
        package.writestr("data.yaml", "train: images/train")
    full_checkpoint = b"previously verified complete native checkpoint"
    config = {
        "base_model": {"filename": "base.pt", "sha256": digest_file(base)},
        "dataset_sha256": digest_file(archive),
        "framework": "ultralytics",
        "parameters": {"epochs": 10},
    }
    journal = {
        "phase": "preparing",
        "assignment": {
            "attempt_id": "attempt",
            "config": config,
            "dataset_url": "dataset",
            "resume": {
                "url": "resume",
                "epoch": 2,
                "sha256": "verified-by-download",
                "size_bytes": len(full_checkpoint),
            },
        },
    }

    def download(url, target, *args, **kwargs):
        target.write_bytes(
            archive.read_bytes() if url == "dataset" else full_checkpoint
        )
        if url == "resume":
            supervisor.stop_training.set()

    supervisor.download = download

    def publish(current, directory):
        assert current["outcome"] == "cancelled"
        pointer = json.loads((directory / "checkpoint.json").read_text())
        assert pointer["epoch"] == 2
        assert (directory / pointer["name"]).read_bytes() == full_checkpoint
        assert "process" not in current
        return False

    supervisor.upload_and_finish = publish
    supervisor.execute(journal)


@pytest.fixture
def controlled_child(tmp_path, monkeypatch):
    """Real local child/process group, with no framework or network workload."""
    from training_contract.catalog import digest

    supervisor = object.__new__(Supervisor)
    supervisor.state = tmp_path
    supervisor.boot = str(uuid.uuid4())
    supervisor.runtime = {"gpu_uuid": "GPU-owned"}
    supervisor.models = tmp_path / "models"
    supervisor.models.mkdir()
    base = supervisor.models / "base.pt"
    base.write_bytes(b"controlled base")
    archive = tmp_path / "source.zip"
    with ZipFile(archive, "w") as package:
        package.writestr("data.yaml", "train: images/train")
    config = {
        "base_model": {"filename": "base.pt", "sha256": digest_file(base)},
        "dataset_sha256": digest_file(archive),
        "framework": "ultralytics",
        "parameters": {"epochs": 10},
    }
    journal = {
        "phase": "preparing",
        "claim": {
            "executor_id": "owned",
            "boot_id": supervisor.boot,
            "runtime": supervisor.runtime,
        },
        "assignment": {
            "run_id": str(uuid.uuid4()),
            "attempt_id": str(uuid.uuid4()),
            "lease_token": "a" * 64,
            "config_digest": digest(config),
            "config": config,
            "dataset_url": "dataset",
            "resume": None,
        },
    }
    supervisor.journal_path = tmp_path / "assignment.json"
    supervisor.stop_training = threading.Event()
    supervisor.shutdown = threading.Event()
    supervisor.heartbeat_done = threading.Event()
    supervisor.heartbeat = lambda *args: None
    supervisor.download = lambda url, target, *args, **kwargs: target.write_bytes(
        archive.read_bytes()
    )
    supervisor.lock = (tmp_path / "gpu.lock").open("a+")
    fcntl.flock(supervisor.lock, fcntl.LOCK_EX)
    supervisor.active_process = supervisor.active_capture = None
    supervisor.test_script = (
        "print('password=private https://store/secret /host/input')"
    )
    supervisor.upload_and_finish = lambda *args: False
    monkeypatch.setattr(
        "training_executor.observability.ObservationSession",
        lambda *args: (_ for _ in ()).throw(RuntimeError("Observation unavailable")),
    )
    original = subprocess.Popen
    children = []

    def spawn(args, **kwargs):
        assert not any(key.startswith("TRAINING_") for key in kwargs["env"])
        child = original([sys.executable, "-c", supervisor.test_script], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("training_executor.worker.subprocess.Popen", spawn)
    try:
        yield supervisor, journal, children
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, 9)
            child.wait(timeout=10)
        if supervisor.active_capture and supervisor.active_capture.thread:
            supervisor.active_capture.thread.join(timeout=10)
        supervisor.lock.close()


def test_log_finalize_failure_retries_packaging_without_retraining(
    controlled_child, monkeypatch
):
    from training_executor.log_capture import LogCapture

    supervisor, journal, children = controlled_child
    original = LogCapture.finish
    calls = []

    def finish(capture):
        if not calls:
            calls.append("failed")
            raise OSError("Temporary log publication failure")
        return original(capture)

    monkeypatch.setattr(LogCapture, "finish", finish)
    published = []
    supervisor.upload_and_finish = lambda *args: published.append(True) or False
    with pytest.raises(OSError):
        supervisor.execute(journal)
    assert journal["outcome"] == "succeeded" and "artifacts" not in journal
    assert not published and supervisor.heartbeat_done.is_set()
    supervisor.execute(journal, recovery=True)
    assert len(children) == 1 and published == [True]
    log = (
        supervisor.state
        / "attempts"
        / journal["assignment"]["attempt_id"]
        / "train.log"
    )
    assert "private" not in log.read_text() and "Controlled training" in log.read_text()


def test_pid_persist_and_stop_failure_retains_popen_until_same_boot_exit_proof(
    controlled_child, monkeypatch
):
    supervisor, journal, children = controlled_child
    supervisor.test_script = "import time; print('started',flush=True); time.sleep(30)"
    original_save = supervisor.save
    original_stop = stop_process
    failed = []
    stopped = []

    def save(body):
        if body.get("process") and not failed:
            failed.append(True)
            raise OSError("PID journal write failed")
        original_save(body)

    def stop(child):
        if not stopped:
            stopped.append(True)
            raise RuntimeError("Group exit not yet confirmed")
        original_stop(child)

    monkeypatch.setattr(supervisor, "save", save)
    monkeypatch.setattr("training_executor.worker.stop_process", stop)
    published = []
    supervisor.upload_and_finish = lambda *args: published.append(True) or False
    with pytest.raises(RuntimeError, match="not yet confirmed"):
        supervisor.execute(journal)
    assert supervisor.active_process is children[0] and not published
    assert supervisor.heartbeat_done.is_set()
    supervisor.execute(journal, recovery=True)
    assert len(children) == 1 and supervisor.active_process is None
    assert children[0].returncode is not None and not live_group(children[0].pid)
    assert journal["failure"]["code"] == "execution_lost" and published == [True]


def test_signal_exit_never_guesses_oom_and_observation_failure_does_not_cancel(
    controlled_child,
):
    supervisor, journal, _ = controlled_child
    supervisor.test_script = "import os,signal; os.kill(os.getpid(),signal.SIGKILL)"
    supervisor.execute(journal)
    assert (
        journal["outcome"] == "failed" and journal["failure"]["code"] == "native_signal"
    )
    assert not supervisor.stop_training.is_set()


def budget_assignment(journal, allowance=0.1):
    from training_contract.budget import POLICY_DIGEST

    assignment = journal["assignment"]
    assignment["native_budget"] = {
        "policy_digest": POLICY_DIGEST,
        "reservation_id": str(uuid.uuid4()),
        "attempt_id": assignment["attempt_id"],
        "config_digest": assignment["config_digest"],
        "allowance_seconds": allowance,
    }


def test_budget_deadline_survives_blocked_pid_journal_and_preserves_exact_exit(
    controlled_child, monkeypatch
):
    supervisor, journal, children = controlled_child
    budget_assignment(journal)
    supervisor.test_script = "import time; time.sleep(30)"
    original = supervisor.save
    blocked = []

    def save(body):
        if body.get("process") and not blocked:
            blocked.append(True)
            assert supervisor.active_budget.done.wait(3), (
                "Deadline must stop independently of PID fsync"
            )
            raise OSError("PID fsync failed after deadline")
        original(body)

    monkeypatch.setattr(supervisor, "save", save)
    supervisor.execute(journal)
    assert len(children) == 1 and not live_group(children[0].pid)
    assert journal["native_accounting"]["state"] == "exact"
    assert journal["native_accounting"]["deadline_triggered"]
    assert journal["native_accounting"]["native_seconds"] >= 0.1


def test_spawn_intent_save_failure_never_starts_native_and_new_boot_intent_is_unknown(
    controlled_child, monkeypatch
):
    supervisor, journal, children = controlled_child
    budget_assignment(journal)
    original = supervisor.save
    failures = []

    def save(body):
        if body.get("spawn_intent") and not failures:
            failures.append(True)
            raise OSError("intent fsync failed")
        original(body)

    monkeypatch.setattr(supervisor, "save", save)
    supervisor.execute(journal)
    assert children == [] and journal["native_accounting"]["state"] == "not_started"
    journal.pop("native_accounting")
    journal.pop("outcome")
    journal["claim"]["boot_id"] = str(uuid.uuid4())
    supervisor.active_budget = None
    supervisor.execute(journal, recovery=True)
    assert children == [] and journal["native_accounting"]["state"] == "unknown"
    assert journal["native_accounting"]["native_seconds"] is None


@pytest.mark.parametrize("fail_save", [False, True])
def test_finish_lost_response_replays_persisted_complete_body(
    tmp_path, monkeypatch, fail_save
):
    supervisor = object.__new__(Supervisor)
    supervisor.shutdown = SimpleNamespace(
        is_set=lambda: False, wait=lambda seconds: None
    )
    supervisor.runtime = {}
    supervisor.journal_path = tmp_path / "assignment.json"
    journal = {
        "claim": {"executor_id": "owned", "boot_id": "boot", "runtime": {}},
        "assignment": {
            "attempt_id": "attempt",
            "run_id": "run",
            "lease_token": "a" * 64,
            "config_digest": "b" * 64,
            "config": {},
        },
        "outcome": "failed",
        "native_accounting": {"state": "unknown", "native_seconds": None},
    }
    (tmp_path / "train.log").write_text("safe diagnostic\n")
    finishes = []
    failed_saves = []
    original_save = supervisor.save

    def save(body):
        if fail_save and body.get("finish_body") and not failed_saves:
            failed_saves.append(True)
            raise OSError("finish fsync failed")
        original_save(body)

    supervisor.save = save

    def api(endpoint, body):
        if endpoint.endswith("uploads"):
            return {
                item["name"]: {"url": "https://storage/" + item["name"], "headers": {}}
                for item in body["artifacts"]
            }
        if endpoint.endswith("finish"):
            assert (
                json.loads(supervisor.journal_path.read_text())["finish_body"] == body
            )
            finishes.append(copy.deepcopy(body))
            if len(finishes) == 1:
                journal["error"] = "Later mutable diagnostic"
                journal["failure"] = {"code": "changed"}
                journal["native_accounting"] = {"state": "exact", "native_seconds": 99}
                raise httpx.ReadTimeout("Completion accepted but reply lost")
        return {}

    supervisor.api = api
    monkeypatch.setattr(
        httpx,
        "put",
        lambda url, **kwargs: httpx.Response(200, request=httpx.Request("PUT", url)),
    )
    assert supervisor.upload_and_finish(journal, tmp_path)
    assert finishes[0] == finishes[1] and "failure" not in finishes[0]
    assert "error_message" in finishes[0] and finishes[0]["error_message"] is None
    assert finishes[0]["native_accounting"] == {
        "state": "unknown",
        "native_seconds": None,
    }
    assert bool(failed_saves) is fail_save


def test_optional_observation_stop_failure_cannot_override_acknowledged_publication(
    controlled_child, monkeypatch
):
    supervisor, journal, _ = controlled_child
    done = threading.Event()
    stopped = []

    def stop():
        stopped.append(True)
        if len(stopped) == 1:
            raise RuntimeError("Optional thread still retiring")

    observation = SimpleNamespace(
        done=done,
        threads=[],
        start=lambda: None,
        stop=stop,
        sampler=SimpleNamespace(attach=lambda *args: None),
        freeze=lambda *args: None,
        phase=lambda *args: None,
        log=lambda *args: None,
    )
    monkeypatch.setattr(
        "training_executor.observability.ObservationSession", lambda *args: observation
    )
    supervisor.upload_and_finish = lambda *args: True
    supervisor.execute(journal)
    assert journal["outcome"] == "succeeded" and done.is_set()
    assert not supervisor.journal_path.exists()
    assert supervisor.retiring_observation is observation
    supervisor.reap_observation()
    assert supervisor.retiring_observation is None and len(stopped) == 2


@pytest.mark.parametrize("reject_terminal", [False, True])
def test_permanent_publication_rejection_never_leaves_native_child_running(
    controlled_child, reject_terminal
):
    supervisor, journal, children = controlled_child
    supervisor.upload_and_finish = Supervisor.upload_and_finish.__get__(supervisor)
    finishes = []

    def api(endpoint, body):
        assert all(
            child.poll() is not None and not live_group(child.pid) for child in children
        )
        if (
            endpoint.endswith("uploads")
            or reject_terminal
            and endpoint.endswith("finish")
        ):
            httpx.Response(
                422, request=httpx.Request("POST", "https://api.test/rejected")
            ).raise_for_status()
        if endpoint.endswith("finish"):
            finishes.append(copy.deepcopy(body))
        return {}

    supervisor.api = api
    supervisor.execute(journal)
    assert len(children) == 1 and not live_group(children[0].pid)
    assert (
        journal["outcome"] == "failed" and journal["failure"]["code"] == "upload_failed"
    )
    if reject_terminal:
        assert not finishes and supervisor.shutdown.is_set()
        assert supervisor.journal_path.is_file()
    else:
        assert finishes == [journal["finish_body"]]
        assert not supervisor.journal_path.exists()
