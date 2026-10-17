import httpx
import pytest

from ai_annotation.errors import (
    APIStatusError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)

from .conftest import API


@pytest.mark.parametrize(
    ("status", "exc_cls"),
    [
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (409, ConflictError),
        (422, ValidationError),
        (500, APIStatusError),
    ],
)
def test_status_error_mapping(client, respx_mock, status, exc_cls):
    respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(status, json={"detail": "nope"})
    )
    with pytest.raises(exc_cls) as ei:
        client.projects.list()
    assert ei.value.status_code == status
    assert ei.value.detail == "nope"


def test_detail_dict_preserved(client, respx_mock):
    detail = {"msg": "文件已存在（内容重复）", "duplicate_of": "abc"}
    respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(409, json={"detail": detail})
    )
    with pytest.raises(ConflictError) as ei:
        client.projects.list()
    assert ei.value.detail == detail


def test_request_id_and_retry_after_are_preserved(client, respx_mock):
    respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(
            503,
            json={"detail": "busy"},
            headers={"X-Request-ID": "req-123", "Retry-After": "3"},
        )
    )
    client._http.retry_backoff = 0

    with pytest.raises(APIStatusError) as error:
        client.projects.list()

    assert error.value.request_id == "req-123"
    assert error.value.retry_after == "3"


def test_non_json_body_falls_back_to_text(client, respx_mock):
    respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(502, text="Bad Gateway")
    )
    # 502 是可重试状态, 3 次后仍失败 → APIStatusError
    client._http.retry_backoff = 0
    with pytest.raises(APIStatusError) as ei:
        client.projects.list()
    assert ei.value.status_code == 502
    assert ei.value.detail == "Bad Gateway"


def test_get_retries_on_503(client, respx_mock):
    client._http.retry_backoff = 0
    route = respx_mock.get(f"{API}/projects").mock(
        side_effect=[
            httpx.Response(503, json={"detail": "busy"}),
            httpx.Response(200, json=[]),
        ]
    )
    assert client.projects.list() == []
    assert route.call_count == 2


def test_post_does_not_retry(client, respx_mock):
    client._http.retry_backoff = 0
    route = respx_mock.post(f"{API}/projects").mock(
        return_value=httpx.Response(503, json={"detail": "busy"})
    )
    with pytest.raises(APIStatusError):
        client.projects.create(name="x", type_key="object_detection")
    assert route.call_count == 1
