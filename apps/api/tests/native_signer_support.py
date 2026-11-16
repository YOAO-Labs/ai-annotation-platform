"""Run the real Assets Source Native signer for independent-process proofs.

The module-data verifier provisions three operation tokens and two bounded S3
identities (the builtin Native identity and one registered-connection
identity).  This support module starts the actual Source process with the
builtin Native identity, so the Images and cleanup executables receive only
Source-issued signed requests.  It never fabricates a signer or reuses the
worker/cleanup S3 credentials.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]

WRITER_TOKEN_SETTING = "AAP_MODULE_DATA_ASSET_SOURCE_NATIVE_WRITER_TOKEN"
READER_TOKEN_SETTING = "AAP_MODULE_DATA_ASSET_SOURCE_NATIVE_READER_TOKEN"
CLEANUP_TOKEN_SETTING = "AAP_MODULE_DATA_ASSET_SOURCE_NATIVE_CLEANUP_TOKEN"
NATIVE_ACCESS_SETTING = "AAP_MODULE_DATA_ASSET_SOURCE_NATIVE_S3_ACCESS_KEY"
NATIVE_SECRET_SETTING = "AAP_MODULE_DATA_ASSET_SOURCE_NATIVE_S3_SECRET_KEY"
CONNECTION_ACCESS_SETTING = (
    "AAP_MODULE_DATA_ASSET_SOURCE_NATIVE_CONNECTION_S3_ACCESS_KEY"
)
CONNECTION_SECRET_SETTING = (
    "AAP_MODULE_DATA_ASSET_SOURCE_NATIVE_CONNECTION_S3_SECRET_KEY"
)
CONNECTION_BUCKET_SETTING = "AAP_MODULE_DATA_NATIVE_B_BUCKET"
CONNECTION_PREFIX_SETTING = "AAP_MODULE_DATA_NATIVE_B_PREFIX"


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"the module-data verifier did not provide {name}")
    return value


def _docker(*arguments: str, timeout: int = 30) -> str:
    result = subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"owned Docker command failed: {arguments[0]}", pytrace=False)
    return result.stdout.strip()


def bridge_gateway(container_id: str | None = None) -> str:
    """Return the verified host bridge address reachable by host and containers."""
    import json

    target = container_id or required_env("AAP_MODULE_DATA_PG_CONTAINER_ID")
    observed = json.loads(_docker("inspect", target))[0]
    candidates = set()
    for network in (observed.get("NetworkSettings", {}).get("Networks") or {}).values():
        gateway = network.get("Gateway")
        if not gateway:
            continue
        address = ipaddress.ip_address(gateway)
        if address.version == 4 and not (
            address.is_loopback
            or address.is_unspecified
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
        ):
            candidates.add(gateway)
    if len(candidates) != 1:
        pytest.fail("the owned bridge gateway is ambiguous", pytrace=False)
    return candidates.pop()


def free_host_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _process_log(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")[-4000:]


@dataclass
class NativeSigner:
    """A running Source signer bound to one owned bridge address."""

    url: str
    endpoint: str
    writer_token: str = field(repr=False)
    reader_token: str = field(repr=False)
    cleanup_token: str = field(repr=False)
    native_access_key: str = field(repr=False)
    native_secret_key: str = field(repr=False)
    connection_access_key: str = field(repr=False)
    connection_secret_key: str = field(repr=False)
    connection_bucket: str
    connection_prefix: str
    process: subprocess.Popen | None = None
    log_path: Path | None = None
    _stream: object | None = None

    def assert_running(self) -> None:
        if self.process is None or self.process.poll() is not None:
            log = "" if self.log_path is None else _process_log(self.log_path)
            pytest.fail(f"Native Source signer is not running: {log}")

    def close(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGCONT)
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        finally:
            if self._stream is not None:
                self._stream.close()


def start_native_signer(tmp_path: Path) -> NativeSigner:
    """Start the real Source process; never substitute a fake signer."""
    from app.config import settings

    writer = required_env(WRITER_TOKEN_SETTING)
    reader = required_env(READER_TOKEN_SETTING)
    cleanup = required_env(CLEANUP_TOKEN_SETTING)
    tokens = (writer, reader, cleanup)
    if len(set(tokens)) != 3 or any(
        len(token) < 32
        or not token.isascii()
        or any(character.isspace() for character in token)
        for token in tokens
    ):
        pytest.fail("Native signer role tokens are not three distinct secrets")

    endpoint = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    endpoint_origin = urlsplit(endpoint)
    host = bridge_gateway()
    port = free_host_port(host)
    service_token = secrets.token_urlsafe(48)
    import_verify_token = secrets.token_urlsafe(48)
    if not settings.connector_encryption_key:
        pytest.fail("connector encryption is not configured for Native targets")
    source_url = required_env("AAP_MODULE_DATA_ASSET_SOURCE_URL")
    environment = {
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join(
            (
                str(ROOT / "apps/assets-source/src"),
                str(ROOT / "apps/assets-media/src"),
            )
        ),
        "TMPDIR": str(tmp_path),
        "ASSETS_SOURCE_DATABASE_URL": source_url,
        "ASSETS_SOURCE_TOKEN": service_token,
        "ASSETS_SOURCE_IMPORT_VERIFY_TOKEN": import_verify_token,
        "ASSETS_SOURCE_S3_ENDPOINT": endpoint,
        "ASSETS_SOURCE_S3_PUBLIC_ENDPOINT": endpoint,
        "ASSETS_SOURCE_S3_REGION": "us-east-1",
        "ASSETS_SOURCE_S3_ACCESS_KEY": required_env(
            "AAP_MODULE_DATA_ASSET_SOURCE_S3_ACCESS_KEY"
        ),
        "ASSETS_SOURCE_S3_SECRET_KEY": required_env(
            "AAP_MODULE_DATA_ASSET_SOURCE_S3_SECRET_KEY"
        ),
        "ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY": settings.connector_encryption_key,
        "ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST": (
            '["' + endpoint_origin.hostname + '"]'
        ),
        "ASSETS_SOURCE_DATASETS_BUCKET": settings.minio_datasets_bucket,
        "ASSETS_SOURCE_IMPORT_BUCKET": settings.minio_import_bucket,
        "ASSETS_SOURCE_MEDIA_CACHE_BUCKET": settings.minio_media_cache_bucket,
        "ASSETS_SOURCE_HOST": host,
        "ASSETS_SOURCE_PORT": str(port),
        "ASSETS_SOURCE_POOL_MIN_SIZE": "1",
        "ASSETS_SOURCE_POOL_MAX_SIZE": "2",
        "ASSETS_SOURCE_NATIVE_PYRAMID_WRITER_TOKEN": writer,
        "ASSETS_SOURCE_NATIVE_PYRAMID_READER_TOKEN": reader,
        "ASSETS_SOURCE_NATIVE_PYRAMID_CLEANUP_TOKEN": cleanup,
        "ASSETS_SOURCE_NATIVE_S3_ACCESS_KEY": required_env(NATIVE_ACCESS_SETTING),
        "ASSETS_SOURCE_NATIVE_S3_SECRET_KEY": required_env(NATIVE_SECRET_SETTING),
    }
    if environment["ASSETS_SOURCE_NATIVE_S3_ACCESS_KEY"] == environment[
        "ASSETS_SOURCE_S3_ACCESS_KEY"
    ]:
        pytest.fail("the Native identity reuses the normal Source identity")
    for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH"):
        if os.environ.get(name):
            environment[name] = os.environ[name]

    log_path = tmp_path / "native-source-signer.log"
    stream = log_path.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "yoao_assets_source_runtime"],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=stream,
        stderr=subprocess.STDOUT,
        close_fds=True,
        start_new_session=True,
    )
    signer = NativeSigner(
        url=f"http://{host}:{port}",
        endpoint=endpoint,
        writer_token=writer,
        reader_token=reader,
        cleanup_token=cleanup,
        native_access_key=environment["ASSETS_SOURCE_NATIVE_S3_ACCESS_KEY"],
        native_secret_key=environment["ASSETS_SOURCE_NATIVE_S3_SECRET_KEY"],
        connection_access_key=required_env(CONNECTION_ACCESS_SETTING),
        connection_secret_key=required_env(CONNECTION_SECRET_SETTING),
        connection_bucket=required_env(CONNECTION_BUCKET_SETTING),
        connection_prefix=required_env(CONNECTION_PREFIX_SETTING),
        process=process,
        log_path=log_path,
        _stream=stream,
    )
    deadline = time.monotonic() + 30
    with httpx.Client(base_url=signer.url, timeout=2, trust_env=False) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                response = client.get("/ready")
            except httpx.HTTPError:
                pass
            else:
                if response.status_code == 200:
                    return signer
            time.sleep(0.1)
    signer.close()
    pytest.fail(
        "Native Source signer did not become ready: " + _process_log(log_path)
    )
