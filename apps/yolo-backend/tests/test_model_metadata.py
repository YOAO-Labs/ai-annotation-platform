"""Artifact-specific class observations without model loading on reads."""

import json
from types import SimpleNamespace

import pytest

from class_names import COCO80
from model_metadata import ModelMetadataStore, file_signature
from aap_protocol_v2.model_metadata import ModelMetadataResponse


def observe(store, path, names=None, **kwargs):
    return store.observe(
        path,
        SimpleNamespace(task="detect", names=dict(enumerate(names or COCO80))),
        series="yolo11",
        size="n",
        loader_version="test-loader",
        **kwargs,
    )


def test_persistent_cache_has_real_classes_and_rejects_replaced_weight(tmp_path):
    path = tmp_path / "yolo11n.pt"
    path.write_bytes(b"first checkpoint")
    store = ModelMetadataStore(tmp_path)
    item = observe(store, path, ["scratch", "dent", "crack"])
    assert item.verification_status == "mismatch"
    assert [c.name for c in item.classes] == ["scratch", "dent", "crack"]
    restarted = ModelMetadataStore(tmp_path)
    assert restarted.read("yolo11", "n", loader_version="test-loader") == item
    path.write_bytes(b"other checkpoint")
    stale = restarted.read("yolo11", "n")
    assert stale.verification_status == "unavailable"
    assert not stale.classes and not stale.artifact_sha256
    replacement = observe(store, path)
    assert replacement.verification_status == "verified"
    assert replacement.artifact_sha256 != item.artifact_sha256


def test_shared_asset_metadata_resolves_the_prepared_digest_instead_of_legacy_filename(
    tmp_path,
):
    import hashlib
    from training_contract.assets import AssetReference, prepare_asset

    content = b"verified shared weight"
    reference = AssetReference(
        hashlib.sha256(content).hexdigest(), "yolo11n.pt", len(content)
    )
    assets = tmp_path / "assets"
    path = prepare_asset(assets, reference, lambda: [content])
    store = ModelMetadataStore(tmp_path / "checkpoints", model_assets=assets)
    observed = observe(store, path)
    assert store.read("yolo11", "n") == observed
    path.chmod(0o644)
    path.write_bytes(b"changed")
    assert store.read("yolo11", "n").verification_status == "unavailable"


def test_signature_fences_loading_race_and_loader_changes(tmp_path):
    path = tmp_path / "yolo11n.pt"
    path.write_bytes(b"first")
    before = file_signature(path)
    path.write_bytes(b"second")
    store = ModelMetadataStore(tmp_path)
    with pytest.raises(ValueError, match="changed during model loading"):
        observe(store, path, before=before)
    observe(store, path)
    assert (
        store.read("yolo11", "n", loader_version="other").verification_status
        == "unavailable"
    )


def test_shared_artifact_does_not_mix_variant_identity(tmp_path):
    store = ModelMetadataStore(tmp_path)
    for size in ("n", "s"):
        path = tmp_path / f"yolo11{size}.pt"
        path.write_bytes(b"identical checkpoint bytes")
        store.observe(
            path,
            SimpleNamespace(task="detect", names=dict(enumerate(COCO80))),
            series="yolo11",
            size=size,
            loader_version="same",
        )
    assert store.read("yolo11", "n").model_variants["size"] == "n"
    assert store.read("yolo11", "s").model_variants["size"] == "s"


def test_snapshot_is_cache_only_and_missing_is_not_empty_verified(
    tmp_path, monkeypatch
):
    store = ModelMetadataStore(tmp_path)
    monkeypatch.setattr(
        store, "observe", lambda *_a, **_kw: pytest.fail("read loaded weights")
    )
    snapshot = store.snapshot()
    assert snapshot.models
    assert all(
        m.verification_status == "uninspected" and not m.classes
        for m in snapshot.models
    )
    assert not store.root.exists()
    assert ModelMetadataResponse.model_validate(snapshot.model_dump()) == snapshot


def test_corrupt_cache_and_sparse_indices_fail_closed(tmp_path):
    store = ModelMetadataStore(tmp_path)
    path = tmp_path / "yolo11n.pt"
    path.write_bytes(b"weight")
    with pytest.raises(ValueError, match="complete native"):
        store.observe(
            path,
            SimpleNamespace(task="detect", names={1: "car"}),
            series="yolo11",
            size="n",
            loader_version="test",
        )
    observe(store, path)
    store._index("yolo11", "n").write_text(
        json.dumps({"artifact": "../private", "signature": file_signature(path)})
    )
    assert store.read("yolo11", "n").verification_status == "unavailable"


def test_metadata_endpoint_is_authenticated_and_never_loads_weights(
    tmp_path, monkeypatch
):
    import importlib.metadata
    import main
    from starlette.testclient import TestClient
    from unittest.mock import Mock

    store = ModelMetadataStore(tmp_path)
    monkeypatch.setattr(main, "_model_metadata", store)
    loader = importlib.metadata.version
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda name: "test-loader" if name == "ultralytics" else loader(name),
    )
    constructor = Mock(side_effect=AssertionError("metadata route loaded a model"))
    monkeypatch.setattr(main, "_build_model", constructor)
    monkeypatch.setattr(store, "observe", constructor)
    client = TestClient(main.app)
    before = client.get("/setup").json()
    for _ in range(2):
        response = client.get("/models/metadata")
        assert response.status_code == 200
        assert all(
            m["verification_status"] == "uninspected" for m in response.json()["models"]
        )
    client.headers.pop("authorization", None)
    assert client.get("/models/metadata").status_code in {401, 403}
    assert client.get(
        "/models/metadata", headers={"Authorization": "Bearer wrong"}
    ).status_code in {401, 403}
    assert main.setup() == before
    constructor.assert_not_called()


@pytest.mark.parametrize("names", [{0: "custom"}, {True: "person"}, {}, {1: "person"}])
def test_builtin_class_filter_rejects_stale_native_names(names, monkeypatch):
    from PIL import Image
    from fastapi import HTTPException
    from unittest.mock import Mock
    import predictor
    from schemas import Context

    model = SimpleNamespace(names=names, predict=Mock(), device="cpu")
    monkeypatch.setattr(predictor, "fetch_image", lambda _: Image.new("RGB", (20, 20)))
    with pytest.raises(HTTPException) as error:
        predictor.YoloPredictor._predict_closed_sync(
            model,
            "unused",
            Context(classes=[0], variants={"series": "yolo11", "size": "n"}),
        )
    assert error.value.status_code == 409
    model.predict.assert_not_called()


def test_verified_builtin_observation_is_fenced_on_actual_borrowed_model(monkeypatch):
    from PIL import Image
    from fastapi import HTTPException
    from unittest.mock import Mock
    import predictor
    from schemas import Context

    monkeypatch.setattr(predictor, "fetch_image", lambda _: Image.new("RGB", (20, 20)))
    identity = {
        "artifact_sha256": "a" * 64,
        "classes_digest": "b" * 64,
        "loader_version": "test",
    }
    model = SimpleNamespace(
        names=dict(enumerate(COCO80)),
        predict=Mock(),
        device="cpu",
        _aap_class_metadata={**identity, "artifact_sha256": "c" * 64},
    )
    with pytest.raises(HTTPException) as error:
        predictor.YoloPredictor._predict_closed_sync(
            model,
            "unused",
            Context(
                builtin_model_ref=identity, variants={"series": "yolo11", "size": "n"}
            ),
        )
    assert error.value.detail["reason"] == "builtin_model_identity_changed"
    model.predict.assert_not_called()
