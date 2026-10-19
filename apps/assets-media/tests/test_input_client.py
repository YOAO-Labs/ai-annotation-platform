import httpx
import pytest
from fastapi import FastAPI
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from aap_assets_inputs import contract as c
from aap_assets_inputs.client import InputClient
from aap_assets_inputs_runtime.api import install_input_routes
from input_fixtures import BODY, input_reference, input_request, input_work

TOKEN = "input-owner-test-token-" + "x" * 32


@pytest.mark.asyncio
async def test_authenticated_http_round_trip_and_crossed_scope():
    work = input_work()
    control = SimpleNamespace(record=AsyncMock(return_value=work.input))
    app = install_input_routes(FastAPI(), control, TOKEN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        client = InputClient("http://owner.test", TOKEN, client=http)
        assert await client.record(input_request(work)) == work.input
        request = input_request(work).model_copy(update={"input_id": uuid4()})
        with pytest.raises(c.InputFailure, match="invalid_response"):
            await client.record(request)


@pytest.mark.asyncio
async def test_http_never_exposes_private_capabilities():
    app = install_input_routes(FastAPI(), object(), TOKEN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://owner.test"
    ) as http:
        for operation in (
            "read_accepted_image_input_work",
            "claim_accepted_image_input",
            "lock_accepted_image_input",
        ):
            result = await http.post(
                "/v1/inputs/" + operation,
                headers={"Authorization": "Bearer " + TOKEN},
                json={},
            )
            assert result.status_code == 403
        result = await http.post("/v1/inputs/record", json={})
        assert result.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [BODY, b"corrupt", BODY[:-1], BODY + b"trailing"])
async def test_download_proves_digest_and_never_forwards_control_credentials(
    tmp_path, body
):
    work = input_work()
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(body))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(serve),
        headers={"Authorization": "Bearer borrowed-secret", "Cookie": "private=value"},
    ) as http:
        client = InputClient("http://owner.test", TOKEN, client=http)
        target = tmp_path / "source"
        if body == BODY:
            await client.download(input_reference(work), target)
            assert target.read_bytes() == BODY
        else:
            with pytest.raises(c.InputFailure, match="input_corrupt"):
                await client.download(input_reference(work), target)
            assert not target.exists()
        assert requests[0].url.params["versionId"] == work.blob.version_id
        assert "authorization" not in requests[0].headers
        assert "cookie" not in requests[0].headers


@pytest.mark.asyncio
async def test_missing_accepted_version_does_not_fetch_latest(tmp_path):
    requests = []

    def missing(request):
        requests.append(request)
        return httpx.Response(404, stream=httpx.ByteStream(b""))

    async with httpx.AsyncClient(transport=httpx.MockTransport(missing)) as http:
        client = InputClient("http://owner.test", TOKEN, client=http)
        with pytest.raises(c.InputFailure, match="input_corrupt"):
            await client.download(input_reference(input_work()), tmp_path / "source")
    assert len(requests) == 1
