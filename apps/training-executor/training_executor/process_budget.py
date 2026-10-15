"""One Popen owner serializes deadline, cancellation and exit accounting."""

import threading
import time

from training_contract.budget import receipt


class ProcessBudget:
    def __init__(self, budget, *, stop, live, on_deadline):
        self.budget = budget
        self.stop_process = stop
        self.live = live
        self.on_deadline = on_deadline
        self.process = None
        self.started = None
        self.finished = None
        self.deadline_triggered = False
        self.reason = None
        self.stop_error = None
        self.lock = threading.RLock()
        self.done = threading.Event()
        self.thread = None

    def launch(self, factory):
        self.started = time.monotonic()
        self.process = factory()
        # Retain the actual handle before starting threads, capture or durable IO.
        self.thread = threading.Thread(
            target=self.watch, name="native-budget", daemon=True
        )
        try:
            self.thread.start()
        except Exception:
            self.stop("failure")
            raise
        return self.process

    def watch(self):
        remaining = self.started + self.budget["allowance_seconds"] - time.monotonic()
        if self.done.wait(max(0, remaining)):
            return
        while not self.done.is_set():
            try:
                with self.lock:
                    if self.finished is not None:
                        return
                    self.process.poll()
                    if self.live(self.process.pid):
                        if not self.deadline_triggered:
                            self.deadline_triggered = True
                            self.reason = "deadline"
                            self.on_deadline()
                        self.stop_process(self.process)
                    self.confirm_exit()
            except Exception as exc:
                # Keep stopping independently even if the main owner is stuck in IO.
                self.stop_error = exc
                self.done.wait(0.2)

    def confirm_exit(self):
        with self.lock:
            if self.finished is not None:
                return
            if self.live(self.process.pid):
                raise RuntimeError("Native process group still owns its lease")
            self.process.wait()
            self.finished = time.monotonic()
            self.done.set()

    def stop(self, reason):
        with self.lock:
            if self.process is None or self.finished is not None:
                return
            if self.reason is None:
                self.reason = reason
            self.stop_process(self.process)
            self.confirm_exit()

    def accounting(self):
        with self.lock:
            if self.process is None:
                return receipt(self.budget, "not_started", seconds=0.0)
            self.confirm_exit()
            return receipt(
                self.budget,
                "exact",
                seconds=max(0.0, self.finished - self.started),
                deadline=self.deadline_triggered,
                exit_code=self.process.returncode,
                reason=self.reason,
            )
