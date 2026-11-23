"""Keep the API signer, API media key, and isolated mail deployment separate."""

from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import uuid

from jose import jwt
from jose.exceptions import JWTError
import pytest
import yaml
from yoao_identity.security import decode_access_token

from app.config import settings
from app.core.security import create_access_token
from app.services import media_cache_urls
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import get_identity_gateway


ROOT = Path(__file__).resolve().parents[3]


def test_identity_overlay_only_starts_the_v2_sender_without_issuer_profile():
    overlay = yaml.safe_load((ROOT / "docker-compose.identity.yml").read_text())
    assert set(overlay["services"]) == {"identity-issuer", "identity-mail-v2"}
    issuer = overlay["services"]["identity-issuer"]
    mail = overlay["services"]["identity-mail-v2"]
    assert issuer["profiles"] == ["identity-issuer"]
    assert "profiles" not in mail
    assert issuer["environment"]["IDENTITY_SIGNING_SECRET_FILE"] == (
        "/run/secrets/identity_signing_secret"
    )
    assert mail["command"] == ["python", "-m", "yoao_identity_runtime.mail_worker_v2"]
    assert mail["restart"] == "unless-stopped"
    assert set(mail["secrets"]) == {
        "identity_mail_v2_database_url",
        "identity_mail_settings_database_url",
        "identity_recovery_mail_key",
        "identity_mail_smtp_password",
    }
    assert all(name.startswith("IDENTITY_MAIL_V2_") for name in mail["environment"])
    assert "ports" not in mail


def test_embedded_jwt_signer_does_not_change_the_api_media_key(monkeypatch):
    signer = "explicit-identity-signer"
    edge_key = "independent-api-media-key"
    monkeypatch.setattr(settings, "identity_url", "")
    monkeypatch.setattr(settings, "identity_embedded_signing_secret", signer)
    monkeypatch.setattr(settings, "secret_key", edge_key)
    actor_id = uuid.uuid4()
    token = create_access_token(str(actor_id), "employee")
    assert get_identity_gateway().require_signing_secret() == signer
    assert decode_access_token(token, secret=signer)["sub"] == str(actor_id)
    with pytest.raises(JWTError):
        decode_access_token(token, secret=edge_key)

    signed_url = media_cache_urls.sign(
        "video-derivative",
        uuid.uuid4(),
        "prefix/clip.mp4",
        {"schema": 2, "prefix": "prefix/"},
        {"sha256": "a" * 64, "size_bytes": 1},
        60,
    )
    media_token = parse_qs(urlsplit(signed_url).query)["token"][0]
    assert (
        jwt.decode(
            media_token,
            edge_key,
            algorithms=["HS256"],
            audience=media_cache_urls.AUDIENCE,
        )["relative"]
        == "clip.mp4"
    )
    with pytest.raises(JWTError):
        jwt.decode(media_token, signer, algorithms=["HS256"])

    monkeypatch.setattr(settings, "identity_url", "http://issuer.invalid:8170")
    with pytest.raises(IdentityGatewayUnavailable):
        get_identity_gateway().require_signing_secret()
