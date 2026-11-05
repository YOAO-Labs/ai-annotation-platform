"""Supervise the finite Mask consumer's process group and observed descendants."""

import builtins
import os
from pathlib import Path
import select
import signal
import subprocess
import time

from storage_s3_contract import ContractError, require

# Keep exact pidfds and Popen handles reachable if cooperative drain fails.
_unsettled_consumers = []


def _proc_stat(pid):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    fields = raw.rsplit(")", 1)[1].split()
    return fields[0], int(fields[1]), int(fields[2])


def _pidfd_live(descriptor):
    poller = select.poll()
    poller.register(descriptor, select.POLLIN)
    return not poller.poll(0)


def _track_consumer_descendants(root_pid, tracked):
    """Capture actual descendants by pidfd before parent exit can orphan them."""
    pending = [root_pid, *tracked]
    visited = set()
    while pending:
        parent = pending.pop()
        if parent in visited or (
            parent in tracked and not _pidfd_live(tracked[parent])
        ):
            continue
        visited.add(parent)
        try:
            tasks = list(Path(f"/proc/{parent}/task").iterdir())
        except FileNotFoundError:
            continue
        for task in tasks:
            try:
                children = (task / "children").read_text().split()
            except FileNotFoundError:
                continue
            for value in children:
                pid = int(value)
                if pid in tracked:
                    pending.append(pid)
                    continue
                observed = _proc_stat(pid)
                if observed is None or observed[1] != parent:
                    continue
                try:
                    descriptor = os.pidfd_open(pid, 0)
                except ProcessLookupError:
                    continue
                try:
                    observed = _proc_stat(pid)
                    if observed is None or observed[1] != parent:
                        continue
                    tracked[pid] = descriptor
                    descriptor = None
                    pending.append(pid)
                finally:
                    if descriptor is not None:
                        os.close(descriptor)


def _group_live(group):
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            observed = _proc_stat(int(path.name))
            if observed and observed[2] == group and observed[0] not in {"Z", "X"}:
                return True
    return False


def _signal_consumer(group, tracked, sig):
    # The leader is not reaped until this group is drained, so its process
    # group identity cannot be recycled while signalling.
    try:
        os.killpg(group, sig)
    except ProcessLookupError:
        pass
    for descriptor in tracked.values():
        try:
            signal.pidfd_send_signal(descriptor, sig)
        except ProcessLookupError:
            pass


def _drain_consumer(child, tracked):
    """Request cooperative exit and prove the exact group and descendants exited."""
    capture_failure = None
    try:
        _track_consumer_descendants(child.pid, tracked)
    except Exception as error:
        capture_failure = error
    _signal_consumer(child.pid, tracked, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while _group_live(child.pid) or any(_pidfd_live(fd) for fd in tracked.values()):
        if time.monotonic() >= deadline:
            unsettled = ContractError("raster_consumer_group_unsettled")
            # The caller must retain these exact handles rather than interpreting
            # a deadline as physical settlement.
            unsettled.child = child
            unsettled.pidfds = tracked
            if capture_failure is not None:
                raise builtins.BaseExceptionGroup(
                    "raster descendant capture and shutdown failed",
                    [capture_failure, unsettled],
                )
            raise unsettled
        time.sleep(0.05)
    result = child.wait(timeout=0.1)
    if capture_failure is not None:
        raise capture_failure
    return result


def run_product(command, environment, api, relay, *, cwd):
    tracked = {}
    observed = None
    retain = False
    failure = None
    interrupted_by = None

    def interrupted(signum, _frame):
        nonlocal interrupted_by
        interrupted_by = interrupted_by or signum

    handlers = {
        sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        child = subprocess.Popen(
            command, cwd=cwd, env=environment, start_new_session=True
        )
        try:
            while True:
                if interrupted_by is not None:
                    raise InterruptedError(f"raster_consumer_signal_{interrupted_by}")
                _track_consumer_descendants(child.pid, tracked)
                observed = os.waitid(
                    os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                )
                if api is not None:
                    require(api.poll() is None, "raster_api_exited")
                    require(
                        relay.seed.poll() is None and relay.failure is None,
                        "raster_fixture_relay_exited",
                    )
                if observed is not None:
                    break
                time.sleep(0.2)
        except BaseException as error:
            failure = error
        try:
            result = _drain_consumer(child, tracked)
        except BaseException as error:
            retain = True
            _unsettled_consumers.append((child, tracked))
            if failure is not None:
                raise builtins.BaseExceptionGroup(
                    "raster consumer and shutdown failed", [failure, error]
                )
            if observed is not None and observed.si_status:
                raise builtins.BaseExceptionGroup(
                    "raster consumer exit and shutdown failed",
                    [RuntimeError(f"raster_consumer_exit_{observed.si_status}"), error],
                )
            raise
        if interrupted_by is not None and failure is None:
            failure = InterruptedError(f"raster_consumer_signal_{interrupted_by}")
        if failure is not None:
            raise failure
        return result
    finally:
        if not retain:
            for descriptor in tracked.values():
                os.close(descriptor)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
