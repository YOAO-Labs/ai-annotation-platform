"""Authentication routes retain their wire contracts and secret boundaries."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner
from typer.main import get_command

from ai_annotation.auth import Auth
from ai_annotation.auth_models import (
    AuthLoginRequest,
    AuthRegisterRequest,
    AuthOpenRegisterRequest,
    AuthResetPasswordRequest,
)
from ai_annotation.cli import auth as auth_cli
from ai_annotation.cli.auth import app

from .conftest import API

runner = CliRunner()
ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.auth-response.signature"
USER_ID = "00000000-0000-0000-0000-000000000011"


def _user() -> dict[str, object]:
    return {
        "id": USER_ID,
        "email": "person@example.com",
        "name": "Person",
        "role": "employee",
        "group_name": None,
        "status": "online",
        "created_at": "2026-10-17T00:00:00Z",
    }


def _registered(**updates: object) -> dict[str, object]:
    return {
        "access_token": ACCESS_TOKEN,
        "token_type": "bearer",
        "email_verification_required": False,
        "user": _user(),
        **updates,
    }


def test_all_auth_routes_keep_json_contracts(client, respx_mock):
    auth = Auth(client._http)
    resolve = respx_mock.get(f"{API}/auth/invitations/invite-token").mock(
        return_value=httpx.Response(
            200,
            json={
                "email": "person@example.com",
                "role": "employee",
                "group_name": None,
                "expires_at": "2026-10-18T00:00:00Z",
            },
        )
    )
    assert auth.resolve_invitation("invite-token").email == "person@example.com"
    assert resolve.calls.last.request.headers["authorization"] == "Bearer ak_test"

    status = respx_mock.get(f"{API}/auth/registration-status").mock(
        return_value=httpx.Response(200, json={"open_registration_enabled": True})
    )
    assert auth.registration_status().open_registration_enabled
    assert status.called

    forgot = respx_mock.post(f"{API}/auth/forgot-password").mock(
        return_value=httpx.Response(
            202, json={"message": "If registered, we sent a link."}
        )
    )
    assert auth.forgot_password("person@example.com", captcha_token="captcha").message
    assert json.loads(forgot.calls.last.request.content) == {
        "email": "person@example.com",
        "captcha_token": "captcha",
    }

    accept = respx_mock.post(f"{API}/auth/invitations/accept").mock(
        return_value=httpx.Response(
            200,
            json={
                "user": _user(),
                "acceptance": {
                    "next_action": "open_project",
                    "next_action_label": "Open project",
                    "active_batch_count": 0,
                },
            },
        )
    )
    assert (
        auth.accept_invitation("invite-token").acceptance.next_action == "open_project"
    )
    assert json.loads(accept.calls.last.request.content) == {"token": "invite-token"}

    login = respx_mock.post(f"{API}/auth/login").mock(
        return_value=httpx.Response(200, json={"access_token": ACCESS_TOKEN})
    )
    assert (
        auth.login(
            "person@example.com", "Private-password9", captcha_token="captcha"
        ).access_token
        == ACCESS_TOKEN
    )
    assert json.loads(login.calls.last.request.content) == {
        "email": "person@example.com",
        "password": "Private-password9",
        "captcha_token": "captcha",
    }
    assert login.calls.last.request.headers["content-type"].startswith(
        "application/json"
    )

    logout = respx_mock.post(f"{API}/auth/logout").mock(
        return_value=httpx.Response(204)
    )
    assert auth.logout() is None
    assert logout.calls.last.request.content == b""

    logout_all = respx_mock.post(f"{API}/auth/logout-all").mock(
        return_value=httpx.Response(200, json={"access_token": ACCESS_TOKEN})
    )
    assert auth.logout_all().access_token == ACCESS_TOKEN
    assert logout_all.calls.last.request.content == b""

    refresh = respx_mock.post(f"{API}/auth/refresh").mock(
        return_value=httpx.Response(200, json={"access_token": ACCESS_TOKEN})
    )
    assert auth.refresh().access_token == ACCESS_TOKEN
    assert refresh.calls.last.request.content == b""
    assert client._http._client.headers["authorization"] == "Bearer ak_test"

    register = respx_mock.post(f"{API}/auth/register").mock(
        return_value=httpx.Response(201, json=_registered())
    )
    assert (
        auth.register("invite-token", "Person", "Private-password9").user.email
        == "person@example.com"
    )
    assert json.loads(register.calls.last.request.content) == {
        "token": "invite-token",
        "name": "Person",
        "password": "Private-password9",
    }

    register_open = respx_mock.post(f"{API}/auth/register-open").mock(
        return_value=httpx.Response(
            201,
            json=_registered(access_token=None, email_verification_required=True),
        )
    )
    assert auth.register_open(
        "person@example.com", "Person", "Private-password9"
    ).email_verification_required
    assert json.loads(register_open.calls.last.request.content) == {
        "email": "person@example.com",
        "name": "Person",
        "password": "Private-password9",
    }

    reset = respx_mock.post(f"{API}/auth/reset-password").mock(
        return_value=httpx.Response(200, json={"message": "Password reset."})
    )
    assert auth.reset_password("reset-token", "New-private-password9").message
    assert json.loads(reset.calls.last.request.content) == {
        "token": "reset-token",
        "new_password": "New-private-password9",
    }

    send = respx_mock.post(f"{API}/auth/send-verification-email").mock(
        return_value=httpx.Response(
            202, json={"message": "If pending, we sent a link."}
        )
    )
    assert auth.send_verification_email("person@example.com").message
    assert json.loads(send.calls.last.request.content) == {
        "email": "person@example.com"
    }

    verify = respx_mock.post(f"{API}/auth/verify-email").mock(
        return_value=httpx.Response(200, json={"message": "Email verified."})
    )
    assert auth.verify_email("verification-token").message
    assert json.loads(verify.calls.last.request.content) == {
        "token": "verification-token"
    }


def test_auth_request_models_reject_extra_fields():
    assert AuthLoginRequest(email="a@example.com", password="x").password == "x"
    assert (
        AuthRegisterRequest(token="invite", name="Person", password="Test1234").name
        == "Person"
    )

    for model, data in (
        (
            AuthLoginRequest,
            {"email": "a@example.com", "password": "x", "access_token": "secret"},
        ),
        (
            AuthRegisterRequest,
            {
                "token": "invite",
                "name": "Person",
                "password": "Test1234",
                "role": "admin",
            },
        ),
    ):
        try:
            model.model_validate(data)
        except ValueError:
            continue
        raise AssertionError(f"{model.__name__} accepted an unknown field")


def test_auth_cli_commands_have_valid_help():
    command = get_command(app)
    assert command.commands is not None
    for name in command.commands:
        result = runner.invoke(app, [name, "--help"])
        assert result.exit_code == 0, (name, result.output, result.exception)


@pytest.mark.parametrize(
    "password", ["12345678", "UPPERCASE9", "lowercase9", "NoDigitsHere"]
)
def test_registration_and_reset_enforce_server_password_policy(password):
    for model, data in (
        (
            AuthRegisterRequest,
            {"token": "invite", "name": "Person", "password": password},
        ),
        (
            AuthOpenRegisterRequest,
            {"email": "a@example.com", "name": "Person", "password": password},
        ),
        (AuthResetPasswordRequest, {"token": "reset", "new_password": password}),
    ):
        with pytest.raises(ValidationError) as error:
            model.model_validate(data)
        assert password not in str(error.value)


def test_registration_normalizes_email_and_credentials_are_not_in_repr():
    request = AuthOpenRegisterRequest(
        email=" Person@Example.com ",
        name="Person",
        password="Private-pass9",
        captcha_token="captcha-private",
    )
    assert request.email == "person@example.com"
    assert "Private-pass9" not in repr(request) and "captcha-private" not in repr(
        request
    )
    with pytest.raises(ValidationError):
        AuthOpenRegisterRequest(email="invalid", name="Person", password="Valid-pass9")


def test_invitation_token_is_one_opaque_path_segment(client, respx_mock):
    route = respx_mock.get(f"{API}/auth/invitations/a%2Fb%3Fc%23d").mock(
        return_value=httpx.Response(
            200,
            json={
                "email": "person@example.com",
                "role": "employee",
                "group_name": None,
                "expires_at": "2026-10-18T00:00:00Z",
            },
        )
    )
    client.auth.resolve_invitation("a/b?c#d")
    assert route.called
    assert route.calls.last.request.url.raw_path.endswith(b"/a%2Fb%3Fc%23d")


def test_auth_cli_reads_secret_from_stdin_hides_text_token_and_confirms_logout_all(
    client, monkeypatch, respx_mock
):
    class ClientContext:
        auth = Auth(client._http)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(auth_cli, "get_client", lambda _json_mode: ClientContext())
    login = respx_mock.post(f"{API}/auth/login").mock(
        return_value=httpx.Response(200, json={"access_token": ACCESS_TOKEN})
    )
    body = json.dumps({"email": "person@example.com", "password": "Private-password9"})
    result = runner.invoke(app, ["login", "--body", "-"], input=body)
    assert result.exit_code == 0, result.output
    assert ACCESS_TOKEN not in result.stdout
    assert "Private-password9" not in result.stdout
    assert (
        json.loads(login.calls.last.request.content)["password"] == "Private-password9"
    )

    logout_all = respx_mock.post(f"{API}/auth/logout-all").mock(
        return_value=httpx.Response(200, json={"access_token": ACCESS_TOKEN})
    )
    result = runner.invoke(app, ["logout-all", "--json"], env={"NO_COLOR": "1"})
    assert result.exit_code != 0
    assert not logout_all.called

    result = runner.invoke(
        app, ["logout-all", "--yes", "--json"], env={"NO_COLOR": "1"}
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["access_token"] == ACCESS_TOKEN
    assert logout_all.called
