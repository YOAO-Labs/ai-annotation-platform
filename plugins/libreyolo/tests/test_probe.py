import json

from aap_libreyolo_plugin import assets
from aap_libreyolo_plugin.probe import run


def test_cpu_developer_probe_never_claims_gpu_qualification(tmp_path, monkeypatch):
    item = {"filename": "weights.pt", "size_bytes": 3, "sha256": "0" * 64}
    monkeypatch.setattr(assets, "assets", lambda: {"t": item})
    report_path = tmp_path / "report.json"
    assert not run(device="cpu", gpu_uuid="GPU-invalid", asset_dir=tmp_path,
                   multi_image=tmp_path / "multi.png", empty_image=tmp_path / "empty.png",
                   output=report_path)
    report = json.loads(report_path.read_text())
    assert report["mode"] == "local-isolated"
    assert report["checks"]["missing_weights"]["passed"]
    assert not report["checks"]["gpu_residency"]["passed"]
    assert not report["checks"]["gpu_release"]["passed"]
    assert not report["platform_managed_qualified"]
    assert "not assessed" in report["cancellation_drain"]
