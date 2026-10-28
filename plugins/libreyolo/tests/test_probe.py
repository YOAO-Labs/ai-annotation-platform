import json
from types import SimpleNamespace

from PIL import Image
import torch

from aap_libreyolo_plugin import assets, device, probe
from aap_libreyolo_plugin.probe import run


def test_cpu_developer_probe_never_claims_gpu_qualification(tmp_path, monkeypatch):
    item = {"filename": "weights.pt", "size_bytes": 3, "sha256": "0" * 64}
    monkeypatch.setattr(assets, "assets", lambda: {"t": item})
    report_path = tmp_path / "report.json"
    args = SimpleNamespace(device="cpu", gpu_uuid="GPU-invalid", asset_dir=tmp_path,
                           multi_image=tmp_path / "multi.png", empty_image=tmp_path / "empty.png",
                           output=report_path)
    assert not run(args)
    report = json.loads(report_path.read_text())
    assert report["mode"] == "local-isolated"
    assert report["checks"]["missing_weights"]["passed"]
    assert not report["checks"]["gpu_residency"]["passed"]
    assert not report["checks"]["gpu_release"]["passed"]
    assert not report["platform_managed_qualified"]
    assert "not assessed" in report["cancellation_drain"]


def test_probe_loads_injected_provider_each_cycle(tmp_path, monkeypatch):
    item = {"filename": "weights.pt"}
    monkeypatch.setattr(assets, "assets", lambda: {"t": item})
    def verify(path, _item):
        if not path.exists():
            raise ValueError("missing")
        return path
    monkeypatch.setattr(assets, "verify", verify)
    def verify_device(selected, uuid):
        if selected != "cuda:0":
            raise ValueError("wrong device")
        return uuid
    monkeypatch.setattr(device, "verify_device", verify_device)
    monkeypatch.setattr(probe, "require_exclusive_gpu", lambda uuid: None)
    monkeypatch.setattr(torch.cuda, "device_count", lambda: 1)
    monkeypatch.setattr(torch.cuda, "set_device", lambda selected: None)
    monkeypatch.setattr(torch.cuda, "synchronize", lambda selected: None)
    monkeypatch.setattr(torch.cuda, "empty_cache", lambda: None)
    memory = iter([0, 10, 0, 10, 0])
    monkeypatch.setattr(torch.cuda, "memory_allocated", lambda selected: next(memory))
    monkeypatch.setattr(torch.cuda, "mem_get_info", lambda selected: (100, 200))
    (tmp_path / "weights.pt").write_bytes(b"weights")
    multi, empty = tmp_path / "multi.png", tmp_path / "empty.png"
    Image.new("RGB", (2, 2), "white").save(multi)
    Image.new("RGB", (2, 2), "black").save(empty)
    calls = []
    class Model:
        def predict(self, image, parameters):
            return [object(), object()] if image.getpixel((0, 0))[0] else []
        def close(self):
            calls.append("close")
    class CustomProvider:
        def load(self, variant, selected, config):
            calls.append((variant, selected, config))
            return Model()
    args = SimpleNamespace(device="cuda:0", gpu_uuid="GPU-123", asset_dir=tmp_path,
                           multi_image=multi, empty_image=empty, output=tmp_path / "report.json")
    assert run(args, provider_factory=CustomProvider)
    assert calls == [("t", "cuda:0", {"path": str(tmp_path / "weights.pt")}), "close"] * 2
    report = json.loads(args.output.read_text())
    assert report["checks"]["native_multi"]["detail"]["counts"] == [2, 2]
    assert report["checks"]["native_empty"]["detail"]["counts"] == [0, 0]
    assert not report["platform_managed_qualified"]
