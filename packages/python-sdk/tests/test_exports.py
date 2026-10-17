import json
from uuid import uuid4

import httpx
import pytest

from ai_annotation.errors import AAPError, APIStatusError
from ai_annotation.export_models import (
    ExportRequestBody,
    VideoExportFrameSelection,
    VideoExportScopeRequest,
)
from ai_annotation.models import Job

from .conftest import API, BASE

PROJECT_ID = str(uuid4())
JOB_ID = str(uuid4())
TASK_ID = str(uuid4())


def _completed_job(download_url: str) -> dict:
    return {
        "id": JOB_ID,
        "kind": "export",
        "status": "completed",
        "progress_pct": 100,
        "payload": {},
        "result": {"download_url": download_url},
        "error_message": None,
    }


def test_create_export_returns_job_id(client, respx_mock):
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/export").mock(
        return_value=httpx.Response(202, json={"job_id": JOB_ID})
    )
    job_id = client.exports.create(
        PROJECT_ID,
        targets=["coco", "yolo-det"],
        include_attributes=True,
        video_frame_mode="keyframes",
    )
    params = route.calls.last.request.url.params
    assert params.get_list("targets") == ["coco", "yolo-det"]
    assert params["include_attributes"] == "true"
    assert params["video_frame_mode"] == "keyframes"
    assert job_id == JOB_ID


def test_create_export_sends_typed_json_body_separately_from_query(client, respx_mock):
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/export").mock(
        return_value=httpx.Response(202, json={"job_id": JOB_ID})
    )
    body = ExportRequestBody(
        scope=VideoExportScopeRequest(
            task_id=TASK_ID,
            selection=VideoExportFrameSelection(from_frame=1, to_frame=3),
        ),
        lidar=None,
    )

    assert client.exports.create(PROJECT_ID, ["video_json"], body=body) == JOB_ID

    request = route.calls.last.request
    assert request.url.params.get_list("targets") == ["video_json"]
    assert "scope" not in request.url.params and "lidar" not in request.url.params
    assert json.loads(request.content) == {
        "scope": {
            "task_id": TASK_ID,
            "selection": {
                "kind": "frames",
                "from_frame": 1,
                "to_frame": 3,
            },
        },
        "lidar": None,
    }


@pytest.mark.parametrize("field", ["scope", "lidar"])
def test_create_export_rejects_body_fields_as_query_options(client, respx_mock, field):
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/export")

    with pytest.raises(AAPError, match="belong in body"):
        client.exports.create(PROJECT_ID, ["coco"], **{field: None})

    assert not route.called


def test_create_export_rejects_voc_with_download_guidance(client, respx_mock):
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/export")

    with pytest.raises(AAPError, match="exports.download_voc"):
        client.exports.create(PROJECT_ID, ["voc"])

    assert not route.called


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


def test_download_voc_posts_authenticated_query_and_streams_atomically(
    client, respx_mock, tmp_path
):
    route = respx_mock.post(f"{BASE}/api/v1/projects/{PROJECT_ID}/export").mock(
        return_value=httpx.Response(200, stream=ChunkedStream([b"PK\x03", b"\x04zip"]))
    )
    dest = tmp_path / "voc.zip"
    dest.write_bytes(b"old")

    assert (
        client.exports.download_voc(PROJECT_ID, dest, include_attributes=False) == dest
    )

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer ak_test"
    assert request.url.params.get_list("targets") == ["voc"]
    assert request.url.params["include_attributes"] == "false"
    assert dest.read_bytes() == b"PK\x03\x04zip"
    assert not list(tmp_path.glob(".aap-download-*.part"))


@pytest.mark.parametrize("failure", ["http", "interrupt"])
def test_download_voc_failure_preserves_destination_and_does_not_retry_post(
    client, respx_mock, tmp_path, failure
):
    route = respx_mock.post(f"{BASE}/api/v1/projects/{PROJECT_ID}/export").mock(
        return_value=(
            httpx.Response(503, json={"detail": "busy"})
            if failure == "http"
            else httpx.Response(200, stream=InterruptedStream())
        )
    )
    dest = tmp_path / "voc.zip"
    dest.write_bytes(b"keep me")

    if failure == "http":
        with pytest.raises(APIStatusError):
            client.exports.download_voc(PROJECT_ID, dest)
    else:
        with pytest.raises(OSError, match="connection interrupted"):
            client.exports.download_voc(PROJECT_ID, dest)

    assert route.calls.call_count == 1
    assert dest.read_bytes() == b"keep me"
    assert not list(tmp_path.glob(".aap-download-*.part"))


def test_download_presigned_absolute_url_no_auth(client, respx_mock, tmp_path):
    url = "http://minio.local/exports/P-1.zip?sig=abc"
    dl = respx_mock.get(host="minio.local", path="/exports/P-1.zip").mock(
        return_value=httpx.Response(200, content=b"PK\x03\x04zipbytes")
    )
    job = Job.model_validate(_completed_job(url))
    dest = client.exports.download(job, tmp_path / "out.zip")
    assert dest.read_bytes() == b"PK\x03\x04zipbytes"
    # 预签名 URL 不带平台 auth header
    assert "Authorization" not in dl.calls.last.request.headers


def test_download_relative_url_with_auth(client, respx_mock, tmp_path):
    # 传 job_id 时先 GET job, 再按相对路径拼回平台 origin 下载 (带 auth)
    respx_mock.get(f"{API}/async-jobs/{JOB_ID}").mock(
        return_value=httpx.Response(
            200, json=_completed_job("/api/v1/files/export.zip")
        )
    )
    dl = respx_mock.get(f"{BASE}/api/v1/files/export.zip").mock(
        return_value=httpx.Response(200, content=b"zip2")
    )
    dest = client.exports.download(JOB_ID, tmp_path / "out2.zip")
    assert dest.read_bytes() == b"zip2"
    assert dl.calls.last.request.headers["Authorization"] == "Bearer ak_test"


def test_download_without_url_raises(client, tmp_path):
    job = Job.model_validate(
        {"id": JOB_ID, "kind": "export", "status": "running", "result": {}}
    )
    with pytest.raises(AAPError):
        client.exports.download(job, tmp_path / "x.zip")
