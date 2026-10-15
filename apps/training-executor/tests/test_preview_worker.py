"""CPU supervision cannot release live children or change training controls."""

import json
import threading
import uuid
from types import SimpleNamespace

import httpx
import pytest

from training_executor.files import digest_file
from training_executor.preview_worker import PreviewSupervisor


@pytest.fixture
def supervisor(tmp_path):
    training = SimpleNamespace(
        state=tmp_path,
        models=tmp_path / "models",
        runtime={"verified": True},
        executor_id="owned",
        boot=str(uuid.uuid4()),
        shutdown=threading.Event(),
        stop_training=threading.Event(),
        heartbeat_done=threading.Event(),
        client=httpx.Client(
            base_url="http://platform/internal/training/",
            headers={"Authorization": "Bearer credential"},
        ),
    )
    owned = PreviewSupervisor(training)
    owned.models.mkdir()
    owned.heartbeat = lambda journal: None
    owned.lock = (tmp_path / "cpu.lock").open("a+")
    try:
        yield owned, training
    finally:
        owned.lock.close()
        owned.client.close()
        training.client.close()


def journal_for(owned):
    base = owned.models / "base.pt"
    base.write_bytes(b"controlled base")
    return {
        "phase": "preparing",
        "claim": {"executor_id": owned.executor_id, "boot_id": owned.boot},
        "assignment": {
            "preview_id": "owned-preview",
            "lease_token": "fence",
            "input_digest": "digest",
            "stop": False,
            "source_urls": {},
            "spec": {
                "count": 1,
                "sources": [],
                "config": {
                    "base_model": {"filename": "base.pt", "sha256": digest_file(base)}
                },
            },
        },
    }


def test_cpu_uses_independent_cancellation_and_publication_receipt(supervisor):
    owned, training = supervisor
    assert owned.stop_training is not training.stop_training
    assert owned.heartbeat_done is not training.heartbeat_done
    owned.stop_training.set()
    assert not training.stop_training.is_set()
    journal = journal_for(owned)
    journal["outcome"] = "failed"
    journal["error"] = "bounded failure"
    calls = []

    def api(endpoint, body):
        calls.append(json.loads(json.dumps(body)))
        if len(calls) == 1:
            raise httpx.ReadTimeout("response lost after accepting finish")
        return {}

    owned.api = api
    owned.shutdown.wait = lambda seconds: False
    assert owned.publish(journal, owned.state)
    assert calls[0] == calls[1]
    assert calls[0]["process_exited"] is True
    assert json.loads(owned.journal_path.read_text())["finish_body"] == calls[0]


def test_live_child_survives_pid_persistence_failure_and_blocks_same_boot_retry(
    supervisor, monkeypatch
):
    owned, _ = supervisor
    journal = journal_for(owned)
    process = SimpleNamespace(pid=424242, returncode=None, wait=lambda: None)
    monkeypatch.setattr(
        "training_executor.preview_worker.subprocess.Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(
        "training_executor.preview_worker.psutil.Process",
        lambda pid: (_ for _ in ()).throw(OSError("cannot record process")),
    )
    monkeypatch.setattr(
        "training_executor.preview_worker.live_group", lambda pid: [pid]
    )
    monkeypatch.setattr(
        "training_executor.preview_worker.stop_process",
        lambda child: (_ for _ in ()).throw(RuntimeError("not stopped")),
    )
    published = []
    owned.publish = lambda *args: published.append(args) or False
    with pytest.raises(RuntimeError, match="not stopped"):
        owned.execute(journal)
    assert owned.process is process and not owned.process_exited
    assert owned.heartbeat_done.is_set()
    persisted = json.loads(owned.journal_path.read_text())
    assert persisted["phase"] == "transforming" and "process" not in persisted
    with pytest.raises(RuntimeError, match="not stopped"):
        owned.execute(persisted, recovery=True)
    assert not published and "finish_body" not in persisted
    monkeypatch.setattr("training_executor.preview_worker.live_group", lambda pid: [])
    owned.execute(persisted, recovery=True)
    assert len(published) == 1 and owned.process_exited and owned.process is None
    assert persisted["outcome"] == "failed"


def test_same_boot_journal_cannot_claim_old_flock_as_exit_proof(
    supervisor, monkeypatch
):
    owned, _ = supervisor
    journal = journal_for(owned)
    journal["process"] = {"pid": 777}
    monkeypatch.setattr(
        "training_executor.preview_worker.live_group", lambda pid: [pid]
    )
    owned.publish = lambda *args: pytest.fail("live process was published")
    with pytest.raises(RuntimeError, match="alive"):
        owned.execute(journal, recovery=True)


def test_preview_child_hides_cuda_secrets_and_inherits_only_cpu_lock(
    supervisor, monkeypatch
):
    owned, training = supervisor
    journal = journal_for(owned)
    process = SimpleNamespace(pid=123, returncode=0, poll=lambda: 0, wait=lambda: 0)
    seen = []

    def popen(*args, **kwargs):
        seen.append(kwargs)
        return process

    monkeypatch.setenv("TRAINING_EXECUTOR_TOKEN", "private credential")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-owned-by-training")
    monkeypatch.setattr("training_executor.preview_worker.subprocess.Popen", popen)
    monkeypatch.setattr(
        "training_executor.preview_worker.psutil.Process",
        lambda pid: SimpleNamespace(create_time=lambda: 1.0),
    )
    monkeypatch.setattr("training_executor.preview_worker.live_group", lambda pid: [])
    owned.publish = lambda current, directory: (
        current["outcome"] == "succeeded" and False
    )
    owned.execute(journal)
    environment = seen[0]["env"]
    assert environment["CUDA_VISIBLE_DEVICES"] == ""
    assert not any(key.startswith("TRAINING_") for key in environment)
    assert seen[0]["pass_fds"] == (owned.lock.fileno(),)
    assert seen[0]["start_new_session"] is True
    assert not training.stop_training.is_set() and owned.process_exited


def test_lost_claim_response_retains_exact_preview_identity(supervisor):
    owned, _ = supervisor
    journal = {"claim": {"claim_id": str(uuid.uuid4())}, "phase": "claiming"}
    owned.save(journal)
    owned.api = lambda *args: (_ for _ in ()).throw(httpx.ReadTimeout("lost claim"))
    with pytest.raises(httpx.ReadTimeout):
        owned.claim(journal)
    recovered = json.loads(owned.journal_path.read_text())
    assert recovered == journal
    owned.api = lambda endpoint, body: {"assignment": {"preview_id": "same-preview"}}
    owned.claim(recovered)
    assert (
        json.loads(owned.journal_path.read_text())["assignment"]["preview_id"]
        == "same-preview"
    )
