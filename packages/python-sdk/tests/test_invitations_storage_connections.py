"""Invitation and storage-connection SDK route contracts."""

import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from ai_annotation.invitation_models import (
    Invitation,
    InvitationEmailSent,
    InvitationPage,
    InvitationResend,
)
from ai_annotation.invitations import Invitations
from ai_annotation.storage_connection_models import (
    ConnectorAllowlistUpdate,
    S3StorageConfig,
    StorageConnection,
    StorageConnectionCreate,
    StorageConnectionUpdate,
    SFTPStorageConfig,
)
from ai_annotation.storage_connections import StorageConnections

from .conftest import API

INVITATION_ID, PROJECT_ID, INVITER_ID, CONNECTION_ID, CREATOR_ID = [
    str(uuid4()) for _ in range(5)
]

INVITATION = {
    "id": INVITATION_ID,
    "email": "person@example.test",
    "role": "employee",
    "group_name": "review",
    "project_id": PROJECT_ID,
    "project_name": "QA",
    "project_member_role": "reviewer",
    "status": "pending",
    "expires_at": "2026-11-01T00:00:00Z",
    "invited_by": INVITER_ID,
    "invited_by_name": "Admin",
    "accepted_at": None,
    "revoked_at": None,
    "created_at": "2026-10-17T00:00:00Z",
}

CONNECTION = {
    "id": CONNECTION_ID,
    "name": "Backups",
    "kind": "s3",
    "config": {"endpoint": "https://s3.example.test", "bucket": "data"},
    "scope": "owner",
    "project_id": None,
    "secret_set": True,
    "created_by": CREATOR_ID,
    "created_at": "2026-10-17T00:00:00Z",
    "updated_at": "2026-10-17T00:00:00Z",
}


def test_invitation_legacy_list_is_array_and_status_alias_is_exact(client, respx_mock):
    route = respx_mock.get(f"{API}/invitations").mock(
        return_value=httpx.Response(200, json=[INVITATION])
    )

    invitations = Invitations(client._http).list(status="pending", scope="me")

    assert isinstance(invitations[0], Invitation)
    assert invitations[0].email == "person@example.test"
    assert route.calls.last.request.url.params["status"] == "pending"
    assert route.calls.last.request.url.params["scope"] == "me"


def test_invitation_query_forwards_page_filters_and_iso_dates(client, respx_mock):
    route = respx_mock.get(f"{API}/invitations/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [INVITATION],
                "total": 1,
                "page": 2,
                "page_size": 10,
                "pages": 1,
            },
        )
    )
    created_from = datetime(2026, 10, 1, tzinfo=timezone.utc)

    page = Invitations(client._http).query(
        page=2,
        page_size=10,
        status="pending",
        scope="all",
        project_id=PROJECT_ID,
        role="employee",
        email="person@example.test",
        search="person",
        created_from=created_from,
    )

    assert isinstance(page, InvitationPage)
    assert str(page.items[0].id) == INVITATION_ID
    assert page.total == 1 and page.page == 2
    params = route.calls.last.request.url.params
    assert params["page"] == "2" and params["page_size"] == "10"
    assert params["status"] == "pending" and params["scope"] == "all"
    assert params["project_id"] == PROJECT_ID
    assert params["created_from"] == created_from.isoformat()
    assert "created_to" not in params


def test_invitation_stats_and_binary_export(client, respx_mock, tmp_path):
    stats_route = respx_mock.get(f"{API}/invitations/stats").mock(
        return_value=httpx.Response(
            200,
            json={"total": 2, "pending": 1, "accepted": 1, "expired": 0, "revoked": 0},
        )
    )
    export_route = respx_mock.get(f"{API}/invitations/export").mock(
        return_value=httpx.Response(
            200,
            content=b"id,email\n1,person@example.test\n",
            headers={"Content-Disposition": 'attachment; filename="invitations.csv"'},
        )
    )
    invitations = Invitations(client._http)

    stats = invitations.stats(status="all", scope="me", search="person")
    out = tmp_path / "invites.csv"
    path = invitations.export(out, format="csv", scope="me", search="person")

    assert stats.total == 2 and stats.pending == 1
    assert stats_route.called
    assert path == out
    assert out.read_bytes() == b"id,email\n1,person@example.test\n"
    assert export_route.calls.last.request.url.params["format"] == "csv"
    assert export_route.calls.last.request.url.params["search"] == "person"


def test_invitation_revoke_resend_and_send_email_use_real_envelopes(client, respx_mock):
    revoke = respx_mock.delete(f"{API}/invitations/{INVITATION_ID}").mock(
        return_value=httpx.Response(204)
    )
    resend = respx_mock.post(f"{API}/invitations/{INVITATION_ID}/resend").mock(
        return_value=httpx.Response(
            200,
            json={
                "invite_url": "https://app.example.test/register?token=rotated",
                "token": "rotated",
                "expires_at": "2026-11-01T00:00:00Z",
            },
        )
    )
    send_email = respx_mock.post(f"{API}/invitations/{INVITATION_ID}/send-email").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "invitation_id": INVITATION_ID,
                "email": "person@example.test",
                "invite_url": "https://app.example.test/register?token=existing",
                "message": "sent",
            },
        )
    )
    invitations = Invitations(client._http)

    assert invitations.revoke(INVITATION_ID) is None
    resent = invitations.resend(INVITATION_ID)
    sent = invitations.send_email(INVITATION_ID)

    assert revoke.call_count == resend.call_count == send_email.call_count == 1
    assert resent.token == "rotated"
    assert sent.ok is True
    assert "existing" in sent.invite_url


def test_storage_connection_create_forbids_unknown_config_keys(client, respx_mock):
    route = respx_mock.post(f"{API}/storage-connections").mock(
        return_value=httpx.Response(201, json=CONNECTION)
    )
    body = StorageConnectionCreate(
        name="Backups",
        kind="s3",
        config={
            "endpoint": "https://s3.example.test",
            "bucket": "data",
            "region": "us-east-1",
        },
        secret={"access_key": "AK-private", "secret_key": "SK-private"},
    )

    connection = StorageConnections(client._http).create(body)

    sent = json.loads(route.calls.last.request.content)
    assert sent["config"]["region"] == "us-east-1"
    assert sent["secret"] == {"access_key": "AK-private", "secret_key": "SK-private"}
    assert connection.secret_set is True
    assert not hasattr(connection, "secret")
    assert "AK-private" not in repr(body) and "SK-private" not in repr(body)

    with pytest.raises(ValidationError):
        StorageConnectionCreate(
            name="Backups",
            kind="s3",
            config={
                "endpoint": "https://s3.example.test",
                "bucket": "data",
                "future_vendor_option": True,
            },
            secret={"access_key": "AK", "secret_key": "SK"},
        )


def test_storage_connection_update_preserves_unset_and_explicit_null(
    client, respx_mock
):
    route = respx_mock.patch(f"{API}/storage-connections/{CONNECTION_ID}").mock(
        return_value=httpx.Response(200, json=CONNECTION)
    )

    StorageConnections(client._http).update(
        conn_id=CONNECTION_ID,
        body=StorageConnectionUpdate(
            name=None, secret={"access_key": "AK", "secret_key": "SK"}
        ),
    )

    assert json.loads(route.calls.last.request.content) == {
        "name": None,
        "secret": {"access_key": "AK", "secret_key": "SK"},
    }


def test_storage_connection_nonsecret_routes_and_negative_test_result(
    client, respx_mock
):
    list_route = respx_mock.get(f"{API}/storage-connections").mock(
        return_value=httpx.Response(200, json=[CONNECTION])
    )
    get_route = respx_mock.get(f"{API}/storage-connections/{CONNECTION_ID}").mock(
        return_value=httpx.Response(200, json=CONNECTION)
    )
    test_route = respx_mock.post(
        f"{API}/storage-connections/{CONNECTION_ID}/test"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"ok": False, "message": "connection refused", "sample_count": None},
        )
    )
    allowlist_route = respx_mock.get(f"{API}/storage-connections/allowlist").mock(
        return_value=httpx.Response(
            200, json={"entries": ["example.test"], "source": "database"}
        )
    )
    allowlist_update = respx_mock.put(f"{API}/storage-connections/allowlist").mock(
        return_value=httpx.Response(
            200, json={"entries": ["example.test"], "source": "database"}
        )
    )
    allowlist_reset = respx_mock.delete(f"{API}/storage-connections/allowlist").mock(
        return_value=httpx.Response(
            200, json={"entries": ["example.test"], "source": "environment"}
        )
    )
    preset_route = respx_mock.get(
        f"{API}/storage-connections/deployment-sftp-preset"
    ).mock(
        return_value=httpx.Response(
            200, json={"enabled": False, "host": None, "port": 22}
        )
    )
    deleted = respx_mock.delete(f"{API}/storage-connections/{CONNECTION_ID}").mock(
        return_value=httpx.Response(204)
    )
    client_api = StorageConnections(client._http)

    assert str(client_api.list()[0].id) == CONNECTION_ID
    assert client_api.get(CONNECTION_ID).secret_set is True
    result = client_api.test(CONNECTION_ID)
    assert result.ok is False
    assert result.sample_count is None
    assert client_api.get_allowlist().entries == ["example.test"]
    assert (
        client_api.update_allowlist(
            ConnectorAllowlistUpdate(entries=["example.test"])
        ).source
        == "database"
    )
    assert client_api.reset_allowlist().source == "environment"
    assert client_api.deployment_sftp_preset().enabled is False
    assert client_api.delete(CONNECTION_ID) is None
    assert all(
        route.call_count == 1
        for route in (
            list_route,
            get_route,
            test_route,
            allowlist_route,
            allowlist_update,
            allowlist_reset,
            preset_route,
            deleted,
        )
    )


def test_allowlist_update_limits_match_api_contract():
    assert len(ConnectorAllowlistUpdate(entries=["a" * 253]).entries[0]) == 253
    with pytest.raises(ValidationError):
        ConnectorAllowlistUpdate(entries=["a" * 254])
    with pytest.raises(ValidationError):
        ConnectorAllowlistUpdate(entries=[f"host-{i}.test" for i in range(257)])
    assert ConnectorAllowlistUpdate(entries=[]).entries == []
    with pytest.raises(ValidationError):
        ConnectorAllowlistUpdate()


def test_storage_models_match_kind_and_hide_unexpected_response_secrets():
    assert isinstance(
        S3StorageConfig(endpoint="https://s3.example.test", bucket="data"),
        S3StorageConfig,
    )
    with pytest.raises(ValidationError):
        StorageConnectionCreate(
            name="Mismatch",
            kind="s3",
            config={"host": "sftp.example.test", "username": "u"},
            secret={"password": "secret"},
        )
    connection = StorageConnection.model_validate(
        {**CONNECTION, "secret": "unexpected"}
    )
    assert not hasattr(connection, "secret")


def test_sftp_credentials_are_exclusive_and_match_auth_type():
    key_only_legacy = StorageConnectionCreate(
        name="SFTP",
        kind="sftp",
        config={"host": "sftp.example.test", "username": "svc"},
        secret={"private_key": "private-key"},
    )
    assert isinstance(key_only_legacy.config, SFTPStorageConfig)
    assert key_only_legacy.config.auth_type is None

    with pytest.raises(ValidationError):
        StorageConnectionCreate(
            name="SFTP",
            kind="sftp",
            config={
                "host": "sftp.example.test",
                "username": "svc",
                "auth_type": "password",
            },
            secret={"private_key": "private-key"},
        )
    with pytest.raises(ValidationError):
        StorageConnectionCreate(
            name="SFTP",
            kind="sftp",
            config={"host": "sftp.example.test", "username": "svc"},
            secret={"password": "password", "private_key": "private-key"},
        )
    with pytest.raises(ValidationError):
        StorageConnectionCreate(
            name="SFTP",
            kind="sftp",
            config={"host": "sftp.example.test", "username": "svc"},
            secret={
                "password": "password",
                "unknown_secret": "must not be accepted",
            },
        )
    with pytest.raises(ValidationError):
        StorageConnectionCreate(
            name="SFTP",
            kind="sftp",
            config={
                "host": "sftp.example.test",
                "username": "svc",
                "timeout": 5,
            },
            secret={"private_key": "private-key"},
        )


def test_invitation_token_and_link_are_hidden_from_model_repr():
    resend = InvitationResend(
        invite_url="https://app.example.test/register?token=private-token",
        token="private-token",
        expires_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )
    sent = InvitationEmailSent(
        invitation_id=INVITATION_ID,
        email="person@example.test",
        invite_url="https://app.example.test/register?token=private-token",
        message="sent",
    )

    assert "private-token" not in repr(resend)
    assert "private-token" not in repr(sent)
