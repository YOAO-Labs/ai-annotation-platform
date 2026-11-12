"""Importer must recover an ambiguous Source reply without reposting work."""

import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from yoao_assets_source.import_verification_contract import ImportVerificationRequest
from yoao_assets_source_import.verification import (
    ExpectedOriginal,
    ImportVerifier,
    VerificationRejected,
    VerificationUnavailable,
)


def _mismatching_receipt(receipt, field):
    wrong = {
        "sha256": "0" * 64,
        "md5": "0" * 32,
        "size_bytes": 999,
        "etag": '"other"',
        "version_id": "other",
    }[field]
    changed = {**receipt, field: wrong}
    if field in {"size_bytes", "etag", "version_id"}:
        observation_field = "size" if field == "size_bytes" else field
        changed["observation"] = {**receipt["observation"], observation_field: wrong}
    return changed


def _expected(receipt):
    return ExpectedOriginal(
        sha256=receipt["sha256"],
        md5=receipt["md5"],
        size_bytes=receipt["size_bytes"],
        etag=receipt["etag"],
        version_id=receipt["version_id"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_interruption", ["unavailable", "headers_lost", "body_lost"]
)
async def test_lost_verification_response_polls_pending_exact_status_without_repost(
    status_interruption,
):
    observed = []
    payload = b"verified bytes"
    receipt = {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "md5": hashlib.md5(payload, usedforsecurity=False).hexdigest(),
        "size_bytes": len(payload),
        "etag": '"saved-etag"',
        "version_id": "saved-version",
        "observation": {
            "etag": '"saved-etag"',
            "version_id": "saved-version",
            "size": len(payload),
        },
        "physical_settled": True,
        "current_head_matches": True,
        "width": None,
        "height": None,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            observed.append((self.path, body, self.headers.get("Authorization")))
            if self.path == "/v1/import/verify":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            status_count = sum(row[0] == "/v1/import/verify-status" for row in observed)
            if status_count == 1 and status_interruption == "headers_lost":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if status_count == 1 and status_interruption == "body_lost":
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b'{"state":')
                self.wfile.flush()
                self.close_connection = True
                return
            if status_count == 1:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            response = (
                {"state": "pending"}
                if status_count < 4
                else {"state": "completed", "result": receipt}
            )
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        token = "v" * 40
        verifier = ImportVerifier(
            SimpleNamespace(
                verify_url=f"http://127.0.0.1:{server.server_port}",
                verify_token=token,
                s3_read_timeout_seconds=2,
            )
        )
        command = ImportVerificationRequest(
            job_id=uuid4(),
            lease_token=uuid4(),
            entry_index=3,
            item_id=uuid4(),
            creation_id=uuid4(),
            operation_id=uuid4(),
            mode="read",
        )
        result = await verifier.verify(
            command,
            expected=_expected(receipt),
            check=lambda: None,
            remaining_lease=lambda: 5,
        )
        assert result.version_id == "saved-version"
        assert [row[0] for row in observed] == [
            "/v1/import/verify",
            "/v1/import/verify-status",
            "/v1/import/verify-status",
            "/v1/import/verify-status",
            "/v1/import/verify-status",
        ]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.asyncio
async def test_pending_verification_stops_at_original_deadline():
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            paths.append(self.path)
            body = b'{"state":"pending"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        verifier = ImportVerifier(
            SimpleNamespace(
                verify_url=f"http://127.0.0.1:{server.server_port}",
                verify_token="v" * 40,
                s3_read_timeout_seconds=0.8,
            )
        )
        command = ImportVerificationRequest(
            job_id=uuid4(),
            lease_token=uuid4(),
            entry_index=0,
            item_id=uuid4(),
            creation_id=uuid4(),
            operation_id=uuid4(),
            mode="write",
        )
        start = time.monotonic()
        with pytest.raises(VerificationUnavailable):
            await verifier.verify(
                command,
                expected=ExpectedOriginal("0" * 64, 0, None, None),
                check=lambda: None,
                remaining_lease=lambda: 5,
            )
        assert time.monotonic() - start < 2
        assert paths[0] == "/v1/import/verify"
        assert paths.count("/v1/import/verify") == 1
        assert "/v1/import/verify-status" in paths
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_delayed_post_closes_connection_before_recovery_or_cancellation(cancel):
    received = threading.Event()
    closed = threading.Event()
    observed = []
    peer_closed = []
    receipt = {
        "sha256": hashlib.sha256(b"").hexdigest(),
        "md5": hashlib.md5(b"", usedforsecurity=False).hexdigest(),
        "size_bytes": 0,
        "etag": '"empty"',
        "version_id": "exact",
        "observation": {"etag": '"empty"', "version_id": "exact", "size": 0},
        "physical_settled": True,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            observed.append(self.path)
            if self.path == "/v1/import/verify":
                received.set()
                self.connection.settimeout(3)
                try:
                    peer_closed.append(self.connection.recv(1) == b"")
                finally:
                    closed.set()
                return
            body = json.dumps({"state": "completed", "result": receipt}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        verifier = ImportVerifier(
            SimpleNamespace(
                verify_url=f"http://127.0.0.1:{server.server_port}",
                verify_token="v" * 40,
                s3_read_timeout_seconds=1.2,
            )
        )
        command = ImportVerificationRequest(
            job_id=uuid4(),
            lease_token=uuid4(),
            entry_index=0,
            item_id=uuid4(),
            creation_id=uuid4(),
            operation_id=uuid4(),
            mode="write",
        )
        attempt = asyncio.create_task(
            verifier.verify(
                command,
                expected=_expected(receipt),
                check=lambda: None,
                remaining_lease=lambda: 5,
            )
        )
        assert await asyncio.to_thread(received.wait, 2)
        if cancel:
            attempt.cancel()
            with pytest.raises(asyncio.CancelledError):
                await attempt
        else:
            assert (await attempt).version_id == "exact"
        assert await asyncio.to_thread(closed.wait, 2)
        assert peer_closed == [True]
        assert observed.count("/v1/import/verify") == 1
        if cancel:
            assert "/v1/import/verify-status" not in observed
        else:
            assert "/v1/import/verify-status" in observed
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "acknowledgement",
    [
        500,
        502,
        504,
        "invalid_json",
        "oversized",
        "truncated",
        "invalid_receipt",
        "wrong_sha256",
        "wrong_md5",
        "wrong_size_bytes",
        "wrong_etag",
        "wrong_version_id",
    ],
)
async def test_committed_verification_recovers_an_untrusted_acknowledgement(
    acknowledgement,
):
    payload = b"committed exact original"
    receipt = {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "md5": hashlib.md5(payload, usedforsecurity=False).hexdigest(),
        "size_bytes": len(payload),
        "etag": '"committed-etag"',
        "version_id": "committed-version",
        "observation": {
            "etag": '"committed-etag"',
            "version_id": "committed-version",
            "size": len(payload),
        },
        "physical_settled": True,
    }
    committed = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            command = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/v1/import/verify":
                committed.append(command)
                if isinstance(acknowledgement, int):
                    self.send_response(acknowledgement)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                response = {"state": "completed", "result": receipt}
                if acknowledgement == "invalid_receipt":
                    response = {
                        "state": "completed",
                        "result": {
                            **receipt,
                            "observation": {
                                **receipt["observation"],
                                "etag": '"other"',
                            },
                        },
                    }
                elif acknowledgement.startswith("wrong_"):
                    response["result"] = _mismatching_receipt(
                        receipt, acknowledgement.removeprefix("wrong_")
                    )
                body = (
                    b"not json"
                    if acknowledgement == "invalid_json"
                    else b"x" * 65_537
                    if acknowledgement == "oversized"
                    else json.dumps(response).encode()
                )
                self.send_response(200)
                self.send_header(
                    "Content-Length",
                    str(len(body) + (10 if acknowledgement == "truncated" else 0)),
                )
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                self.close_connection = True
                return
            if self.path != "/v1/import/verify-status" or committed != [command]:
                self.send_error(409)
                return
            body = json.dumps({"state": "completed", "result": receipt}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        verifier = ImportVerifier(
            SimpleNamespace(
                verify_url=f"http://127.0.0.1:{server.server_port}",
                verify_token="v" * 40,
                s3_read_timeout_seconds=2,
            )
        )
        command = ImportVerificationRequest(
            job_id=uuid4(),
            lease_token=uuid4(),
            entry_index=0,
            item_id=uuid4(),
            creation_id=uuid4(),
            operation_id=uuid4(),
            mode="write",
        )
        result = await verifier.verify(
            command,
            expected=_expected(receipt),
            check=lambda: None,
            remaining_lease=lambda: 5,
        )
        assert {
            key: getattr(result, key)
            for key in ("sha256", "md5", "size_bytes", "etag", "version_id")
        } == {
            key: receipt[key]
            for key in ("sha256", "md5", "size_bytes", "etag", "version_id")
        }
        assert len(committed) == 1
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "failure"),
    [
        *(
            (phase, status)
            for phase in ("verify", "status")
            for status in (400, 401, 403, 422)
        ),
        ("status", 500),
        ("status", "invalid_receipt"),
        ("status", "malformed_http"),
        ("status", "malformed_chunk"),
        *(
            ("status", "wrong_" + field)
            for field in ("sha256", "md5", "size_bytes", "etag", "version_id")
        ),
    ],
)
async def test_definitive_verification_failures_are_not_retried(phase, failure):
    observed = []
    receipt = {
        "sha256": hashlib.sha256(b"data").hexdigest(),
        "md5": hashlib.md5(b"data", usedforsecurity=False).hexdigest(),
        "size_bytes": 4,
        "etag": '"actual"',
        "version_id": "saved",
        "observation": {"etag": '"actual"', "version_id": "saved", "size": 4},
        "physical_settled": True,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            observed.append(self.path)
            if phase == "status" and self.path == "/v1/import/verify":
                status, response = 200, {"state": "pending"}
            elif failure == "invalid_receipt":
                status, response = (
                    200,
                    {
                        "state": "completed",
                        "result": {
                            **receipt,
                            "observation": {
                                **receipt["observation"],
                                "etag": '"different"',
                            },
                        },
                    },
                )
            elif isinstance(failure, str) and failure.startswith("wrong_"):
                status, response = (
                    200,
                    {
                        "state": "completed",
                        "result": _mismatching_receipt(
                            receipt, failure.removeprefix("wrong_")
                        ),
                    },
                )
            elif failure == "malformed_http":
                self.wfile.write(b"INVALID RESPONSE\r\n\r\n")
                self.wfile.flush()
                self.close_connection = True
                return
            elif failure == "malformed_chunk":
                self.wfile.write(
                    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nINVALID\r\n"
                )
                self.wfile.flush()
                self.close_connection = True
                return
            else:
                status, response = failure, {}
            body = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        verifier = ImportVerifier(
            SimpleNamespace(
                verify_url=f"http://127.0.0.1:{server.server_port}",
                verify_token="v" * 40,
                s3_read_timeout_seconds=1.5,
            )
        )
        request = ImportVerificationRequest(
            job_id=uuid4(),
            lease_token=uuid4(),
            entry_index=0,
            item_id=uuid4(),
            creation_id=uuid4(),
            operation_id=uuid4(),
            mode="write",
        )
        with pytest.raises(VerificationRejected) as rejected:
            await verifier.verify(
                request,
                expected=_expected(receipt),
                check=lambda: None,
                remaining_lease=lambda: 5,
            )
        assert rejected.value.code == (
            "storage_unavailable" if failure in {401, 403, 500} else "invalid_response"
        )
        assert observed == (
            ["/v1/import/verify", "/v1/import/verify-status"]
            if phase == "status"
            else ["/v1/import/verify"]
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.asyncio
async def test_untrusted_tls_identity_is_not_retried(tmp_path):
    from datetime import datetime, timedelta, timezone
    import ipaddress
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            False,
        )
        .sign(key, hashes.SHA256())
    )
    key_file = tmp_path / "key.pem"
    cert_file = tmp_path / "cert.pem"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    accepted = []

    class Server(ThreadingHTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            accepted.append(address)
            try:
                return context.wrap_socket(connection, server_side=True), address
            except BaseException:
                connection.close()
                raise

    server = Server(("127.0.0.1", 0), BaseHTTPRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        verifier = ImportVerifier(
            SimpleNamespace(
                verify_url=f"https://127.0.0.1:{server.server_port}",
                verify_token="v" * 40,
                s3_read_timeout_seconds=1.5,
            )
        )
        request = ImportVerificationRequest(
            job_id=uuid4(),
            lease_token=uuid4(),
            entry_index=0,
            item_id=uuid4(),
            creation_id=uuid4(),
            operation_id=uuid4(),
            mode="write",
        )
        with pytest.raises(VerificationRejected) as rejected:
            await verifier.verify(
                request,
                expected=ExpectedOriginal("0" * 64, 0, None, None),
                check=lambda: None,
                remaining_lease=lambda: 5,
            )
        assert rejected.value.code == "storage_unavailable"
        assert len(accepted) == 1
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
