"""API transport selection and secret boundaries for the Notifications owner."""

import asyncio
from types import SimpleNamespace
import uuid

import pytest

from aap_notifications.publication_contract import (
    ImmediatePublicationRequest,
    ImmediatePublicationUnavailable,
)
from app.services import notification_publication as binding


def request():
    return ImmediatePublicationRequest(
        schema="aap-notifications-immediate-publication/v1",
        user_id=uuid.uuid4(),
        message={"type": "notifications.sync", "reason": "read"},
        authorization=None,
    )


def configure(monkeypatch, *, origin="", token="", path=""):
    monkeypatch.setattr(
        binding,
        "settings",
        SimpleNamespace(
            notification_publication_url=origin,
            notification_publication_token=token,
            notification_publication_token_file=path,
        ),
    )


@pytest.mark.asyncio
async def test_embedded_selection_keeps_existing_publish_seam_and_native_values(
    monkeypatch,
):
    configure(monkeypatch, token="unused", path="/unused/credential")
    value = request()
    calls = []

    async def embedded(**kwargs):
        calls.append(kwargs)

    assert await binding.publish_notification_request(value, embedded=embedded)
    assert calls == [{"user_id": value.user_id, "message": value.message}]
    assert calls[0]["message"] is value.message


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_selected_owner_returns_actual_acceptance_without_embedded_publish(
    monkeypatch, accepted
):
    configure(monkeypatch, origin="http://publisher:8181", token="owner-token")
    value = request()
    calls = []

    class Remote:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        async def publish(self, command):
            assert command is value
            return accepted

    async def embedded(**_kwargs):
        raise AssertionError("selected owner fell back to the embedded transport")

    monkeypatch.setattr(binding, "RemotePublicationClient", Remote)
    assert (
        await binding.publish_notification_request(value, embedded=embedded) is accepted
    )
    assert calls == [
        {"origin": "http://publisher:8181", "service_token": "owner-token"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_selected_failure_and_cancellation_propagate_without_fallback(
    monkeypatch, cancelled
):
    configure(monkeypatch, origin="http://publisher:8181", token="owner-token")

    class Remote:
        def __init__(self, **_kwargs):
            pass

        async def publish(self, _command):
            if cancelled:
                raise asyncio.CancelledError
            raise ImmediatePublicationUnavailable

    async def embedded(**_kwargs):
        raise AssertionError("selected owner fell back after a transport failure")

    monkeypatch.setattr(binding, "RemotePublicationClient", Remote)
    error = asyncio.CancelledError if cancelled else ImmediatePublicationUnavailable
    with pytest.raises(error):
        await binding.publish_notification_request(request(), embedded=embedded)


@pytest.mark.asyncio
async def test_selected_file_credential_is_read_without_exposing_the_path(
    monkeypatch, tmp_path
):
    path = tmp_path / "publication-token"
    path.write_text(" file-token\n")
    configure(monkeypatch, origin="http://publisher:8181", path=str(path))

    class Remote:
        def __init__(self, *, origin, service_token):
            assert origin == "http://publisher:8181"
            assert service_token == "file-token"

        async def publish(self, _command):
            return True

    async def embedded(**_kwargs):
        raise AssertionError("file credential selected embedded transport")

    monkeypatch.setattr(binding, "RemotePublicationClient", Remote)
    assert await binding.publish_notification_request(request(), embedded=embedded)


@pytest.mark.parametrize(
    ("token", "content", "missing"),
    [
        ("both-sources", b"file-token", False),
        ("", b"", False),
        ("", b" \n", False),
        ("", b"\xff", False),
        ("", b"x" * 4097, False),
        ("", b"", True),
    ],
)
def test_invalid_file_credential_is_rejected_without_echoing_input(
    monkeypatch, tmp_path, token, content, missing
):
    path = tmp_path / "private-credential-file"
    if not missing:
        path.write_bytes(content)
    configure(monkeypatch, origin="http://publisher:8181", token=token, path=str(path))
    with pytest.raises(ImmediatePublicationUnavailable) as caught:
        binding._service_token()
    assert str(path) not in str(caught.value)
    assert "both-sources" not in str(caught.value)
