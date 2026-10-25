"""Real SigV4 requests through distinct origins and explicit proxy contracts."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading
from urllib.parse import parse_qs, urlsplit, urlunsplit
import uuid

from botocore.config import Config
import httpx
import pytest

from app.config import settings
from app.services.storage import StorageService

pytestmark = pytest.mark.skipif(
    os.environ.get("AAP_WORKTREE_MODE") != "test",
    reason="Real signing acceptance uses the owned worktree test buckets",
)


@contextmanager
def proxy(endpoint, *, preserve_host):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def forward(self):
            if not self.path.startswith("/files/"):
                self.send_error(404)
                return
            headers = {"Host": self.headers["Host"]} if preserve_host else {}
            if self.headers.get("Content-Type"):
                headers["Content-Type"] = self.headers["Content-Type"]
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with httpx.Client(trust_env=False) as client:
                result = client.request(
                    self.command,
                    endpoint + self.path.removeprefix("/files"),
                    headers=headers,
                    content=body,
                )
            self.send_response(result.status_code)
            self.send_header("Content-Length", str(len(result.content)))
            self.end_headers()
            self.wfile.write(result.content)

        do_GET = do_PUT = forward

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.fixture
def original(monkeypatch):
    assert os.environ.get("AAP_WORKTREE_MODE") == "test"
    service = StorageService(
        client_config=Config(signature_version="s3v4", s3={"addressing_style": "path"})
    )
    bucket = service.import_bucket
    assert bucket.startswith("aap-wt-")
    service.ensure_bucket(bucket)
    service.client.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    key = f"endpoint-tests/{uuid.uuid4()}/目录 +%#.bin"
    body = b"verified-original\x00\xff"
    version = service.client.put_object(Bucket=bucket, Key=key, Body=body)["VersionId"]
    upload_key = key + ".uploaded"
    monkeypatch.setattr(settings, "minio_public_url", "")
    try:
        yield service, bucket, key, version, upload_key, body
    finally:
        for current_key in [key, upload_key]:
            versions = service.client.list_object_versions(
                Bucket=bucket, Prefix=current_key
            )
            for item in versions.get("Versions", []) + versions.get(
                "DeleteMarkers", []
            ):
                if item["Key"] == current_key:
                    service.client.delete_object(
                        Bucket=bucket, Key=current_key, VersionId=item["VersionId"]
                    )
        service.client.close()


def test_real_get_and_put_sign_actual_origin_not_post_sign_host_replacement(
    original, monkeypatch
):
    service, bucket, key, version, upload_key, body = original
    endpoint = urlsplit(service.client.meta.endpoint_url)
    alternate = "localhost" if endpoint.hostname == "127.0.0.1" else "127.0.0.1"
    host = f"{alternate}:{endpoint.port}"
    assert host != endpoint.netloc
    monkeypatch.setattr(settings, "ml_backend_storage_host", host)
    internal = service.generate_download_url(
        key, bucket=bucket, align=False, version_id=version, audience="internal"
    )
    ml = service.generate_download_url(
        key, bucket=bucket, align=False, version_id=version, audience="ml"
    )
    assert parse_qs(urlsplit(ml).query)["versionId"] == [version]
    assert urlsplit(ml).netloc == host
    wrong = urlunsplit(urlsplit(internal)._replace(netloc=host))
    with httpx.Client(trust_env=False) as client:
        assert client.get(internal).content == body
        result = client.get(ml)
        assert result.status_code == 200 and result.content == body
        denied = client.get(wrong)
        assert denied.status_code == 403 and "SignatureDoesNotMatch" in denied.text
        with monkeypatch.context() as expired_clock:
            instant = datetime.now(timezone.utc) - timedelta(minutes=2)
            expired_clock.setattr(
                "botocore.auth.get_current_datetime", lambda **_: instant
            )
            expired = service.generate_download_url(
                key,
                bucket=bucket,
                align=False,
                expires_in=30,
                version_id=version,
                audience="internal",
            )
        denied = client.get(expired)
        assert denied.status_code == 403 and "Request has expired" in denied.text
        monkeypatch.setattr(settings, "minio_public_url", f"{endpoint.scheme}://{host}")
        public = service.generate_download_url(
            key, bucket=bucket, align=False, version_id=version
        )
        assert client.get(public).content == body
        upload = service.generate_upload_url(
            upload_key, "application/octet-stream", bucket=bucket
        )
        assert (
            client.put(
                upload,
                content=body,
                headers={"Content-Type": "application/octet-stream"},
            ).status_code
            == 200
        )
        uploaded = service.client.get_object(Bucket=bucket, Key=upload_key)["Body"]
        try:
            assert uploaded.read() == body
        finally:
            uploaded.close()


@pytest.mark.parametrize("preserve_host", [False, True])
def test_real_proxy_prefix_has_explicit_upstream_host_contract(
    original, monkeypatch, preserve_host
):
    service, bucket, key, version, _, body = original
    with proxy(service.client.meta.endpoint_url, preserve_host=preserve_host) as origin:
        public = origin + "/files" if preserve_host else "/files"
        monkeypatch.setattr(settings, "minio_public_url", public)
        signed = service.generate_download_url(
            key, bucket=bucket, align=False, version_id=version
        )
        request = signed if preserve_host else origin + signed
        with httpx.Client(trust_env=False) as client:
            result = client.get(request)
        assert result.status_code == 200 and result.content == body
