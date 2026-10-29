from __future__ import annotations

import hashlib
import json

import pytest

import plugin_assets


def test_asset_verification_rejects_tampering_and_unapproved_files(
    tmp_path, monkeypatch
):
    content = b"approved model"
    digest = hashlib.sha256(content).hexdigest()
    relative = "PP-OCRv6/det/example.onnx"
    monkeypatch.setattr(plugin_assets, "_known_assets", lambda: {relative: digest})
    model = tmp_path / "models" / relative
    model.parent.mkdir(parents=True)
    model.write_bytes(content)
    manifest = json.dumps({"files": [{"path": relative, "sha256": digest}]}).encode()
    (tmp_path / "assets.json").write_bytes(manifest)
    manifest_digest = hashlib.sha256(manifest).hexdigest()

    plugin_assets.verify_assets(tmp_path, manifest_digest)
    model.write_bytes(b"altered model")
    with pytest.raises(ValueError, match="digest"):
        plugin_assets.verify_assets(tmp_path, manifest_digest)
    model.write_bytes(content)
    (model.parent / "unapproved.onnx").write_bytes(content)
    with pytest.raises(ValueError, match="unapproved"):
        plugin_assets.verify_assets(tmp_path, manifest_digest)
    with pytest.raises(RuntimeError, match="manifest digest"):
        plugin_assets.verify_assets(tmp_path, "0" * 64)
