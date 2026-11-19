import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import ssl
import subprocess
import threading
import time
from uuid import uuid4

import pytest

from yoao_filesystem_read import RootSpec, capture_anchor
from yoao_site_agent_contract import canonical_json

from storage_agent.config import Config, Credentials
from storage_agent.journal import Journal
from storage_agent.runner import Runner

from .support import acknowledgement


@pytest.fixture
def agent(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "image.bin").write_bytes(b"source pixels")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    config = Config(
        "https://center.example",
        state,
        tmp_path / "credentials.json",
        {"images": RootSpec("images", str(source))},
    )
    credentials = Credentials(str(uuid4()), 1, "s" * 40, int(time.time()) + 3600)
    journal = Journal(state)
    with journal:
        yield config, credentials, journal, Runner(config, credentials, journal), source


@pytest.fixture
def command(agent):
    config, credentials, _, _, _ = agent
    now = int(time.time())
    return {
        "protocol_version": 1,
        "command_id": str(uuid4()),
        "agent_id": credentials.agent_id,
        "credential_revision": 1,
        "root_handle": "images",
        "root_revision": 1,
        "operation": "read",
        "issued_at": now,
        "expires_at": now + 60,
        "max_bytes": 1024 * 1024,
        "timeout_seconds": 10,
        "arguments": {
            "relative_path": "",
            "expected_anchor": capture_anchor(config.roots["images"]),
            "path": "image.bin",
        },
        "fence": {
            "location_id": str(uuid4()),
            "location_revision": 1,
            "maintenance_revision": 1,
            "purpose": "reference_read",
        },
    }


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
        "sessions": [],
        "pair_requests": [],
        "pair_response": None,
        "pair_status": 200,
        "drop_pair_response": False,
        "poll_command": None,
        "ack_state": "applied",
        "chunk_posts": [],
        "response_gate": None,
        "response_waiting": threading.Event(),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                self.post()
            except (BrokenPipeError, ConnectionResetError, ssl.SSLEOFError):
                if state["response_gate"] is None:
                    raise

        def post(self):
            size = int(self.headers["Content-Length"])
            body = self.rfile.read(size)
            state["paths"].append(self.path)
            state["authorization"].append(self.headers["Authorization"])
            state["sessions"].append(self.headers["X-Agent-Session"])
            if state["response_gate"] is not None:
                state["response_waiting"].set()
                state["response_gate"].wait(timeout=5)
            if state["redirect"]:
                self.send_response(302)
                self.send_header("Location", "/credential-sink")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 200
            if self.path == "/api/v1/site-agents/pair":
                state["pair_requests"].append(json.loads(body))
                if state["drop_pair_response"]:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                value = state["pair_response"]
                status = state["pair_status"]
            elif self.path.endswith("/chunks"):
                state["chunk_posts"].append(body)
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
                received = b"".join(
                    chunk
                    for (command_id, _), chunk in sorted(state["chunks"].items())
                    if command_id == result["command_id"]
                )
                if (
                    state["ack_state"] != "rejected"
                    and result["status"] == "completed"
                    and "sha256" in result["result"]
                    and (
                        len(received) != result["result"]["size_bytes"]
                        or hashlib.sha256(received).hexdigest()
                        != result["result"]["sha256"]
                    )
                ):
                    payload = canonical_json(
                        {"detail": {"code": "agent_content_incomplete"}}
                    )
                    self.send_response(409)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if state["drop_first_result"]:
                    state["drop_first_result"] = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                value = acknowledgement(result, state["ack_state"])
            else:
                value = {"protocol_version": 1, "command": state["poll_command"]}
            payload = canonical_json(value)
            self.send_response(status)
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
        if state["response_gate"] is not None:
            state["response_gate"].set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
