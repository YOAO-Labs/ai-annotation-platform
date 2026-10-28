"""Immutable controlled installation and precise model keys without a GPU."""

import hashlib
import sys
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from managed_models import InstallRequest, ManagedModelStore, authenticate, digest
from schemas import Context, model_key_for_context
from training_contract.model_return import libre_identity


def installation(raw=b"controlled checkpoint"):
    return InstallRequest(
        model_version_id=uuid.uuid4(),
        sha256=hashlib.sha256(raw).hexdigest(),
        classes_digest=digest(["car", "person"]),
        classes=["car", "person"],
        framework="ultralytics",
        framework_version="8.4.177",
        task="detection",
        size_bytes=len(raw),
        download_url="http://owned-storage.test/weights",
    )


async def test_libre_receipt_carries_native_qualification_and_rejects_stale_proof(
    tmp_path, monkeypatch
):
    body = installation()
    values = {
        **body.model_dump(),
        "framework": "libreyolo",
        "framework_version": "1.6.0",
        "artifact_size_bytes": body.size_bytes,
        **libre_identity("LibreYOLO9t"),
    }
    body = InstallRequest.model_validate(values)
    download(monkeypatch, b"controlled checkpoint")
    monkeypatch.setattr(ManagedModelStore, "inspect", staticmethod(lambda *_: None))
    store = ManagedModelStore(tmp_path)
    receipt = await store.install(body)
    assert receipt["framework"] == "libreyolo" and receipt["model_size"] == "t"
    assert receipt["runtime_qualification"]["device"] == "cpu"
    assert await store.install(body) == receipt
    import json

    receipt["runtime_qualification"]["revision"] = 99
    (tmp_path / str(body.model_version_id) / "receipt.json").write_text(
        json.dumps(receipt)
    )
    with pytest.raises(HTTPException):
        store.receipt(str(body.model_version_id))
    assert not list(tmp_path.glob(".install-*"))


def test_verified_receipt_framework_and_class_identity_are_in_the_pool_key():
    body = installation()
    context = Context(
        type="detection",
        model_id=f"trained:{body.model_version_id}",
        frozen_model_ref=body.model_dump(
            mode="json", include={"model_version_id", "sha256", "classes_digest"}
        ),
    )
    ultra = {"framework": "ultralytics", "classes": body.classes}
    libre = {**ultra, "framework": "libreyolo", **libre_identity("LibreYOLO9t")}
    context._managed_receipt_digest = digest(ultra)
    first = model_key_for_context(context)
    context._managed_receipt_digest = digest(libre)
    second = model_key_for_context(context)
    context._managed_receipt_digest = digest(
        {**libre, "classes": list(reversed(body.classes))}
    )
    third = model_key_for_context(context)
    assert len({first, second, third}) == 3
    assert all(
        key[1] == f"trained:{body.model_version_id}" for key in (first, second, third)
    )


def download(monkeypatch, raw, *, declared=None):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=raw,
            headers={
                "Content-Length": str(declared if declared is not None else len(raw))
            },
        )
    )
    monkeypatch.setattr(
        "aap_backend_runtime.managed_models.httpx.AsyncClient",
        lambda **kwargs: original(transport=transport, **kwargs),
    )


def test_controlled_authentication_is_required_and_constant_token_scoped(monkeypatch):
    monkeypatch.setenv("YOLO_MANAGED_MODELS_TOKEN", "a" * 40)
    for token in ("", "Bearer " + "b" * 40):
        request = Request(
            {"type": "http", "headers": [(b"authorization", token.encode())]}
        )
        with pytest.raises(HTTPException) as error:
            authenticate(request)
        assert error.value.status_code == 403
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", ("Bearer " + "a" * 40).encode())],
        }
    )
    authenticate(request)


async def test_installation_is_atomic_immutable_and_detects_disk_corruption(
    tmp_path, monkeypatch
):
    body = installation()
    download(monkeypatch, b"controlled checkpoint")
    inspected = []
    monkeypatch.setattr(
        ManagedModelStore,
        "inspect",
        staticmethod(
            lambda path, expected: inspected.append((path.read_bytes(), expected))
        ),
    )
    store = ManagedModelStore(tmp_path)
    receipt = await store.install(body)
    assert receipt["sha256"] == body.sha256 and len(inspected) == 1
    assert await store.install(body) == receipt
    assert len(inspected) == 1
    path, actual = store.resolve(str(body.model_version_id), body.sha256)
    assert actual == receipt
    path.chmod(0o600)
    path.write_bytes(b"corrupt")
    with pytest.raises(HTTPException):
        store.receipt(str(body.model_version_id))
    assert await store.install(body) == receipt
    assert path.read_bytes() == b"controlled checkpoint"
    conflict = body.model_copy(
        update={
            "classes": ["person", "car"],
            "classes_digest": digest(["person", "car"]),
        }
    )
    with pytest.raises(HTTPException) as error:
        await store.install(conflict)
    assert error.value.status_code == 409
    assert not list(tmp_path.glob(".install-*"))


@pytest.mark.parametrize("fault", ["hash", "size", "checkpoint"])
async def test_invalid_install_never_publishes_partial_weights(
    tmp_path, monkeypatch, fault
):
    body = installation()
    if fault == "hash":
        body.sha256 = "0" * 64
    raw = b"controlled checkpoint" if fault != "size" else b"short"
    download(monkeypatch, raw)

    def inspect(_path, _expected):
        if fault == "checkpoint":
            raise ValueError("corrupt checkpoint")

    monkeypatch.setattr(ManagedModelStore, "inspect", staticmethod(inspect))
    store = ManagedModelStore(tmp_path)
    with pytest.raises(HTTPException) as error:
        await store.install(body)
    assert error.value.status_code == 422
    assert not [path for path in tmp_path.iterdir() if not path.name.startswith(".")]
    assert not list(tmp_path.glob(".install-*"))
    assert not list(tmp_path.glob(".blobs/sha256/*.pt"))


@pytest.mark.parametrize("fault", ["classes", "task", "runtime"])
def test_native_checkpoint_contract_is_checked(tmp_path, monkeypatch, fault):
    expected = installation().model_dump(mode="json")
    model = SimpleNamespace(
        task="segment" if fault == "task" else "detect",
        names={0: "person", 1: "car"}
        if fault == "classes"
        else {0: "car", 1: "person"},
        to=lambda _device: None,
    )
    fake = SimpleNamespace(
        __version__="8.4.61" if fault == "runtime" else "8.4.177",
        YOLO=lambda _path: model,
    )
    monkeypatch.setitem(sys.modules, "ultralytics", fake)
    with pytest.raises(ValueError):
        ManagedModelStore.inspect(tmp_path / "own.pt", expected)


def test_controlled_context_has_a_separate_pool_key_and_never_uses_builtin_defaults():
    body = installation()
    ref = body.model_dump(
        mode="json", include={"model_version_id", "sha256", "classes_digest"}
    )
    context = Context(
        type="detection",
        model_id=f"trained:{body.model_version_id}",
        frozen_model_ref=ref,
        model_variants={},
    )
    assert model_key_for_context(context) == (
        "detect",
        f"trained:{body.model_version_id}",
        body.sha256,
    )
    assert context.variants is None
    for unsupported in ("text", "keypoint", "tracker", "exemplar"):
        with pytest.raises(ValidationError):
            Context(type=unsupported, model_id=context.model_id, frozen_model_ref=ref)
    with pytest.raises(ValidationError):
        Context(
            type="detection",
            model_id=context.model_id,
            model_variants={"series": "yolo11", "size": "n"},
        )


async def test_two_versions_keep_separate_classes_and_cannot_evict_an_active_borrower(
    monkeypatch,
):
    from model_pool import ModelPool, PoolBusyError

    def build(_task, series, _size):
        return SimpleNamespace(names={0: "car" if series.endswith("one") else "person"})

    pool = ModelPool(
        cap=1, build_model=build, free_gpu_memory=lambda: None, build_timeout=1
    )
    async with pool.borrow("detection", "trained:one", "a" * 64):
        with pytest.raises(PoolBusyError):
            await pool.warmup("detection", "trained:two", "b" * 64)
    await pool.warmup("detection", "trained:two", "b" * 64)
    assert pool.class_names("detection", "trained:one", "a" * 64) == [
        {"index": 0, "name": "car"}
    ]
    assert pool.class_names("detection", "trained:two", "b" * 64) == [
        {"index": 0, "name": "person"}
    ]
    assert pool.class_names("detection") is None


async def test_runtime_receipt_is_separate_from_immutable_installation(
    tmp_path, monkeypatch
):
    import json
    from managed_models import runtime_receipt

    body = installation()
    download(monkeypatch, b"controlled checkpoint")
    monkeypatch.setattr(ManagedModelStore, "inspect", staticmethod(lambda *_: None))
    monkeypatch.setitem(
        sys.modules, "ultralytics", SimpleNamespace(__version__="8.4.183")
    )
    store = ManagedModelStore(tmp_path)
    receipt = await store.install(body)
    raw = (tmp_path / str(body.model_version_id) / "receipt.json").read_bytes()
    wire = runtime_receipt(receipt)
    assert wire["framework_version"] == "8.4.177"
    assert wire["inference_runtime"]["version"] == "8.4.183"
    assert json.loads(raw) == receipt
    assert "inference_runtime" not in store.receipt(str(body.model_version_id))
    assert await store.install(body) == receipt
    assert (tmp_path / str(body.model_version_id) / "receipt.json").read_bytes() == raw
