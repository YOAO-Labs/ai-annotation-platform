"""Pure signal envelopes and SQL lock ordering for training dataset attempts."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.db.models.async_job import AsyncJob
from app.db.models.training_dataset import TrainingDatasetVersion
from app.workers import signals


class Session:
    """Exercise production SQL/mark helpers with independently held row locks."""

    def __init__(self, version, job):
        self.version, self.job = version, job
        self.locks = {"version": asyncio.Lock(), "job": asyncio.Lock()}
        self.owned = []
        self.events = []
        self.commits = 0

    async def scalar(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        name, row = (
            ("version", self.version)
            if entity is TrainingDatasetVersion
            else ("job", self.job)
        )
        assert entity in {TrainingDatasetVersion, AsyncJob}
        assert statement._for_update_arg is not None
        assert not statement._for_update_arg.read
        assert statement.get_execution_options()["populate_existing"] is True
        expected_id = statement.whereclause.right.value
        self.events.append(("waiting", name))
        await self.locks[name].acquire()
        self.owned.append(name)
        self.events.append(("locked", name))
        return row if row is not None and row.id == expected_id else None

    async def get(self, entity, identifier):
        assert entity is AsyncJob and identifier == self.job.id
        return self.job

    async def commit(self):
        self.commits += 1
        self.events.append(("commit",))
        self.release()

    def release(self):
        for name in reversed(self.owned):
            self.locks[name].release()
        self.owned.clear()


@pytest.fixture
def world(monkeypatch):
    version_id, job_id, attempt_id, project_id, user_id = (
        uuid.uuid4() for _ in range(5)
    )
    task_id = str(uuid.uuid4())
    version = SimpleNamespace(
        id=version_id,
        async_job_id=job_id,
        attempt_id=attempt_id,
        project_id=project_id,
        creator_id=user_id,
        status="running",
        completed_at=None,
        error_code=None,
        error_message=None,
    )
    job = SimpleNamespace(
        id=job_id,
        kind="training_dataset",
        celery_task_id=task_id,
        project_id=project_id,
        user_id=user_id,
        payload={"training_dataset_id": str(version_id)},
        status="running",
        completed_at=None,
        error_message=None,
    )
    db = Session(version, job)
    engine = SimpleNamespace(dispose=AsyncMock())

    @asynccontextmanager
    async def session():
        try:
            yield db
        finally:
            db.release()

    monkeypatch.setattr(signals, "create_async_engine", lambda *a, **k: engine)
    monkeypatch.setattr(signals, "async_sessionmaker", lambda *a, **k: session)
    monkeypatch.setattr(
        signals.async_job_svc,
        "find_by_celery_task_id",
        AsyncMock(return_value=job),
    )
    notify = AsyncMock()
    generic = AsyncMock()
    monkeypatch.setattr(signals, "notify_job_terminal", notify)
    monkeypatch.setattr(signals, "reconcile_domain_terminal", generic)
    value = SimpleNamespace(
        version=version,
        job=job,
        db=db,
        engine=engine,
        notify=notify,
        generic=generic,
        task_id=task_id,
        args=[str(version_id), str(job_id), str(attempt_id)],
    )
    yield value
    db.release()


async def apply(world, *, cancelled=False, task_args=..., task_kwargs=None):
    await signals._apply_terminal(
        world.task_id,
        cancelled=cancelled,
        error="decoder crashed",
        task_args=world.args if task_args is ... else task_args,
        task_kwargs=task_kwargs,
    )


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("status", ["pending", "running"])
async def test_current_signal_terminalizes_domain_and_job_in_one_ordered_commit(
    world, cancelled, status
):
    world.version.status = world.job.status = status
    await apply(world, cancelled=cancelled)
    expected = "cancelled" if cancelled else "failed"
    assert world.version.status == world.job.status == expected
    assert world.version.completed_at is not None and world.job.completed_at is not None
    assert world.version.error_code == ("cancelled" if cancelled else "worker_failed")
    assert world.db.events == [
        ("waiting", "version"),
        ("locked", "version"),
        ("waiting", "job"),
        ("locked", "job"),
        ("commit",),
    ]
    assert world.db.commits == 1
    world.notify.assert_awaited_once_with(world.db, job_id=world.job.id)
    world.generic.assert_not_awaited()
    world.engine.dispose.assert_awaited_once()


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize(
    "target,status",
    [
        ("version", "ready"),
        ("version", "failed"),
        ("version", "cancelled"),
        ("job", "completed"),
        ("job", "failed"),
        ("job", "cancelled"),
    ],
)
async def test_late_signal_preserves_both_terminal_ledgers(
    world, target, status, cancelled
):
    getattr(world, target).status = status
    before = deepcopy((vars(world.version), vars(world.job)))
    await apply(world, cancelled=cancelled)
    assert (vars(world.version), vars(world.job)) == before
    world.notify.assert_not_awaited()
    assert world.db.commits == 0


@pytest.mark.parametrize(
    "change",
    [
        "other_version",
        "old_job",
        "old_attempt",
        "changed_celery_id",
        "other_project",
        "other_user",
        "changed_kind",
        "other_payload_owner",
        "invalid_payload",
        "missing_version",
        "missing_job",
        "other_envelope_job",
    ],
)
async def test_replaced_attempt_or_owner_signal_cannot_modify_current_attempt(
    world, change
):
    if change == "other_version":
        world.args[0] = str(uuid.uuid4())
    elif change == "old_job":
        world.version.async_job_id = uuid.uuid4()
    elif change == "old_attempt":
        world.version.attempt_id = uuid.uuid4()
    elif change == "changed_celery_id":
        world.job.celery_task_id = str(uuid.uuid4())
    elif change == "other_project":
        world.job.project_id = uuid.uuid4()
    elif change == "other_user":
        world.job.user_id = uuid.uuid4()
    elif change == "changed_kind":
        world.job.kind = "export"
        # The lookup saw training, but the fresh locked row changed kind.
        found = SimpleNamespace(**(vars(world.job) | {"kind": "training_dataset"}))
        signals.async_job_svc.find_by_celery_task_id.return_value = found
    elif change == "other_payload_owner":
        world.job.payload["training_dataset_id"] = str(uuid.uuid4())
    elif change == "invalid_payload":
        world.job.payload = []
    elif change == "missing_version":
        world.db.version = None
    elif change == "missing_job":
        world.db.job = None
    else:
        world.args[1] = str(uuid.uuid4())
    before = deepcopy((vars(world.version), vars(world.job)))
    await apply(world)
    assert (vars(world.version), vars(world.job)) == before
    assert world.db.commits == 0
    world.notify.assert_not_awaited()
    world.generic.assert_not_awaited()


@pytest.mark.parametrize(
    "args,kwargs",
    [
        (None, None),
        ([], {}),
        (["invalid", "invalid", "invalid"], {}),
        ([True, True, True], {}),
        ({"version_id": "invalid"}, {}),
        ([], []),
        (["invalid"] * 4, {}),
        (["invalid"] * 3, {"attempt_id": "invalid"}),
        ([], {"version_id": "invalid", "job_id": "invalid", "extra": "invalid"}),
    ],
)
async def test_missing_or_ambiguous_task_envelope_never_guesses_live_attempt(
    world, args, kwargs
):
    await apply(world, task_args=args, task_kwargs=kwargs)
    assert world.db.events == []
    assert world.version.status == world.job.status == "running"
    assert world.db.commits == 0
    world.notify.assert_not_awaited()


@pytest.mark.parametrize("positional", [0, 1, 2, 3])
async def test_actual_bound_task_positional_and_keyword_arguments_are_supported(
    world, positional
):
    names = ("version_id", "job_id", "attempt_id")
    await apply(
        world,
        task_args=world.args[:positional],
        task_kwargs=dict(zip(names[positional:], world.args[positional:], strict=True)),
    )
    assert world.version.status == world.job.status == "failed"
    assert world.db.commits == 1


async def test_late_fallback_does_not_deadlock_a_worker_already_holding_version(world):
    await world.db.locks["version"].acquire()
    pending = asyncio.create_task(apply(world))
    try:
        await asyncio.sleep(0)
        assert world.db.events == [("waiting", "version")]
        # This is the worker's publication order. A Job-first fallback would
        # hold this second lock and deadlock against the worker's Version lock.
        await asyncio.wait_for(world.db.locks["job"].acquire(), 0.1)
        world.version.status, world.job.status = "ready", "completed"
        world.db.locks["job"].release()
    finally:
        world.db.locks["version"].release()
    await asyncio.wait_for(pending, 1)
    assert world.version.status == "ready" and world.job.status == "completed"
    assert world.db.events == [("waiting", "version"), ("locked", "version")]
    assert world.db.commits == 0
    world.notify.assert_not_awaited()


async def test_other_domain_keeps_existing_generic_signal_path(world):
    world.job.kind = "batch_predict"
    await apply(world, task_args=None)
    assert world.job.status == "failed" and world.version.status == "running"
    assert world.db.events == [("waiting", "job"), ("locked", "job"), ("commit",)]
    world.generic.assert_awaited_once_with(
        world.db, world.job, cancelled=False, error="decoder crashed"
    )


def test_failure_callback_preserves_actual_training_task_envelope(monkeypatch):
    failed = AsyncMock()
    monkeypatch.setattr(signals, "_mark_failed", failed)
    args = [str(uuid.uuid4()) for _ in range(3)]
    signals._on_task_failure(
        task_id="celery-failed", exception=ValueError("bad"), args=args, kwargs={}
    )
    failed.assert_awaited_once_with(
        "celery-failed", "ValueError: bad", task_args=args, task_kwargs={}
    )


def test_revoke_callback_preserves_actual_training_task_envelope(monkeypatch):
    cancelled = AsyncMock()
    monkeypatch.setattr(signals, "_mark_cancelled", cancelled)
    args = [str(uuid.uuid4()) for _ in range(3)]
    signals._on_task_revoked(
        request=SimpleNamespace(id="celery-revoked", args=args, kwargs={})
    )
    cancelled.assert_awaited_once_with("celery-revoked", task_args=args, task_kwargs={})
