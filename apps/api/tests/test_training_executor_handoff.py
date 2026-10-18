"""Opt-in real-TCP handoff checks for the existing Training Executor contract.

Run only from this worktree's explicitly provisioned disposable test mode with
``AAP_TRAINING_EXECUTOR_HANDOFF_ACCEPTANCE=1``. The API uses its normal
``get_db`` factory; executor peers use the production Supervisor's journal,
claim, fence and publication code with a controlled runtime identity and tiny
CPU child. These checks do not qualify a model, GPU, production capacity or Go.
"""

from __future__ import annotations

import asyncio
import json
import os
import select as select_io
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.engine import make_url

from app.config import settings
from app.core.security import create_access_token
from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import (
    TrainingAttempt,
    TrainingResourceLease,
    TrainingRun,
)
from app.db.models.user import User
from tests.factory import create_user
from tests.test_training_runs import ready_dataset

pytestmark = pytest.mark.skipif(
    os.environ.get("AAP_TRAINING_EXECUTOR_HANDOFF_ACCEPTANCE") != "1"
    or os.environ.get("AAP_WORKTREE_MODE") != "test",
    reason="Requires the explicitly provisioned local disposable test environment",
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_API_ROOT = _REPO_ROOT / "apps" / "api"
_PEER_MODULE = "tests.fixtures.training_executor_handoff_peer"
_TOKEN = "local-handoff-executor-test-credential-" + "x" * 40
_GPU = "GPU-11111111-2222-3333-4444-555555555555"
_IMAGE = "sha256:" + "1" * 64
_RUNTIME = {
    "image_digest": _IMAGE,
    "gpu_uuid": _GPU,
    "frameworks": {"ultralytics": "8.4.177"},
}
_CATALOG = {
    "ultralytics": {
        "version": "8.4.177",
        "image_digest": _IMAGE,
        "base_models": {"yolo11n": {"sha256": "2" * 64, "filename": "yolo11n.pt"}},
    }
}
_EVENT_PREFIX = b"AAP_HANDOFF_EVENT "


@dataclass
class HandoffWorld:
    maker: async_sessionmaker
    api_base: str
    internal_api: str
    executor_token: str
    resource_id: str
    owner_id: uuid.UUID
    replacement_owner_id: uuid.UUID
    project_id: uuid.UUID
    dataset_id: uuid.UUID
    owner_token: str


class Peer:
    def __init__(self, process: subprocess.Popen[bytes]):
        self.process = process
        self._buffer = bytearray()
        self._diagnostics: list[str] = []

    def read_event(self, *, timeout: float = 10) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                if not line.startswith(_EVENT_PREFIX):
                    self._diagnostics.append(line.decode(errors="replace")[:200])
                    self._diagnostics = self._diagnostics[-12:]
                    continue
                return json.loads(line[len(_EVENT_PREFIX) :])
            remaining = max(0, deadline - time.monotonic())
            ready, _, _ = select_io.select([self.process.stdout], [], [], remaining)
            if not ready:
                break
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                raise AssertionError(
                    f"executor peer exited before its next event ({self.process.poll()}): "
                    + "\n".join(self._diagnostics)
                )
            self._buffer.extend(chunk)
        raise AssertionError("timed out waiting for the controlled executor event")

    def read_until(
        self, event_name: str, *, timeout: float = 15
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        events: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            event = self.read_event(timeout=max(0.1, deadline - time.monotonic()))
            events.append(event)
            if event.get("event") == event_name:
                return events
        raise AssertionError(f"executor peer did not emit {event_name!r}")


class OwnedProcesses:
    """Track only this test's process groups and intentionally orphaned child PIDs."""

    def __init__(self):
        self.peers: list[Peer] = []
        self.child_groups: dict[int, float] = {}

    def add(self, process: subprocess.Popen[bytes]) -> Peer:
        peer = Peer(process)
        self.peers.append(peer)
        return peer

    def remember_child(self, pid: int, started_at: float) -> None:
        self.child_groups[pid] = started_at

    def kill_child(self, pid: int) -> bool:
        import psutil

        try:
            child = psutil.Process(pid)
            if child.create_time() != self.child_groups[pid] or os.getpgid(pid) != pid:
                return False
            os.killpg(pid, signal.SIGKILL)
            return True
        except (ProcessLookupError, psutil.NoSuchProcess):
            return False

    def terminate_peer(self, peer: Peer, *, sig: int = signal.SIGTERM) -> None:
        process = peer.process
        if process.poll() is None:
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)

    def cleanup(self) -> None:
        for peer in reversed(self.peers):
            self.terminate_peer(peer)
        for pgid in self.child_groups:
            self.kill_child(pgid)


def _write_executor_identity(state_dir: Path, executor_id: str) -> None:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (state_dir / "executor.json").write_text(json.dumps({"id": executor_id}))
    (state_dir / "executor.json").chmod(0o600)


def _spawn_peer(
    mode: str,
    world: HandoffWorld,
    state_dir: Path,
    *,
    lose_claim: bool = False,
    lose_finish: bool = False,
    stop_gate: Path | None = None,
    retry_gate: Path | None = None,
) -> subprocess.Popen[bytes]:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "TMPDIR", "PYTHONPATH")
        if name in os.environ
    }
    training_executor = str(_REPO_ROOT / "apps" / "training-executor")
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (training_executor, existing_pythonpath) if item
    )
    env.update(
        TRAINING_API_URL=world.internal_api,
        TRAINING_EXECUTOR_TOKEN=world.executor_token,
        TRAINING_EXECUTOR_TOKEN_FILE="",
        TRAINING_NODE_ID=world.resource_id.split("/", 1)[0],
        TRAINING_GPU_UUID=_GPU,
        TRAINING_STATE_DIR=str(state_dir),
        TRAINING_MODELS_DIR=str(state_dir / "models"),
        AAP_HANDOFF_RUNTIME_JSON=json.dumps(_RUNTIME),
    )
    if lose_claim:
        env["AAP_HANDOFF_LOSE_CLAIM"] = "1"
    if lose_finish:
        env["AAP_HANDOFF_LOSE_FINISH"] = "1"
    if stop_gate is not None:
        env["AAP_HANDOFF_ALLOW_STOP_FILE"] = str(stop_gate)
    if retry_gate is not None:
        env["AAP_HANDOFF_RETRY_GATE"] = str(retry_gate)
    process = subprocess.Popen(
        [sys.executable, "-m", _PEER_MODULE, mode],
        cwd=_API_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        start_new_session=True,
    )
    return process


@pytest.fixture
async def training_handoff_api(test_db_url: str, test_engine, tmp_path: Path):
    parsed = make_url(test_db_url)
    if parsed.get_backend_name() != "postgresql" or not (
        parsed.database or ""
    ).endswith("_test"):
        pytest.fail(
            "handoff acceptance requires the guard-validated disposable _test database"
        )
    if os.name != "posix":
        pytest.skip("executor flock/process-group handoff is POSIX-only")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    log_path = tmp_path / "training-handoff-api.log"
    log_path.touch(mode=0o600)
    log_file = log_path.open("ab", buffering=0)

    env = os.environ.copy()
    env.update(
        AAP_WORKTREE_MODE="test",
        DATABASE_URL=test_db_url,
        MIGRATION_DATABASE_URL="",
        TEST_DATABASE_URL="",
        E2E_SEED_ENABLED="false",
        SECRET_KEY=settings.secret_key,
        TRAINING_ENABLED="true",
        TRAINING_EXECUTOR_TOKEN=_TOKEN,
        TRAINING_NODE_ID=f"handoff-{uuid.uuid4().hex[:12]}",
        TRAINING_GPU_UUID=_GPU,
        TRAINING_CATALOG_JSON=json.dumps(_CATALOG),
        TRAINING_HEARTBEAT_TIMEOUT_SECONDS="30",
        MINIO_ENDPOINT=settings.minio_endpoint,
        MINIO_ACCESS_KEY=settings.minio_access_key,
        MINIO_SECRET_KEY=settings.minio_secret_key,
        MINIO_TRAINING_BUCKET=settings.minio_training_bucket,
        MINIO_USE_SSL=str(settings.minio_use_ssl).lower(),
        AAP_HANDOFF_LISTEN_FD=str(listener.fileno()),
    )
    script = (
        "import os,socket,uvicorn; "
        "sock=socket.socket(fileno=int(os.environ['AAP_HANDOFF_LISTEN_FD'])); "
        "cfg=uvicorn.Config('app.main:app',log_level='warning',access_log=False); "
        "uvicorn.Server(cfg).run(sockets=[sock])"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=_API_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        pass_fds=(listener.fileno(),),
        start_new_session=True,
    )
    listener.close()
    api_base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 25
    try:
        async with httpx.AsyncClient(timeout=1, trust_env=False) as probe:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(
                        f"opt-in Uvicorn exited during startup ({process.returncode}); "
                        f"bounded log is at {log_path}"
                    )
                try:
                    response = await probe.get(api_base + "/openapi.json")
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            else:
                pytest.fail(
                    f"opt-in Uvicorn did not become ready; bounded log is at {log_path}"
                )
        yield HandoffWorld(
            maker=async_sessionmaker(test_engine, expire_on_commit=False),
            api_base=api_base,
            internal_api=api_base + "/api/v1/internal/training",
            executor_token=_TOKEN,
            resource_id=f"{env['TRAINING_NODE_ID']}/{_GPU}",
            owner_id=uuid.UUID(int=0),
            replacement_owner_id=uuid.UUID(int=0),
            project_id=uuid.UUID(int=0),
            dataset_id=uuid.UUID(int=0),
            owner_token="",
        )
    finally:
        try:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
        finally:
            log_file.close()


@pytest.fixture
async def handoff_world(training_handoff_api: HandoffWorld):
    world = training_handoff_api
    suffix = uuid.uuid4().hex
    async with world.maker() as setup:
        owner = await create_user(
            setup,
            "project_admin",
            f"handoff-owner-{suffix}@test.local",
            "Handoff owner",
        )
        replacement = await create_user(
            setup,
            "project_admin",
            f"handoff-replacement-{suffix}@test.local",
            "Handoff replacement",
        )
        project, dataset = await ready_dataset(setup, owner)
        await setup.commit()
        world.owner_id = owner.id
        world.replacement_owner_id = replacement.id
        world.project_id = project.id
        world.dataset_id = dataset.id
        world.owner_token = create_access_token(subject=str(owner.id), role=owner.role)
    try:
        yield world
    finally:
        async with world.maker() as cleanup:
            await cleanup.execute(
                delete(TrainingResourceLease).where(
                    TrainingResourceLease.resource_id == world.resource_id
                )
            )
            await cleanup.execute(
                delete(TrainingRun).where(TrainingRun.project_id == world.project_id)
            )
            await cleanup.execute(
                delete(AsyncJob).where(AsyncJob.project_id == world.project_id)
            )
            await cleanup.execute(
                delete(TrainingDatasetVersion).where(
                    TrainingDatasetVersion.id == world.dataset_id
                )
            )
            await cleanup.execute(delete(Project).where(Project.id == world.project_id))
            await cleanup.execute(
                delete(User).where(
                    User.id.in_([world.owner_id, world.replacement_owner_id])
                )
            )
            await cleanup.commit()


async def _create_run(client: httpx.AsyncClient, world: HandoffWorld, *, name: str):
    payload: dict[str, Any] = {
        "name": name,
        "dataset_id": str(world.dataset_id),
        "request_id": str(uuid.uuid4()),
        "parameters": {"epochs": 3},
    }
    headers = {"Authorization": f"Bearer {world.owner_token}"}
    preflight = await client.post(
        f"/api/v1/projects/{world.project_id}/training-runs:preflight",
        headers=headers,
        json=payload,
    )
    assert preflight.status_code == 200, "training preflight should use the live API"
    payload["preflight_digest"] = preflight.json()["config_digest"]
    response = await client.post(
        f"/api/v1/projects/{world.project_id}/training-runs",
        headers=headers,
        json=payload,
    )
    assert response.status_code == 202, (
        "training admission should persist through the live API"
    )
    return response.json(), payload


async def _observed_run(world: HandoffWorld, run_id: str):
    async with world.maker() as observer:
        run = await observer.get(TrainingRun, uuid.UUID(run_id))
        if run is None:
            return None
        attempt = await observer.get(TrainingAttempt, run.current_attempt_id)
        lease = await observer.get(TrainingResourceLease, world.resource_id)
        return {
            "run_status": run.status,
            "attempt_id": attempt.id if attempt else None,
            "attempt_status": attempt.status if attempt else None,
            "attempt_executor_id": attempt.executor_id if attempt else None,
            "attempt_boot_id": attempt.boot_id if attempt else None,
            "attempt_failure": attempt.failure if attempt else None,
            "finish_request_digest": (
                attempt.finish_request_digest if attempt else None
            ),
            "lease_attempt_id": lease.attempt_id if lease else None,
            # This is a resource-poll projection. Attempt ownership is asserted
            # separately from TrainingAttempt.executor_id/boot_id and attempt_id.
            "lease_executor_projection": lease.executor_id if lease else None,
        }


async def _observed_attempts(
    world: HandoffWorld, run_id: str
) -> list[tuple[uuid.UUID, str]]:
    async with world.maker() as observer:
        rows = await observer.execute(
            select(TrainingAttempt.id, TrainingAttempt.status).where(
                TrainingAttempt.run_id == uuid.UUID(run_id)
            )
        )
        return list(rows.all())


def _peer_state(tmp_path: Path, label: str, executor_id: str) -> Path:
    state = tmp_path / label
    _write_executor_identity(state, executor_id)
    (state / "models").mkdir(mode=0o700)
    return state


async def _post_heartbeat(
    client: httpx.AsyncClient, world: HandoffWorld, journal: dict[str, Any]
):
    assignment = journal["assignment"]
    claim = journal["claim"]
    return await client.post(
        f"/api/v1/internal/training/attempts/{assignment['attempt_id']}/heartbeat",
        headers={"x-training-executor-token": world.executor_token},
        json={
            "executor_id": claim["executor_id"],
            "boot_id": claim["boot_id"],
            "lease_token": assignment["lease_token"],
            "events": [],
        },
    )


async def _assert_stale_heartbeat(
    client: httpx.AsyncClient, world: HandoffWorld, journal: dict[str, Any]
) -> None:
    response = await _post_heartbeat(client, world, journal)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_attempt"


async def test_live_executor_claim_replay_competition_cancel_and_finish_replay(
    handoff_world: HandoffWorld, tmp_path: Path, record_testsuite_property
):
    world = handoff_world
    owner_headers = {"Authorization": f"Bearer {world.owner_token}"}
    async with httpx.AsyncClient(
        base_url=world.api_base, timeout=10, trust_env=False
    ) as client:
        first_run, payload = await _create_run(client, world, name="Handoff run one")
        next_run, _ = await _create_run(client, world, name="Handoff run two")
        replay = await client.post(
            f"/api/v1/projects/{world.project_id}/training-runs",
            headers=owner_headers,
            json=payload,
        )
        assert replay.status_code == 202
        assert replay.json()["id"] == first_run["id"]
        assert await _observed_attempts(world, first_run["id"]) == [
            (uuid.UUID(first_run["current_attempt_id"]), "queued")
        ]

        processes = OwnedProcesses()
        try:
            owner_id = f"handoff-owner-{uuid.uuid4().hex[:12]}"
            owner_state = _peer_state(tmp_path, "owner-state", owner_id)
            stop_gate = tmp_path / "allow-owner-stop"
            owner = processes.add(
                _spawn_peer(
                    "hold",
                    world,
                    owner_state,
                    lose_claim=True,
                    lose_finish=True,
                    stop_gate=stop_gate,
                )
            )
            claim_events = await asyncio.to_thread(
                owner.read_until, "child_active", timeout=15
            )
            assert any(
                item["event"] == "claim_response_lost_after_http_commit"
                for item in claim_events
            )
            replay_event = next(
                item
                for item in claim_events
                if item["event"] == "claim_response_replayed"
            )
            assert replay_event["assigned"] is True
            active = claim_events[-1]
            assert active["platform_modules_loaded"] == []
            assert not {
                "DATABASE_URL",
                "MIGRATION_DATABASE_URL",
                "TEST_DATABASE_URL",
                "MINIO_ACCESS_KEY",
                "MINIO_SECRET_KEY",
                "REDIS_URL",
            }.intersection(active["environment_keys"])
            record_testsuite_property(
                "executor_peer_environment_keys", ",".join(active["environment_keys"])
            )
            processes.remember_child(active["child_pid"], active["child_started_at"])
            journal_path = owner_state / "assignment.json"
            durable_journal = json.loads(journal_path.read_text())
            assert durable_journal["claim"]["claim_id"]
            assert durable_journal["assignment"]["attempt_id"] == active["attempt_id"]

            before = await _observed_run(world, first_run["id"])
            assert before["run_status"] == before["attempt_status"] == "preparing"
            assert str(before["lease_attempt_id"]) == active["attempt_id"]
            assert before["attempt_executor_id"] == owner_id

            contender_id = f"handoff-contender-{uuid.uuid4().hex[:12]}"
            contender_state = _peer_state(tmp_path, "contender-state", contender_id)
            contender = processes.add(_spawn_peer("claim-once", world, contender_state))
            denied = await asyncio.to_thread(contender.read_event, timeout=10)
            assert denied["event"] == "no_assignment"
            assert contender.process.wait(timeout=5) == 0
            still_owned = await _observed_run(world, first_run["id"])
            assert still_owned["attempt_executor_id"] == owner_id
            assert str(still_owned["lease_attempt_id"]) == active["attempt_id"]
            record_testsuite_property(
                "resource_lease_executor_projection_during_competition",
                still_owned["lease_executor_projection"],
            )
            assert await _observed_attempts(world, next_run["id"]) == [
                (uuid.UUID(next_run["current_attempt_id"]), "queued")
            ]

            cancelled = await client.post(
                f"/api/v1/projects/{world.project_id}/training-runs/{first_run['id']}/cancel",
                headers=owner_headers,
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelling"
            stopped = await asyncio.to_thread(owner.read_event, timeout=10)
            assert stopped["event"] == "stop_requested"
            assert os.kill(active["child_pid"], 0) is None
            during_cancel = await _observed_run(world, first_run["id"])
            assert (
                during_cancel["run_status"]
                == during_cancel["attempt_status"]
                == "cancelling"
            )
            assert str(during_cancel["lease_attempt_id"]) == active["attempt_id"]

            stop_gate.touch()
            finish_events = await asyncio.to_thread(
                owner.read_until, "finish_published", timeout=15
            )
            assert any(
                item["event"] == "finish_response_lost_after_http_commit"
                for item in finish_events
            )
            published = finish_events[-1]
            assert published["status"] == "cancelled"
            assert published["replay_count"] == 2
            assert published["exact_replay"] is True
            assert owner.process.wait(timeout=5) == 0

            after_cancel = await _observed_run(world, first_run["id"])
            assert (
                after_cancel["run_status"]
                == after_cancel["attempt_status"]
                == "cancelled"
            )
            assert after_cancel["lease_attempt_id"] is None
            await _assert_stale_heartbeat(client, world, durable_journal)

            successor = processes.add(_spawn_peer("claim-once", world, contender_state))
            next_claim = await asyncio.to_thread(successor.read_event, timeout=10)
            assert next_claim["event"] == "assigned"
            assert next_claim["run_id"] == next_run["id"]
            assert successor.process.wait(timeout=5) == 0
        finally:
            processes.cleanup()


async def test_live_executor_stops_after_authority_revocation_without_releasing_early(
    handoff_world: HandoffWorld,
    tmp_path: Path,
):
    world = handoff_world
    async with httpx.AsyncClient(
        base_url=world.api_base, timeout=10, trust_env=False
    ) as client:
        run_data, _ = await _create_run(client, world, name="Controlled stop")
        owner_id = f"handoff-stop-{uuid.uuid4().hex[:12]}"
        state_dir = _peer_state(tmp_path, "stop-state", owner_id)
        stop_gate = tmp_path / "allow-stop"
        processes = OwnedProcesses()
        try:
            owner = processes.add(
                _spawn_peer("hold", world, state_dir, stop_gate=stop_gate)
            )
            active = await asyncio.to_thread(
                owner.read_until, "child_active", timeout=15
            )
            active_event = active[-1]
            processes.remember_child(
                active_event["child_pid"], active_event["child_started_at"]
            )
            journal = json.loads((state_dir / "assignment.json").read_text())

            async with world.maker() as transfer:
                project = await transfer.get(Project, world.project_id)
                project.owner_id = world.replacement_owner_id
                await transfer.commit()

            stop_requested = await asyncio.to_thread(owner.read_event, timeout=10)
            assert stop_requested["event"] == "stop_requested"
            assert "权限已撤销" in stop_requested["reason"]
            assert os.kill(active_event["child_pid"], 0) is None
            before_exit = await _observed_run(world, run_data["id"])
            assert before_exit["run_status"] == "cancelling"
            assert before_exit["attempt_status"] == "cancelling"
            assert str(before_exit["lease_attempt_id"]) == active_event["attempt_id"]

            stop_gate.touch()
            done_events = await asyncio.to_thread(
                owner.read_until, "finish_published", timeout=15
            )
            assert done_events[-1]["status"] == "cancelled"
            assert owner.process.wait(timeout=5) == 0
            after_exit = await _observed_run(world, run_data["id"])
            assert (
                after_exit["run_status"] == after_exit["attempt_status"] == "cancelled"
            )
            assert after_exit["lease_attempt_id"] is None
            await _assert_stale_heartbeat(client, world, journal)
        finally:
            processes.cleanup()


async def test_hard_exit_holds_inherited_flock_until_recovery_and_next_claim(
    handoff_world: HandoffWorld, tmp_path: Path
):
    world = handoff_world
    async with httpx.AsyncClient(
        base_url=world.api_base, timeout=10, trust_env=False
    ) as client:
        first_run, _ = await _create_run(client, world, name="Interrupted run")
        next_run, _ = await _create_run(client, world, name="Post-restart run")
        state_dir = _peer_state(
            tmp_path, "shared-state", f"handoff-restart-{uuid.uuid4().hex[:12]}"
        )
        processes = OwnedProcesses()
        try:
            old = processes.add(_spawn_peer("hold", world, state_dir))
            active_events = await asyncio.to_thread(
                old.read_until, "child_active", timeout=15
            )
            active = active_events[-1]
            processes.remember_child(active["child_pid"], active["child_started_at"])
            old_journal = json.loads((state_dir / "assignment.json").read_text())

            replacement = processes.add(
                _spawn_peer("recover-and-claim", world, state_dir)
            )
            waiting = await asyncio.to_thread(replacement.read_event, timeout=10)
            assert waiting["event"] == "waiting_local_lock"

            # Kill only the old supervisor. The controlled CPU child has its own
            # process group and inherited the fsynced resource flock descriptor.
            old.process.kill()
            assert old.process.wait(timeout=5) == -signal.SIGKILL
            assert os.kill(active["child_pid"], 0) is None
            while_owned = await _observed_run(world, first_run["id"])
            assert str(while_owned["lease_attempt_id"]) == active["attempt_id"]
            with pytest.raises(AssertionError, match="timed out"):
                await asyncio.to_thread(replacement.read_event, timeout=0.25)

            # The parent already died; only terminate the exact child process
            # group reported by its owned test fixture to release the inherited lock.
            assert processes.kill_child(active["child_pid"])
            recovered = await asyncio.to_thread(
                replacement.read_until, "next_attempt_claimed", timeout=30
            )
            recovery_event = next(
                item for item in recovered if item["event"] == "old_attempt_recovered"
            )
            next_claim = recovered[-1]
            assert recovery_event["attempt_id"] == active["attempt_id"]
            assert next_claim["run_id"] == next_run["id"]
            assert replacement.process.wait(timeout=5) == 0

            old_state = await _observed_run(world, first_run["id"])
            next_state = await _observed_run(world, next_run["id"])
            assert old_state["run_status"] == old_state["attempt_status"] == "failed"
            assert old_state["attempt_failure"]["code"] == "execution_lost"
            assert old_state["lease_attempt_id"] == next_state["attempt_id"]
            assert (
                next_state["run_status"] == next_state["attempt_status"] == "preparing"
            )
            assert next_state["attempt_executor_id"] == next_claim["executor_id"]
            assert next_state["attempt_boot_id"] == uuid.UUID(next_claim["boot_id"])
            await _assert_stale_heartbeat(client, world, old_journal)

            finisher = processes.add(_spawn_peer("finish-once", world, state_dir))
            finish_events = await asyncio.to_thread(
                finisher.read_until, "finish_peer_done", timeout=15
            )
            assert finish_events[-1]["published"] is True
            assert finisher.process.wait(timeout=5) == 0
            assert (await _observed_run(world, next_run["id"]))[
                "lease_attempt_id"
            ] is None
        finally:
            processes.cleanup()


async def _install_deferred_finish_fault(world: HandoffWorld, attempt_id: uuid.UUID):
    suffix = uuid.uuid4().hex
    function_name = f"aap_handoff_fail_{suffix}"
    trigger_name = f"aap_handoff_fail_trg_{suffix}"
    function_sql = f"""
CREATE FUNCTION {function_name}() RETURNS trigger LANGUAGE plpgsql AS $body$
BEGIN
    IF NEW.id = '{attempt_id}'::uuid AND NEW.status IN ('failed', 'cancelled') THEN
        RAISE EXCEPTION 'controlled handoff commit failure' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$body$
"""
    trigger_sql = f"""
CREATE CONSTRAINT TRIGGER {trigger_name}
AFTER UPDATE ON training_attempts
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION {function_name}()
"""
    async with world.maker.begin() as setup:
        await setup.execute(text(function_sql))
        await setup.execute(text(trigger_sql))
    return function_name, trigger_name


async def _drop_deferred_finish_fault(world: HandoffWorld, names):
    if names is None:
        return
    function_name, trigger_name = names
    async with world.maker.begin() as cleanup:
        await cleanup.execute(
            text(f"DROP TRIGGER IF EXISTS {trigger_name} ON training_attempts")
        )
        await cleanup.execute(text(f"DROP FUNCTION IF EXISTS {function_name}()"))


async def test_finish_commit_failure_rolls_back_attempt_and_lease_before_retry(
    handoff_world: HandoffWorld, tmp_path: Path
):
    world = handoff_world
    async with httpx.AsyncClient(
        base_url=world.api_base, timeout=10, trust_env=False
    ) as client:
        run_data, _ = await _create_run(client, world, name="Deferred finish rollback")
        state_dir = _peer_state(
            tmp_path, "commit-fault-state", f"handoff-commit-{uuid.uuid4().hex[:12]}"
        )
        processes = OwnedProcesses()
        fault = None
        try:
            claimant = processes.add(_spawn_peer("claim-once", world, state_dir))
            claimed = await asyncio.to_thread(claimant.read_event, timeout=15)
            assert claimed["event"] == "assigned"
            assert claimant.process.wait(timeout=5) == 0
            journal = json.loads((state_dir / "assignment.json").read_text())
            attempt_id = uuid.UUID(journal["assignment"]["attempt_id"])
            fault = await _install_deferred_finish_fault(world, attempt_id)

            retry_gate = tmp_path / "allow-finish-retry"
            finisher = processes.add(
                _spawn_peer("finish-once", world, state_dir, retry_gate=retry_gate)
            )
            failed_commit = await asyncio.to_thread(finisher.read_event, timeout=15)
            assert failed_commit["event"] == "finish_failed"
            assert failed_commit["status"] == 500

            rolled_back = await _observed_run(world, run_data["id"])
            assert (
                rolled_back["run_status"]
                == rolled_back["attempt_status"]
                == "preparing"
            )
            assert rolled_back["finish_request_digest"] is None
            assert str(rolled_back["lease_attempt_id"]) == str(attempt_id)

            await _drop_deferred_finish_fault(world, fault)
            fault = None
            retry_gate.touch()
            finish_events = await asyncio.to_thread(
                finisher.read_until, "finish_peer_done", timeout=15
            )
            assert any(item["event"] == "finish_succeeded" for item in finish_events)
            assert finish_events[-1]["published"] is True
            assert finish_events[-1]["finish_count"] == 2
            assert finish_events[-1]["exact_replay"] is True
            assert finisher.process.wait(timeout=5) == 0

            committed = await _observed_run(world, run_data["id"])
            assert committed["run_status"] == committed["attempt_status"] == "failed"
            assert committed["finish_request_digest"]
            assert committed["lease_attempt_id"] is None
        finally:
            await _drop_deferred_finish_fault(world, fault)
            processes.cleanup()


async def test_local_training_control_plane_capacity_observation(
    handoff_world: HandoffWorld, tmp_path: Path, record_testsuite_property
):
    world = handoff_world
    async with httpx.AsyncClient(
        base_url=world.api_base, timeout=10, trust_env=False
    ) as client:
        run_data, _ = await _create_run(client, world, name="Local control-plane load")
        state = _peer_state(tmp_path, "capacity-state", f"capacity-{uuid.uuid4().hex}")
        processes = OwnedProcesses()
        try:
            peer = processes.add(_spawn_peer("hold", world, state))
            active = (await asyncio.to_thread(peer.read_until, "child_active"))[-1]
            processes.remember_child(active["child_pid"], active["child_started_at"])
            journal = json.loads((state / "assignment.json").read_text())
            samples = []
            groups = []

            async def heartbeat():
                started = time.perf_counter()
                response = await _post_heartbeat(client, world, journal)
                elapsed = (time.perf_counter() - started) * 1000
                assert response.status_code == 200
                assert response.json()["stop"] is False
                return elapsed

            for concurrency in (1, 2, 4, 8):
                await heartbeat()
                latencies = []
                started = time.perf_counter()
                for _ in range(10):
                    latencies.extend(
                        await asyncio.gather(*(heartbeat() for _ in range(concurrency)))
                    )
                seconds = time.perf_counter() - started
                ordered = sorted(latencies)
                groups.append(
                    {
                        "clients": concurrency,
                        "requests": len(latencies),
                        "p50_ms": round(ordered[(len(ordered) + 1) // 2 - 1], 3),
                        "p95_ms": round(
                            ordered[(95 * len(ordered) + 99) // 100 - 1], 3
                        ),
                        "accepted_per_second": round(len(latencies) / seconds, 3),
                    }
                )
                samples.append({"clients": concurrency, "latencies_ms": latencies})
            observed = await _observed_run(world, run_data["id"])
            assert str(observed["lease_attempt_id"]) == active["attempt_id"]
            assert observed["attempt_executor_id"] == active["executor_id"]
            record_testsuite_property(
                "local_training_control_plane_capacity",
                json.dumps(
                    {
                        "scope": "One local TCP API, one lease, one controlled CPU child; "
                        "heartbeat HTTP/authority/SQL only, plus the peer's periodic "
                        "heartbeat; warmups excluded, no production/GPU/SLO qualification",
                        "percentile_method": "nearest_rank",
                        "groups": groups,
                        "samples": samples,
                    }
                ),
            )
            cancelled = await client.post(
                f"/api/v1/projects/{world.project_id}/training-runs/{run_data['id']}/cancel",
                headers={"Authorization": f"Bearer {world.owner_token}"},
            )
            assert cancelled.status_code == 200
            await asyncio.to_thread(peer.read_until, "finish_published")
            assert peer.process.wait(timeout=5) == 0
            assert (await _observed_run(world, run_data["id"]))[
                "lease_attempt_id"
            ] is None
        finally:
            processes.cleanup()
