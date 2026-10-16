from __future__ import annotations

import hashlib

import httpx
import pytest

from ai_annotation.errors import AAPError, NotFoundError

from .conftest import BASE


class ChunkedStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks

    def close(self) -> None:
        pass


class InterruptedStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"partial"
        raise OSError("connection interrupted")

    def close(self) -> None:
        pass


def _temporary_files(tmp_path) -> list:
    return list(tmp_path.glob(".aap-download-*.part"))


def test_relative_download_streams_with_auth_params_and_integrity(
    client, respx_mock, tmp_path
):
    chunks = [b"first-", b"second"]
    payload = b"".join(chunks)
    route = respx_mock.get(f"{BASE}/api/v1/files/artifact").mock(
        return_value=httpx.Response(200, stream=ChunkedStream(chunks))
    )
    dest = tmp_path / "artifact.bin"
    dest.write_bytes(b"old destination")

    result = client._http.stream_download(
        "/api/v1/files/artifact",
        dest,
        params={"variant": "best", "include_preview": False},
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        expected_size=len(payload),
    )

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer ak_test"
    assert dict(request.url.params) == {
        "variant": "best",
        "include_preview": "false",
    }
    assert result == dest
    assert dest.read_bytes() == payload
    assert _temporary_files(tmp_path) == []


def test_presigned_download_uses_bare_client_and_forwards_params(
    client, respx_mock, tmp_path
):
    route = respx_mock.get(host="storage.local", path="/artifact.bin").mock(
        return_value=httpx.Response(200, stream=ChunkedStream([b"a", b"b"]))
    )
    dest = tmp_path / "artifact.bin"
    client._http.stream_download(
        "https://storage.local/artifact.bin?signature=abc",
        dest,
        params={"response-content-type": "application/octet-stream"},
        expected_size=2,
    )

    request = route.calls.last.request
    assert "Authorization" not in request.headers
    assert request.url.params["signature"] == "abc"
    assert request.url.params["response-content-type"] == "application/octet-stream"
    assert dest.read_bytes() == b"ab"


@pytest.mark.parametrize(
    "metadata",
    [
        {"expected_sha256": "0" * 64},
        {"expected_size": 99},
    ],
)
def test_integrity_mismatch_preserves_destination_and_cleans_temp(
    client, respx_mock, tmp_path, metadata
):
    route = respx_mock.get(f"{BASE}/api/v1/files/bad").mock(
        return_value=httpx.Response(200, stream=ChunkedStream([b"new bytes"]))
    )
    dest = tmp_path / "artifact.bin"
    dest.write_bytes(b"keep me")

    with pytest.raises(AAPError, match="mismatch"):
        client._http.stream_download("/api/v1/files/bad", dest, **metadata)

    assert route.called
    assert dest.read_bytes() == b"keep me"
    assert _temporary_files(tmp_path) == []


@pytest.mark.parametrize(
    ("expected_sha256", "expected_size"),
    [("not-a-digest", None), (None, -1), (None, True)],
)
def test_invalid_integrity_metadata_raises_sdk_error_before_http(
    client, respx_mock, tmp_path, expected_sha256, expected_size
):
    with pytest.raises(AAPError):
        client._http.stream_download(
            "/api/v1/files/artifact",
            tmp_path / "artifact.bin",
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )
    assert not respx_mock.calls


def test_http_error_preserves_destination_and_cleans_temp(client, respx_mock, tmp_path):
    respx_mock.get(f"{BASE}/api/v1/files/missing").mock(
        return_value=httpx.Response(404, json={"detail": "missing artifact"})
    )
    dest = tmp_path / "artifact.bin"
    dest.write_bytes(b"keep me")

    with pytest.raises(NotFoundError) as caught:
        client._http.stream_download("/api/v1/files/missing", dest)

    assert caught.value.detail == "missing artifact"
    assert dest.read_bytes() == b"keep me"
    assert _temporary_files(tmp_path) == []


def test_interrupted_stream_preserves_destination_and_cleans_temp(
    client, respx_mock, tmp_path
):
    respx_mock.get(f"{BASE}/api/v1/files/interrupted").mock(
        return_value=httpx.Response(200, stream=InterruptedStream())
    )
    dest = tmp_path / "artifact.bin"
    dest.write_bytes(b"keep me")

    with pytest.raises(OSError, match="interrupted"):
        client._http.stream_download("/api/v1/files/interrupted", dest)

    assert dest.read_bytes() == b"keep me"
    assert _temporary_files(tmp_path) == []
