"""Administrator preparation uses immutable objects without logging credentials."""

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from training_contract.assets import AssetReference, asset_object_key, resolve_asset


@pytest.fixture
def preparation(monkeypatch):
    class ClientError(Exception):
        def __init__(self, response, operation):
            super().__init__(operation)
            self.response = response

    # Storage SDKs belong to the administrator preparation environment, not the
    # inference image. Keep contract tests independent of that optional SDK.
    monkeypatch.setitem(
        sys.modules, "botocore.exceptions", SimpleNamespace(ClientError=ClientError)
    )
    script = Path(__file__).resolve().parents[1] / "scripts" / "download_weights.py"
    spec = importlib.util.spec_from_file_location(
        "controlled_asset_preparation", script
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def asset(content=b"reviewed weight"):
    sha = hashlib.sha256(content).hexdigest()
    return AssetReference(sha, "yolo11n.pt", len(content), asset_object_key(sha))


def test_reviewed_asset_manifest_covers_exactly_the_controlled_model_matrix():
    from training_contract.assets import read_manifest
    from training_contract.models import MODEL_SPECS

    path = (
        Path(__file__).resolve().parents[1] / "scripts" / "controlled-model-assets.json"
    )
    values = json.loads(path.read_text())["assets"]
    references = read_manifest(path)
    assert {item["base_model"] for item in values} == {spec.id for spec in MODEL_SPECS}
    assert len(references) == len(MODEL_SPECS)
    assert all(reference.size_bytes is not None for reference in references)
    for item in values:
        expected_source = (
            "https://huggingface.co/LibreYOLO/"
            if item["framework"] == "libreyolo"
            else "https://github.com/ultralytics/assets/releases/download/"
        )
        assert item["source_url"].startswith(expected_source)


def test_admin_storage_client_uses_the_platform_ssl_setting(preparation, monkeypatch):
    monkeypatch.setenv("MINIO_ENDPOINT", "storage.example:9000")
    monkeypatch.setenv("MINIO_USE_SSL", "true")
    monkeypatch.setitem(
        sys.modules, "boto3", SimpleNamespace(client=lambda service, **kwargs: kwargs)
    )
    monkeypatch.setitem(
        sys.modules, "botocore.config", SimpleNamespace(Config=lambda **kwargs: kwargs)
    )
    client = preparation._storage_client(SimpleNamespace(endpoint=None))
    assert client["endpoint_url"] == "https://storage.example:9000"


def test_existing_preparation_cli_imports_without_native_loader(
    preparation, tmp_path, monkeypatch, capsys
):
    item = asset()
    original = tmp_path / "original"
    original.mkdir()
    (original / item.filename).write_bytes(b"reviewed weight")
    manifest = tmp_path / "reviewed-assets.json"
    manifest.write_text(
        json.dumps({"version": 1, "assets": [item.metadata(item.size_bytes)]})
    )
    target = tmp_path / "cache"
    monkeypatch.setitem(sys.modules, "ultralytics", None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "download_weights",
            "--asset-manifest",
            str(manifest),
            "--source-dir",
            str(original),
            "--target-dir",
            str(target),
        ],
    )
    assert preparation.main() == 0
    assert resolve_asset(target, item).read_bytes() == b"reviewed weight"
    assert "ready yolo11n.pt" in capsys.readouterr().out


def test_bucket_preparation_retries_authorized_get_and_closes_each_body(
    preparation, tmp_path, monkeypatch
):
    item = asset()
    manifest = tmp_path / "assets-source.json"
    manifest.write_text(
        json.dumps({"version": 1, "assets": [item.metadata(item.size_bytes)]})
    )
    calls, bodies = [], []

    def get_object(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ConnectionError("expired request with secret signature")
        body = io.BytesIO(b"reviewed weight")
        bodies.append(body)
        return {"Body": body}

    client = SimpleNamespace(get_object=get_object)
    monkeypatch.setattr(preparation, "_storage_client", lambda args: client)
    monkeypatch.setattr(preparation.time, "sleep", lambda delay: None)
    args = SimpleNamespace(
        asset_manifest=manifest,
        from_bucket=True,
        source_dir=None,
        bucket="training",
        publish=False,
    )
    assert preparation._prepare_manifest(args, tmp_path / "cache") == 0
    assert calls == [{"Bucket": "training", "Key": item.object_key}] * 2
    assert all(body.closed for body in bodies)
    assert "secret" not in (tmp_path / "cache" / "assets.json").read_text()


def test_failed_bucket_preparation_never_prints_signed_url(
    preparation, tmp_path, monkeypatch, capsys
):
    item = asset()
    manifest = tmp_path / "reviewed-assets.json"
    manifest.write_text(
        json.dumps({"version": 1, "assets": [item.metadata(item.size_bytes)]})
    )
    monkeypatch.setattr(
        preparation,
        "_storage_client",
        lambda args: SimpleNamespace(
            get_object=lambda **kwargs: (_ for _ in ()).throw(
                RuntimeError("https://storage/?X-Amz-Signature=secret")
            )
        ),
    )
    monkeypatch.setattr(preparation.time, "sleep", lambda delay: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "download_weights",
            "--asset-manifest",
            str(manifest),
            "--from-bucket",
            "--target-dir",
            str(tmp_path / "cache"),
        ],
    )
    assert preparation.main() == 1
    output = capsys.readouterr()
    assert "Signature" not in output.err and "secret" not in output.err
    assert not list((tmp_path / "cache").glob("*.pt"))


def test_authority_is_verified_by_digest_after_conditional_creation(preparation):
    from botocore.exceptions import ClientError

    item = asset()
    uploaded = []

    def missing(**kwargs):
        raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

    def upload(**kwargs):
        uploaded.append({**kwargs, "Body": kwargs["Body"].read()})

    client = SimpleNamespace(
        head_object=missing,
        put_object=upload,
        get_object=lambda **kwargs: {"Body": io.BytesIO(b"reviewed weight")},
    )
    source = SimpleNamespace(open=lambda mode: io.BytesIO(b"reviewed weight"))
    preparation._publish_object(client, "training", item, source)
    assert uploaded[0]["IfNoneMatch"] == "*"
    assert uploaded[0]["Key"] == item.object_key
    assert uploaded[0]["Metadata"] == {"sha256": item.sha256}


def test_existing_bad_authority_is_never_overwritten(preparation):
    item = asset()
    client = SimpleNamespace(
        head_object=lambda **kwargs: {"ContentLength": item.size_bytes},
        put_object=lambda **kwargs: pytest.fail(
            "Immutable authority must not be overwritten"
        ),
        get_object=lambda **kwargs: {"Body": io.BytesIO(b"corrupt")},
    )
    with pytest.raises(ValueError, match="immutable identity"):
        preparation._publish_object(client, "training", item, None)
