"""Optional real native-model regression; run with explicit prepared fixtures."""
import importlib.util
import os
from pathlib import Path

import pytest
from PIL import Image

from aap_inference_plugin import Detection
from aap_inference_plugin.cli import init_project


def load_project(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path / "my_libreyolo_plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_real_provider_executes_edited_predict_hook(tmp_path):
    pytest.importorskip("aap_libreyolo_plugin")
    asset_dir = os.environ.get("AAP_TEST_LIBRE_ASSET_DIR")
    image_path = os.environ.get("AAP_TEST_LIBRE_IMAGE")
    if not asset_dir or not image_path:
        pytest.skip("set AAP_TEST_LIBRE_ASSET_DIR and AAP_TEST_LIBRE_IMAGE for real native provider test")
    from aap_libreyolo_plugin.assets import assets

    asset = assets()["t"]
    config = {"path": str(Path(asset_dir) / asset["filename"]), "asset": asset}
    first, edited = tmp_path / "first", tmp_path / "edited"
    init_project(first)
    init_project(edited)
    generated = load_project(first, "generated_libreyolo_first")
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    model = generated.Provider().load("t", "cpu", config)
    try:
        detections = model.predict(image, {"conf": .25, "iou": .7, "max_det": 300})
        assert all(isinstance(detection, Detection) for detection in detections)
        assert model.classes
    finally:
        model.close()

    source = edited / "my_libreyolo_plugin.py"
    content = source.read_text(encoding="utf-8")
    content = content.replace("return self.native.predict(image, parameters)", 'raise RuntimeError("edited author hook executed")')
    source.write_text(content, encoding="utf-8")
    customized = load_project(edited, "generated_libreyolo_edited")
    model = customized.Provider().load("t", "cpu", config)
    try:
        with pytest.raises(RuntimeError, match="edited author hook executed"):
            model.predict(image, {"conf": .25, "iou": .7, "max_det": 300})
    finally:
        model.close()
