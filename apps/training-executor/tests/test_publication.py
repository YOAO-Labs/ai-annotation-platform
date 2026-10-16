"""Publication failures must end safely without changing acknowledged receipts."""

import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from training_executor.worker import RetiredAttempt, Supervisor


@pytest.fixture
def publication(tmp_path, monkeypatch):
    supervisor = object.__new__(Supervisor)
    supervisor.runtime = {}
    supervisor.journal_path = tmp_path / "assignment.json"
    stopped = []
    waits = []

    def wait(seconds):
        waits.append(seconds)
        assert len(waits) <= 3, "A deterministic rejection was retried forever"

    supervisor.shutdown = SimpleNamespace(
        is_set=lambda: bool(stopped), set=lambda: stopped.append(True), wait=wait
    )
    supervisor.stop_training = SimpleNamespace(is_set=lambda: False)
    journal = {
        "claim": {"executor_id": "owned", "boot_id": "boot", "runtime": {}},
        "assignment": {
            "run_id": "run",
            "attempt_id": "attempt",
            "lease_token": "a" * 64,
            "config_digest": "b" * 64,
            "config": {},
        },
        "outcome": "succeeded",
        "native_accounting": {"state": "exact", "native_seconds": 12},
    }
    (tmp_path / "train.log").write_text("safe diagnostic\n")
    monkeypatch.setattr(
        httpx,
        "put",
        lambda url, **kwargs: httpx.Response(200, request=httpx.Request("PUT", url)),
    )
    return supervisor, journal, tmp_path, waits


def reject(status):
    response = httpx.Response(
        status, request=httpx.Request("POST", "https://api.test/rejected")
    )
    response.raise_for_status()


def targets(body):
    return {
        item["name"]: {"url": "https://storage/" + item["name"], "headers": {}}
        for item in body["artifacts"]
    }


@pytest.mark.parametrize("stage", ["heartbeat", "uploads", "finish"])
@pytest.mark.parametrize("status", [403, 409, 413, 422])
def test_permanent_api_rejection_publishes_persisted_failure(
    publication, stage, status
):
    supervisor, journal, directory, waits = publication
    (directory / "events.jsonl").write_text('{"kind":"uploading"}\n')
    calls = []
    finishes = []

    def api(endpoint, body):
        suffix = endpoint.rsplit("/", 1)[-1]
        calls.append(suffix)
        if suffix == stage and calls.count(stage) == 1:
            reject(status)
        if suffix == "uploads":
            return targets(body)
        if suffix == "finish":
            assert (
                json.loads(supervisor.journal_path.read_text())["finish_body"] == body
            )
            finishes.append(copy.deepcopy(body))
        return {"stop": False}

    supervisor.api = api
    assert supervisor.upload_and_finish(journal, directory)
    assert not waits
    assert calls.count(stage) == (2 if stage == "finish" else 1)
    assert finishes == [journal["finish_body"]]
    assert finishes[0]["status"] == "failed"
    assert finishes[0]["process_exited"] is True
    assert finishes[0]["failure"]["code"] == "upload_failed"
    assert finishes[0]["failure"]["config_digest"] == "b" * 64
    assert finishes[0]["native_accounting"] == {"state": "exact", "native_seconds": 12}


@pytest.mark.parametrize("failure", ["transport", 500, 408, 425, 429])
def test_transient_finish_failure_replays_exact_receipt_only(publication, failure):
    supervisor, journal, directory, waits = publication
    finishes = []
    calls = []

    def api(endpoint, body):
        suffix = endpoint.rsplit("/", 1)[-1]
        calls.append(suffix)
        if suffix == "uploads":
            return targets(body)
        if suffix == "finish":
            assert (
                json.loads(supervisor.journal_path.read_text())["finish_body"] == body
            )
            finishes.append(copy.deepcopy(body))
            if len(finishes) == 1:
                journal["outcome"] = "failed"
                if failure == "transport":
                    raise httpx.ReadTimeout("accepted, response lost")
                reject(failure)
        return {}

    supervisor.api = api
    assert supervisor.upload_and_finish(journal, directory)
    assert calls == ["uploads", "finish", "finish"]
    assert finishes[0] == finishes[1] and finishes[1]["status"] == "succeeded"
    assert len(waits) == 1


@pytest.mark.parametrize("stage", ["heartbeat", "finish"])
@pytest.mark.parametrize("reason", ["account_busy", "membership_busy", "project_busy"])
def test_named_authority_lock_conflict_retries_without_failed_terminal(
    publication, stage, reason
):
    supervisor, journal, directory, waits = publication
    (directory / "events.jsonl").write_text('{"kind":"uploading"}\n')
    requests = []
    finishes = []

    def respond(request):
        suffix = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        requests.append((suffix, body))
        if suffix == stage and sum(kind == stage for kind, _ in requests) == 1:
            return httpx.Response(409, json={"detail": {"reason": reason}})
        if suffix == "uploads":
            return httpx.Response(200, json=targets(body))
        if suffix == "finish":
            finishes.append(copy.deepcopy(body))
        return httpx.Response(200, json={"stop": False})

    with httpx.Client(
        base_url="https://api.test/", transport=httpx.MockTransport(respond)
    ) as supervisor.client:
        assert supervisor.upload_and_finish(journal, directory)
    repeated = [body for kind, body in requests if kind == stage]
    assert len(repeated) == 2
    assert repeated[0] == repeated[1]
    assert finishes == [journal["finish_body"]]
    assert finishes[0]["status"] == "succeeded"
    assert "publication_rejection" not in journal and len(waits) == 1


def test_failed_terminal_rejection_retains_journal_and_stops(publication, capsys):
    supervisor, journal, directory, waits = publication
    finishes = []

    def api(endpoint, body):
        if endpoint.endswith("uploads"):
            reject(422)
        if endpoint.endswith("finish"):
            finishes.append(copy.deepcopy(body))
            reject(422)
        return {}

    supervisor.api = api
    assert supervisor.upload_and_finish(journal, directory) is False
    assert supervisor.shutdown.is_set() and not waits
    persisted = json.loads(supervisor.journal_path.read_text())
    assert persisted["finish_body"] == finishes[0]
    assert persisted["finish_body"]["status"] == "failed"
    assert (directory / "train.log").is_file()
    assert "rejected (HTTP 422)" in capsys.readouterr().out


def test_storage_expired_signature_refreshes_authorization(publication, monkeypatch):
    supervisor, journal, directory, waits = publication
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
            403 if len(puts) == 1 else 200, request=httpx.Request("PUT", url)
        )

    supervisor.api = api
    monkeypatch.setattr(httpx, "put", put)
    assert supervisor.upload_and_finish(journal, directory)
    assert len(uploads) == 2 and len(waits) == 1
    assert journal["finish_body"]["status"] == "succeeded"


@pytest.mark.parametrize("status", [400, 413, 422])
def test_storage_permanent_rejection_publishes_failure(
    publication, monkeypatch, status
):
    supervisor, journal, directory, waits = publication
    puts = []

    def api(endpoint, body):
        return targets(body) if endpoint.endswith("uploads") else {}

    def put(url, **kwargs):
        puts.append(url)
        return httpx.Response(status, request=httpx.Request("PUT", url))

    supervisor.api = api
    monkeypatch.setattr(httpx, "put", put)
    assert supervisor.upload_and_finish(journal, directory)
    assert len(puts) == 1 and not waits
    assert journal["finish_body"]["status"] == "failed"
    assert journal["publication_rejection"] == {
        "stage": "storage",
        "status_code": status,
    }


def test_local_artifact_change_publishes_failure_without_upload(
    publication, monkeypatch
):
    supervisor, journal, directory, waits = publication
    calls = []

    def api(endpoint, body):
        suffix = endpoint.rsplit("/", 1)[-1]
        calls.append(suffix)
        if suffix == "uploads":
            (directory / "train.log").write_text("changed immutable file")
            return targets(body)
        return {}

    supervisor.api = api
    monkeypatch.setattr(
        httpx, "put", lambda *args, **kwargs: pytest.fail("Corrupt upload")
    )
    assert supervisor.upload_and_finish(journal, directory)
    assert calls == ["uploads", "finish"] and not waits
    assert journal["finish_body"]["failure"]["code"] == "upload_failed"


def test_failed_finish_lost_response_survives_restart_without_reupload(publication):
    supervisor, journal, directory, waits = publication
    bodies = []

    def api(endpoint, body):
        if endpoint.endswith("uploads"):
            reject(422)
        assert endpoint.endswith("finish")
        bodies.append(copy.deepcopy(body))
        supervisor.shutdown.set()
        raise httpx.ReadTimeout("Failed finish accepted, response lost")

    supervisor.api = api
    assert supervisor.upload_and_finish(journal, directory) is False
    recovered = json.loads(supervisor.journal_path.read_text())
    supervisor.shutdown.is_set = lambda: False
    supervisor.api = lambda endpoint, body: bodies.append(copy.deepcopy(body)) or {}
    assert supervisor.upload_and_finish(recovered, directory)
    assert bodies[0] == bodies[1] and bodies[1]["status"] == "failed"


def test_stale_attempt_acknowledges_fencing_without_fabricated_terminal(publication):
    supervisor, journal, directory, waits = publication
    supervisor.api = lambda *args: (_ for _ in ()).throw(RetiredAttempt())
    assert supervisor.upload_and_finish(journal, directory)
    assert "finish_body" not in journal and not waits


def test_shutdown_during_upload_retains_files_and_never_finishes(publication):
    supervisor, journal, directory, waits = publication
    calls = []

    def api(endpoint, body):
        calls.append(endpoint.rsplit("/", 1)[-1])
        supervisor.shutdown.set()
        return targets(body)

    supervisor.api = api
    assert supervisor.upload_and_finish(journal, directory) is False
    assert calls == ["uploads"] and "finish_body" not in journal
    assert supervisor.journal_path.is_file() and (directory / "train.log").is_file()


def test_failure_receipt_save_failure_cannot_publish_until_durable(publication):
    supervisor, journal, directory, waits = publication
    original_save = supervisor.save
    finishes = []

    def api(endpoint, body):
        if endpoint.endswith("uploads"):
            reject(422)
        finishes.append(copy.deepcopy(body))
        return {}

    def save(body):
        if "publication_rejection" in body:
            raise OSError("Failed terminal fsync rejected")
        original_save(body)

    supervisor.api = api
    supervisor.save = save
    with pytest.raises(OSError, match="fsync"):
        supervisor.upload_and_finish(journal, directory)
    assert not finishes
    supervisor.save = original_save
    assert supervisor.upload_and_finish(journal, directory)
    assert finishes == [json.loads(supervisor.journal_path.read_text())["finish_body"]]
