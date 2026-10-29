from __future__ import annotations

import main


def test_plugin_setup_does_not_accept_first_party_lifecycle_flag(monkeypatch):
    monkeypatch.setattr(main, "MANAGED_LIFECYCLE_VERIFIED", True)
    monkeypatch.setenv("AAP_PLUGIN_TOKEN_FILE", "/private/token")
    monkeypatch.setenv("AAP_PLUGIN_REGISTRY_ID", "12345678-1234-4234-8234-123456789abc")
    monkeypatch.setenv("AAP_ROUTE_REGISTRY_ID", "12345678-1234-4234-8234-123456789abc")
    monkeypatch.setenv("AAP_PLUGIN_DEVICE", "cpu")
    monkeypatch.setenv("AAP_PLUGIN_ID", "rapidocr")
    monkeypatch.setenv("AAP_PLUGIN_VERSION", "0.1.0")
    monkeypatch.delenv("AAP_PLUGIN_GPU_RESOURCE_ID", raising=False)
    monkeypatch.delenv("AAP_PLUGIN_GPU_UUID", raising=False)

    setup = main.setup()

    assert "managed_lifecycle" not in setup
    assert {entry["id"] for entry in setup["models"]} == {
        "ocr-det",
        "ocr-rec",
        "ocr-e2e",
    }
