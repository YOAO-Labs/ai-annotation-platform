from dataclasses import replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import ssl
import subprocess
import threading
from urllib import error

import pytest

from aap_site_agent_contract import canonical_json

from storage_agent.client import Bandwidth, Client, RetryableError
from storage_agent.config import AgentError
from storage_agent.journal import Journal
from storage_agent.runner import Runner

from test_agent import acknowledgement


class Response:
    status = 200

    def __init__(self, value):
        self.data = canonical_json(value)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit):
        return self.data[:limit]


class Opener:
    def __init__(self, callback):
        self.callback = callback
        self.calls = []

    def open(self, req, timeout):
        self.calls.append(req)
        return self.callback(req)


@pytest.fixture
def tls_center(tmp_path):
    """A real local TLS transport peer, not the unimplemented platform API."""
    key, cert = tmp_path / "server.key", tmp_path / "server.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    state = {
        "result_posts": [],
        "chunks": {},
        "drop_first_result": True,
        "redirect": False,
        "paths": [],
        "authorization": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            size = int(self.headers["Content-Length"])
            body = self.rfile.read(size)
            state["paths"].append(self.path)
            state["authorization"].append(self.headers["Authorization"])
            if state["redirect"]:
                self.send_response(302)
                self.send_header("Location", "/credential-sink")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path.endswith("/chunks"):
                command_id = self.path.split("/")[-2]
                offset = int(self.headers["X-Chunk-Offset"])
                key = (command_id, offset)
                if key in state["chunks"]:
                    assert state["chunks"][key] == body
                state["chunks"][key] = body
                value = {
                    "command_id": command_id,
                    "command_digest": self.headers["X-Command-Digest"],
                    "offset": offset + size,
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
            elif self.path.endswith("/result"):
                result = json.loads(body)
                state["result_posts"].append(result)
                if state["drop_first_result"]:
                    state["drop_first_result"] = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                value = acknowledgement(result)
            else:
                value = {"protocol_version": 1, "command": None}
            payload = canonical_json(value)
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}", str(cert), state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_actual_tls_lost_ack_then_restart_replays_same_stage(
    agent, command, tls_center
):
    config, credentials, journal, runner, source = agent
    origin, ca, peer = tls_center
    config = replace(config, center_url=origin, ca_file=ca)
    runner.config = config
    result = runner.execute(command)
    with pytest.raises(RetryableError, match="center_unreachable"):
        Client(config, credentials).submit(runner, result)
    assert journal.get(command["command_id"])["state"] == "completed"
    journal.close()
    (source / "image.bin").write_bytes(b"changed source after receipt")
    with Journal(config.state_dir) as restarted_journal:
        restarted = Runner(config, credentials, restarted_journal)
        Client(config, credentials).run(restarted, once=True)
        assert restarted_journal.get(command["command_id"])["state"] == "applied"
        assert not list(restarted_journal.stages.iterdir())
    assert peer["result_posts"] == [result, result]
    assert list(peer["chunks"].values()) == [b"source pixels"]
    assert set(peer["authorization"]) == {"Bearer " + credentials.token}


def test_actual_tls_certificate_is_required_and_redirect_not_followed(
    agent, tls_center
):
    config, credentials, _, _, _ = agent
    origin, ca, peer = tls_center
    with pytest.raises(RetryableError, match="center_unreachable"):
        Client(replace(config, center_url=origin), credentials).poll()
    assert peer["paths"] == []
    peer["redirect"] = True
    with pytest.raises(AgentError, match="center_request_rejected"):
        Client(replace(config, center_url=origin, ca_file=ca), credentials).poll()
    assert peer["paths"] == ["/api/v1/site-agents/commands/poll"]


@pytest.mark.parametrize(
    "status,retryable,code",
    [
        (401, False, "agent_unauthorized"),
        (403, False, "agent_revoked"),
        (409, False, "agent_fence_rejected"),
        (426, False, "agent_protocol_incompatible"),
        (404, False, "center_request_rejected"),
        (429, True, "center_temporarily_unavailable"),
        (503, True, "center_temporarily_unavailable"),
    ],
)
def test_http_failures_never_look_like_ack(agent, status, retryable, code):
    config, credentials, _, _, _ = agent

    def fail(req):
        raise error.HTTPError(req.full_url, status, "untrusted body", {}, None)

    with pytest.raises(RetryableError if retryable else AgentError, match=code):
        Client(config, credentials, opener=Opener(fail)).poll()


def test_wrong_chunk_ack_keeps_local_stage(agent, command):
    config, credentials, journal, runner, _ = agent
    result = runner.execute(command)
    opener = Opener(
        lambda req: Response(
            {
                "command_id": command["command_id"],
                "command_digest": result["command_digest"],
                "offset": True,
                "sha256": "0" * 64,
            }
        )
    )
    with pytest.raises(AgentError, match="chunk_ack_mismatch"):
        Client(config, credentials, opener=opener).submit(runner, result)
    assert journal.get(command["command_id"])["state"] == "completed"
    assert len(opener.calls) == 1


def test_expired_command_never_replays_source_bytes(agent, command):
    config, credentials, journal, runner, _ = agent
    result = runner.execute(command)
    opener = Opener(lambda req: Response(acknowledgement(result, "rejected")))
    Client(
        config, credentials, opener=opener, clock=lambda: command["expires_at"]
    ).submit(runner, result)
    assert len(opener.calls) == 1 and opener.calls[0].full_url.endswith("/result")
    assert journal.get(command["command_id"])["state"] == "rejected"


def test_bandwidth_paces_aggregate_bytes_after_one_bounded_burst():
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    budget = Bandwidth(1024, clock=lambda: now[0], sleep=sleep)
    budget.consume(1024)
    budget.consume(512)
    budget.consume(512)
    assert sleeps == [1.0, 0.5]


def test_transient_retry_is_bounded_and_retains_result(agent, command):
    config, credentials, journal, runner, _ = agent
    result = runner.execute(command)
    sleeps = []

    def fail(req):
        raise error.URLError("test disconnect")

    opener = Opener(fail)
    with pytest.raises(RetryableError):
        Client(config, credentials, opener=opener, sleep=sleeps.append).run(runner)
    assert len(opener.calls) == 5
    assert sleeps == [2, 4, 8, 16]
    assert journal.get(command["command_id"])["result"] == result


def test_drain_never_polls_new_work(agent):
    config, credentials, _, runner, _ = agent
    opener = Opener(lambda req: pytest.fail("drain must not poll"))
    Client(config, credentials, opener=opener).run(runner, drain=True)
    assert not opener.calls


def test_poll_reports_handles_and_limits_without_absolute_roots(agent, command):
    config, credentials, _, _, source = agent
    opener = Opener(lambda req: Response({"protocol_version": 1, "command": command}))
    normalized = Client(config, credentials, opener=opener).poll()
    assert normalized["command_id"] == command["command_id"]
    payload = json.loads(opener.calls[0].data)
    assert payload["roots"] == [{"root_handle": "images", "root_revision": 1}]
    assert str(source).encode() not in opener.calls[0].data
    assert payload["limits"]["max_concurrency"] == 1


@pytest.mark.parametrize("ack_state", ["received", "rejected", "applied"])
def test_unsettled_source_close_stops_further_work_after_reporting(
    agent, command, monkeypatch, ack_state
):
    config, credentials, journal, runner, _ = agent
    from aap_filesystem_read import FilesystemStorageError

    def fail(*_args, **_kwargs):
        exc = FilesystemStorageError("source_close_failed")
        exc.storage_operation_unsettled = True
        raise exc

    monkeypatch.setattr("storage_agent.runner.stage_content", fail)
    result = runner.execute(command)
    assert not result["closed"]
    opener = Opener(lambda req: Response(acknowledgement(result, ack_state)))
    with pytest.raises(
        AgentError,
        match="ack_close_mismatch"
        if ack_state == "applied"
        else "source_close_unconfirmed",
    ):
        Client(config, credentials, opener=opener).run(runner)
    assert len(opener.calls) == 1 and opener.calls[0].full_url.endswith("/result")
    row = journal.get(command["command_id"])
    assert row["state"] == ("completed" if ack_state == "applied" else "unsettled")
    assert row["result"] == result
    assert row in journal.pending()


def test_actual_tls_expiry_during_handshake_sends_no_headers_or_bytes(
    agent, command, tls_center, monkeypatch
):
    import http.client

    config, credentials, journal, runner, _ = agent
    origin, ca, peer = tls_center
    result = runner.execute(command)
    now = [command["issued_at"]]
    original = http.client.HTTPSConnection.connect

    def delayed_tls(connection):
        original(connection)
        now[0] = command["expires_at"]

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", delayed_tls)
    client = Client(
        replace(config, center_url=origin, ca_file=ca),
        credentials,
        clock=lambda: now[0],
    )
    with pytest.raises(AgentError, match="command_expired"):
        client.submit(runner, result)
    assert peer["paths"] == []
    assert journal.get(command["command_id"])["state"] == "completed"
