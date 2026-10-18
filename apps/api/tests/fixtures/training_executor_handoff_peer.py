"""Controlled real-HTTP executor peer used only by the opt-in handoff test.

This process uses the production Supervisor's claim, fence, journal and receipt
methods. Only runtime identity and the child workload are controlled; no model,
CUDA runtime, API/SQL adapter, or business owner is replaced.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx


_EVENT_PREFIX = "AAP_HANDOFF_EVENT "


def _emit(event: str, **fields: Any) -> None:
    sys.stdout.write(_EVENT_PREFIX + json.dumps({"event": event, **fields}) + "\n")
    sys.stdout.flush()


def _wait_for_file(path: Path, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.025)
    return path.exists()


def _load_runtime():
    runtime_json = os.environ["AAP_HANDOFF_RUNTIME_JSON"]
    runtime = json.loads(runtime_json)
    repo_root = Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(repo_root / "apps" / "training-executor"))
    from training_executor import worker

    # Keep the worker implementation, but make its hardware identity explicit
    # to this opt-in controlled-child fixture. No GPU or model code is loaded.
    worker.runtime_identity = lambda: runtime
    return worker


def _supervisor(worker):
    supervisor = worker.Supervisor()
    # Keep the real retry loop while avoiding five-second test backoff delays.
    supervisor.shutdown.wait = lambda _seconds: time.sleep(0.025)
    return supervisor


def _claim_body(supervisor, *, claim_id: uuid.UUID | None = None) -> dict[str, Any]:
    return {
        "executor_id": supervisor.executor_id,
        "boot_id": supervisor.boot,
        "claim_id": str(claim_id or uuid.uuid4()),
        "resource_id": supervisor.resource,
        "runtime": supervisor.runtime,
    }


def _lock_resource(worker, supervisor, *, wait: bool, timeout: float = 20.0):
    lock_path = supervisor.state / (
        "gpu-" + hashlib.sha256(supervisor.resource.encode()).hexdigest()[:24] + ".lock"
    )
    lock = lock_path.open("a+")
    deadline = time.monotonic() + timeout
    announced_wait = False
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock
        except BlockingIOError:
            if not wait:
                lock.close()
                return None
            if not announced_wait:
                _emit("waiting_local_lock")
                announced_wait = True
            if time.monotonic() >= deadline:
                lock.close()
                raise TimeoutError("timed out waiting for the owned resource flock")
            time.sleep(0.025)


def _new_journal(supervisor) -> dict[str, Any]:
    return {"claim": _claim_body(supervisor), "phase": "claiming"}


def _claim(supervisor, worker, journal, *, lose_first_response: bool):
    supervisor.save(journal)
    if not lose_first_response:
        assignment = supervisor.claim(journal)
        if assignment is not None:
            journal["assignment"] = assignment
            journal["phase"] = "preparing"
            supervisor.save(journal)
        return assignment

    base_url = os.environ["TRAINING_API_URL"].rstrip("/") + "/"
    headers = {"X-Training-Executor-Token": os.environ["TRAINING_EXECUTOR_TOKEN"]}
    first_claim = True

    class DropCommittedClaimResponse(httpx.BaseTransport):
        def __init__(self):
            self.inner = httpx.HTTPTransport()

        def handle_request(self, request):
            nonlocal first_claim
            response = self.inner.handle_request(request)
            if first_claim and request.url.path.endswith("/claim"):
                first_claim = False
                status_code = response.status_code
                response.read()
                response.close()
                if status_code != 200:
                    raise AssertionError("claim loss injection expected HTTP 200")
                _emit("claim_response_lost_after_http_commit")
                raise httpx.ReadTimeout(
                    "injected committed claim response loss", request=request
                )
            return response

        def close(self):
            self.inner.close()

    supervisor.client.close()
    supervisor.client = httpx.Client(
        base_url=base_url,
        headers=headers,
        timeout=10,
        trust_env=False,
        transport=DropCommittedClaimResponse(),
    )
    try:
        supervisor.claim(journal)
        raise AssertionError("claim response-loss injection did not fire")
    except httpx.ReadTimeout:
        pass

    replay = json.loads(supervisor.journal_path.read_text())
    if replay["claim"] != journal["claim"]:
        raise AssertionError("durable claim request identity changed before replay")
    supervisor.client.close()
    supervisor.client = httpx.Client(
        base_url=base_url,
        headers=headers,
        timeout=10,
        trust_env=False,
    )
    assignment = supervisor.claim(replay)
    if assignment is not None:
        replay["assignment"] = assignment
        replay["phase"] = "preparing"
        supervisor.save(replay)
        journal.update(replay)
    _emit(
        "claim_response_replayed",
        assigned=assignment is not None,
        run_id=assignment.get("run_id") if assignment else None,
        attempt_id=assignment.get("attempt_id") if assignment else None,
    )
    return assignment


def _spawn_locked_child(supervisor, worker, journal):
    lock = supervisor.lock
    if lock is None:
        raise RuntimeError("resource flock must be acquired before child startup")
    child_code = (
        "import signal,sys,time; "
        "signal.signal(signal.SIGINT, lambda *_: sys.exit(0)); "
        "time.sleep(600)"
    )
    journal["spawn_intent"] = True
    supervisor.save(journal)
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        start_new_session=True,
        pass_fds=(lock.fileno(),),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    journal["process"] = {
        "pid": child.pid,
        "started_at": worker.psutil.Process(child.pid).create_time(),
    }
    supervisor.save(journal)
    return child


def _finish_after_stop(supervisor, journal, child, *, lose_finish_response: bool):
    supervisor.terminate_process(child, "cancel")
    child.wait(timeout=5)
    journal["outcome"] = "cancelled"
    journal["error"] = journal.get("stop_reason") or "controlled handoff stop"
    journal["finish_body"] = supervisor.finish_body(journal)
    supervisor.save(journal)
    finish_requests: list[dict[str, Any]] = []
    original_api = supervisor.api

    def observe_api(endpoint, payload):
        if endpoint.endswith("/finish"):
            finish_requests.append(deepcopy(payload))
        return original_api(endpoint, payload)

    supervisor.api = observe_api
    if lose_finish_response:
        base_url = os.environ["TRAINING_API_URL"].rstrip("/") + "/"
        headers = {"X-Training-Executor-Token": os.environ["TRAINING_EXECUTOR_TOKEN"]}

        class DropCommittedFinishResponse(httpx.BaseTransport):
            def __init__(self):
                self.inner = httpx.HTTPTransport()
                self.dropped = False

            def handle_request(self, request):
                response = self.inner.handle_request(request)
                if not self.dropped and request.url.path.endswith("/finish"):
                    self.dropped = True
                    body = json.loads(request.content)
                    persisted = json.loads(supervisor.journal_path.read_text())[
                        "finish_body"
                    ]
                    if body != persisted:
                        raise AssertionError("finish body was not durable before HTTP")
                    status_code = response.status_code
                    response.read()
                    response.close()
                    if status_code != 200:
                        raise AssertionError("finish loss injection expected HTTP 200")
                    _emit("finish_response_lost_after_http_commit")
                    raise httpx.ReadTimeout(
                        "injected committed finish response loss", request=request
                    )
                return response

            def close(self):
                self.inner.close()

        supervisor.client.close()
        supervisor.client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=10,
            trust_env=False,
            transport=DropCommittedFinishResponse(),
        )
        supervisor.api = lambda endpoint, payload: observe_api(endpoint, payload)

    published = supervisor.upload_and_finish(journal, supervisor.state)
    if not published:
        raise RuntimeError("controlled stop receipt was not acknowledged")
    exact_replay = (
        len(finish_requests) == 2 and finish_requests[0] == finish_requests[1]
    )
    _emit(
        "finish_published",
        status=journal["finish_body"]["status"],
        replay_count=len(finish_requests),
        exact_replay=exact_replay,
    )


def _hold(supervisor, worker, *, lose_claim_response: bool, lose_finish_response: bool):
    lock = _lock_resource(worker, supervisor, wait=False)
    if lock is None:
        _emit("local_lock_busy")
        return
    supervisor.lock = lock
    journal = _new_journal(supervisor)
    assignment = _claim(
        supervisor, worker, journal, lose_first_response=lose_claim_response
    )
    if assignment is None:
        lock.close()
        _emit("no_assignment")
        return
    supervisor.lock = lock
    child = _spawn_locked_child(supervisor, worker, journal)
    _emit(
        "child_active",
        run_id=assignment["run_id"],
        attempt_id=assignment["attempt_id"],
        executor_id=supervisor.executor_id,
        boot_id=journal["claim"]["boot_id"],
        child_pid=child.pid,
        child_started_at=journal["process"]["started_at"],
        environment_keys=sorted(os.environ),
        platform_modules_loaded=[
            name
            for name in ("torch", "fastapi", "sqlalchemy", "app.main")
            if name in sys.modules
        ],
    )
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                result = supervisor.api(
                    supervisor.endpoint(journal, "heartbeat"),
                    supervisor.fence(journal),
                )
            except httpx.HTTPError:
                time.sleep(0.1)
                continue
            if result.get("stop"):
                journal["stop_reason"] = result.get("reason") or "stop requested"
                _emit(
                    "stop_requested", reason=journal["stop_reason"], child_pid=child.pid
                )
                gate = os.environ.get("AAP_HANDOFF_ALLOW_STOP_FILE")
                if gate and not _wait_for_file(Path(gate), timeout=20):
                    raise TimeoutError("parent did not release controlled stop gate")
                _finish_after_stop(
                    supervisor,
                    journal,
                    child,
                    lose_finish_response=lose_finish_response,
                )
                return
            time.sleep(0.1)
        raise TimeoutError("controlled child did not receive a stop request")
    finally:
        if child.poll() is None:
            worker.stop_process(child)
        lock.close()


def _claim_once(supervisor, worker):
    lock = _lock_resource(worker, supervisor, wait=False)
    if lock is None:
        _emit("local_lock_busy")
        return
    supervisor.lock = lock
    journal = _new_journal(supervisor)
    assignment = _claim(supervisor, worker, journal, lose_first_response=False)
    if assignment is None:
        _emit("no_assignment", executor_id=supervisor.executor_id)
    else:
        _emit(
            "assigned",
            run_id=assignment["run_id"],
            attempt_id=assignment["attempt_id"],
            executor_id=supervisor.executor_id,
            boot_id=journal["claim"]["boot_id"],
        )
    lock.close()


def _finish_once(supervisor, worker, *, gate_file: str | None):
    lock = _lock_resource(worker, supervisor, wait=False)
    if lock is None:
        _emit("local_lock_busy")
        return
    supervisor.lock = lock
    journal = json.loads(supervisor.journal_path.read_text())
    if "assignment" not in journal:
        raise RuntimeError("finish peer requires a durable assignment")
    journal["outcome"] = "failed"
    journal["error"] = "controlled commit rollback acceptance"
    journal["finish_body"] = supervisor.finish_body(journal)
    supervisor.save(journal)
    original_api = supervisor.api
    finish_count = 0
    finish_payloads: list[dict[str, Any]] = []

    def gated_api(endpoint, payload):
        nonlocal finish_count
        if endpoint.endswith("/finish"):
            finish_payloads.append(deepcopy(payload))
        try:
            result = original_api(endpoint, payload)
        except Exception as exc:
            if endpoint.endswith("/finish"):
                finish_count += 1
                status = (
                    exc.response.status_code
                    if isinstance(exc, httpx.HTTPStatusError)
                    else None
                )
                _emit("finish_failed", attempt=finish_count, status=status)
                if gate_file and not _wait_for_file(Path(gate_file), timeout=20):
                    raise TimeoutError(
                        "parent did not release finish retry gate"
                    ) from exc
            raise
        if endpoint.endswith("/finish"):
            finish_count += 1
            _emit("finish_succeeded", attempt=finish_count)
        return result

    supervisor.api = gated_api
    published = supervisor.upload_and_finish(journal, supervisor.state)
    _emit(
        "finish_peer_done",
        published=published,
        finish_count=finish_count,
        exact_replay=(
            len(finish_payloads) == 2 and finish_payloads[0] == finish_payloads[1]
        ),
    )
    lock.close()


def _recover_and_claim(supervisor, worker):
    lock = _lock_resource(worker, supervisor, wait=True, timeout=25)
    if lock is None:
        raise RuntimeError("resource lock was not acquired")
    supervisor.lock = lock
    journal = json.loads(supervisor.journal_path.read_text())
    previous = journal.get("process") or {}
    if previous.get("pid") and worker.live_group(previous["pid"]):
        raise RuntimeError("old child still holds the execution process group")
    old_attempt_id = journal["assignment"]["attempt_id"]
    # Exercise the actual Supervisor recovery path after the replacement
    # process has acquired the inherited resource flock and proven the child
    # process group is gone. This terminal receipt is committed over real HTTP.
    supervisor.execute(journal, recovery=True)
    if supervisor.journal_path.exists():
        raise RuntimeError("recovered attempt journal was not acknowledged/retired")
    _emit(
        "old_attempt_recovered",
        attempt_id=old_attempt_id,
        new_boot_id=str(supervisor.boot),
    )
    next_journal = _new_journal(supervisor)
    assignment = _claim(supervisor, worker, next_journal, lose_first_response=False)
    if assignment is None:
        _emit("no_assignment_after_recovery")
    else:
        _emit(
            "next_attempt_claimed",
            run_id=assignment["run_id"],
            attempt_id=assignment["attempt_id"],
            executor_id=supervisor.executor_id,
            boot_id=next_journal["claim"]["boot_id"],
        )
    lock.close()


def main() -> int:
    if os.name != "posix":
        raise RuntimeError(
            "the real executor flock/process-group acceptance requires POSIX"
        )
    worker = _load_runtime()
    supervisor = _supervisor(worker)
    mode = sys.argv[1]
    try:
        if mode == "hold":
            _hold(
                supervisor,
                worker,
                lose_claim_response=os.environ.get("AAP_HANDOFF_LOSE_CLAIM") == "1",
                lose_finish_response=os.environ.get("AAP_HANDOFF_LOSE_FINISH") == "1",
            )
        elif mode == "claim-once":
            _claim_once(supervisor, worker)
        elif mode == "finish-once":
            _finish_once(
                supervisor,
                worker,
                gate_file=os.environ.get("AAP_HANDOFF_RETRY_GATE"),
            )
        elif mode == "recover-and-claim":
            _recover_and_claim(supervisor, worker)
        else:
            raise ValueError("unknown controlled executor peer mode")
    finally:
        supervisor.client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
