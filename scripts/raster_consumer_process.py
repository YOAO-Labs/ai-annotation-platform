"""Supervise the finite Mask consumer's process group and observed descendants."""

import builtins
import errno
import os
from pathlib import Path
import signal
import subprocess
import time

from linux_pidfd import (
    open_pidfd,
    pidfd_alive as _pidfd_live,
    pidfd_send_signal,
    waitid_pidfd,
)
from storage_s3_contract import ContractError, require

_unsettled_consumers = []


def _proc_stat(pid):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    fields = raw.rsplit(")", 1)[1].split()
    return fields[0], int(fields[1]), int(fields[2])


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
        except (FileNotFoundError, ProcessLookupError):
            continue
        for task in tasks:
            try:
                children = (task / "children").read_text().split()
            except (FileNotFoundError, ProcessLookupError):
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
                    descriptor = open_pidfd(pid)
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


def _track_group_members(group, tracked):
    """Hold exact identities for live group members before signalling them."""
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        pid = int(path.name)
        if pid == group or pid in tracked:
            continue
        observed = _proc_stat(pid)
        if observed is None or observed[2] != group or observed[0] in {"Z", "X"}:
            continue
        try:
            descriptor = open_pidfd(pid)
        except ProcessLookupError:
            continue
        observed = _proc_stat(pid)
        if observed is None or observed[2] != group:
            os.close(descriptor)
            continue
        tracked[pid] = descriptor


def _signal_consumer(group, tracked, sig):
    leader = open_pidfd(group)
    try:
        if _pidfd_live(leader):
            pidfd_send_signal(leader, sig)
    finally:
        os.close(leader)
    signalled = {}
    for pid, descriptor in tracked.items():
        if not _pidfd_live(descriptor):
            continue
        try:
            pidfd_send_signal(descriptor, sig)
        except ProcessLookupError:
            continue
        signalled[pid] = descriptor
    return signalled


def _descendant_outcomes(signalled, *, reap):
    outcomes = {}
    options = os.WEXITED | os.WNOHANG | (0 if reap else os.WNOWAIT)
    for pid, descriptor in signalled.items():
        if _pidfd_live(descriptor):
            outcomes[pid] = {"state": "live"}
            continue
        try:
            observed = waitid_pidfd(descriptor, options)
        except OSError as error:
            outcomes[pid] = {
                "state": "unknown",
                "error": type(error).__name__,
                "errno": error.errno,
            }
            continue
        outcomes[pid] = (
            None
            if observed is None
            else {
                "code": observed.si_code,
                "status": observed.si_status,
            }
        )
    return outcomes


def _drain_consumer(child, tracked, *, request_stop=True):
    """Observe exact exit; only interrupted/failed consumers need a stop request."""
    capture_failure = None
    try:
        _track_consumer_descendants(child.pid, tracked)
        _track_group_members(child.pid, tracked)
    except Exception as error:
        capture_failure = error
    signalled = (
        _signal_consumer(child.pid, tracked, signal.SIGTERM) if request_stop else {}
    )
    deadline = time.monotonic() + 3
    while _group_live(child.pid) or any(_pidfd_live(fd) for fd in tracked.values()):
        if time.monotonic() >= deadline:
            unsettled = ContractError("raster_consumer_group_unsettled")
            unsettled.child = child
            unsettled.pidfds = tracked
            unsettled.descendant_outcomes = _descendant_outcomes(tracked, reap=False)
            observed = os.waitid(
                os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
            )
            unsettled.leader_status = (
                {"code": observed.si_code, "status": observed.si_status}
                if observed
                else None
            )
            if capture_failure is not None:
                raise builtins.BaseExceptionGroup(
                    "raster descendant capture and shutdown failed",
                    [capture_failure, unsettled],
                )
            raise unsettled
        time.sleep(0.05)
    result = child.wait(timeout=0.1)
    outcomes = _descendant_outcomes(tracked, reap=True)
    if any(
        outcome is None
        or outcome.get("code") != os.CLD_EXITED
        or outcome.get("status") != 0
        for pid, outcome in outcomes.items()
        if not (
            pid not in signalled
            and outcome is not None
            and outcome.get("errno") == errno.ECHILD
        )
    ):
        error = ContractError("raster_consumer_descendant_exit_unsettled")
        error.descendant_outcomes = outcomes
        error.leader_status = result
        raise error
    if capture_failure is not None:
        capture_failure.descendant_outcomes = outcomes
        capture_failure.leader_status = result
        raise capture_failure
    return result


def run_product(command, environment, api, relay, *, cwd, web=None, images=None):
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
                if web is not None:
                    web.observe()
                if images is not None:
                    images.observe()
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
            result = _drain_consumer(
                child,
                tracked,
                request_stop=(
                    failure is not None
                    or observed is None
                    or observed.si_code != os.CLD_EXITED
                    or observed.si_status != 0
                ),
            )
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
