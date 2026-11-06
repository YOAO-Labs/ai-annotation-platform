"""Native image polling readiness and stop-admission boundaries."""

import asyncio
import signal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from yoao_assets_images_runtime import worker


def _runtime(monkeypatch, *, input_poll, image_poll, poll_seconds=30):
    closed = []
    database = SimpleNamespace(pool=object())

    async def close_database():
        closed.append("database")

    database.close = close_database

    async def connect(_config):
        return database

    async def capabilities(_pool, _role):
        return True

    class ImageStorage:
        async def close(self):
            closed.append("image")

    class InputStorage:
        def __init__(self, *_args, **_kwargs):
            pass

        async def close(self):
            closed.append("input")

    class Images:
        worker_id = uuid4()

        def __init__(self, _database, storage, **_kwargs):
            self.storage = storage

        async def reconcile_once(self):
            return None

        async def execute_once(self, **_kwargs):
            return await image_poll()

        async def close(self):
            await self.storage.close()

    class Inputs:
        def __init__(self, *_args, **_kwargs):
            self.unsettled_claim = False

        async def execute_once(self, **_kwargs):
            return await input_poll(self)

    monkeypatch.setattr(
        worker,
        "load_config",
        lambda: SimpleNamespace(
            storage=object(), srgb_profile="profile", poll_seconds=poll_seconds
        ),
    )
    monkeypatch.setattr(worker, "_connect_worker_database", connect)
    monkeypatch.setattr(worker, "check_capabilities", capabilities)
    monkeypatch.setattr(worker, "S3ImageStorage", lambda _config: ImageStorage())
    monkeypatch.setattr(worker, "S3InputStorage", InputStorage)
    monkeypatch.setattr(worker, "AsyncpgInputDatabase", lambda *_a, **_k: object())
    monkeypatch.setattr(worker, "ImageExecutor", Images)
    monkeypatch.setattr(worker, "InputExecutor", Inputs)
    return closed


@pytest.mark.asyncio
async def test_readiness_follows_both_successful_polls_and_stop_drains(monkeypatch):
    input_entered, input_release = asyncio.Event(), asyncio.Event()
    image_entered, image_release = asyncio.Event(), asyncio.Event()
    ready, stopped = asyncio.Event(), asyncio.Event()
    calls = []

    async def poll_input(_executor):
        calls.append("input")
        input_entered.set()
        await input_release.wait()
        return None

    async def poll_image():
        calls.append("image")
        image_entered.set()
        await image_release.wait()
        return None

    closed = _runtime(monkeypatch, input_poll=poll_input, image_poll=poll_image)
    task = asyncio.create_task(
        worker._run_worker(
            once=False,
            job_id=None,
            shutdown_event=stopped,
            ready_callback=ready.set,
            strict=True,
        )
    )
    await asyncio.wait_for(input_entered.wait(), 1)
    assert not ready.is_set()
    input_release.set()
    await asyncio.wait_for(image_entered.wait(), 1)
    assert not ready.is_set()
    image_release.set()
    await asyncio.wait_for(ready.wait(), 1)
    stopped.set()
    await asyncio.wait_for(task, 1)
    assert calls == ["input", "image"]
    assert closed == ["input", "image", "database"]


@pytest.mark.asyncio
async def test_stop_during_claim_waits_for_its_outcome_without_admitting_next(
    monkeypatch,
):
    entered, release, stopped, ready = (asyncio.Event() for _ in range(4))
    calls = []

    async def poll_input(_executor):
        calls.append("input")
        entered.set()
        await release.wait()
        calls.append("input-finished")
        return SimpleNamespace(status="ready")

    async def poll_image():
        calls.append("image")
        return None

    closed = _runtime(monkeypatch, input_poll=poll_input, image_poll=poll_image)
    task = asyncio.create_task(
        worker._run_worker(
            once=False,
            job_id=None,
            shutdown_event=stopped,
            ready_callback=ready.set,
            strict=True,
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    stopped.set()
    await asyncio.sleep(0)
    assert not task.done()
    assert not closed
    release.set()
    await asyncio.wait_for(task, 1)
    assert calls == ["input", "input-finished"]
    assert not ready.is_set()
    assert closed == ["input", "image", "database"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["published", "lease_lost", "source_changed"])
async def test_stop_during_image_claim_requires_confirmed_native_outcome(
    monkeypatch, outcome
):
    entered, release, stopped, ready = (asyncio.Event() for _ in range(4))
    calls = []

    async def poll_input(_executor):
        calls.append("input")
        return None

    async def poll_image():
        calls.append("image")
        entered.set()
        await release.wait()
        return SimpleNamespace(outcome=outcome)

    closed = _runtime(monkeypatch, input_poll=poll_input, image_poll=poll_image)
    task = asyncio.create_task(
        worker._run_worker(
            once=False,
            job_id=None,
            shutdown_event=stopped,
            ready_callback=ready.set,
            strict=True,
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    stopped.set()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    if outcome == "published":
        await asyncio.wait_for(task, 1)
    else:
        with pytest.raises(RuntimeError, match="claim is unsettled"):
            await asyncio.wait_for(task, 1)
    assert calls == ["input", "image"]
    assert not ready.is_set()
    assert closed == ["input", "image", "database"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["input-error", "input-lost", "image-error", "image-lost", "image-canceled"],
)
async def test_uncertain_or_lost_claim_never_signals_readiness(monkeypatch, failure):
    ready = asyncio.Event()

    async def poll_input(executor):
        if failure == "input-error":
            raise RuntimeError("claim result unknown")
        if failure == "input-lost":
            executor.unsettled_claim = True
        return None

    async def poll_image():
        if failure == "image-error":
            raise RuntimeError("finish acknowledgement unknown")
        if failure in {"image-lost", "image-canceled"}:
            return SimpleNamespace(
                outcome="lease_lost" if failure == "image-lost" else "source_changed"
            )
        return None

    closed = _runtime(monkeypatch, input_poll=poll_input, image_poll=poll_image)
    with pytest.raises(RuntimeError):
        await worker._run_worker(
            once=False,
            job_id=None,
            shutdown_event=asyncio.Event(),
            ready_callback=ready.set,
            strict=True,
        )
    assert not ready.is_set()
    assert closed == ["input", "image", "database"]


@pytest.mark.asyncio
async def test_unsupervised_stop_preserves_ambiguous_claim_failure(monkeypatch):
    stopped = asyncio.Event()

    async def poll_input(_executor):
        stopped.set()
        raise RuntimeError("claim response lost")

    async def poll_image():
        raise AssertionError("must not admit image work")

    closed = _runtime(monkeypatch, input_poll=poll_input, image_poll=poll_image)
    with pytest.raises(RuntimeError, match="unsettled work"):
        await worker._run_worker(
            once=False,
            job_id=None,
            shutdown_event=stopped,
        )
    assert closed == ["input", "image", "database"]


@pytest.mark.asyncio
async def test_selected_one_shot_signal_cancels_and_waits_for_physical_drain(
    monkeypatch,
):
    entered, cancelled, release = (asyncio.Event() for _ in range(3))
    handlers = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, handler: handlers.update({sig: handler})
    )
    monkeypatch.setattr(
        loop, "remove_signal_handler", lambda sig: handlers.pop(sig, None)
    )

    async def poll_input(_executor):
        raise AssertionError("selected image job must not poll input")

    async def poll_image():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            raise

    closed = _runtime(monkeypatch, input_poll=poll_input, image_poll=poll_image)
    task = asyncio.create_task(
        worker._with_signals(worker._run_worker(once=True, job_id=uuid4()))
    )
    await asyncio.wait_for(entered.wait(), 1)
    handlers[signal.SIGINT]()
    await asyncio.wait_for(cancelled.wait(), 1)
    assert not task.done()
    assert not closed
    release.set()
    await asyncio.wait_for(task, 1)
    assert closed == ["input", "image", "database"]


@pytest.mark.asyncio
@pytest.mark.parametrize("once", [False, True])
async def test_only_continuous_polling_retries_a_rejected_native_claim(
    monkeypatch, once
):
    stopped, ready = asyncio.Event(), asyncio.Event()
    attempts = 0

    async def claim(_operation, _request):
        nonlocal attempts
        attempts += 1
        assert not ready.is_set()
        if attempts == 1:
            raise worker.ImageFailure("operation_busy")
        return worker.ClaimedImageWork(claim=None)

    executor = worker.ImageExecutor(SimpleNamespace(call=claim), object())

    async def poll_input(_executor):
        return None

    closed = _runtime(
        monkeypatch,
        input_poll=poll_input,
        image_poll=executor.execute_once,
        poll_seconds=0,
    )

    def mark_ready():
        ready.set()
        stopped.set()

    execution = worker._run_worker(
        once=once,
        job_id=None,
        shutdown_event=stopped,
        ready_callback=None if once else mark_ready,
        strict=True,
    )
    if once:
        with pytest.raises(worker.ImageFailure) as failure:
            await asyncio.wait_for(execution, 1)
        assert failure.value.code == "operation_busy"
    else:
        await asyncio.wait_for(execution, 1)
    assert attempts == (1 if once else 2)
    assert ready.is_set() is not once
    assert closed == ["input", "image", "database"]


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_busy_after_admission_cannot_be_treated_as_a_rejected_claim(
    monkeypatch, strict
):
    stopped, ready = asyncio.Event(), asyncio.Event()

    async def poll_input(_executor):
        return None

    async def poll_image():
        stopped.set()
        raise worker.ImageFailure("operation_busy")

    closed = _runtime(monkeypatch, input_poll=poll_input, image_poll=poll_image)
    execution = worker._run_worker(
        once=False,
        job_id=None,
        shutdown_event=stopped,
        ready_callback=ready.set if strict else None,
        strict=strict,
    )
    if strict:
        with pytest.raises(worker.ImageFailure) as failure:
            await asyncio.wait_for(execution, 1)
        assert failure.value.code == "operation_busy"
    else:
        with pytest.raises(RuntimeError) as failure:
            await asyncio.wait_for(execution, 1)
        assert not isinstance(failure.value, worker.ImageFailure)
    assert not ready.is_set()
    assert closed == ["input", "image", "database"]
