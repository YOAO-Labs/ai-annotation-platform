import os
import subprocess
import sys
import threading
import time
import uuid

import pytest

from training_contract.budget import POLICY_DIGEST
from training_executor.process_budget import ProcessBudget
from training_executor.worker import Supervisor, live_group, stop_process


def owner(seconds=0.15, **options):
    pin = {
        "policy_digest": POLICY_DIGEST,
        "reservation_id": str(uuid.uuid4()),
        "attempt_id": str(uuid.uuid4()),
        "config_digest": "a" * 64,
        "allowance_seconds": seconds,
    }
    return ProcessBudget(
        pin,
        stop=options.get("stop", stop_process),
        live=live_group,
        on_deadline=options.get("on_deadline", lambda: None),
    )


def spawn(source="import time; time.sleep(30)"):
    return subprocess.Popen(
        [sys.executable, "-c", source],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def clean(process):
    if live_group(process.pid):
        os.killpg(process.pid, 9)
    process.wait()


def test_deadline_stops_process_while_main_owner_cannot_journal_or_call_api():
    called = threading.Event()
    timer = owner(on_deadline=called.set)
    process = timer.launch(spawn)
    try:
        # The supervisor's main owner can be blocked; the deadline has no HTTP/IO dependency.
        assert timer.done.wait(3)
        assert called.is_set() and not live_group(process.pid)
        measured = timer.accounting()
        assert measured["deadline_triggered"] and measured["native_seconds"] >= 0.15
        assert measured["overrun_seconds"] == pytest.approx(
            measured["native_seconds"] - 0.15
        )
    finally:
        clean(process)


def test_natural_exit_freezes_once_and_late_deadline_cannot_rewrite_it():
    timer = owner(1)
    process = timer.launch(lambda: spawn("pass"))
    try:
        process.wait(timeout=2)
        first = timer.accounting()
        assert first["exit_code"] == 0 and not first["deadline_triggered"]
        timer.stop("cancel")
        assert timer.accounting() == first
    finally:
        clean(process)


def test_parent_exit_does_not_end_accounting_while_descendant_is_alive():
    timer = owner(0.3)
    process = timer.launch(
        lambda: spawn(
            "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])"
        )
    )
    try:
        process.wait(timeout=2)
        with pytest.raises(RuntimeError, match="still owns"):
            timer.accounting()
        assert timer.done.wait(3)
        assert timer.accounting()["deadline_triggered"]
        assert not live_group(process.pid)
    finally:
        clean(process)


def test_stop_failure_retains_handle_and_counts_retry_interval():
    calls = []
    failed = threading.Event()

    def flaky(process):
        calls.append(process)
        if len(calls) == 1:
            failed.set()
            raise OSError("stop unavailable")
        stop_process(process)

    timer = owner(0.05, stop=flaky)
    process = timer.launch(spawn)
    try:
        assert failed.wait(2)
        assert timer.process is process and live_group(process.pid)
        assert timer.done.wait(3)
        assert timer.stop_error
        measured = timer.accounting()
        assert measured["native_seconds"] > measured["allowance_seconds"]
        assert len(calls) == 2 and measured["stop_reason"] == "deadline"
    finally:
        clean(process)


def test_manual_and_deadline_stop_are_serialized():
    calls = []
    in_stop = threading.Event()

    def slow(process):
        assert not in_stop.is_set()
        in_stop.set()
        calls.append(process)
        time.sleep(0.1)
        stop_process(process)
        in_stop.clear()

    timer = owner(0.05, stop=slow)
    process = timer.launch(spawn)
    try:
        manual = threading.Thread(target=timer.stop, args=("cancel",))
        manual.start()
        manual.join(timeout=3)
        timer.thread.join(timeout=3)
        assert len(calls) == 1 and timer.done.is_set()
        assert timer.accounting()["overrun_seconds"] > 0
    finally:
        clean(process)


def test_failed_spawn_is_proven_zero_and_failed_watchdog_start_stops_child(monkeypatch):
    timer = owner()
    with pytest.raises(OSError):
        timer.launch(lambda: (_ for _ in ()).throw(OSError("spawn failed")))
    assert timer.accounting()["state"] == "not_started"
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda self: (_ for _ in ()).throw(RuntimeError("thread unavailable")),
    )
    with pytest.raises(RuntimeError, match="thread unavailable"):
        timer.launch(spawn)
    assert timer.process and not live_group(timer.process.pid)
    assert timer.accounting()["state"] == "exact"


def test_recovery_without_timer_never_fabricates_elapsed_time():
    supervisor = object.__new__(Supervisor)
    journal = {"assignment": {"native_budget": owner().budget}, "spawn_intent": True}
    supervisor.freeze_accounting(journal)
    assert journal["native_accounting"]["state"] == "unknown"
    assert journal["native_accounting"]["native_seconds"] is None
    no_spawn = {"assignment": journal["assignment"]}
    supervisor.freeze_accounting(no_spawn)
    assert no_spawn["native_accounting"]["state"] == "not_started"
