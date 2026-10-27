"""Signed limits govern socket bodies and survive consecutive requests."""

from dataclasses import replace
import hashlib
from io import BytesIO
import json
from urllib import error

import pytest

from aap_site_agent_contract import canonical_json

from storage_agent.client import Bandwidth, Client, DeadlineConnection
from storage_agent.config import AgentError
from storage_agent.runner import Runner

from test_agent import acknowledgement
from test_client import Opener, Response


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Socket:
    def __init__(self, clock):
        self.clock = clock
        self.sent = []

    def settimeout(self, _timeout):
        pass

    def sendall(self, data):
        self.sent.append((self.clock(), data))


def connection(clock, budget, *, deadline=None):
    conn = DeadlineConnection(
        "center.example",
        timeout=25,
        command_deadline=deadline,
        clock=clock,
        bandwidth=budget,
    )
    conn.sock = Socket(clock)
    return conn


@pytest.mark.parametrize("metadata", [False, True])
def test_socket_body_is_bounded_and_paced_after_one_second_burst(metadata):
    clock = Clock()
    budget = Bandwidth(1024, clock=clock, sleep=clock.sleep)
    body = canonical_json({"manifest": "x" * 3072}) if metadata else b"x" * 3072
    conn = connection(clock, budget)
    conn.request("POST", "/result" if metadata else "/chunks", body=body)
    headers, *parts = conn.sock.sent
    assert headers[1].startswith(b"POST ") and headers[0] == 0
    assert b"".join(part for _, part in parts) == body
    assert max(len(part) for _, part in parts) <= 1024
    assert [at for at, _ in parts] == list(range(len(parts)))


def test_rate_change_and_new_connection_preserve_aggregate_debt():
    clock = Clock()
    budget = Bandwidth(1024, clock=clock, sleep=clock.sleep)
    first = connection(clock, budget)
    first.request("POST", "/result", body=b"x" * 1024)
    budget.rate = 2048
    second = connection(clock, budget)
    second.request("POST", "/chunks", body=b"x" * 1024)
    third = connection(clock, budget)
    third.request("POST", "/result", body=b"x" * 512)
    assert [conn.sock.sent[1][0] for conn in (first, second, third)] == [0, 1, 1.5]
    assert clock.sleeps == [1.0, 0.5]


def test_socket_never_sends_next_source_segment_after_pacing_crosses_expiry():
    clock = Clock()
    budget = Bandwidth(1024, clock=clock, sleep=clock.sleep)
    conn = connection(clock, budget, deadline=0.75)
    with pytest.raises(AgentError, match="command_expired"):
        conn.request("POST", "/chunks", body=b"x" * 2048)
    assert len(conn.sock.sent) == 2
    assert conn.sock.sent[1] == (0, b"x" * 1024)
    assert clock.now == 1


def test_closure_metadata_keeps_pacing_without_a_source_grant_deadline():
    clock = Clock()
    budget = Bandwidth(1024, clock=clock, sleep=clock.sleep)
    conn = connection(clock, budget)
    conn.request("POST", "/result", body=b"x" * 2048)
    assert [at for at, _ in conn.sock.sent[1:]] == [0, 1]


@pytest.mark.parametrize("local,central", [(4096, 1024), (1024, 4096)])
def test_client_caps_both_result_metadata_and_image_requests(
    agent, command, local, central
):
    config, credentials, _, runner, source = agent
    config = replace(config, bandwidth_bytes_per_second=local)
    command["bandwidth_bytes_per_second"] = central
    (source / "image.bin").write_bytes(b"x" * 2500)
    result = runner.execute(command)
    chunks = []

    def reply(req):
        if req.full_url.endswith("/chunks"):
            chunks.append(req.data)
            return Response(
                {
                    "command_id": command["command_id"],
                    "command_digest": result["command_digest"],
                    "offset": sum(map(len, chunks)),
                    "sha256": hashlib.sha256(req.data).hexdigest(),
                }
            )
        assert json.loads(req.data) == result
        if not chunks:
            raise error.HTTPError(
                req.full_url,
                409,
                "incomplete",
                {},
                BytesIO(
                    canonical_json(
                        {
                            "detail": {"code": "agent_content_incomplete"},
                        }
                    )
                ),
            )
        return Response(acknowledgement(result, "applied"))

    opener = Opener(reply)
    client = Client(config, credentials, opener=opener)
    client.submit(runner, result)
    assert list(map(len, chunks)) == [1024, 1024, 452]
    for req in opener.calls:
        assert req.agent_bandwidth is client.bandwidth
        assert req.agent_bandwidth.rate == 1024
        assert int(req.get_header("Content-length")) == len(req.data)
        assert isinstance(req.data, bytes)


def test_expired_replay_closes_without_source_access_even_with_removed_root(
    agent, command, monkeypatch
):
    config, credentials, journal, _, _ = agent
    config = replace(config, roots={})
    for operation in (
        "stage_content",
        "scan_directory",
        "probe_directory",
        "browse_directory",
    ):
        monkeypatch.setattr(
            "storage_agent.runner." + operation,
            lambda *_args, **_kwargs: pytest.fail("expired replay touched source"),
        )
    runner = Runner(config, credentials, journal, clock=lambda: command["expires_at"])
    result = runner.execute(command)
    assert result["status"] == "failed"
    assert result["error_code"] == "command_expired"
    assert result["closed"] is True
    assert result["result"] == {}
    assert runner.execute(command) == result
    assert not list(journal.stages.iterdir())
