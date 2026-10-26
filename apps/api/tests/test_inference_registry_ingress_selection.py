"""Startup environment synchronization requires the configured HTTP owner."""

from __future__ import annotations

import pytest

from aap_inference_contract.registry_ingress import (
    REGISTRY_ENV_SYNC_RESULT_PROTOCOL,
    RegistryEnvSyncUnavailable,
    registry_env_sync_command_sha256,
)
from app.services import inference_registry_ingress as selection

_URLS = ["http://backend-one.example.test", "http://backend-two.example.test"]


@pytest.fixture
def configured(monkeypatch):
    for name in ("url", "token", "token_file"):
        monkeypatch.setattr(
            selection.settings, "inference_registry_ingress_" + name, ""
        )
    return []


def _bind_http(monkeypatch, url="http://owner", token="owner-bearer", token_file=""):
    monkeypatch.setattr(selection.settings, "inference_registry_ingress_url", url)
    monkeypatch.setattr(selection.settings, "inference_registry_ingress_token", token)
    monkeypatch.setattr(
        selection.settings, "inference_registry_ingress_token_file", token_file
    )


def _reply(command, state="committed"):
    return {
        "schema": REGISTRY_ENV_SYNC_RESULT_PROTOCOL,
        "request_id": str(command.request_id),
        "command_sha256": registry_env_sync_command_sha256(command),
        "state": state,
        "observed_urls": None if state == "unknown" else len(command.urls),
    }


@pytest.mark.parametrize(
    ("url", "token"), [("", ""), ("http://owner", ""), ("", "owner-bearer")]
)
async def test_missing_http_owner_selection_is_unavailable_without_fallback(
    configured, monkeypatch, url, token
):
    _bind_http(monkeypatch, url=url, token=token)
    with pytest.raises(RegistryEnvSyncUnavailable):
        await selection.sync_selected_environment(_URLS)
    assert configured == []


async def test_conflicting_token_material_is_unavailable(configured, monkeypatch):
    _bind_http(monkeypatch, token_file="owner-bearer-file")
    with pytest.raises(RegistryEnvSyncUnavailable):
        await selection.sync_selected_environment(_URLS)
    assert configured == []


async def test_unreadable_token_file_is_unavailable(configured, monkeypatch, tmp_path):
    _bind_http(monkeypatch, token="", token_file=str(tmp_path / "missing-token-file"))
    with pytest.raises(RegistryEnvSyncUnavailable):
        await selection.sync_selected_environment(_URLS)
    assert configured == []


@pytest.mark.parametrize("state", ["committed", "caller_pending", "unknown"])
async def test_only_a_committed_bound_result_completes_the_command(
    configured, monkeypatch, state
):
    _bind_http(monkeypatch)

    class Client:
        def __init__(self, url, token, *, timeout_seconds):
            assert (url, token) == ("http://owner", "owner-bearer")

        async def sync(self, command):
            configured.append(("http", command.urls))
            return _reply(command, state)

    monkeypatch.setattr(selection, "RegistryEnvSyncHttpClient", Client)
    if state == "committed":
        assert await selection.sync_selected_environment(_URLS) == len(_URLS)
    else:
        with pytest.raises(RegistryEnvSyncUnavailable):
            await selection.sync_selected_environment(_URLS)
    assert configured == [("http", _URLS)]


async def test_owner_unavailability_is_propagated_without_a_second_command(
    configured, monkeypatch
):
    _bind_http(monkeypatch)

    class Client:
        def __init__(self, url, token, *, timeout_seconds):
            assert (url, token) == ("http://owner", "owner-bearer")

        async def sync(self, command):
            configured.append(("http", command.urls))
            raise RegistryEnvSyncUnavailable()

    monkeypatch.setattr(selection, "RegistryEnvSyncHttpClient", Client)
    with pytest.raises(RegistryEnvSyncUnavailable):
        await selection.sync_selected_environment(_URLS)
    assert configured == [("http", _URLS)]


@pytest.mark.parametrize("field", ["request_id", "command_sha256", "schema"])
async def test_result_must_remain_bound_to_the_submitted_command(
    configured, monkeypatch, field
):
    _bind_http(monkeypatch)

    class Client:
        def __init__(self, url, token, *, timeout_seconds):
            assert (url, token) == ("http://owner", "owner-bearer")

        async def sync(self, command):
            configured.append(("http", command.urls))
            reply = _reply(command)
            reply[field] = (
                "00000000-0000-0000-0000-000000000000"
                if field == "request_id"
                else "0" * 64
                if field == "command_sha256"
                else "aap-inference-registry-env-sync-result/v2"
            )
            return reply

    monkeypatch.setattr(selection, "RegistryEnvSyncHttpClient", Client)
    with pytest.raises(RegistryEnvSyncUnavailable):
        await selection.sync_selected_environment(_URLS)
    assert configured == [("http", _URLS)]
