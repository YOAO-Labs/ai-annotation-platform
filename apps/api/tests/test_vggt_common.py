"""Preserve extracted geometry and distinguish historical proof from runtime."""

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import vggt_common  # noqa: E402


def test_exposure_box_interpolation_and_clamping():
    track = [
        {
            "timestamp": 0,
            "translation": [0, 0, 0],
            "size": [2, 4, 2],
            "rotation": [1, 0, 0, 0],
        },
        {
            "timestamp": 10,
            "translation": [10, 0, 0],
            "size": [4, 6, 4],
            "rotation": [2**-0.5, 0, 0, 2**-0.5],
        },
    ]
    middle = vggt_common.interpolate_box(track, 5)
    np.testing.assert_allclose(middle["translation"], [5, 0, 0])
    np.testing.assert_allclose(middle["size"], [3, 5, 3])
    np.testing.assert_allclose(
        middle["rotation_matrix"][:2, :2], [[2**-0.5, -(2**-0.5)], [2**-0.5, 2**-0.5]]
    )
    clamped = vggt_common.interpolate_box(track, -5)
    assert clamped["clamped_timestamp"]
    np.testing.assert_allclose(clamped["translation"], [0, 0, 0])


def test_scene_chain_rejects_cycles_and_foreign_samples():
    scene = {"token": "scene", "first_sample_token": "first"}
    samples = {"first": {"scene_token": "scene", "next": ""}}
    assert vggt_common._scene_samples(scene, samples) == [samples["first"]]
    samples["first"]["next"] = "first"
    with pytest.raises(ValueError, match="cycle"):
        vggt_common._scene_samples(scene, samples)
    samples["first"].update(next="", scene_token="foreign")
    with pytest.raises(ValueError, match="scene boundary"):
        vggt_common._scene_samples(scene, samples)


def test_consolidation_rejects_changed_proof_and_changed_extracted_geometry(tmp_path):
    assert not vggt_common.verify_runtime_sources(tmp_path)
    common = Path(vggt_common.__file__)
    workspace = common.resolve().parents[3]
    (tmp_path / "proof.json").write_text("original proof")
    (tmp_path / "historical.py").write_text(common.read_text())

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    record = {
        "runtime_source_hashes": {str(common.relative_to(workspace)): sha(common)},
        "historical_record_hashes": {"proof.json": sha(tmp_path / "proof.json")},
        "extracted_symbols": [{"archive": "historical.py", "symbol": "_scene_samples"}],
    }
    (tmp_path / "code-consolidation.json").write_text(json.dumps(record))
    assert vggt_common.verify_runtime_sources(tmp_path)
    (tmp_path / "proof.json").write_text("replaced proof")
    with pytest.raises(ValueError, match="source fence"):
        vggt_common.verify_runtime_sources(tmp_path)
    (tmp_path / "proof.json").write_text("original proof")
    (tmp_path / "historical.py").write_text(
        common.read_text().replace("while token:", "while False:")
    )
    with pytest.raises(ValueError, match="extracted geometry"):
        vggt_common.verify_runtime_sources(tmp_path)
