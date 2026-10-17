"""Explicit signed upload stage retains task identity without completing it."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from ai_annotation import FileUploadInit
from ai_annotation.cli.main import app
from ai_annotation.errors import APIStatusError

from .conftest import BASE

TASK = "11111111-1111-4111-8111-111111111111"
URL = "https://storage.example/signed-put"
ENV = {"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1"}


def _init(url=URL):
    return FileUploadInit(task_id=TASK, upload_url=url, expires_in=900)


@pytest.mark.parametrize("status", [200, 201, 204])
def test_signed_content_upload_is_separate_and_has_no_platform_auth(
    client, respx_mock, tmp_path, status
):
    file = tmp_path / "frame.jpg"
    file.write_bytes(b"image bytes")
    route = respx_mock.put(URL).respond(status)
    assert client.files.upload_content(_init(), file, content_type="image/jpeg") is None
    request = route.calls.last.request
    assert request.content == b"image bytes"
    assert request.headers["content-type"] == "image/jpeg"
    assert "authorization" not in request.headers
    assert len(respx_mock.calls) == 1


def test_upload_redirect_is_not_acknowledged_as_success(client, respx_mock, tmp_path):
    file = tmp_path / "frame.jpg"
    file.write_bytes(b"image bytes")
    route = respx_mock.put(URL).respond(
        307, headers={"Location": "https://other.example/put"}
    )
    with pytest.raises(APIStatusError) as error:
        client.files.upload_content(_init(), file, content_type="image/jpeg")
    assert error.value.status_code == 307
    assert route.call_count == len(respx_mock.calls) == 1


def test_upload_url_must_be_absolute(client, respx_mock, tmp_path):
    with pytest.raises(ValueError):
        client.files.upload_content(
            _init("/files/not-signed"),
            tmp_path / "frame.jpg",
            content_type="image/jpeg",
        )
    assert not respx_mock.calls


def test_upload_network_error_does_not_retry_or_complete(client, respx_mock, tmp_path):
    file = tmp_path / "frame.jpg"
    file.write_bytes(b"image bytes")
    route = respx_mock.put(URL).mock(side_effect=httpx.ReadError("lost PUT response"))
    with pytest.raises(httpx.ReadError):
        client.files.upload_content(_init(), file, content_type="image/jpeg")
    assert route.call_count == len(respx_mock.calls) == 1


def test_cli_stdin_init_and_explicit_upload_confirmation(respx_mock, tmp_path):
    file = tmp_path / "frame.jpg"
    file.write_bytes(b"image bytes")
    route = respx_mock.put(URL).respond(200)
    body = _init().model_dump_json()
    args = [
        "files",
        "upload-content",
        "--init",
        "-",
        "--file",
        str(file),
        "--content-type",
        "image/jpeg",
        "--json",
    ]
    refusal = CliRunner().invoke(app, args, input=body, env=ENV)
    assert refusal.exit_code == 2 and not respx_mock.calls
    result = CliRunner().invoke(app, [*args, "--yes"], input=body, env=ENV)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"uploaded": True, "task_id": TASK}
    assert route.call_count == len(respx_mock.calls) == 1
