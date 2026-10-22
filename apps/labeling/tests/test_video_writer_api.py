from __future__ import annotations

from datetime import datetime, timezone
import uuid

import httpx
import pytest

from aap_assets_mask_content.codec import build_rle_reference
from aap_labeling.video_writer_api import (
    AssetsMaskContentVerifier,
    create_video_writer_app,
)
from aap_labeling.video_writer_config import VideoWriterSettings
from aap_labeling.video_writer_contract import (
    LABELING_VIDEO_WRITER_PROTOCOL,
    MAX_VIDEO_WRITER_REQUEST_BYTES,
    VIDEO_WRITER_FUNCTION,
    VideoWriterPolicyData,
    VideoWriterReceipt,
    VideoWriterRejected,
    VideoWriterUnavailable,
    make_video_writer_command,
    video_writer_command_digest,
    video_writer_operation_id,
)
from aap_preannotation_results.video_execution import ExecutionProof


def command():
    proof = ExecutionProof(
        job_id=uuid.UUID(int=11),
        run_id=uuid.UUID(int=12),
        worker_id=uuid.UUID(int=13),
        generation=2,
        lease_token=uuid.UUID(int=14),
    )
    return make_video_writer_command(
        proof,
        outcome="failed",
        expected_input=None,
        frame_results=[],
        error_message="inference failed",
    )


def receipt_for(value):
    return VideoWriterReceipt(
        operation_id=video_writer_operation_id(value.execution.run_id),
        job_id=value.execution.job_id,
        run_id=value.execution.run_id,
        request_digest=video_writer_command_digest(value),
        outcome="failed",
        status="failed",
        completed_at=datetime.now(timezone.utc),
        staged_sha256=None,
        staged_byte_size=0,
    )


class Data:
    def __init__(self, *, unavailable=False):
        self.unavailable = unavailable
        self.calls = 0

    async def policy(self):
        self.calls += 1
        if self.unavailable:
            raise VideoWriterUnavailable()
        return VideoWriterPolicyData(
            functions=(VIDEO_WRITER_FUNCTION,),
            max_request_bytes=MAX_VIDEO_WRITER_REQUEST_BYTES,
            max_staged_bytes=67_108_864,
        )


class Manager:
    def __init__(self):
        self.calls = []

    async def execute(self, value):
        self.calls.append(("execute", value))
        return receipt_for(value)

    async def publication(self, execution):
        self.calls.append(("publication", execution))
        return None

    async def status(self, operation_id, execution, request_digest):
        self.calls.append(("status", operation_id, execution, request_digest))
        return None


def make_http(data=None, manager=None):
    settings = VideoWriterSettings(control_token="v" * 40)
    app = create_video_writer_app(
        settings,
        data=data or Data(),
        manager=manager or Manager(),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://writer"
    )


@pytest.mark.asyncio
async def test_private_api_requires_auth_and_bounds_wire_before_manager_call(
    monkeypatch,
):
    manager = Manager()
    async with make_http(manager=manager) as http:
        unauthorized = await http.post(
            "/v1/labeling/video-writer/execute", content=b"not-json"
        )
        assert unauthorized.status_code == 401
        assert unauthorized.headers["www-authenticate"] == "Bearer"
        assert unauthorized.json() == {
            "schema": LABELING_VIDEO_WRITER_PROTOCOL,
            "error": {"code": "unauthorized"},
        }

        monkeypatch.setattr(
            "aap_labeling.video_writer_api.MAX_VIDEO_WRITER_REQUEST_BYTES", 16
        )
        too_large = await http.post(
            "/v1/labeling/video-writer/execute",
            headers={"Authorization": "Bearer " + "v" * 40},
            content=b"x" * 17,
        )
    assert too_large.status_code == 413
    assert manager.calls == []


@pytest.mark.asyncio
async def test_execute_status_and_readiness_use_closed_owner_replies():
    data = Data()
    manager = Manager()
    command_value = command()
    async with make_http(data=data, manager=manager) as http:
        headers = {"Authorization": "Bearer " + "v" * 40}
        result = await http.post(
            "/v1/labeling/video-writer/execute",
            headers=headers,
            json=command_value.model_dump(mode="json", by_alias=True),
        )
        status = await http.post(
            "/v1/labeling/video-writer/status",
            headers=headers,
            json={
                "schema": LABELING_VIDEO_WRITER_PROTOCOL,
                "operation_id": str(command_value.operation_id),
                "execution": command_value.execution.model_dump(
                    mode="json", by_alias=True
                ),
                "request_digest": video_writer_command_digest(command_value),
            },
        )
        ready = await http.get("/ready")

    assert result.status_code == 200
    assert result.json()["receipt"]["request_digest"] == video_writer_command_digest(
        command_value
    )
    assert status.status_code == 200
    assert status.json() == {
        "schema": LABELING_VIDEO_WRITER_PROTOCOL,
        "receipt": None,
    }
    assert ready.status_code == 200
    assert data.calls == 1
    assert [call[0] for call in manager.calls] == ["execute", "status"]


@pytest.mark.asyncio
async def test_readiness_requires_the_owner_policy():
    async with make_http(data=Data(unavailable=True)) as http:
        response = await http.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


@pytest.mark.asyncio
async def test_mask_configuration_is_lazy_and_mask_references_fail_closed():
    settings = VideoWriterSettings(
        control_token="v" * 40,
        port=8180,
        assets_mask_content_url="",
        assets_mask_content_token=None,
        assets_mask_content_token_file="",
    )
    assert settings.port == 8180
    verifier = AssetsMaskContentVerifier(settings)
    await verifier({"results": []})

    reference = build_rle_reference(
        {"encoding": "coco_rle", "size": [1, 1], "counts": [1]}
    )
    staged = {
        "results": [
            {
                "geometry": {
                    "type": "raster_mask",
                    "mask": reference.model_dump(mode="json", by_alias=True),
                }
            }
        ]
    }
    with pytest.raises(VideoWriterUnavailable):
        await verifier(staged)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_code", "failure_type"),
    [
        ("object_missing", VideoWriterRejected),
        ("object_corrupt", VideoWriterRejected),
        ("storage_unavailable", VideoWriterUnavailable),
    ],
)
async def test_mask_owner_failures_are_classified_without_provider_details(
    monkeypatch, failure_code, failure_type
):
    from aap_assets_mask_content.contract import MaskContentFailure
    import aap_assets_mask_content.client

    class Client:
        def __init__(self, origin, token, *, timeout_seconds):
            assert origin == "http://assets-mask-owner"
            assert token == "m" * 40

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

        def get(self, request):
            raise MaskContentFailure(failure_code)

    monkeypatch.setattr(aap_assets_mask_content.client, "MaskContentClient", Client)
    settings = VideoWriterSettings(
        control_token="v" * 40,
        assets_mask_content_url="http://assets-mask-owner",
        assets_mask_content_token="m" * 40,
    )
    reference = build_rle_reference(
        {"encoding": "coco_rle", "size": [1, 1], "counts": [1]}
    )
    staged = {
        "results": [
            {"geometry": {"mask": reference.model_dump(mode="json", by_alias=True)}}
        ]
    }

    with pytest.raises(failure_type) as captured:
        await AssetsMaskContentVerifier(settings)(staged)
    if failure_type is VideoWriterRejected:
        assert captured.value.code == "mask_content_invalid"


@pytest.mark.asyncio
async def test_status_supports_closed_current_proof_publication_lookup():
    value = command()
    manager = Manager()
    async with make_http(manager=manager) as client:
        response = await client.post(
            "/v1/labeling/video-writer/status",
            headers={"Authorization": "Bearer " + "v" * 40},
            json={
                "schema": LABELING_VIDEO_WRITER_PROTOCOL,
                "execution": value.execution.model_dump(mode="json"),
            },
        )
    assert response.status_code == 200
    assert response.json()["receipt"] is None
    assert manager.calls == [("publication", value.execution)]
