"""An explicitly selected startup command owner never falls back to a writer."""

from __future__ import annotations

import pytest

from aap_inference_contract.registry_ingress import (
    REGISTRY_ENV_SYNC_RESULT_PROTOCOL,
    RegistryEnvSyncUnavailable,
    registry_env_sync_command_sha256,
)
from app.services import inference_registry_ingress as selection


@pytest.fixture
def configured(monkeypatch):
    for name in ("url", "token", "token_file", "database_url", "database_url_file"):
        monkeypatch.setattr(
            selection.settings, "inference_registry_ingress_" + name, ""
        )
    calls = []

    async def borrowed(urls):
        calls.append(("borrowed", urls))
        return len(urls)

    return calls, borrowed


def _reply(command, state="committed"):
    return {
        "schema": REGISTRY_ENV_SYNC_RESULT_PROTOCOL,
        "request_id": str(command.request_id),
        "command_sha256": registry_env_sync_command_sha256(command),
        "state": state,
        "observed_urls": None if state == "unknown" else len(command.urls),
    }


async def test_unconfigured_owner_keeps_borrowed_empty_environment(configured):
    calls, borrowed = configured
    assert await selection.sync_selected_environment([], borrowed=borrowed) == 0
    assert calls == [("borrowed", [])]


async def test_borrowed_owner_keeps_legacy_materialization_error(configured):
    async def borrowed(urls):
        raise ValueError("legacy JSON materialization")

    with pytest.raises(ValueError, match="legacy JSON materialization"):
        await selection.sync_selected_environment([], borrowed=borrowed)


@pytest.mark.parametrize("kind", ["conflict", "missing_file"])
async def test_invalid_explicit_selection_never_calls_borrowed(
    configured, monkeypatch, tmp_path, kind
):
    calls, borrowed = configured
    if kind == "conflict":
        monkeypatch.setattr(
            selection.settings, "inference_registry_ingress_url", "http://owner"
        )
        monkeypatch.setattr(
            selection.settings,
            "inference_registry_ingress_database_url",
            "postgresql://restricted",
        )
    else:
        monkeypatch.setattr(
            selection.settings,
            "inference_registry_ingress_database_url_file",
            str(tmp_path / "missing"),
        )
    with pytest.raises(RegistryEnvSyncUnavailable):
        await selection.sync_selected_environment([], borrowed=borrowed)
    assert calls == []


@pytest.mark.parametrize(
    "outcome", ["committed", "caller_pending", "unknown", "unavailable"]
)
async def test_http_command_requires_committed_bound_result_and_never_borrows(
    configured, monkeypatch, outcome
):
    calls, borrowed = configured
    monkeypatch.setattr(
        selection.settings, "inference_registry_ingress_url", "http://owner"
    )
    monkeypatch.setattr(
        selection.settings, "inference_registry_ingress_token", "owner-bearer"
    )

    class Client:
        def __init__(self, url, token, *, timeout_seconds):
            assert (url, token) == ("http://owner", "owner-bearer")

        async def sync(self, command):
            calls.append(("http", command.urls))
            if outcome == "unavailable":
                raise RegistryEnvSyncUnavailable()
            return _reply(command, outcome)

    monkeypatch.setattr(selection, "RegistryEnvSyncHttpClient", Client)
    if outcome == "committed":
        assert await selection.sync_selected_environment([], borrowed=borrowed) == 0
    else:
        with pytest.raises(RegistryEnvSyncUnavailable):
            await selection.sync_selected_environment([], borrowed=borrowed)
    assert calls == [("http", [])]


async def test_dsn_ambiguous_ack_reads_status_once_without_resubmitting(
    configured, monkeypatch
):
    from aap_inference import registry_ingress_manager

    calls, borrowed = configured
    monkeypatch.setattr(
        selection.settings,
        "inference_registry_ingress_database_url",
        "postgresql://restricted",
    )

    class Manager:
        def __init__(self, dsn):
            assert dsn == "postgresql://restricted"

        async def sync(self, command):
            self.command = command
            calls.append(("sync", command.urls))
            raise RegistryEnvSyncUnavailable()

        async def status(self, request):
            assert request.request_id == self.command.request_id
            calls.append(("status",))
            return _reply(self.command)

        async def close(self):
            calls.append(("close",))

    monkeypatch.setattr(registry_ingress_manager, "RegistryEnvSyncManager", Manager)
    assert await selection.sync_selected_environment([], borrowed=borrowed) == 0
    assert calls == [("sync", []), ("status",), ("close",)]
