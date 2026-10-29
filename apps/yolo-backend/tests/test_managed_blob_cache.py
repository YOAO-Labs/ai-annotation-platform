"""Protected local artifact reuse preserves per-installation qualification."""

import asyncio
import fcntl
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import time
from types import SimpleNamespace
import uuid

import httpx
import pytest
from fastapi import HTTPException

from managed_models import InstallRequest, ManagedModelStore, digest
from training_contract.model_return import segmentation_identity

RAW = b"qualified private checkpoint"
HTTP_CLIENT = httpx.AsyncClient


def installation(raw=RAW, **changes):
    values = {
        "model_version_id": uuid.uuid4(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "classes_digest": digest(["car", "person"]),
        "classes": ["car", "person"],
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "task": "detection",
        "size_bytes": len(raw),
        "download_url": "https://authorized-storage.test/checkpoint?signature=private",
    }
    values.update(changes)
    return InstallRequest.model_validate(values)


def transport(monkeypatch, handler):
    monkeypatch.setattr(
        "aap_backend_runtime.managed_models.httpx.AsyncClient",
        lambda **kwargs: HTTP_CLIENT(transport=httpx.MockTransport(handler), **kwargs),
    )


def lightweight_inspection(monkeypatch):
    def inspect(path, expected):
        if (
            path.read_bytes() != RAW
            or expected["classes"] != ["car", "person"]
            or expected["task"] != "detection"
        ):
            raise ValueError("Unqualified task or class identity")

    monkeypatch.setattr(ManagedModelStore, "inspect", staticmethod(inspect))


async def test_two_versions_share_one_get_and_inode_but_keep_distinct_receipts(
    tmp_path, monkeypatch
):
    calls, inspections = [], []

    async def get(request):
        calls.append(request)
        await asyncio.sleep(0.02)
        return httpx.Response(200, content=RAW)

    def inspect(path, expected):
        inspections.append((path.read_bytes(), expected["model_version_id"]))

    transport(monkeypatch, get)
    monkeypatch.setattr(ManagedModelStore, "inspect", staticmethod(inspect))
    store = ManagedModelStore(tmp_path)
    bodies = [installation(), installation()]
    receipts = await asyncio.gather(*(store.install(body) for body in bodies))
    paths = [
        store.resolve(str(body.model_version_id), body.sha256)[0] for body in bodies
    ]
    blob = tmp_path / ".blobs" / "sha256" / f"{bodies[0].sha256}.pt"
    assert len(calls) == 1
    assert len(inspections) == 2
    assert paths[0].stat().st_ino == paths[1].stat().st_ino == blob.stat().st_ino
    assert receipts[0]["model_version_id"] != receipts[1]["model_version_id"]
    assert all(
        "download_url" not in item and "signature" not in json.dumps(item)
        for item in receipts
    )
    assert blob.stat().st_mode & 0o077 == 0
    assert (tmp_path / ".blobs").stat().st_mode & 0o077 == 0
    assert all((path.parent / "receipt.json").is_file() for path in paths)


@pytest.mark.parametrize("fault", ["classes", "task", "runtime"])
async def test_warm_blob_still_requires_each_installations_native_qualification(
    tmp_path, monkeypatch, fault
):
    calls = []
    transport(
        monkeypatch,
        lambda request: calls.append(request) or httpx.Response(200, content=RAW),
    )
    model = SimpleNamespace(
        task="detect", names={0: "car", 1: "person"}, to=lambda device: None
    )
    monkeypatch.setitem(
        sys.modules,
        "ultralytics",
        SimpleNamespace(__version__="8.4.177", YOLO=lambda path: model),
    )
    store = ManagedModelStore(tmp_path)
    first = installation()
    await store.install(first)
    changes = {}
    if fault == "classes":
        changes.update(
            classes=["person", "car"], classes_digest=digest(["person", "car"])
        )
    elif fault == "task":
        changes.update(task="segmentation", **segmentation_identity("yolo11n-seg"))
    second = installation(**changes)
    if fault == "runtime":
        # The request is already authenticated and parsed by the route; emulate
        # a differently qualified native image without relying on real packages.
        monkeypatch.setattr(sys.modules["ultralytics"], "__version__", "8.4.61")
    with pytest.raises(HTTPException) as failure:
        await store.install(second)
    assert failure.value.status_code == 422
    assert len(calls) == 1
    assert not (tmp_path / str(second.model_version_id)).exists()
    assert (
        store.resolve(str(first.model_version_id), first.sha256)[0].read_bytes() == RAW
    )
    assert not list(tmp_path.glob(".install-*"))


@pytest.mark.parametrize("fault", ["missing", "corrupt"])
async def test_corrupt_blob_is_atomically_replaced_without_mutating_retained_inodes(
    tmp_path, monkeypatch, fault
):
    calls = []
    transport(
        monkeypatch,
        lambda request: calls.append(request) or httpx.Response(200, content=RAW),
    )
    lightweight_inspection(monkeypatch)
    store = ManagedModelStore(tmp_path)
    first, second, third = installation(), installation(), installation()
    await store.install(first)
    await store.install(second)
    retained = [
        store.resolve(str(body.model_version_id), body.sha256)[0]
        for body in (first, second)
    ]
    original_inode = retained[0].stat().st_ino
    receipts = [(path.parent / "receipt.json").read_bytes() for path in retained]
    blob = tmp_path / ".blobs" / "sha256" / f"{first.sha256}.pt"
    if fault == "missing":
        blob.unlink()
    else:
        corrupt = tmp_path / "external-corruption"
        corrupt.write_bytes(b"corrupt blob")
        os.replace(corrupt, blob)
    await store.install(third)
    installed = store.resolve(str(third.model_version_id), third.sha256)[0]
    assert len(calls) == 2
    assert blob.stat().st_ino == installed.stat().st_ino != original_inode
    assert all(
        path.stat().st_ino == original_inode and path.read_bytes() == RAW
        for path in retained
    )
    assert [
        (path.parent / "receipt.json").read_bytes() for path in retained
    ] == receipts


async def test_repair_preserves_original_receipt_bytes_and_never_rebinds_same_uuid(
    tmp_path, monkeypatch
):
    calls = []
    transport(
        monkeypatch,
        lambda request: calls.append(request) or httpx.Response(200, content=RAW),
    )
    lightweight_inspection(monkeypatch)
    store = ManagedModelStore(tmp_path)
    body = installation()
    receipt = await store.install(body)
    path, _ = store.resolve(str(body.model_version_id), body.sha256)
    receipt_path = path.parent / "receipt.json"
    historical_bytes = (json.dumps(receipt, indent=3) + "\n").encode()
    receipt_path.write_bytes(historical_bytes)
    path.chmod(0o600)
    path.write_bytes(b"corrupt linked bytes")
    conflict = installation(
        b"different artifact", model_version_id=body.model_version_id
    )
    with pytest.raises(HTTPException) as failure:
        await store.install(conflict)
    assert failure.value.status_code == 409 and len(calls) == 1
    runtime_conflict = body.model_copy(update={"framework_version": "8.4.61"})
    with pytest.raises(HTTPException) as failure:
        await store.install(runtime_conflict)
    assert failure.value.status_code == 409 and len(calls) == 1
    await store.install(body)
    assert path.read_bytes() == RAW
    assert receipt_path.read_bytes() == historical_bytes
    receipt_path.write_bytes(b"{corrupt receipt")
    with pytest.raises(HTTPException) as failure:
        await store.install(body)
    assert failure.value.status_code == 409 and len(calls) == 2
    assert receipt_path.read_bytes() == b"{corrupt receipt"


async def test_valid_legacy_install_remains_unchanged_without_a_blob_or_download(
    tmp_path, monkeypatch
):
    body = installation()
    directory = tmp_path / str(body.model_version_id)
    directory.mkdir()
    path = directory / "weights.pt"
    path.write_bytes(RAW)
    inode = path.stat().st_ino
    expected = body.model_dump(
        mode="json", exclude={"download_url", "size_bytes"}, exclude_none=True
    )
    raw_receipt = (json.dumps(expected, indent=4) + "\n").encode()
    (directory / "receipt.json").write_bytes(raw_receipt)
    transport(
        monkeypatch,
        lambda request: pytest.fail(
            "A retained valid installation must remain offline"
        ),
    )
    monkeypatch.setattr(
        ManagedModelStore,
        "inspect",
        staticmethod(
            lambda *args: pytest.fail(
                "No native migration of a valid retained installation"
            )
        ),
    )
    assert await ManagedModelStore(tmp_path).install(body) == expected
    assert (
        path.stat().st_ino == inode
        and (directory / "receipt.json").read_bytes() == raw_receipt
    )
    assert not (tmp_path / ".blobs").exists()


async def test_cancelled_transfer_cleans_staging_but_retains_other_installed_blobs(
    tmp_path, monkeypatch
):
    started = asyncio.Event()
    complete = asyncio.Event()
    closed = []
    other_raw = RAW + b" new"
    first, second = installation(), installation(other_raw)

    class Delayed(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield other_raw[:5]
            started.set()
            await complete.wait()
            yield other_raw[5:]

        async def aclose(self):
            closed.append(True)

    transport(monkeypatch, lambda request: httpx.Response(200, content=RAW))
    monkeypatch.setattr(
        ManagedModelStore, "inspect", staticmethod(lambda path, expected: None)
    )
    store = ManagedModelStore(tmp_path)
    await store.install(first)
    transport(monkeypatch, lambda request: httpx.Response(200, stream=Delayed()))
    task = asyncio.create_task(store.install(second))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed
    assert not list(tmp_path.glob(".install-*"))
    assert not (tmp_path / str(second.model_version_id)).exists()
    assert not (tmp_path / ".blobs" / "sha256" / f"{second.sha256}.pt").exists()
    assert (
        store.resolve(str(first.model_version_id), first.sha256)[0].read_bytes() == RAW
    )
    complete.set()
    await store.install(second)
    assert (
        store.resolve(str(second.model_version_id), second.sha256)[0].read_bytes()
        == other_raw
    )


async def test_cancel_while_waiting_for_another_process_lock_keeps_loop_responsive(
    tmp_path, monkeypatch
):
    body = installation()
    lightweight_inspection(monkeypatch)
    directory = tmp_path / ".locks" / "sha256"
    directory.mkdir(parents=True)
    with (directory / f"{body.sha256}.lock").open("a+") as other_owner:
        fcntl.flock(other_owner, fcntl.LOCK_EX)
        transport(
            monkeypatch,
            lambda request: pytest.fail(
                "No transfer while another process owns the digest"
            ),
        )
        store = ManagedModelStore(tmp_path)
        task = asyncio.create_task(store.install(body))
        await asyncio.wait_for(asyncio.sleep(0.1), 0.5)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not list(tmp_path.glob(".install-*"))
    transport(monkeypatch, lambda request: httpx.Response(200, content=RAW))
    await store.install(body)


def _install_in_process(root, payload, barrier, result):
    try:
        barrier.wait(10)
        body = InstallRequest.model_validate(payload)
        store = ManagedModelStore(Path(root))
        asyncio.run(store.install(body))
        path, _ = store.resolve(str(body.model_version_id), body.sha256)
        result.put(("ok", path.stat().st_ino))
    except HTTPException as failure:
        result.put(("http", failure.status_code))
    except BaseException as failure:
        result.put(("error", type(failure).__name__))


@pytest.mark.parametrize("same_version", [False, True])
def test_cross_process_installation_deduplicates_gets_and_preserves_version_identity(
    tmp_path, monkeypatch, same_version
):
    context = multiprocessing.get_context("fork")
    counter = context.Value("i", 0)
    barrier, result = context.Barrier(2), context.Queue()
    first = installation()
    second_raw = RAW + b" conflicting" if same_version else RAW
    second = installation(
        second_raw,
        model_version_id=first.model_version_id if same_version else uuid.uuid4(),
        download_url="https://authorized-storage.test/second",
    )

    def get(request):
        with counter.get_lock():
            counter.value += 1
        time.sleep(0.1)
        return httpx.Response(
            200, content=second_raw if request.url.path == "/second" else RAW
        )

    transport(monkeypatch, get)
    monkeypatch.setattr(
        ManagedModelStore, "inspect", staticmethod(lambda path, expected: None)
    )
    children = [
        context.Process(
            target=_install_in_process,
            args=(tmp_path, body.model_dump(mode="json"), barrier, result),
        )
        for body in (first, second)
    ]
    try:
        for child in children:
            child.start()
        for child in children:
            child.join(15)
            assert child.exitcode == 0
        outcomes = [result.get(timeout=2) for _ in children]
        assert counter.value == 1
        if same_version:
            assert sorted(item[0] for item in outcomes) == ["http", "ok"]
            assert next(item[1] for item in outcomes if item[0] == "http") == 409
        else:
            assert outcomes[0][0] == outcomes[1][0] == "ok"
            assert outcomes[0][1] == outcomes[1][1]
        assert not list(tmp_path.glob(".install-*"))
    finally:
        for child in children:
            if child.is_alive():
                child.kill()
                child.join(5)
        result.close()
        result.join_thread()


@pytest.mark.parametrize("crash_phase", ["before", "after"])
def test_process_death_during_repair_cannot_hide_or_rebind_immutable_receipt(
    tmp_path, monkeypatch, crash_phase
):
    transport(monkeypatch, lambda request: httpx.Response(200, content=RAW))
    lightweight_inspection(monkeypatch)
    body = installation()
    store = ManagedModelStore(tmp_path)
    receipt = asyncio.run(store.install(body))
    directory = tmp_path / str(body.model_version_id)
    receipt_path = directory / "receipt.json"
    historical_bytes = (json.dumps(receipt, indent=3) + "\n").encode()
    receipt_path.write_bytes(historical_bytes)
    (directory / "weights.pt").unlink()

    def repair_and_die():
        replace_file = os.replace

        def interrupted_replace(source, destination):
            publishing = Path(source) == directory or Path(destination) in {
                directory,
                directory / "weights.pt",
            }
            if publishing and crash_phase == "before":
                os.kill(os.getpid(), signal.SIGKILL)
            replace_file(source, destination)
            if publishing and crash_phase == "after":
                os.kill(os.getpid(), signal.SIGKILL)

        os.replace = interrupted_replace
        asyncio.run(ManagedModelStore(tmp_path).install(body))

    child = multiprocessing.get_context("fork").Process(target=repair_and_die)
    try:
        child.start()
        child.join(15)
        assert child.exitcode == -signal.SIGKILL
    finally:
        if child.is_alive():
            child.kill()
            child.join(5)

    assert receipt_path.read_bytes() == historical_bytes
    restarted = ManagedModelStore(tmp_path)
    conflicting = body.model_copy(update={"framework_version": "8.4.61"})
    with pytest.raises(HTTPException) as failure:
        asyncio.run(restarted.install(conflicting))
    assert failure.value.status_code == 409
    asyncio.run(restarted.install(body))
    assert restarted.receipt(str(body.model_version_id)) == receipt
    assert (directory / "weights.pt").read_bytes() == RAW
    assert receipt_path.read_bytes() == historical_bytes
