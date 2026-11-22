"""ML Backend 实时统计 WS 消费器：URL、帧与重连策略。"""

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from yoao.models import MLBackendStatsSnapshot
from yoao.tui.ml_stats_ws import MlStatsStream, build_ws_url, parse_frame


def test_build_ws_url_http_to_ws():
    url = build_ws_url("http://localhost:8000", "ak_abc")
    assert url == "ws://localhost:8000/ws/ml-backend-stats?token=ak_abc"


def test_build_ws_url_https_to_wss_and_strip_slash():
    url = build_ws_url("https://api.example.com/", "ak_x/y+z")
    assert url.startswith("wss://api.example.com/ws/ml-backend-stats?token=")
    assert "ak_x%2Fy%2Bz" in url


def test_parse_frame_stats():
    frame = json.dumps(
        {
            "backends": [
                {
                    "backend_id": "11111111-1111-1111-1111-111111111111",
                    "backend_name": "sam",
                    "state": "connected",
                    "gpu_info": {
                        "gpu_utilization_percent": 73,
                        "memory_used_mb": 100,
                        "memory_total_mb": 200,
                    },
                    "cache": {"hit_rate": 0.9},
                    "loaded": True,
                    "idle_unload_seconds": 42.0,
                    "pool": {"size": 1, "capacity": 4},
                    "timestamp": "2026-06-11T00:00:00Z",
                }
            ],
            "timestamp": "2026-06-11T00:00:00Z",
        }
    )
    snaps = parse_frame(frame)
    assert snaps is not None and len(snaps) == 1
    s = snaps[0]
    assert isinstance(s, MLBackendStatsSnapshot)
    assert s.backend_name == "sam"
    assert s.gpu_info.gpu_utilization_percent == 73
    assert s.loaded is True
    assert s.pool == {"size": 1, "capacity": 4}


def test_parse_frame_ping_and_garbage():
    assert parse_frame(json.dumps({"type": "ping"})) is None
    assert parse_frame("not json") is None
    assert parse_frame(json.dumps([1, 2, 3])) is None


def test_parse_frame_skips_bad_snapshot():
    frame = json.dumps(
        {
            "backends": [
                {
                    "backend_id": "11111111-1111-1111-1111-111111111111",
                    "state": "connected",
                },
                {"backend_name": "broken"},
            ]
        }
    )
    snaps = parse_frame(frame)
    assert snaps is not None and len(snaps) == 1


class _Closed(Exception):
    def __init__(self, code):
        self.code = code


class _Socket:
    def __init__(self, messages):
        self._messages = iter(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._messages)
        except StopIteration:
            raise _Closed(1008) from None


class _Connection:
    def __init__(self, socket=None, error=None):
        self._socket = socket
        self._error = error

    async def __aenter__(self):
        if self._error:
            raise self._error
        return self._socket

    async def __aexit__(self, *_args):
        return False


@pytest.mark.asyncio
async def test_stream_dispatches_frame_and_stops_after_auth_close(monkeypatch):
    frame = json.dumps(
        {
            "backends": [
                {
                    "backend_id": "11111111-1111-1111-1111-111111111111",
                    "backend_name": "sam",
                    "state": "connected",
                },
                {"backend_name": "bad"},
            ]
        }
    )
    calls = 0
    received = []
    errors = []

    def connect(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _Connection(_Socket([frame]))

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    stream = MlStatsStream("http://test", "ak_x", received.append, errors.append)
    await stream.run()

    assert calls == 1
    assert len(received) == 1 and len(received[0]) == 1
    assert "鉴权失败" in errors[-1]


@pytest.mark.asyncio
async def test_stream_auth_failure_does_not_reconnect(monkeypatch):
    calls = 0

    def connect(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _Connection(error=_Closed(1008))

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    errors = []
    await MlStatsStream("http://test", "ak_x", lambda _snaps: None, errors.append).run()
    assert calls == 1
    assert "鉴权失败" in errors[-1]


@pytest.mark.asyncio
async def test_stream_transient_error_uses_exponential_backoff(monkeypatch):
    calls = 0
    delays = []

    def connect(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _Connection(error=OSError("offline"))

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await MlStatsStream("http://test", "ak_x", lambda _snaps: None).run()
    assert calls == 2
    assert delays == [1.0, 2.0]
