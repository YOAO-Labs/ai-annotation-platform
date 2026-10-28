from types import SimpleNamespace

from aap_libreyolo_plugin import qualification_server


def test_local_server_uses_injected_provider_and_loopback(tmp_path, monkeypatch):
    token, keyring = tmp_path / "token", tmp_path / "keyring"
    token.write_text("local-token-" * 4)
    keyring.write_text("keys")
    args = SimpleNamespace(device="cuda:0", gpu_uuid="GPU-123", asset_dir=tmp_path,
                           token_file=token, keyring_file=keyring, registry="registry",
                           resource="resource", port=12345)
    monkeypatch.setattr(qualification_server, "verify_device", lambda device, uuid: uuid)
    monkeypatch.setattr(qualification_server, "assets", lambda: {"t": {"filename": "weights.pt"}})
    monkeypatch.setattr(qualification_server, "verify", lambda path, item: path)
    monkeypatch.setattr(qualification_server, "load_verify_keyring", lambda data: {"local": data})
    routes = {}
    class App:
        def get(self, path):
            def register(fn):
                routes[path] = fn
                return fn
            return register
    app = App()
    provider = object()
    captured = {}
    def create(actual_provider, config):
        captured.update(provider=actual_provider, config=config)
        return app
    def serve(actual_app, **kwargs):
        captured.update(app=actual_app, serve=kwargs)
    monkeypatch.setattr(qualification_server, "create_detection_app", create)
    monkeypatch.setattr(qualification_server.uvicorn, "run", serve)
    qualification_server.run(args, provider_factory=lambda: provider)
    assert captured["provider"] is provider
    assert captured["app"] is app
    assert captured["serve"] == {"host": "127.0.0.1", "port": 12345, "log_level": "warning"}
    assert captured["config"].physical_gpu_uuid == "GPU-123"
    assert captured["config"].expected_registry_id == "registry"
    assert captured["config"].expected_gpu_resource_id == "resource"
    assert captured["config"].default_model == "t"
    assert set(routes) == {"/_local/gpu-memory", "/_local/gpu-exclusivity"}
