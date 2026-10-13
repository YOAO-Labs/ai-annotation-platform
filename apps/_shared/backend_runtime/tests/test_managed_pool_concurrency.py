"""Ownership, cancellation, capacity, and cleanup regressions for managed pools."""

from __future__ import annotations

import asyncio
import gc
import threading
import weakref
from dataclasses import dataclass

import pytest

from aap_backend_runtime import BuildArtifact, ManagedLruPool, ManagedPoolBusyError


@dataclass
class _Resource:
    key: str
    device: str = "cpu"
    cleanup_uncertain: bool = False


@dataclass
class _Attachment:
    key: str


def _run(coro):
    return asyncio.run(coro)


def test_same_key_single_flight_and_per_entry_use_lock() -> None:
    async def scenario() -> None:
        builds = 0
        entered: list[str] = []
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        def build(key: str) -> BuildArtifact[_Resource]:
            nonlocal builds
            builds += 1
            return BuildArtifact(_Resource(key))

        pool = ManagedLruPool(1, build, str, lambda: None)

        async def use(name: str) -> None:
            async with pool.borrow("same"):
                entered.append(name)
                if name == "first":
                    first_entered.set()
                    await release_first.wait()

        first = asyncio.create_task(use("first"))
        await first_entered.wait()
        second = asyncio.create_task(use("second"))
        await asyncio.sleep(0)
        snapshot = await pool.snapshot()
        assert snapshot["borrowers"] == 2
        assert entered == ["first"]
        release_first.set()
        await asyncio.gather(first, second)
        assert entered == ["first", "second"]
        assert builds == 1
        await pool.shutdown()

    _run(scenario())


def test_active_entry_cannot_be_evicted() -> None:
    async def scenario() -> None:
        pool = ManagedLruPool(
            1,
            lambda key: BuildArtifact(_Resource(key)),
            str,
            lambda: None,
        )
        async with pool.borrow("active"):
            with pytest.raises(ManagedPoolBusyError):
                async with pool.borrow("other"):
                    pass
        await pool.shutdown()

    _run(scenario())


def test_cancelled_use_lock_waiter_releases_borrower() -> None:
    async def scenario() -> None:
        pool = ManagedLruPool(
            1,
            lambda key: BuildArtifact(_Resource(key)),
            str,
            lambda: None,
        )
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def first() -> None:
            async with pool.borrow("same"):
                first_entered.set()
                await release_first.wait()

        async def waiting() -> None:
            async with pool.borrow("same"):
                raise AssertionError("cancelled waiter entered use section")

        owner = asyncio.create_task(first())
        await first_entered.wait()
        waiter = asyncio.create_task(waiting())
        while (await pool.snapshot())["borrowers"] != 2:
            await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert (await pool.snapshot())["borrowers"] == 1
        release_first.set()
        await owner
        assert (await pool.snapshot())["borrowers"] == 0
        await pool.shutdown()

    _run(scenario())


def test_prestart_builder_cancel_still_cleans_evicted_root() -> None:
    async def scenario() -> None:
        attachment_cleanups: list[str] = []
        strict_cleanups = 0

        def strict_cleanup() -> None:
            nonlocal strict_cleanups
            strict_cleanups += 1

        pool = ManagedLruPool(
            1,
            lambda key: BuildArtifact(_Resource(key), (_Attachment(key),)),
            str,
            strict_cleanup,
            cleanup_attachments=lambda items: attachment_cleanups.extend(
                item.key for item in items
            ),
        )
        await pool.warmup("old")
        async with pool._lock:  # noqa: SLF001 - exact prestart cancellation regression
            builder = pool._get_or_start_builder_locked("new")  # noqa: SLF001
            builder.cancel()
        while (await pool.snapshot())["builders"]:
            await asyncio.sleep(0)
        snapshot = await pool.snapshot()
        assert attachment_cleanups == ["old"]
        assert strict_cleanups == 1
        assert snapshot["cleanup_failed"] is False
        assert snapshot["gpu_resident"] is False
        await pool.shutdown()

    _run(scenario())


def test_failed_attachment_cleanup_keeps_root_for_force_retry() -> None:
    async def scenario() -> None:
        attempts = 0
        attachment_ref: weakref.ReferenceType[_Attachment] | None = None

        def build(key: str) -> BuildArtifact[_Resource]:
            nonlocal attachment_ref
            attachment = _Attachment(key)
            attachment_ref = weakref.ref(attachment)
            return BuildArtifact(_Resource(key), (attachment,))

        def cleanup_attachments(_items: tuple[object, ...]) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("first cleanup fails")

        pool = ManagedLruPool(
            1,
            build,
            str,
            lambda: None,
            cleanup_attachments=cleanup_attachments,
        )
        await pool.warmup("model")
        with pytest.raises(RuntimeError, match="first cleanup fails"):
            await pool.unload_all(reason="manual", force_cleanup=True)
        assert attachment_ref is not None and attachment_ref() is not None
        failed = await pool.snapshot()
        assert failed["cleanup_failed"] is True
        assert failed["gpu_resident"] is None

        await pool.unload_all(reason="manual", force_cleanup=True)
        gc.collect()
        assert attempts == 2
        assert attachment_ref() is None
        recovered = await pool.snapshot()
        assert recovered["cleanup_failed"] is False
        assert recovered["gpu_resident"] is False
        await pool.shutdown()

    _run(scenario())


def test_builder_file_error_is_unknown_until_force_cleanup() -> None:
    async def scenario() -> None:
        strict_calls = 0

        def build(key: str) -> BuildArtifact[_Resource]:
            if key == "missing":
                raise FileNotFoundError(key)
            return BuildArtifact(_Resource(key))

        def strict_cleanup() -> None:
            nonlocal strict_calls
            strict_calls += 1

        pool = ManagedLruPool(1, build, str, strict_cleanup)
        with pytest.raises(FileNotFoundError):
            await pool.warmup("missing")
        failed = await pool.snapshot()
        assert failed["cleanup_failed"] is True
        assert failed["gpu_resident"] is None
        with pytest.raises(ManagedPoolBusyError):
            await pool.warmup("available")

        await pool.unload_all(reason="manual", force_cleanup=True)
        hit, _load_ms, _evicted = await pool.warmup("available")
        assert hit is False
        assert strict_calls >= 1
        await pool.shutdown()

    _run(scenario())


def test_partial_build_root_in_exception_traceback_never_reports_empty() -> None:
    async def scenario() -> None:
        retained_errors: list[FileNotFoundError] = []
        partial_ref: weakref.ReferenceType[_Resource] | None = None

        def build(_key: str) -> BuildArtifact[_Resource]:
            nonlocal partial_ref
            partial = _Resource("partial", device="cuda:0")
            partial_ref = weakref.ref(partial)
            try:
                raise FileNotFoundError("later composite component is missing")
            except FileNotFoundError as exc:
                retained_errors.append(exc)
                raise

        pool = ManagedLruPool(1, build, str, lambda: None)
        with pytest.raises(FileNotFoundError):
            await pool.warmup("composite")
        gc.collect()
        assert partial_ref is not None and partial_ref() is not None
        snapshot = await pool.snapshot()
        assert snapshot["current_size"] == 0
        assert snapshot["cleanup_failed"] is True
        assert snapshot["gpu_resident"] is None

        retained_errors.clear()
        await pool.unload_all(reason="manual", force_cleanup=True)
        await pool.shutdown()

    _run(scenario())


def test_preflight_missing_weight_does_not_start_or_poison_builder() -> None:
    async def scenario() -> None:
        builds: list[str] = []

        def preflight(key: str) -> None:
            if key == "missing":
                raise FileNotFoundError(key)

        def build(key: str) -> BuildArtifact[_Resource]:
            builds.append(key)
            return BuildArtifact(_Resource(key))

        pool = ManagedLruPool(1, build, str, lambda: None, preflight=preflight)
        with pytest.raises(FileNotFoundError):
            await pool.warmup("missing")
        snapshot = await pool.snapshot()
        assert snapshot["builders"] == 0
        assert snapshot["cleanup_failed"] is False
        assert snapshot["gpu_resident"] is False

        hit, _load_ms, _evicted = await pool.warmup("available")
        assert hit is False
        assert builds == ["available"]
        await pool.shutdown()

    _run(scenario())


def test_executor_thread_owner_is_not_a_daemon_assumption() -> None:
    """Document that cleanup callbacks may run on ordinary executor threads."""

    async def scenario() -> None:
        seen = threading.Event()
        pool = ManagedLruPool(
            1,
            lambda key: BuildArtifact(_Resource(key)),
            str,
            seen.set,
        )
        await pool.warmup("model")
        await pool.shutdown()
        assert seen.is_set()

    _run(scenario())
