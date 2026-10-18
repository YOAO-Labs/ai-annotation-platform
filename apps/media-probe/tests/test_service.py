import asyncio
import hashlib
import json
import shutil
import subprocess
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from aap_media_probe import app as app_module
from aap_media_probe.app import create_app

TOKEN = "local-media-probe-test-token-000000000000"


@pytest.fixture
def app():
    return create_app(token=TOKEN, max_file_bytes=1024 * 1024)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def auth_headers():
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/octet-stream",
    }


def test_health_and_openapi_are_independent_and_schema_is_explicit(client):
    assert client.get("/health").json() == {"status": "ok"}
    schema = client.get("/openapi.json").json()
    assert schema["components"]["schemas"]["VideoMetadata"]["properties"]["duration_ms"]
    frame_response = schema["paths"]["/v1/frames"]["post"]["responses"]["200"]
    assert frame_response["content"]["application/x-ndjson"]["schema"][
        "x-ndjson-record-schemas"
    ] == [
        {"$ref": "#/components/schemas/VideoFrameRecord"},
        {"$ref": "#/components/schemas/StreamComplete"},
    ]
    assert schema["components"]["securitySchemes"]["MediaProbeBearer"] == {
        "type": "http",
        "scheme": "bearer",
    }
    assert "sha256" in schema["components"]["schemas"]["StreamComplete"]["properties"]
    for path, operation_id in (
        ("/v1/metadata", "probeVideoMetadata"),
        ("/v1/frames", "probeVideoFrameTimetable"),
        ("/v1/packets", "probeChunkPacketSamples"),
    ):
        operation = schema["paths"][path]["post"]
        assert operation["operationId"] == operation_id
        assert operation["requestBody"]["content"]["application/octet-stream"][
            "schema"
        ] == {"type": "string", "format": "binary"}
    packet_operation = schema["paths"]["/v1/packets"]["post"]
    assert packet_operation["parameters"] == [
        {
            "name": "start_frame",
            "in": "query",
            "required": True,
            "description": "Non-negative safe integer used as the first presentation-rank offset.",
            "schema": {"type": "integer", "minimum": 0, "maximum": 9007199254740991},
        }
    ]
    ndjson_description = frame_response["content"]["application/x-ndjson"]["schema"][
        "description"
    ]
    assert "LF" in ndjson_description and "including LF" in ndjson_description
    assert "completion line" in ndjson_description
    assert "app.main" not in json.dumps(schema)


def test_authentication_is_scoped_to_probe_routes_and_checked_before_read(
    client, monkeypatch
):
    def must_not_probe(*_args, **_kwargs):
        raise AssertionError("unauthorized requests must not reach ffprobe")

    monkeypatch.setattr(app_module.probe, "probe_video_file", must_not_probe)
    response = client.post(
        "/v1/metadata",
        content=b"sample",
        headers={
            "Content-Type": "application/octet-stream",
            "Authorization": "Bearer wrong",
        },
    )
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_request"
    assert client.get("/health").status_code == 200


def test_upload_content_type_empty_and_configured_size_limit(client):
    wrong_type = client.post(
        "/v1/metadata",
        content=b"x",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )
    assert wrong_type.status_code == 400
    assert wrong_type.json()["code"] == "invalid_request"

    empty = client.post("/v1/metadata", content=b"", headers=auth_headers())
    assert empty.status_code == 400
    assert empty.json()["code"] == "invalid_request"

    with TestClient(create_app(token=TOKEN, max_file_bytes=8)) as small_client:
        oversized = small_client.post(
            "/v1/metadata", content=b"123456789", headers=auth_headers()
        )
        assert oversized.status_code == 413
        assert oversized.json()["code"] == "file_too_large"


@pytest.mark.asyncio
async def test_streaming_upload_size_limit_without_content_length(
    tmp_path, monkeypatch
):
    app = create_app(token=TOKEN, max_file_bytes=8)
    paths = []
    real_mkstemp = app_module.tempfile.mkstemp

    def capture_path(*args, **kwargs):
        fd, path = real_mkstemp(*args, dir=tmp_path, **kwargs)
        paths.append(path)
        return fd, path

    monkeypatch.setattr(app_module.tempfile, "mkstemp", capture_path)

    async def chunks():
        yield b"12345"
        yield b"6789"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/metadata",
            content=chunks(),
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/octet-stream",
            },
        )
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"
    assert paths and all(not Path(path).exists() for path in paths)


def test_metadata_success_and_typed_probe_errors(client, monkeypatch):
    monkeypatch.setattr(
        app_module.probe,
        "probe_video_file",
        lambda _path: {
            "duration_ms": 1000,
            "fps": 2.0,
            "frame_count": 2,
            "width": 16,
            "height": 16,
            "codec": "mpeg4",
        },
    )
    response = client.post("/v1/metadata", content=b"video", headers=auth_headers())
    assert response.status_code == 200
    assert response.json()["frame_count"] == 2

    def fail(_path):
        raise app_module.probe.FFProbeError("secret stderr must not cross the boundary")

    monkeypatch.setattr(app_module.probe, "probe_video_file", fail)
    failed = client.post("/v1/metadata", content=b"video", headers=auth_headers())
    assert failed.status_code == 422
    assert failed.json() == {
        "code": "probe_failed",
        "message": "The uploaded media could not be probed",
    }
    assert "secret stderr" not in failed.text

    def unexpected(_path):
        raise RuntimeError("internal details must not cross the boundary")

    monkeypatch.setattr(app_module.probe, "probe_video_file", unexpected)
    server_error = client.post("/v1/metadata", content=b"video", headers=auth_headers())
    assert server_error.status_code == 500
    assert server_error.json()["code"] == "probe_failed"
    assert "internal details" not in server_error.text


def test_probe_timeout_is_typed(client, monkeypatch):
    def timeout(_path):
        raise subprocess.TimeoutExpired("ffprobe", 30)

    monkeypatch.setattr(app_module.probe, "probe_video_file", timeout)
    response = client.post("/v1/metadata", content=b"video", headers=auth_headers())
    assert response.status_code == 504
    assert response.json()["code"] == "probe_timeout"


def test_frame_and_packet_ndjson_include_typed_data_and_completion(client, monkeypatch):
    monkeypatch.setattr(
        app_module.probe,
        "probe_video_frame_timetable",
        lambda _path: [
            {
                "frame_index": 0,
                "pts_ms": 0,
                "is_keyframe": True,
                "pict_type": "I",
                "byte_offset": 48,
            },
            {
                "frame_index": 1,
                "pts_ms": 33,
                "is_keyframe": False,
                "pict_type": "P",
                "byte_offset": 4096,
            },
        ],
    )
    frames = client.post("/v1/frames", content=b"video", headers=auth_headers())
    frame_bytes = frames.content.splitlines(keepends=True)
    frame_lines = [json.loads(line) for line in frame_bytes]
    assert [line["type"] for line in frame_lines] == ["frame", "frame", "complete"]
    assert [line["data"]["frame_index"] for line in frame_lines[:-1]] == [0, 1]
    assert frame_lines[-1] == {
        "type": "complete",
        "count": 2,
        "sha256": hashlib.sha256(b"".join(frame_bytes[:-1])).hexdigest(),
    }
    assert frames.headers["content-type"].startswith("application/x-ndjson")

    monkeypatch.setattr(
        app_module.probe,
        "probe_chunk_samples",
        lambda _path, start: [
            {
                "pts_ms": 0,
                "duration_ms": 33,
                "is_keyframe": True,
                "size_bytes": 50,
                "offset_in_chunk": 0,
                "frame_index": start,
            },
            {
                "pts_ms": 66,
                "duration_ms": 33,
                "is_keyframe": False,
                "size_bytes": 50,
                "offset_in_chunk": 50,
                "frame_index": start + 2,
            },
            {
                "pts_ms": 33,
                "duration_ms": 33,
                "is_keyframe": False,
                "size_bytes": 50,
                "offset_in_chunk": 100,
                "frame_index": start + 1,
            },
        ],
    )
    packets = client.post(
        "/v1/packets?start_frame=9", content=b"video", headers=auth_headers()
    )
    packet_bytes = packets.content.splitlines(keepends=True)
    packet_lines = [json.loads(line) for line in packet_bytes]
    assert [line["data"]["frame_index"] for line in packet_lines[:-1]] == [9, 11, 10]
    assert packet_lines[-1] == {
        "type": "complete",
        "count": 3,
        "sha256": hashlib.sha256(b"".join(packet_bytes[:-1])).hexdigest(),
    }


def test_empty_packet_stream_has_sha256_of_empty_data(client, monkeypatch):
    monkeypatch.setattr(
        app_module.probe, "probe_chunk_samples", lambda _path, _start: []
    )
    response = client.post(
        "/v1/packets?start_frame=9", content=b"video", headers=auth_headers()
    )
    lines = response.content.splitlines(keepends=True)
    assert [json.loads(line) for line in lines] == [
        {"type": "complete", "count": 0, "sha256": hashlib.sha256(b"").hexdigest()}
    ]


@pytest.mark.asyncio
async def test_large_result_validation_does_not_block_health(app, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(
        app_module.probe, "probe_video_frame_timetable", lambda _path: []
    )
    original_validate = app_module._validate_stream

    def slow_validate(*args, **kwargs):
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release frame validation")
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(app_module, "_validate_stream", slow_validate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        pending = asyncio.create_task(
            client.post("/v1/frames", content=b"video", headers=auth_headers())
        )
        assert await asyncio.to_thread(entered.wait, 2)
        health = await client.get("/health")
        release.set()
        response = await pending
    assert health.status_code == 200
    assert response.status_code == 200


def test_invalid_packet_query_and_invalid_stream_rows_fail_before_stream(
    client, monkeypatch
):
    invalid_query = client.post(
        "/v1/packets?start_frame=-1", content=b"video", headers=auth_headers()
    )
    assert invalid_query.status_code == 400
    assert invalid_query.json()["code"] == "invalid_request"

    monkeypatch.setattr(
        app_module.probe,
        "probe_video_frame_timetable",
        lambda _path: [
            {
                "frame_index": 1,
                "pts_ms": 0,
                "is_keyframe": False,
                "pict_type": None,
                "byte_offset": None,
            }
        ],
    )
    response = client.post("/v1/frames", content=b"video", headers=auth_headers())
    assert response.status_code == 422
    assert response.json()["code"] == "probe_failed"


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe not installed",
)
def test_http_service_handles_real_media(client, tmp_path):
    video = tmp_path / "tiny.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=16x16:r=2:d=1",
            "-c:v",
            "mpeg4",
            str(video),
        ],
        check=True,
        timeout=30,
    )
    body = video.read_bytes()
    metadata = client.post("/v1/metadata", content=body, headers=auth_headers())
    assert metadata.status_code == 200
    assert metadata.json()["width"] == 16

    frames = client.post("/v1/frames", content=body, headers=auth_headers())
    frame_bytes = frames.content.splitlines(keepends=True)
    records = [json.loads(line) for line in frame_bytes]
    assert records[-1] == {
        "type": "complete",
        "count": 2,
        "sha256": hashlib.sha256(b"".join(frame_bytes[:-1])).hexdigest(),
    }
    assert [record["data"]["frame_index"] for record in records[:-1]] == [0, 1]


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe not installed")
def test_invalid_truncated_media_returns_typed_failure(client):
    response = client.post(
        "/v1/metadata", content=b"partial media bytes", headers=auth_headers()
    )
    assert response.status_code == 422
    assert response.json()["code"] == "probe_failed"


def test_create_app_requires_strong_token_and_positive_limit(monkeypatch):
    with pytest.raises(ValueError, match="MEDIA_PROBE_TOKEN"):
        create_app(token="too-short")
    with pytest.raises(ValueError, match="MEDIA_PROBE_MAX_FILE_BYTES"):
        create_app(token=TOKEN, max_file_bytes=0)
