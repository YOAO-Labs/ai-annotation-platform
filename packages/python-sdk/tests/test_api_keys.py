import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ai_annotation.api_key_models import ApiKeyUpdateRequest
from ai_annotation.cli.main import app
from ai_annotation.models import ApiKey, ApiKeyCreated

from .conftest import API

KEY_ID = str(uuid4())
KEY = {
    "id": KEY_ID,
    "name": "ci",
    "key_prefix": "ak_abc1",
    "scopes": [],
    "expires_at": None,
    "last_used_at": None,
    "revoked_at": None,
    "created_at": "2026-06-11T00:00:00Z",
}


def test_list_keys(client, respx_mock):
    respx_mock.get(f"{API}/me/api-keys").mock(
        return_value=httpx.Response(200, json=[KEY])
    )
    keys = client.api_keys.list()
    assert isinstance(keys[0], ApiKey)
    assert keys[0].key_prefix == "ak_abc1"


def test_create_key_returns_plaintext_once(client, respx_mock):
    route = respx_mock.post(f"{API}/me/api-keys").mock(
        return_value=httpx.Response(201, json={**KEY, "plaintext": "ak_abc1xyz"})
    )
    created = client.api_keys.create("ci", scopes=["read"])
    assert json.loads(route.calls.last.request.content) == {
        "name": "ci",
        "scopes": ["read"],
    }
    assert isinstance(created, ApiKeyCreated)
    assert created.plaintext == "ak_abc1xyz"


def test_create_key_with_expiry(client, respx_mock):
    route = respx_mock.post(f"{API}/me/api-keys").mock(
        return_value=httpx.Response(201, json={**KEY, "plaintext": "ak_abc1xyz"})
    )
    client.api_keys.create("ci", scopes=["*"], expires_in_days=30)
    assert json.loads(route.calls.last.request.content) == {
        "name": "ci",
        "scopes": ["*"],
        "expires_in_days": 30,
    }


def test_rotate_key(client, respx_mock):
    route = respx_mock.post(f"{API}/me/api-keys/{KEY_ID}/rotate").mock(
        return_value=httpx.Response(200, json={**KEY, "plaintext": "ak_newplain"})
    )
    created = client.api_keys.rotate(KEY_ID)
    assert route.called
    assert isinstance(created, ApiKeyCreated)
    assert created.plaintext == "ak_newplain"


def test_update_key(client, respx_mock):
    route = respx_mock.patch(f"{API}/me/api-keys/{KEY_ID}").mock(
        return_value=httpx.Response(
            200, json={**KEY, "name": "renamed", "scopes": ["*"]}
        )
    )
    out = client.api_keys.update(KEY_ID, name="renamed", scopes=["*"])
    assert json.loads(route.calls.last.request.content) == {
        "name": "renamed",
        "scopes": ["*"],
    }
    assert isinstance(out, ApiKey)
    assert out.name == "renamed"


def test_revoke_key(client, respx_mock):
    route = respx_mock.delete(f"{API}/me/api-keys/{KEY_ID}").mock(
        return_value=httpx.Response(204)
    )
    client.api_keys.revoke(KEY_ID)
    assert route.called


def test_patch_preserves_explicit_expiry_clear_and_omission(client, respx_mock):
    route = respx_mock.patch(f"{API}/me/api-keys/{KEY_ID}").mock(
        return_value=httpx.Response(200, json=KEY)
    )
    client.api_keys.patch(KEY_ID, ApiKeyUpdateRequest(expires_in_days=None))
    assert json.loads(route.calls.last.request.content) == {"expires_in_days": None}
    client.api_keys.patch(KEY_ID, ApiKeyUpdateRequest(name="renamed"))
    assert json.loads(route.calls.last.request.content) == {"name": "renamed"}
    for field in ("name", "scopes"):
        with pytest.raises(ValidationError):
            ApiKeyUpdateRequest(**{field: None})


def test_cli_key_creation_requires_secret_output_before_request(
    tmp_path, respx_mock, monkeypatch
):
    monkeypatch.setenv("AAP_BASE_URL", "http://testserver")
    monkeypatch.setenv("AAP_API_KEY", "ak_test")
    body = tmp_path / "key.json"
    body.write_text('{"name":"agent"}')
    route = respx_mock.post(f"{API}/me/api-keys").mock(
        return_value=httpx.Response(201, json={**KEY, "plaintext": "ak_one_time"})
    )
    runner = CliRunner()
    refused = runner.invoke(app, ["api-keys", "create", "--body", str(body), "--yes"])
    assert refused.exit_code != 0 and not route.called
    created = runner.invoke(
        app, ["api-keys", "create", "--body", str(body), "--yes", "--json"]
    )
    assert created.exit_code == 0, created.output
    assert json.loads(created.stdout)["plaintext"] == "ak_one_time"
    assert "ak_one_time" not in repr(
        ApiKeyCreated.model_validate({**KEY, "plaintext": "ak_one_time"})
    )
