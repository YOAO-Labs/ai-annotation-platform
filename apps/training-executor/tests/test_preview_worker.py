"""CPU supervision cannot release live children or change training controls."""

import copy
import json
import threading
import uuid
from types import SimpleNamespace

import httpx
import pytest

from training_executor.files import digest_file
from training_executor.preview_worker import PreviewSupervisor
from training_executor.worker import RetiredAttempt


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


@pytest.fixture
def publication(supervisor, monkeypatch):
    owned, training = supervisor
    journal = journal_for(owned)
    journal["outcome"] = "succeeded"
    directory = owned.state / "previews" / journal["assignment"]["preview_id"]
    directory.mkdir(parents=True)
    (directory / "preview-0.png").write_bytes(b"bounded preview image")
    (directory / "report.json").write_text("{}")
    waits = []

    def wait(seconds):
        waits.append(seconds)
        assert len(waits) <= 3, "A deterministic preview rejection was retried forever"

    monkeypatch.setattr(owned.shutdown, "wait", wait)
    monkeypatch.setattr(
        httpx,
        "put",
        lambda url, **kwargs: httpx.Response(200, request=httpx.Request("PUT", url)),
    )
    return owned, training, journal, directory, waits


def targets(body):
    return {
        item["name"]: {"url": "https://storage.test/" + item["name"], "headers": {}}
        for item in body["artifacts"]
    }


def reject(status, *, reason=None):
    httpx.Response(
        status,
        json={"detail": {"reason": reason}} if reason else {},
        request=httpx.Request("POST", "https://api.test/rejected"),
    ).raise_for_status()


@pytest.mark.parametrize("stage", ["uploads", "finish"])
@pytest.mark.parametrize("status", [403, 409, 413, 422])
def test_permanent_preview_rejection_persists_failed_terminal(
    publication, stage, status
):
    owned, training, journal, directory, waits = publication
    calls = []
    finishes = []

    def api(endpoint, body):
        suffix = endpoint.rsplit("/", 1)[-1]
        calls.append(suffix)
        if suffix == stage and calls.count(stage) == 1:
            reject(status)
        if suffix == "uploads":
            return targets(body)
        finishes.append(copy.deepcopy(body))
        assert json.loads(owned.journal_path.read_text())["finish_body"] == body
        return {}

    owned.api = api
    assert owned.publish(journal, directory)
    assert not waits and finishes == [journal["finish_body"]]
    assert finishes[0]["status"] == "failed" and finishes[0]["process_exited"] is True
    assert calls.count(stage) == (2 if stage == "finish" else 1)
    assert not training.shutdown.is_set() and not training.stop_training.is_set()


def test_rejected_failed_preview_terminal_blocks_only_cpu_and_retains_journal(
    publication, capsys
):
    owned, training, journal, directory, waits = publication
    finishes = []

    def api(endpoint, body):
        if endpoint.endswith("finish"):
            finishes.append(copy.deepcopy(body))
        reject(422)

    owned.api = api
    owned.execute(journal, recovery=True)
    assert owned.publication_blocked and not waits
    persisted = json.loads(owned.journal_path.read_text())
    assert persisted["finish_body"] == finishes[0]
    assert persisted["finish_body"]["status"] == "failed"
    assert owned.process is None and owned.process_exited
    assert (directory / "report.json").is_file()
    assert not training.shutdown.is_set() and not training.stop_training.is_set()
    assert "rejected (HTTP 422)" in capsys.readouterr().out
    owned.execute = lambda *args, **kwargs: pytest.fail("Blocked CPU slot retried")
    owned.run()
    assert owned.journal_path.is_file() and (directory / "report.json").is_file()


@pytest.mark.parametrize("stage", ["uploads", "finish"])
@pytest.mark.parametrize("reason", ["account_busy", "membership_busy", "project_busy"])
def test_preview_named_lock_conflict_preserves_success(publication, stage, reason):
    owned, _, journal, directory, waits = publication
    requests = []

    def api(endpoint, body):
        suffix = endpoint.rsplit("/", 1)[-1]
        requests.append((suffix, copy.deepcopy(body)))
        if suffix == stage and sum(kind == stage for kind, _ in requests) == 1:
            reject(409, reason=reason)
        return targets(body) if suffix == "uploads" else {}

    owned.api = api
    assert owned.publish(journal, directory)
    repeated = [body for kind, body in requests if kind == stage]
    assert len(repeated) == 2 and repeated[0] == repeated[1]
    assert journal["outcome"] == journal["finish_body"]["status"] == "succeeded"
    assert len(waits) == 1 and "publication_rejection" not in journal


@pytest.mark.parametrize("status", [403, 404])
def test_expired_preview_put_refreshes_authorization(publication, monkeypatch, status):
    owned, _, journal, directory, waits = publication
    uploads = []
    puts = []

    def api(endpoint, body):
        if endpoint.endswith("uploads"):
            uploads.append(True)
            return targets(body)
        return {}

    def put(url, **kwargs):
        puts.append(url)
        return httpx.Response(
            status if len(puts) == 1 else 200, request=httpx.Request("PUT", url)
        )

    owned.api = api
    monkeypatch.setattr(httpx, "put", put)
    assert owned.publish(journal, directory)
    assert len(uploads) == 2 and len(waits) == 1
    assert journal["finish_body"]["status"] == "succeeded"


def test_preview_finish_save_failure_retries_durability_before_http(publication):
    owned, _, journal, directory, waits = publication
    original_save = owned.save
    failed = []
    finishes = []

    def save(body):
        if "finish_body" in body and not failed:
            failed.append(True)
            raise OSError("finish fsync failed")
        original_save(body)

    def api(endpoint, body):
        if endpoint.endswith("uploads"):
            return targets(body)
        assert json.loads(owned.journal_path.read_text())["finish_body"] == body
        finishes.append(copy.deepcopy(body))
        return {}

    owned.api = api
    owned.save = save
    assert owned.publish(journal, directory)
    assert failed and len(finishes) == 1 and len(waits) == 1


@pytest.mark.parametrize("status", [408, 425, 429, 500])
def test_transient_preview_finish_replays_exact_receipt(publication, status):
    owned, _, journal, directory, waits = publication
    bodies = []

    def api(endpoint, body):
        if endpoint.endswith("uploads"):
            return targets(body)
        bodies.append(copy.deepcopy(body))
        if len(bodies) == 1:
            journal["error"] = "later mutable error"
            owned.stop_training.set()
            reject(status)
        return {}

    owned.api = api
    assert owned.publish(journal, directory)
    assert bodies[0] == bodies[1] and bodies[1]["status"] == "succeeded"
    assert len(waits) == 1


@pytest.mark.parametrize("retired", [False, True])
def test_preview_acknowledgement_allows_owned_cleanup(publication, retired):
    owned, _, journal, directory, waits = publication

    def api(endpoint, body):
        if retired:
            raise RetiredAttempt()
        return targets(body) if endpoint.endswith("uploads") else {}

    owned.api = api
    owned.execute(journal, recovery=True)
    assert not directory.exists() and not owned.journal_path.exists()
    assert not waits


def test_restarted_preview_replays_retained_failed_receipt(publication):
    owned, training, journal, directory, waits = publication
    owned.api = lambda *args: reject(422)
    assert owned.publish(journal, directory) is False
    recovered = json.loads(owned.journal_path.read_text())
    previous = copy.deepcopy(recovered["finish_body"])
    restarted = PreviewSupervisor(training)
    bodies = []

    def api(endpoint, body):
        assert endpoint.endswith("finish")
        bodies.append(copy.deepcopy(body))
        return {}

    restarted.api = api
    try:
        assert restarted.publish(recovered, directory)
        assert bodies == [previous] and previous["status"] == "failed"
        assert not restarted.publication_blocked and not waits
    finally:
        restarted.client.close()


def test_cancellation_during_preview_upload_preserves_independent_stop_guard(
    publication, monkeypatch
):
    owned, training, journal, directory, waits = publication
    bodies = []

    def api(endpoint, body):
        if endpoint.endswith("uploads"):
            return targets(body)
        bodies.append(copy.deepcopy(body))
        return {}

    def put(url, **kwargs):
        owned.stop_training.set()
        return httpx.Response(200, request=httpx.Request("PUT", url))

    owned.api = api
    monkeypatch.setattr(httpx, "put", put)
    assert owned.publish(journal, directory)
    assert bodies[0]["status"] == "cancelled" and bodies[0]["process_exited"] is True
    assert not waits and not training.stop_training.is_set()
