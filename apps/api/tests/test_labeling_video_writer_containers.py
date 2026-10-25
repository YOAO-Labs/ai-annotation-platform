"""Restricted-image proof for the independent Labeling video-writer owner."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import pytest

from aap_labeling.video_writer_contract import VideoWriterRejected
from sqlalchemy.engine import make_url

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_MASK_CONTENT_ACCEPTANCE") != "1",
        reason="requires the owned module-data and Mask content environments",
    ),
    pytest.mark.skipif(
        not os.environ.get("AAP_LABELING_VIDEO_WRITER_IMAGE"),
        reason="the verifier did not provide the immutable Labeling owner image",
    ),
]

_IMAGE_TAG = "aap-labeling-video-writer:qualified-10a6a8299"
_SOURCE_REVISION = "10a6a829991555fce58ccb28a0b0ff4b31881dd4"
_OWNER_LABEL = "org.aap.acceptance"
_IDENTITY_LABEL = "org.aap.acceptance-id"
_FORBIDDEN_DISTRIBUTIONS = {
    "aap-api",
    "aap-inference",
    "sqlalchemy",
    "celery",
    "boto3",
    "botocore",
    "aioboto3",
    "torch",
    "transformers",
    "ultralytics",
}
_FORBIDDEN_COMMANDS = {
    "alembic",
    "celery",
    "aws",
    "torchrun",
    "transformers-cli",
}


def _docker(*arguments: str, timeout: int = 90) -> str:
    result = subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"owned Labeling writer Docker command failed: {arguments[0]}")
    return result.stdout.strip()


def _inspect_docker(target: str) -> dict:
    try:
        values = json.loads(_docker("inspect", target, timeout=15))
    except json.JSONDecodeError:
        pytest.fail("Docker returned an invalid Labeling writer identity record")
    if not isinstance(values, list) or len(values) != 1:
        pytest.fail("Labeling writer Docker identity was not unique")
    return values[0]


def _inspect_image(image_ref: str) -> tuple[str, dict]:
    if (
        image_ref != _IMAGE_TAG
        and re.fullmatch(r"sha256:[0-9a-f]{64}", image_ref) is None
    ):
        pytest.fail("the verifier must pass the qualified image tag or immutable ID")
    try:
        values = json.loads(_docker("image", "inspect", image_ref, timeout=20))
    except json.JSONDecodeError:
        pytest.fail("Docker returned an invalid Labeling writer image record")
    if not isinstance(values, list) or len(values) != 1:
        pytest.fail("Labeling writer image identity was not unique")
    image = values[0]
    image_id = image.get("Id")
    tags = image.get("RepoTags") or []
    labels = image.get("Config", {}).get("Labels") or {}
    if not (
        isinstance(image_id, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
        and (image_ref == image_id or image_ref == _IMAGE_TAG)
        and _IMAGE_TAG in tags
        and labels.get("org.aap.source-revision") == _SOURCE_REVISION
    ):
        pytest.fail(
            "Labeling writer image digest, tag, or source revision did not match"
        )
    return image_id, image


def _container_origin(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.port is None
    ):
        pytest.fail("the local Mask content origin was not loopback-only")
    userinfo, at, _hostport = parsed.netloc.rpartition("@")
    authority = f"{userinfo}{at if at else ''}host.docker.internal:{parsed.port}"
    return urlunsplit(
        (parsed.scheme, authority, parsed.path, parsed.query, parsed.fragment)
    )


def _mapped_database_url(dsn: str) -> str:
    try:
        original = make_url(dsn)
        mapped = original.set(host="host.docker.internal").render_as_string(
            hide_password=False
        )
        roundtrip = make_url(mapped)
    except Exception:
        pytest.fail("the restricted writer DSN could not be mapped to the host gateway")
    if not (
        roundtrip.host == "host.docker.internal"
        and roundtrip.drivername == original.drivername
        and roundtrip.username == original.username
        and roundtrip.password == original.password
        and roundtrip.port == original.port
        and roundtrip.database == original.database
        and roundtrip.query == original.query
    ):
        pytest.fail("mapping the restricted writer DSN changed its credentials")
    return mapped


def _write_secret(directory: Path, name: str, value: str) -> Path:
    path = directory / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o644)
    return path.resolve()


_INVENTORY_SCRIPT = r"""
import importlib.metadata as metadata
import importlib.util
import json
import re

normalize = lambda value: re.sub(r"[-_.]+", "-", value).lower()
distributions = sorted(
    {
        normalize(item.metadata.get("Name", ""))
        for item in metadata.distributions()
    }
)
commands = sorted(
    {
        entry.name
        for item in metadata.distributions()
        for entry in item.entry_points
        if entry.group == "console_scripts"
    }
)
module_names = (
    "app",
    "aap_api",
    "aap_inference",
    "sqlalchemy",
    "celery",
    "boto3",
    "botocore",
    "torch",
    "transformers",
    "ultralytics",
)
modules = sorted(
    name for name in module_names if importlib.util.find_spec(name) is not None
)
print(
    json.dumps(
        {"distributions": distributions, "commands": commands, "modules": modules},
        sort_keys=True,
    )
)
"""


class _RestrictedWriterContainer:
    def __init__(
        self,
        *,
        dsn: str,
        output_geometry: str,
        mask_url: str,
        mask_token: str,
        image_id: str,
        tmp_path: Path,
    ):
        self.image_id = image_id
        self.output_geometry = output_geometry
        self.origin = ""
        self.token = secrets.token_urlsafe(36)
        self.name = f"aap-video-writer-{uuid4().hex}"
        self.identity = uuid4().hex
        self.container_id: str | None = None
        self.status_sample: dict | None = None
        self._database_url = _mapped_database_url(dsn)
        self._mask_url = (
            _container_origin(mask_url) if output_geometry == "mask" else ""
        )
        self._mask_token = mask_token if output_geometry == "mask" else ""
        self._secret_dir = tmp_path / f"video-writer-secrets-{self.identity}"
        self._secret_dir.mkdir(mode=0o700)
        os.chmod(self._secret_dir, 0o700)
        self._secret_paths: dict[str, Path] = {}

    def _inspect_owned(self) -> dict:
        if (
            self.container_id is None
            or re.fullmatch(r"[0-9a-f]{64}", self.container_id) is None
        ):
            pytest.fail("Labeling writer container ID was not a full Docker identity")
        observed = _inspect_docker(self.container_id)
        labels = observed.get("Config", {}).get("Labels") or {}
        if not (
            observed.get("Id") == self.container_id
            and observed.get("Name") == f"/{self.name}"
            and labels.get(_OWNER_LABEL) == "labeling-video-writer-owner"
            and labels.get(_IDENTITY_LABEL) == self.identity
        ):
            pytest.fail("refusing to operate on an unowned Labeling writer container")
        return observed

    def _write_credentials(self) -> None:
        self._secret_paths = {
            "/run/secrets/video_writer_database_url": _write_secret(
                self._secret_dir, "database_url", self._database_url
            ),
            "/run/secrets/video_writer_control_token": _write_secret(
                self._secret_dir, "control_token", self.token
            ),
        }
        if self.output_geometry == "mask":
            self._secret_paths["/run/secrets/video_writer_mask_content_token"] = (
                _write_secret(self._secret_dir, "mask_content_token", self._mask_token)
            )

    def _verify_runtime_profile(self) -> None:
        observed = self._inspect_owned()
        config = observed.get("Config", {})
        host = observed.get("HostConfig", {})
        labels = config.get("Labels") or {}
        bindings = (observed.get("NetworkSettings", {}).get("Ports") or {}).get(
            "8180/tcp"
        ) or []
        if not (
            config.get("Image") == self.image_id
            and config.get("Cmd") == ["aap-labeling-video-writer"]
            and config.get("User") == "10006:10006"
            and labels.get("org.aap.source-revision") == _SOURCE_REVISION
            and host.get("ReadonlyRootfs") is True
            and host.get("CapDrop") == ["ALL"]
            and host.get("Privileged") is False
            and "no-new-privileges:true" in (host.get("SecurityOpt") or [])
            and host.get("PidsLimit") == 64
            and host.get("NanoCpus") == 1_000_000_000
            and host.get("Memory") == 768 * 1024**2
            and "/tmp" in (host.get("Tmpfs") or {})
            and observed.get("State", {}).get("Running") is True
            and len(bindings) == 1
            and bindings[0].get("HostIp") == "127.0.0.1"
            and re.fullmatch(r"[1-9][0-9]*", bindings[0].get("HostPort", ""))
            and set(self._secret_paths)
            == {
                mount.get("Destination")
                for mount in observed.get("Mounts", [])
                if mount.get("Type") == "bind"
            }
        ):
            pytest.fail(
                "Labeling writer container did not match its restricted profile"
            )

        mounts = {
            mount.get("Destination"): mount
            for mount in observed.get("Mounts", [])
            if mount.get("Type") == "bind"
        }
        if not all(
            mounts[destination].get("Source") == str(source)
            and mounts[destination].get("RW") is False
            for destination, source in self._secret_paths.items()
        ):
            pytest.fail("Labeling writer secret files were not exact read-only mounts")

        environment = dict(
            item.split("=", 1) for item in config.get("Env", []) if "=" in item
        )
        expected_paths = {
            "LABELING_VIDEO_WRITER_DATABASE_URL_FILE": (
                "/run/secrets/video_writer_database_url"
            ),
            "LABELING_VIDEO_WRITER_CONTROL_TOKEN_FILE": (
                "/run/secrets/video_writer_control_token"
            ),
        }
        if self.output_geometry == "mask":
            expected_paths["ASSETS_MASK_CONTENT_TOKEN_FILE"] = (
                "/run/secrets/video_writer_mask_content_token"
            )
        forbidden_env = {
            name
            for name in environment
            if name == "DATABASE_URL"
            or name.startswith(("MINIO_", "AWS_", "CELERY_", "REDIS_", "APP_"))
            or "S3_" in name
            or name
            in {
                "LABELING_VIDEO_WRITER_DATABASE_URL",
                "LABELING_VIDEO_WRITER_CONTROL_TOKEN",
            }
        }
        secret_values = [self.token, self._database_url]
        if self._mask_token:
            secret_values.append(self._mask_token)
        if not (
            not forbidden_env
            and all(
                environment.get(key) == value for key, value in expected_paths.items()
            )
            and all(
                value not in "\n".join(environment.values()) for value in secret_values
            )
            and environment.get("LABELING_VIDEO_WRITER_HOST") == "0.0.0.0"
            and environment.get("LABELING_VIDEO_WRITER_PORT") == "8180"
            and (
                self.output_geometry != "mask"
                or environment.get("ASSETS_MASK_CONTENT_URL") == self._mask_url
            )
        ):
            pytest.fail("Labeling writer received credentials outside secret files")

        inventory_text = _docker(
            "exec",
            self.container_id,
            "python",
            "-c",
            _INVENTORY_SCRIPT,
            timeout=20,
        )
        try:
            inventory = json.loads(inventory_text)
        except json.JSONDecodeError:
            pytest.fail("Labeling writer package fingerprint was not valid JSON")
        distributions = set(inventory.get("distributions", []))
        commands = set(inventory.get("commands", []))
        modules = set(inventory.get("modules", []))
        forbidden_distributions = distributions & _FORBIDDEN_DISTRIBUTIONS
        forbidden_commands = commands & _FORBIDDEN_COMMANDS
        if not (
            not forbidden_distributions
            and not forbidden_commands
            and not modules
            and "LABELING_VIDEO_WRITER_DATABASE_URL" not in environment
        ):
            pytest.fail(
                "Labeling writer image contains API, ORM, worker, S3, or ML tooling"
            )

        self.origin = f"http://127.0.0.1:{bindings[0]['HostPort']}"

    def start(self) -> None:
        self._write_credentials()
        arguments = [
            "run",
            "--detach",
            "--name",
            self.name,
            "--label",
            f"{_OWNER_LABEL}=labeling-video-writer-owner",
            "--label",
            f"{_IDENTITY_LABEL}={self.identity}",
            "--add-host",
            "host.docker.internal:host-gateway",
            "--publish",
            "127.0.0.1::8180",
            "--cpus",
            "1",
            "--memory",
            "768m",
            "--stop-timeout",
            "20",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "10006:10006",
            "--pids-limit",
            "64",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=32m",
        ]
        for destination, source in self._secret_paths.items():
            arguments.extend(
                [
                    "--mount",
                    f"type=bind,src={source},dst={destination},readonly",
                ]
            )
        arguments.extend(
            [
                "--env",
                "LABELING_VIDEO_WRITER_HOST=0.0.0.0",
                "--env",
                "LABELING_VIDEO_WRITER_PORT=8180",
                "--env",
                "LABELING_VIDEO_WRITER_DATABASE_URL_FILE="
                "/run/secrets/video_writer_database_url",
                "--env",
                "LABELING_VIDEO_WRITER_CONTROL_TOKEN_FILE="
                "/run/secrets/video_writer_control_token",
            ]
        )
        if self.output_geometry == "mask":
            arguments.extend(
                [
                    "--env",
                    f"ASSETS_MASK_CONTENT_URL={self._mask_url}",
                    "--env",
                    "ASSETS_MASK_CONTENT_TOKEN_FILE="
                    "/run/secrets/video_writer_mask_content_token",
                ]
            )
        arguments.append(self.image_id)
        self.container_id = _docker(*arguments, timeout=120)
        self._inspect_owned()
        self._verify_runtime_profile()

    async def wait_ready(self) -> None:
        if self.container_id is None:
            pytest.fail("Labeling writer container was not started")
        async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
            async with asyncio.timeout(30):
                while True:
                    observed = self._inspect_owned()
                    if observed.get("State", {}).get("Running") is not True:
                        pytest.fail("Labeling writer container exited before readiness")
                    try:
                        response = await client.get(self.origin + "/ready")
                        if response.status_code == 200 and response.json() == {
                            "status": "ready"
                        }:
                            return
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)

    async def restart(self) -> None:
        observed = self._inspect_owned()
        if observed.get("State", {}).get("Running") is True:
            _docker("stop", "--time", "15", self.container_id, timeout=30)
        self._inspect_owned()
        _docker("start", self.container_id, timeout=30)
        self._inspect_owned()
        # Docker may assign a new ephemeral host port when the container starts.
        self._verify_runtime_profile()
        await self.wait_ready()

    def stop(self) -> None:
        if self.container_id is None:
            shutil.rmtree(self._secret_dir, ignore_errors=True)
            return
        observed = self._inspect_owned()
        if observed.get("State", {}).get("Running") is True:
            _docker("stop", "--time", "15", self.container_id, timeout=30)
        self._inspect_owned()
        _docker("rm", self.container_id, timeout=30)
        self.container_id = None
        shutil.rmtree(self._secret_dir, ignore_errors=True)


def _instrument_status_samples(
    original_client, writer: _RestrictedWriterContainer, record_sample
):
    sampling_started = False

    class SampledVideoWriterHttpClient(original_client):
        async def status(self, operation_id, execution, request_digest):
            nonlocal sampling_started
            receipt = await super().status(operation_id, execution, request_digest)
            if sampling_started:
                return receipt
            sampling_started = True

            semaphore = asyncio.Semaphore(4)
            active = 0
            peak = 0
            receipts = []
            retries = 0
            latencies_ms = []

            async def sample_one():
                nonlocal active, peak, retries
                async with semaphore:
                    active += 1
                    peak = max(peak, active)
                    try:
                        async with original_client(
                            writer.origin, writer.token, timeout_seconds=20
                        ) as client:
                            started = time.monotonic()
                            for attempt in range(21):
                                try:
                                    result = await client.status(
                                        operation_id, execution, request_digest
                                    )
                                except VideoWriterRejected as error:
                                    if error.code != "execution_busy" or attempt == 20:
                                        raise
                                    retries += 1
                                    await asyncio.sleep(0.025)
                                else:
                                    receipts.append(result)
                                    latencies_ms.append(
                                        (time.monotonic() - started) * 1000
                                    )
                                    break
                    finally:
                        active -= 1

            await asyncio.wait_for(
                asyncio.gather(*(sample_one() for _ in range(16))), timeout=60
            )
            if not (
                len(receipts) == 16
                and all(value is not None for value in receipts)
                and all(
                    value.request_digest == request_digest
                    and value.operation_id == operation_id
                    and value.outcome == "completed"
                    and value.status == "pending_review"
                    for value in receipts
                )
                and 1 <= peak <= 4
            ):
                pytest.fail("the bounded video-writer receipt sample did not match")
            writer.status_sample = {
                "requests": len(receipts),
                "concurrency_limit": 4,
                "observed_peak": peak,
                "busy_retries": retries,
                "http_attempts": len(receipts) + retries,
                "latency_ms": latencies_ms,
            }
            record_sample(writer.status_sample)
            return receipt

    return SampledVideoWriterHttpClient


@pytest.mark.parametrize("output_geometry", ["bbox", "mask"])
async def test_video_writer_owner_container_restarts_and_serves_bounded_status_sample(
    test_engine, tmp_path, monkeypatch, output_geometry, request
):
    from tests import test_labeling_video_writer_live as live_module
    from app.services.video_tracking import execution

    monkeypatch.setattr(execution, "_RENEW_SECONDS", 0.2)
    image_ref = os.environ["AAP_LABELING_VIDEO_WRITER_IMAGE"]
    image_id, _ = _inspect_image(image_ref)
    containers: list[_RestrictedWriterContainer] = []

    def create_writer(**values):
        writer = _RestrictedWriterContainer(**values, image_id=image_id)
        containers.append(writer)
        return writer

    monkeypatch.setattr(live_module, "_StandaloneWriter", create_writer)
    original_client = live_module.VideoWriterHttpClient

    def client_for_current_writer(base_url, token, **options):
        if not containers:
            return original_client(base_url, token, **options)
        sampled = _instrument_status_samples(
            original_client,
            containers[-1],
            lambda sample: request.node.user_properties.append(
                ("receipt_status_sample", json.dumps(sample))
            ),
        )
        return sampled(base_url, token, **options)

    monkeypatch.setattr(live_module, "VideoWriterHttpClient", client_for_current_writer)
    await live_module.test_video_job_finalizes_through_real_labeling_writer_owner(
        test_engine,
        tmp_path,
        monkeypatch,
        output_geometry,
        "http",
    )
    assert len(containers) == 1
    sample = containers[0].status_sample
    assert sample is not None
    assert sample["requests"] == 16
    assert sample["concurrency_limit"] == 4
    assert 1 <= sample["observed_peak"] <= 4
