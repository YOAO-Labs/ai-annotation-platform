from contextlib import asynccontextmanager
import uuid

import httpx
import pytest

from aap_labeling.api import create_app
from aap_labeling.class_queries import materialize_labeling_class_query_data
from aap_labeling.class_query_contract import (
    LabelingClassQueryUnavailable,
    decode_labeling_class_query_reply,
    labeling_class_query_request,
)
from aap_labeling.config import RuntimeSettings, secret_source


class Data:
    def __init__(self, attributes=None, *, unavailable=False):
        self.calls = []
        self.attributes = (
            attributes
            if attributes is not None
            else ['{"color":"red","雪":1}', "null", '["ignored"]', '{"color":"blue"}']
        )
        self.unavailable = unavailable

    async def class_query_policy(self):
        self.calls.append("policy")
        if self.unavailable:
            raise LabelingClassQueryUnavailable()

    @asynccontextmanager
    async def transaction(self):
        self.calls.append("begin")
        if self.unavailable:
            raise LabelingClassQueryUnavailable()
        try:
            yield self
        finally:
            self.calls.append("end")

    async def class_rows(self, project_id):
        self.calls.append(("class", project_id))
        return [("car", 2), ("", 1)]

    async def attribute_rows(self, project_id):
        self.calls.append(("attributes", project_id))
        return self.attributes


def client(data):
    app = create_app(RuntimeSettings(control_token="test-control"), data=data)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://owner",
    )


def headers():
    return {"Authorization": "Bearer test-control"}


@pytest.mark.asyncio
async def test_private_owner_returns_bound_maps_after_two_reads():
    data = Data()
    request = labeling_class_query_request(uuid.uuid4())
    async with client(data) as http:
        response = await http.post(
            "/v1/labeling/class-queries/execute",
            headers=headers(),
            json=request.model_dump(mode="json", by_alias=True),
        )
    assert response.status_code == 200
    reply = decode_labeling_class_query_reply(request, response.content)
    assert materialize_labeling_class_query_data(reply) == {
        "classes": {"car": 2},
        "attributes": {"color": 2, "雪": 1},
    }
    assert data.calls == [
        "begin",
        ("class", request.project_id),
        ("attributes", request.project_id),
        "end",
    ]


@pytest.mark.asyncio
async def test_private_owner_rejects_unadmitted_or_invalid_body_before_data():
    data = Data()
    async with client(data) as http:
        response = await http.post("/v1/labeling/class-queries/execute", content=b"{")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        response = await http.post(
            "/v1/labeling/class-queries/execute", headers=headers(), json={}
        )
        assert response.status_code == 422
        response = await http.post(
            "/v1/labeling/class-queries/execute",
            headers=headers(),
            content=b"x" * 16385,
        )
        assert response.status_code == 413
    assert data.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_late_decode_failure_keeps_plain_500_and_unavailable_keeps_503(
    unavailable,
):
    data = Data(["9" * 5000], unavailable=unavailable)
    request = labeling_class_query_request(uuid.uuid4())
    async with client(data) as http:
        response = await http.post(
            "/v1/labeling/class-queries/execute",
            headers=headers(),
            json=request.model_dump(mode="json", by_alias=True),
        )
    assert response.status_code == (503 if unavailable else 500)
    if not unavailable:
        assert response.text == "Internal Server Error"
        assert data.calls == [
            "begin",
            ("class", request.project_id),
            ("attributes", request.project_id),
            "end",
        ]


@pytest.mark.asyncio
async def test_readiness_uses_restricted_data_policy():
    data = Data(unavailable=True)
    async with client(data) as http:
        assert (await http.get("/health")).status_code == 200
        assert (await http.get("/ready")).status_code == 503
    assert data.calls == ["policy"]


def test_secret_sources_are_explicit_and_preserve_non_newline_characters(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("credential with space \n")
    assert secret_source("", str(secret)) == "credential with space "
    with pytest.raises(ValueError):
        secret_source("direct", str(secret))
    with pytest.raises(ValueError):
        secret_source("", str(tmp_path / "absent"))
    empty = tmp_path / "empty"
    empty.write_text("\n")
    with pytest.raises(ValueError, match="empty"):
        secret_source("", str(empty))
    settings = RuntimeSettings(
        database_url="private-dsn", control_token="private-control"
    )
    assert "private-dsn" not in repr(settings)
    assert "private-control" not in repr(settings)
