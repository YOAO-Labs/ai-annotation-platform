import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aap_protocol_v2.backend_auth import BACKEND_SERVICES, validate_backend_urls
from aap_backend_runtime.backend_auth import install_backend_auth
from aap_protocol_v2.managed_credential import ensure_credential


@pytest.mark.parametrize("provider", BACKEND_SERVICES)
def test_every_service_protects_business_and_handshake_but_keeps_health_public(
    tmp_path, monkeypatch, provider
):
    monkeypatch.delenv("AAP_BACKEND_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("YOLO_MANAGED_MODELS_TOKEN", raising=False)
    monkeypatch.setenv("AAP_BACKEND_AUTH_DIR", str(tmp_path / provider))
    app = FastAPI()
    install_backend_auth(app, provider)
    for path in ("/health", "/metrics", "/setup", "/predict", "/control/test"):
        app.get(path)(lambda: {"ok": True})
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/metrics").status_code == 200
        for path in ("/managed-auth", "/setup", "/predict", "/control/test"):
            assert client.get(path).status_code == 403
        token = ensure_credential(tmp_path / provider)
        headers = {"Authorization": f"Bearer {token}"}
        response = client.get("/managed-auth", headers=headers)
        assert response.json() == {"mode": "managed", "provider": provider}
        assert token not in response.text
        for path in ("/setup", "/predict", "/control/test"):
            assert client.get(path, headers=headers).status_code == 200
        assert (
            client.get("/setup", headers={"Authorization": "Bearer wrong"}).status_code
            == 403
        )
        assert (
            client.get(
                "/setup",
                headers=[
                    ("Authorization", f"Bearer {token}"),
                    ("Authorization", f"Bearer {token}"),
                ],
            ).status_code
            == 403
        )


@pytest.mark.parametrize("provider", BACKEND_SERVICES)
def test_manual_token_always_takes_precedence_without_changing_private_file(
    tmp_path, monkeypatch, provider
):
    directory = tmp_path / provider
    token = ensure_credential(directory)
    monkeypatch.setenv("AAP_BACKEND_AUTH_DIR", str(directory))
    monkeypatch.setenv("AAP_BACKEND_AUTH_TOKEN", "m" * 64)
    monkeypatch.delenv("YOLO_MANAGED_MODELS_TOKEN", raising=False)
    app = FastAPI()
    install_backend_auth(app, provider)
    with TestClient(app) as client:
        assert (
            client.get(
                "/managed-auth", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 403
        )
        assert client.get(
            "/managed-auth", headers={"Authorization": "Bearer " + "m" * 64}
        ).json() == {"provider": provider, "mode": "manual"}
        if provider == "yolo":
            monkeypatch.setenv("YOLO_MANAGED_MODELS_TOKEN", "y" * 64)
            assert (
                client.get(
                    "/managed-auth", headers={"Authorization": "Bearer " + "m" * 64}
                ).status_code
                == 403
            )
            assert (
                client.get(
                    "/managed-auth", headers={"Authorization": "Bearer " + "y" * 64}
                ).status_code
                == 200
            )
    assert (directory / "token").read_text().strip() == token


@pytest.mark.parametrize(
    "mapping",
    [
        {"external": "http://elsewhere"},
        {"yolo": "http://backend:8003", "sam3": "http://BACKEND:8003/"},
        {"yolo": "http://backend:8003/path"},
        {"yolo": "http://user@backend:8003"},
        {"yolo": "http://backend:8003?secret=value"},
        {"yolo": "http://backend:8003\n"},
    ],
)
def test_deployment_mapping_rejects_ambiguous_or_untrusted_urls(mapping):
    with pytest.raises(ValueError):
        validate_backend_urls(mapping)
