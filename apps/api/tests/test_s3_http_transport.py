"""S3 response framing survives early final responses on reused HTTP connections."""

from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError


@contextmanager
def _s3_peer(first_status):
    received = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def _respond(self, body):
            received.append((self.path, body, self.client_address))
            status = first_status if not body else 200
            response = (
                b"<Error><Code>AccessDenied</Code><Message>Denied</Message></Error>"
                if status == 403
                else b""
            )
            self.send_response(status)
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Content-Type", "application/xml")
            self.send_header("ETag", '"payload-etag"' if body else '"empty-etag"')
            self.end_headers()
            self.wfile.write(response)
            self.wfile.flush()

        def handle_expect_100(self):
            if int(self.headers.get("Content-Length", "0")) == 0:
                self._respond(b"")
                return False
            return super().handle_expect_100()

        def do_PUT(self):
            length = int(self.headers.get("Content-Length", "0"))
            self._respond(self.rfile.read(length))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.mark.parametrize(
    "first_status", [200, 403], ids=["empty-success", "early-access-denied"]
)
def test_empty_put_preserves_response_framing_on_reused_connection(first_status):
    payload = bytes(range(188))
    with _s3_peer(first_status) as (endpoint, received):
        with closing(
            boto3.client(
                "s3",
                endpoint_url=endpoint,
                region_name="us-east-1",
                aws_access_key_id="local-test",
                aws_secret_access_key="local-test",
                config=Config(
                    signature_version=UNSIGNED,
                    connect_timeout=2,
                    read_timeout=2,
                    retries={"max_attempts": 0},
                    proxies={},
                    s3={"addressing_style": "path"},
                ),
            )
        ) as client:
            if first_status == 403:
                with pytest.raises(ClientError) as failure:
                    client.put_object(Bucket="fixture", Key="empty-marker", Body=b"")
                assert failure.value.response["Error"]["Code"] == "AccessDenied"
                assert (
                    failure.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403
                )
            else:
                empty = client.put_object(
                    Bucket="fixture", Key="empty-marker", Body=b""
                )
                assert empty["ETag"] == '"empty-etag"'
                assert empty["ResponseMetadata"]["HTTPStatusCode"] == 200

            uploaded = client.put_object(Bucket="fixture", Key="payload", Body=payload)
            assert uploaded["ETag"] == '"payload-etag"'
            metadata = uploaded["ResponseMetadata"]
            assert metadata["HTTPStatusCode"] == 200
            assert metadata["HTTPHeaders"]["content-length"] == "0"
            assert metadata["RetryAttempts"] == 0

    assert [(path, body) for path, body, _peer in received] == [
        ("/fixture/empty-marker", b""),
        ("/fixture/payload", payload),
    ]
    assert received[0][2] == received[1][2]
