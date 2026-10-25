import asyncio
import json
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from ai_annotation import AAPError, AuthenticationError, StreamError, describe_operation
from ai_annotation.streams import describe_stream_channel, list_stream_channels

ID = "11111111-1111-4111-8111-111111111111"
NOW = "2026-10-17T00:00:00Z"


class Socket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            value = next(self.messages)
        except StopIteration:
            raise StopAsyncIteration from None
        if isinstance(value, Exception):
            raise value
        return value

    async def close(self):
        self.closed = True


CASES = [
    (
        "notifications",
        [],
        "jwt-secret/+?&",
        "/ws/notifications",
        {
            "id": ID,
            "type": "new_type",
            "target_type": "task",
            "target_id": ID,
            "payload": {},
            "created_at": NOW,
        },
    ),
    (
        "preannotation",
        [ID],
        "ak_secret/+?&",
        f"/ws/projects/{ID}/preannotate",
        {
            "current": 1,
            "total": 3,
            "status": "running",
            "pipeline_stages": [{"stage": 0, "future": True}],
        },
    ),
    (
        "batch_events",
        [ID],
        "ak_secret/+?&",
        f"/ws/batches/project/{ID}",
        {
            "type": "batch.status_changed",
            "batch_id": ID,
            "from": "draft",
            "to": "annotating",
            "at": NOW,
        },
    ),
    (
        "prediction_jobs",
        [],
        "jwt-secret/+?&",
        "/ws/prediction-jobs",
        {"current": 1, "total": 3, "status": "running", "project_id": ID, "job_id": ID},
    ),
    (
        "video_tracker",
        [ID],
        "jwt-secret/+?&",
        f"/ws/video-tracker-jobs/{ID}",
        {
            "type": "frame_result",
            "job_id": ID,
            "task_id": ID,
            "at": NOW,
            "frame_index": 3,
            "geometry": {"type": "bbox"},
        },
    ),
    (
        "ml_stats",
        [],
        "ak_secret/+?&",
        "/ws/ml-backend-stats",
        {"backends": [{"backend_id": ID, "state": "connected"}], "timestamp": NOW},
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,token,path,payload", CASES)
async def test_named_streams_use_exact_paths_typed_events_and_close(
    client, monkeypatch, name, args, token, path, payload
):
    socket = Socket(
        [json.dumps({"type": "ping"}), json.dumps({**payload, "future_field": True})]
    )
    calls = []

    async def connect(url, **kwargs):
        calls.append((url, kwargs))
        return socket

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    client._http._client.headers["Authorization"] = "Bearer " + token
    async with getattr(client.streams, name)(*args) as events:
        received = [event async for event in events]
    assert len(calls) == 1 and socket.closed
    parsed = urlsplit(calls[0][0])
    assert parsed.path == path and parsed.scheme == "ws"
    assert parse_qs(parsed.query) == {"token": [token]}
    assert "+?&" not in calls[0][0]
    assert not calls[0][1]["logger"].propagate
    assert received[0].model_dump(mode="json", by_alias=True)["future_field"] is True
    assert describe_stream_channel(name)["path"] == path.replace(
        ID, "{job_id}" if name == "video_tracker" else "{project_id}"
    )


@pytest.mark.asyncio
async def test_notification_stream_preserves_native_payloads_and_continues_after_sync(
    client, monkeypatch
):
    first_payload = ["legacy", None, 2**100, {"label": "雪"}]
    third_payload = None
    socket = Socket(
        [
            json.dumps(
                {
                    "id": ID,
                    "type": "legacy.notification",
                    "target_type": "task",
                    "target_id": ID,
                    "payload": first_payload,
                    "created_at": NOW,
                }
            ),
            json.dumps({"type": "notifications.sync", "reason": "read"}),
            json.dumps(
                {
                    "id": ID,
                    "type": "legacy.notification",
                    "target_type": "task",
                    "target_id": ID,
                    "payload": third_payload,
                    "created_at": NOW,
                }
            ),
        ]
    )

    async def connect(*_args, **_kwargs):
        return socket

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    client._http._client.headers["Authorization"] = "Bearer valid-jwt"

    async with client.streams.notifications() as events:
        received = [event async for event in events]

    assert len(received) == 3
    assert received[0].payload == first_payload
    assert received[1].type == "notifications.sync"
    assert received[2].payload is None
    assert socket.closed


def test_offline_channels_and_method_descriptions_are_separate_from_rest():
    rows = list_stream_channels()
    assert len(rows) == 6
    assert describe_stream_channel("preannotation")["heartbeat_seconds"] is None
    for row in rows:
        description = describe_operation(row["sdk_method"])
        assert description["channel"]["name"] == row["name"]
        assert description["api_operations"] == []
        assert description["channel"]["event_schema"]
    rows[0]["reconcile"].clear()
    assert list_stream_channels()[0]["reconcile"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["notifications", "prediction_jobs", "video_tracker"])
async def test_jwt_only_channels_reject_api_key_before_connect(
    client, monkeypatch, name
):
    async def connect(*args, **kwargs):
        raise AssertionError("must reject before connecting")

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    with pytest.raises(AuthenticationError, match="requires a JWT"):
        async with getattr(client.streams, name)(
            *([ID] if name == "video_tracker" else [])
        ):
            pass


@pytest.mark.asyncio
async def test_missing_optional_dependency_has_safe_install_hint(client, monkeypatch):
    monkeypatch.setitem(sys.modules, "websockets", None)
    with pytest.raises(AAPError, match=r"ai-annotation-sdk\[streams\]"):
        async with client.streams.ml_stats():
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "secret not JSON",
        "[]",
        '{"backends":[],"timestamp":"secret"}',
        '{"backends":[],"timestamp":"2026-10-17T00:00:00Z","secret":NaN}',
        '{"backends":[],"timestamp":"2026-10-17T00:00:00Z","secret":1e1000}',
    ],
)
async def test_invalid_events_do_not_expose_input_and_close(client, monkeypatch, raw):
    socket = Socket([raw])

    async def connect(*args, **kwargs):
        return socket

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    with pytest.raises(StreamError) as caught:
        async with client.streams.ml_stats() as events:
            await anext(events)
    assert caught.value.code == "stream_invalid_event"
    assert "secret" not in str(caught.value)
    assert socket.closed


@pytest.mark.asyncio
async def test_consumer_exception_is_preserved_while_socket_closes(client, monkeypatch):
    socket = Socket([])

    async def connect(*args, **kwargs):
        return socket

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    with pytest.raises(ValueError, match="consumer error"):
        async with client.streams.ml_stats():
            raise ValueError("consumer error")
    assert socket.closed


@pytest.mark.asyncio
async def test_connection_error_exposes_status_without_token(client, monkeypatch):
    class Failure(Exception):
        status_code = 403

    async def connect(*args, **kwargs):
        raise Failure("token=secret")

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    with pytest.raises(StreamError) as caught:
        async with client.streams.ml_stats():
            pass
    assert caught.value.status_code == 403
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_disconnect_keeps_close_code_without_reconnecting(client, monkeypatch):
    class Closed(Exception):
        rcvd = SimpleNamespace(code=1008)

    socket = Socket([Closed("token=secret")])
    calls = []

    async def connect(*args, **kwargs):
        calls.append(args)
        return socket

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    with pytest.raises(StreamError) as caught:
        async with client.streams.ml_stats() as events:
            await anext(events)
    assert (
        caught.value.close_code == 1008 and caught.value.code == "stream_disconnected"
    )
    assert "secret" not in str(caught.value)
    assert len(calls) == 1 and socket.closed


@pytest.mark.asyncio
async def test_client_close_prevents_new_stream_connections(client):
    client.close()
    with pytest.raises(AAPError, match="Client is closed"):
        async with client.streams.ml_stats():
            pass


@pytest.mark.asyncio
async def test_cancellation_releases_socket(client, monkeypatch):
    started = asyncio.Event()

    class WaitingSocket(Socket):
        async def __anext__(self):
            started.set()
            await asyncio.Event().wait()

    socket = WaitingSocket([])

    async def connect(*args, **kwargs):
        return socket

    async def consume():
        async with client.streams.ml_stats() as events:
            await anext(events)

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.closed


@pytest.mark.asyncio
async def test_real_websocket_handshake_heartbeat_and_delivery(client):
    import websockets

    requests = []

    async def handle(connection, *_args):
        if hasattr(connection, "request"):
            path, headers = connection.request.path, connection.request.headers
        else:
            path, headers = connection.path, connection.request_headers
        requests.append((path, headers.get("Authorization")))
        await connection.send(json.dumps({"type": "ping"}))
        await connection.send(json.dumps({"backends": [], "timestamp": NOW}))
        await connection.close()

    async with websockets.serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        from ai_annotation import Client

        with Client(
            base_url=f"http://127.0.0.1:{port}", api_key="ak_+/?secret"
        ) as live:
            async with live.streams.ml_stats() as events:
                received = [event async for event in events]
    assert len(received) == 1 and received[0].backends == []
    path, authorization = requests[0]
    assert urlsplit(path).path == "/ws/ml-backend-stats"
    assert parse_qs(urlsplit(path).query)["token"] == ["ak_+/?secret"]
    assert authorization is None


@pytest.mark.parametrize("annotation_id", [None, "None", ID])
def test_tracker_event_normalizes_legacy_absent_source(annotation_id):
    from uuid import UUID
    from ai_annotation import VideoTrackerEvent

    event = VideoTrackerEvent(
        type="job_started", job_id=ID, task_id=ID, at=NOW, annotation_id=annotation_id
    )
    assert event.annotation_id == (UUID(ID) if annotation_id == ID else None)
    assert event.model_dump(mode="json")["annotation_id"] == (
        ID if annotation_id == ID else None
    )
