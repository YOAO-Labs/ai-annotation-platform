import asyncio
import json
import sys
from types import SimpleNamespace

from typer.testing import CliRunner

from ai_annotation.cli.main import app
from .test_streams import NOW, Socket

runner = CliRunner()
ENV = {"AAP_BASE_URL": "http://testserver", "AAP_API_KEY": "ak_test", "NO_COLOR": "1"}


def test_stream_discovery_is_offline_without_optional_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "websockets", None)
    listed = runner.invoke(app, ["streams", "list", "--json"])
    described = runner.invoke(app, ["streams", "describe", "batch-events", "--json"])
    assert listed.exit_code == described.exit_code == 0
    assert len(json.loads(listed.stdout)) == 6
    assert json.loads(described.stdout)["event_schema"]["properties"]["from"]


def test_bounded_listener_emits_exact_event_count_then_closes(monkeypatch):
    socket = Socket(
        [json.dumps({"type": "ping"})]
        + [json.dumps({"backends": [], "timestamp": NOW, "index": i}) for i in range(3)]
    )
    calls = []

    async def connect(*args, **kwargs):
        calls.append(args)
        return socket

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    result = runner.invoke(
        app, ["streams", "ml-stats", "--max-events", "2", "--timeout", "1"], env=ENV
    )
    assert result.exit_code == 0, result.stderr
    assert [json.loads(line)["index"] for line in result.stdout.splitlines()] == [0, 1]
    assert len(calls) == 1 and socket.closed


def test_listener_timeout_closes_without_inventing_completion(monkeypatch):
    class WaitingSocket(Socket):
        async def __anext__(self):
            await asyncio.Event().wait()

    socket = WaitingSocket([])

    async def connect(*args, **kwargs):
        return socket

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    result = runner.invoke(app, ["streams", "ml-stats", "--timeout", "0.1"], env=ENV)
    assert result.exit_code == 0 and result.stdout == ""
    assert socket.closed


def test_listener_disconnect_preserves_emitted_events_and_safe_error(monkeypatch):
    class Closed(Exception):
        rcvd = SimpleNamespace(code=1008)

    socket = Socket(
        [json.dumps({"backends": [], "timestamp": NOW}), Closed("token=secret")]
    )

    async def connect(*args, **kwargs):
        return socket

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    result = runner.invoke(
        app, ["--error-format", "json", "streams", "ml-stats"], env=ENV
    )
    assert result.exit_code == 1
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout)["backends"] == []
    error = json.loads(result.stderr)
    assert error["kind"] == "stream_error" and error["close_code"] == 1008
    assert error["code"] == "stream_disconnected"
    assert "secret" not in result.stderr


def test_listener_missing_extra_reports_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "websockets", None)
    result = runner.invoke(
        app, ["--error-format", "json", "streams", "ml-stats"], env=ENV
    )
    assert result.exit_code == 1
    assert "ai-annotation-sdk[streams]" in json.loads(result.stderr)["detail"]
