from copy import deepcopy
import hashlib
import json

import pytest
from training_contract.model_return import libre_identity

from aap_libreyolo_plugin.install import install_request_model, validate_installed_receipt
from aap_libreyolo_plugin.provider import validate_receipt


def receipt():
    classes = ["car", "person"]
    digest = hashlib.sha256(json.dumps(classes, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"model_version_id": "c4b2d973-f22e-4218-9e39-9fcfce5fc956", "sha256": "a" * 64,
            "classes_digest": digest, "classes": classes, "framework": "libreyolo",
            "framework_version": "1.6.0", "task": "detection", "artifact_size_bytes": 123,
            **libre_identity("LibreYOLO9t")}


def test_receipt_class_order_and_qualification_are_immutable():
    valid = receipt()
    validate_receipt(valid)
    for changed in ({"classes": ["person", "car"]}, {"model_size": "e"},
                    {"runtime_qualification": {}}, {"artifact_format": "foreign"}):
        broken = deepcopy(valid)
        broken.update(changed)
        with pytest.raises(ValueError):
            validate_receipt(broken)


def test_install_restricts_artifact_origin_and_size():
    request = install_request_model(["https://assets.example"])
    fields = {**receipt(), "size_bytes": 123, "download_url": "https://assets.example/object"}
    request.model_validate(fields)
    for changed in ({"download_url": "http://assets.example/object"},
                    {"download_url": "https://assets.example@evil.example/object"},
                    {"download_url": "https://assets.example/object#fragment"},
                    {"size_bytes": 124}):
        with pytest.raises(ValueError):
            request.model_validate({**fields, **changed})


def test_installed_receipt_checks_artifact_length(tmp_path):
    path = tmp_path / "weights.pt"
    path.write_bytes(b"x" * 123)
    validate_installed_receipt(receipt(), path)
    path.write_bytes(b"x")
    with pytest.raises(ValueError, match="size"):
        validate_installed_receipt(receipt(), path)


@pytest.mark.asyncio
async def test_installed_native_receipt_rejects_corrupted_qualification(tmp_path, monkeypatch):
    import httpx
    from fastapi import HTTPException
    from aap_backend_runtime.managed_models import ManagedModelStore

    raw = b"controlled checkpoint"
    fields = {**receipt(), "sha256": hashlib.sha256(raw).hexdigest(),
              "size_bytes": len(raw), "artifact_size_bytes": len(raw),
              "download_url": "https://assets.example/object"}
    body = install_request_model(["https://assets.example"]).model_validate(fields)
    client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=raw))
    monkeypatch.setattr("aap_backend_runtime.managed_models.httpx.AsyncClient",
                        lambda **kwargs: client(transport=transport, **kwargs))
    store = ManagedModelStore(tmp_path, inspect=lambda *_: None,
                              validate_receipt=validate_installed_receipt)
    installed = await store.install(body)
    assert await store.install(body) == installed
    installed["runtime_qualification"]["revision"] = 99
    (tmp_path / str(body.model_version_id) / "receipt.json").write_text(json.dumps(installed))
    with pytest.raises(HTTPException):
        store.receipt(str(body.model_version_id))
