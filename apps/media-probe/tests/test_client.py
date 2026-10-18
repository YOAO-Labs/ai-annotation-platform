from __future__ import annotations

import hashlib
import json
import subprocess

import httpx
import pytest

from aap_media_probe.client import MediaProbeClient, MediaProbeError

_TOKEN = "t" * 40
_METADATA = {
    "duration_ms": 2002,
    "fps": 29.97,
    "frame_count": 60,
    "width": 1280,
    "height": 720,
    "codec": "h264",
}
_FRAMES = [
    {
        "frame_index": index,
        "pts_ms": index * 33,
        "is_keyframe": index == 0,
        "pict_type": "I" if index == 0 else "P",
        "byte_offset": 48 + index * 100,
    }
    for index in range(2)
]
_PACKETS = [
    {
        "frame_index": index,
        "pts_ms": pts,
        "duration_ms": 33,
        "is_keyframe": index == 10,
        "size_bytes": 100,
        "offset_in_chunk": offset,
    }
    for index, pts, offset in ((10, 0, 48), (12, 67, 148), (11, 33, 248))
]


class _SplitStream(httpx.SyncByteStream):
    def __init__(self, body: bytes):
        self.body = body

    def __iter__(self):
        for offset in range(0, len(self.body), 7):
            yield self.body[offset : offset + 7]


def _ndjson(rows, kind="frame", *, count=None):
    records = [{"type": kind, "data": row} for row in rows]
    lines = b"".join(json.dumps(row).encode() + b"\n" for row in records)
    footer = {
        "type": "complete",
        "count": len(rows) if count is None else count,
        "sha256": hashlib.sha256(lines).hexdigest(),
    }
    return lines + json.dumps(footer).encode() + b"\n"


def _client(handler):
    return MediaProbeClient("http://probe.local", _TOKEN, timeout_seconds=12.5), handler


def _install_transport(monkeypatch, handler):
    original_client = httpx.Client

    def make_client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"] == 12.5
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", make_client)


def test_metadata_request_uses_bearer_and_streams_exact_file(tmp_path, monkeypatch):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"owned video bytes")

    def handler(request):
        assert request.url.path == "/v1/metadata"
        assert not request.url.query
        assert request.headers["Authorization"] == f"Bearer {_TOKEN}"
        assert request.headers["Content-Type"] == "application/octet-stream"
        assert int(request.headers["Content-Length"]) == source.stat().st_size
        assert request.read() == source.read_bytes()
        return httpx.Response(200, json=_METADATA)

    client, _ = _client(handler)
    _install_transport(monkeypatch, handler)

    assert client.probe_video_file(source) == _METADATA
    assert _TOKEN not in repr(client)


def test_frame_ndjson_requires_complete_count_digest_and_order(tmp_path, monkeypatch):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"video")
    bodies = iter(
        (
            _ndjson(_FRAMES),
            _ndjson(_FRAMES, count=3),
            _ndjson([_FRAMES[1]]),
            _ndjson(_FRAMES).rsplit(b'{"type": "complete"', 1)[0],
        )
    )

    def handler(request):
        return httpx.Response(
            200,
            headers={"Content-Type": "application/x-ndjson"},
            stream=_SplitStream(next(bodies)),
        )

    client, _ = _client(handler)
    _install_transport(monkeypatch, handler)

    assert client.probe_video_frame_timetable(source) == _FRAMES
    for _ in range(3):
        with pytest.raises(MediaProbeError) as failure:
            client.probe_video_frame_timetable(source)
        assert failure.value.code == "invalid_response"


def test_packet_rank_query_and_decode_order_are_preserved(tmp_path, monkeypatch):
    source = tmp_path / "chunk.mp4"
    source.write_bytes(b"chunk bytes")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/x-ndjson"},
            stream=_SplitStream(_ndjson(_PACKETS, "packet")),
        )

    client, _ = _client(handler)
    _install_transport(monkeypatch, handler)

    assert client.probe_chunk_samples(source, 10) == _PACKETS
    assert seen[0].url.params["start_frame"] == "10"


def test_remote_timeout_preserves_ffprobe_timeout_exception(tmp_path, monkeypatch):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"video")

    def handler(_request):
        return httpx.Response(
            504,
            json={"code": "probe_timeout", "message": "ffprobe timed out"},
        )

    client, _ = _client(handler)
    _install_transport(monkeypatch, handler)

    with pytest.raises(subprocess.TimeoutExpired) as failure:
        client.probe_video_file(source)
    from aap_media_probe.probe import FFPROBE_TIMEOUT_SECONDS

    assert failure.value.timeout == FFPROBE_TIMEOUT_SECONDS


def test_redirect_is_not_followed_and_client_failure_is_not_rewritten(
    tmp_path, monkeypatch
):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"video")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            307,
            headers={"Location": "https://other.invalid/v1/metadata"},
            json={"code": "probe_failed", "message": "selected owner failed"},
        )

    client, _ = _client(handler)
    _install_transport(monkeypatch, handler)

    with pytest.raises(MediaProbeError) as failure:
        client.probe_video_file(source)
    assert failure.value.code == "probe_failed"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "url,token,timeout",
    [
        ("file:///tmp/probe", _TOKEN, 10),
        ("http://user:secret@probe.local", _TOKEN, 10),
        ("http://probe.local?token=secret", _TOKEN, 10),
        ("http://probe.local", "short", 10),
        ("http://probe.local", _TOKEN, 0),
    ],
)
def test_invalid_client_configuration_does_not_expose_credentials(url, token, timeout):
    with pytest.raises(MediaProbeError) as failure:
        MediaProbeClient(url, token, timeout)
    assert failure.value.code == "invalid_configuration"
    assert "secret" not in str(failure.value)
    assert token not in repr(failure.value)
