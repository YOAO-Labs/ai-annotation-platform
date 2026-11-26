from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from yoao._plugin_docker import (
    PLUGIN_PREPARE_ASSETS_COMMAND_OCI_LABEL,
    ContainerConfig,
    Docker,
    QualificationRunConfig,
)
from yoao.container_runtime import ContainerInspection, ContainerStateInfo
from yoao.errors import SDKError
from yoao_inference_contract.plugin import (
    PLUGIN_MANIFEST_OCI_LABEL,
    PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL,
)


def config():
    return ContainerConfig(
        name="yoao-plugin-123",
        installation_id="123",
        owner_hash="a" * 64,
        image="example.invalid/plugin@sha256:" + "b" * 64,
        entrypoint=("author-plugin", "serve", "--host", "0.0.0.0", "--port", "8000"),
        gpu_uuid="GPU-12345678-1234-1234-1234-123456789abc",
        network="approved",
        host_port=9080,
        container_port=8000,
        memory_mb=4096,
        cpus=2,
        env_file=Path("/private/env"),
        data_dir=Path("/private/data"),
        data_target="/var/lib/yoao-plugin",
        qualification_file=Path("/private/report.json"),
    )


def _image_inspect_payload(
    image: str,
    *,
    image_id: str = "sha256:" + "d" * 64,
    repo_digests: list[str] | None = None,
    labels: dict[str, str] | None = None,
) -> str:
    return json.dumps(
        {
            "Id": image_id,
            "RepoDigests": [image] if repo_digests is None else repo_digests,
            "Config": {"Labels": labels},
        }
    )


def _container_inspect_payload(
    cfg: ContainerConfig,
    *,
    container_id: str = "c" * 64,
    image_id: str = "sha256:" + "d" * 64,
    inspected_script: str | None = None,
    keyring_rw: bool = False,
    running: bool = False,
    started_at: str = "2026-10-29T12:00:00Z",
    finished_at: str = "0001-01-01T00:00:00Z",
    labels: dict[str, str] | None = None,
    pid_mode: str | None = "",
    mounts: list[dict] | None = None,
) -> str:
    host_config: dict = {
        "Privileged": False,
        "NetworkMode": cfg.network,
        "Memory": cfg.memory_mb * 1024 * 1024,
        "NanoCpus": int(cfg.cpus * 1_000_000_000),
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges"],
        "DeviceRequests": (
            [{"DeviceIDs": [cfg.gpu_uuid], "Capabilities": [["gpu"]]}]
            if cfg.gpu_uuid
            else []
        ),
        "PortBindings": {
            f"{cfg.container_port}/tcp": [
                {"HostPort": str(cfg.host_port), "HostIp": ""}
            ]
        },
    }
    if pid_mode is not None:
        host_config["PidMode"] = pid_mode
    resolved_labels = (
        {
            "yoao.installation": cfg.installation_id,
            "yoao.owner": cfg.owner_hash,
            "yoao.image": cfg.image,
            "yoao.config": cfg.identity(),
            **(
                {"yoao.candidate": cfg.candidate_token}
                if cfg.candidate_token is not None
                else {}
            ),
        }
        if labels is None
        else labels
    )
    resolved_mounts = (
        [
            {
                "Type": "bind",
                "Source": str(cfg.data_dir),
                "Destination": cfg.data_target,
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": str(cfg.data_dir / "gpu-keyring.json"),
                "Destination": f"{cfg.data_target}/gpu-keyring.json",
                "RW": keyring_rw,
            },
            {
                "Type": "bind",
                "Source": str(cfg.qualification_file),
                "Destination": "/var/lib/yoao-plugin-qualification/report.json",
                "RW": False,
            },
        ]
        if mounts is None
        else mounts
    )
    script = inspected_script or cfg.entrypoint[0]
    return json.dumps(
        {
            "Id": container_id,
            "Name": f"/{cfg.name}",
            "Image": image_id,
            "Config": {
                "Image": cfg.image,
                "Labels": resolved_labels,
                "User": "1000:1000",
                "Entrypoint": [script, *cfg.entrypoint[1:]],
                "Cmd": None,
            },
            "HostConfig": host_config,
            "Mounts": resolved_mounts,
            "State": {
                "Running": running,
                "Restarting": False,
                "Paused": False,
                "Dead": False,
                "StartedAt": started_at,
                "FinishedAt": finished_at,
            },
        }
    )


def test_asset_preparation_rejects_shell_or_invalid_command(monkeypatch):
    cfg = config()
    calls = []
    monkeypatch.setattr(
        "yoao.container_runtime.subprocess.run",
        lambda argv, **kwargs: calls.append(argv),
    )

    for bad_command in (
        (),
        ("sh", "-c", "id"),
        ("BASH", "-c", "id"),
        ("python3", "-c", "import os"),
        ("python3", "-Ic", "import os"),
        ("/usr/bin/custom-plugin", "prepare-assets"),
    ):
        with pytest.raises(SDKError, match="asset-preparation command"):
            Docker().prepare_assets(cfg, bad_command)
    assert calls == []


def test_asset_preparation_uses_validated_oci_command_for_any_plugin(monkeypatch):
    cfg = replace(config(), entrypoint=("custom-detector", "serve", "--port", "8000"))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        output = (
            _image_inspect_payload(cfg.image)
            if argv[1:3] == ["image", "inspect"]
            else ""
        )
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr("yoao.container_runtime.subprocess.run", run)
    monkeypatch.setattr("yoao._plugin_docker.os.geteuid", lambda: 1000)
    monkeypatch.setattr("yoao._plugin_docker.os.getegid", lambda: 1000)
    Docker().prepare_assets(
        cfg,
        (
            "custom-detector",
            "download-assets",
            "--dest",
            "/var/lib/yoao-plugin/assets",
        ),
    )
    assert calls[0][-1] == cfg.image
    cmd = calls[1]
    assert cmd[:4] == ["docker", "container", "run", "--rm"]
    assert cmd[cmd.index("--entrypoint") + 1] == "custom-detector"
    assert cmd[cmd.index("--") + 1 :] == [
        cfg.image,
        "download-assets",
        "--dest",
        "/var/lib/yoao-plugin/assets",
    ]


@pytest.mark.parametrize(
    "inspected_script,keyring_rw",
    [
        ("author-plugin", False),
        ("other-plugin", False),
        ("author-plugin", True),
    ],
)
def test_create_uses_one_gpu_and_no_shell_or_socket(
    monkeypatch, inspected_script, keyring_rw
):
    cfg = config()
    calls = []
    exists = False
    running = False

    def run(argv, **kwargs):
        nonlocal exists, running
        assert kwargs["check"] is False
        calls.append(argv)
        if argv[1:4] == ["container", "ls", "-a"]:
            out = "c" * 64 if exists else ""
        elif argv[1:3] == ["container", "create"]:
            exists = True
            out = "c" * 64
        elif argv[1:3] == ["image", "inspect"]:
            out = _image_inspect_payload(cfg.image)
        elif argv[1:3] == ["container", "inspect"]:
            out = _container_inspect_payload(
                cfg,
                inspected_script=inspected_script,
                keyring_rw=keyring_rw,
                running=running,
            )
        elif argv[1:3] == ["container", "start"]:
            running, out = True, ""
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr("yoao.container_runtime.subprocess.run", run)
    monkeypatch.setattr(Docker, "_user", staticmethod(lambda: "1000:1000"))
    docker = Docker()
    if keyring_rw:
        with pytest.raises(SDKError):
            docker.ensure_started(cfg)
        assert not any(call[1:3] == ["container", "start"] for call in calls)
        return
    if inspected_script != cfg.entrypoint[0]:
        with pytest.raises(SDKError, match="entrypoint"):
            docker.ensure_started(cfg)
        return
    docker.ensure_started(cfg)
    docker.ensure_started(cfg)
    creates = [x for x in calls if x[1:3] == ["container", "create"]]
    assert len(creates) == 1
    assert (
        creates[0][creates[0].index("--gpus") + 1]
        == "device=GPU-12345678-1234-1234-1234-123456789abc"
    )
    assert creates[0][-1] == cfg.image
    assert not any("docker.sock" in arg for arg in creates[0])
    assert creates[0][-2:] == ["--", cfg.image]
    assert any("report.json,readonly" in arg for arg in creates[0])
    assert all(
        call[-1] == "c" * 64
        for call in calls
        if call[1:3] in (["container", "inspect"], ["container", "start"])
    )


def test_unknown_container_is_not_adopted_or_stopped(monkeypatch):
    cfg = config()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        out = (
            "c" * 64
            if "ls" in argv
            else _container_inspect_payload(cfg, labels={"yoao.installation": "other"})
        )
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr("yoao.container_runtime.subprocess.run", run)
    with pytest.raises(SDKError, match="another installation"):
        Docker().stop_and_remove(cfg, "c" * 64, "original")
    assert not any("stop" in argv or "rm" in argv for argv in calls)


def test_candidate_intent_rejects_unknown_container(monkeypatch):
    cfg = replace(config(), candidate_token="e" * 64)
    docker = Docker()
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("container", "ls", "-a"):
            return "c" * 64
        if args[:2] == ("container", "inspect"):
            return _container_inspect_payload(
                cfg,
                labels={
                    "yoao.installation": cfg.installation_id,
                    "yoao.owner": cfg.owner_hash,
                    "yoao.image": cfg.image,
                    "yoao.config": cfg.identity(),
                    "yoao.candidate": "f" * 64,
                },
            )
        raise AssertionError(args)

    monkeypatch.setattr(docker.runtime, "_run", run)
    with pytest.raises(SDKError, match="another installation"):
        docker.create_candidate(cfg)
    assert not any(
        args[:2] in (("container", "start"), ("container", "create")) for args in calls
    )


def test_candidate_does_not_restart_exited_process_or_replaced_id(monkeypatch):
    cfg = replace(config(), candidate_token="e" * 64)
    docker = Docker()
    monkeypatch.setattr(docker.runtime, "inspect_image", lambda _: None)
    monkeypatch.setattr(
        docker, "_inspect", lambda _: {"id": "c" * 64, "name": cfg.name}
    )
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("container", "inspect"):
            return _container_inspect_payload(
                cfg,
                running=False,
                started_at="2026-10-29T12:00:00Z",
                finished_at="2026-10-29T12:01:00Z",
            )
        raise AssertionError(args)

    monkeypatch.setattr(docker.runtime, "_run", run)
    with pytest.raises(SDKError, match="exited"):
        docker.start_candidate(cfg, "c" * 64, may_start=True)
    with pytest.raises(SDKError, match="ID differs"):
        docker.start_candidate(cfg, "d" * 64, may_start=True)
    assert not any(args[:2] == ("container", "start") for args in calls)


def test_running_candidate_with_prior_exit_is_not_adopted(monkeypatch):
    cfg = replace(config(), candidate_token="e" * 64)
    docker = Docker()
    monkeypatch.setattr(docker.runtime, "inspect_image", lambda _: None)
    monkeypatch.setattr(
        docker, "_inspect", lambda _: {"id": "c" * 64, "name": cfg.name}
    )
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("container", "inspect"):
            return _container_inspect_payload(
                cfg,
                running=True,
                started_at="2026-10-29T12:02:00Z",
                finished_at="2026-10-29T12:01:00Z",
            )
        raise AssertionError(args)

    monkeypatch.setattr(docker.runtime, "_run", run)
    with pytest.raises(SDKError, match="previously exited"):
        docker.start_candidate(cfg, "c" * 64, may_start=True)
    assert not any(args[:2] == ("container", "start") for args in calls)


def test_rejects_floating_image_and_nonphysical_gpu():
    with pytest.raises(SDKError):
        replace(config(), image="example.invalid/plugin:latest").validate()
    with pytest.raises(SDKError):
        replace(config(), gpu_uuid="all").validate()


def test_approved_entrypoint_is_validated_and_part_of_identity():
    cfg = config()
    assert (
        cfg.identity()
        != replace(cfg, entrypoint=("other-plugin", *cfg.entrypoint[1:])).identity()
    )
    for entry in (
        ("/tmp/plugin", *cfg.entrypoint[1:]),
        ("plugin;echo", *cfg.entrypoint[1:]),
        ("plugin", "serve", "--host", "0.0.0.0", "--port", "9000"),
    ):
        with pytest.raises(SDKError, match="entrypoint"):
            replace(cfg, entrypoint=entry).validate()


def test_rejects_digest_mismatch_before_creating_container(monkeypatch):
    cfg = config()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=_image_inspect_payload(
                cfg.image,
                repo_digests=["example.invalid/plugin@sha256:" + "c" * 64],
            ),
            stderr="",
        )

    monkeypatch.setattr("yoao.container_runtime.subprocess.run", run)
    with pytest.raises(SDKError, match="approved digest"):
        Docker().ensure_started(cfg)
    assert not any("create" in argv or "start" in argv for argv in calls)


@pytest.mark.parametrize("pid_mode", [None, "host", "container:" + "d" * 64, "private"])
def test_inspect_rejects_unapproved_pid_mode(monkeypatch, pid_mode):
    cfg = config()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:4] == ["container", "ls", "-a"]:
            out = "c" * 64
        elif argv[1:3] == ["container", "inspect"]:
            out = _container_inspect_payload(cfg, pid_mode=pid_mode, mounts=[])
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr("yoao.container_runtime.subprocess.run", run)
    with pytest.raises(SDKError, match="resources"):
        Docker().exists(cfg)
    assert all(
        call[-1] == "c" * 64 for call in calls if call[1:3] == ["container", "inspect"]
    )


def test_stop_rejects_replacement_and_accepts_absence(monkeypatch):
    cfg = config()
    docker = Docker()
    calls = []
    monkeypatch.setattr(
        docker, "_inspect", lambda config: {"id": "d" * 64, "name": config.name}
    )
    monkeypatch.setattr(
        docker.runtime, "_run", lambda *args, **kwargs: calls.append(args)
    )
    with pytest.raises(SDKError, match="proven container"):
        docker.stop_and_remove(cfg, "c" * 64, "original")
    assert calls == []
    monkeypatch.setattr(docker, "_inspect", lambda config: None)
    docker.stop_and_remove(cfg, "c" * 64, "original")
    assert calls == []


@pytest.mark.parametrize("running", [True, False])
def test_stop_and_remove_uses_proven_full_id(monkeypatch, running):
    cfg = config()
    docker = Docker()
    calls = []
    monkeypatch.setattr(
        docker, "_inspect", lambda config: {"id": "c" * 64, "name": config.name}
    )

    def run(*args, **kwargs):
        nonlocal running
        calls.append(args)
        if args[:2] == ("container", "inspect"):
            return _container_inspect_payload(
                cfg,
                running=running,
                started_at="original",
                finished_at="previous",
            )
        if args[:2] == ("container", "stop"):
            running = False
        return ""

    monkeypatch.setattr(docker.runtime, "_run", run)
    docker.stop_and_remove(cfg, "c" * 64, "original")
    assert calls[-1] == ("container", "rm", "c" * 64)
    assert all(call[-1] == "c" * 64 for call in calls)


@pytest.mark.parametrize("expected", [None, "original"])
def test_stop_rejects_unknown_or_restarted_process(monkeypatch, expected):
    cfg = config()
    docker = Docker()
    monkeypatch.setattr(
        docker, "_inspect", lambda config: {"id": "c" * 64, "name": config.name}
    )
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("container", "inspect"):
            return _container_inspect_payload(
                cfg,
                running=True,
                started_at="restarted",
                finished_at="original-finished",
            )
        raise AssertionError(args)

    monkeypatch.setattr(docker.runtime, "_run", run)
    with pytest.raises(SDKError, match="process identity"):
        docker.stop_and_remove(cfg, "c" * 64, expected)
    assert all(call[:2] != ("container", "stop") for call in calls)


def test_startup_disconnect_waits_for_healthy_response(monkeypatch):
    disconnected = Event()
    unavailable = Event()
    allow_ready = Event()
    delivered_ready = Event()

    class StartupHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not disconnected.is_set():
                disconnected.set()
                self.close_connection = True
                return
            ready = allow_ready.is_set()
            self.send_response(200 if ready else 503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            (delivered_ready if ready else unavailable).set()

        def log_message(self, *_args):
            pass

    with HTTPServer(("127.0.0.1", 0), StartupHandler) as server:
        serving = Thread(target=server.serve_forever, daemon=True)
        serving.start()
        docker = Docker()
        cfg = replace(config(), host_port=server.server_address[1])
        running_info = ContainerInspection(
            container_id="c" * 64,
            name=cfg.name,
            image_id="sha256:" + "d" * 64,
            image_ref=cfg.image,
            labels={},
            host_config={},
            mounts=(),
            user="1000:1000",
            entrypoint=cfg.entrypoint,
            cmd=None,
            state=ContainerStateInfo(
                running=True,
                restarting=False,
                paused=False,
                dead=False,
                started_at="2026-10-29T12:00:00Z",
                finished_at="0001-01-01T00:00:00Z",
            ),
        )
        monkeypatch.setattr(
            docker.runtime, "inspect_container", lambda **_kwargs: running_info
        )
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                readiness = executor.submit(docker.wait_ready, cfg, "c" * 64)
                try:
                    assert unavailable.wait(5), (
                        "Startup disconnect must permit the next health probe"
                    )
                    assert not readiness.done(), (
                        "An unavailable plugin must not be activated"
                    )
                finally:
                    allow_ready.set()
                readiness.result(timeout=5)
                assert delivered_ready.wait(1)
        finally:
            allow_ready.set()
            server.shutdown()
            serving.join(timeout=5)


def _oci_manifest_label(
    *, device: str = "cuda", plugin_id: str = "custom-detector"
) -> str:
    return json.dumps(
        {
            "plugin_id": plugin_id,
            "version": "1.0",
            "publisher": "example",
            "license": "Apache-2.0",
            "sdk_compatibility": ">=1",
            "entrypoint": [
                "custom-detector",
                "serve",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
            ],
            "capabilities": ["inference", "detection"],
            "models": [
                {
                    "id": "detect",
                    "task": "detect",
                    "is_interactive": False,
                    "supported_prompts": ["none"],
                    "supported_inputs": ["full_image"],
                    "supported_geometric_outputs": ["bbox"],
                }
            ],
            "pools": {"models": {"cap": 1}},
            "resource": {
                "device": device,
                "memory_mb": 4096,
                "gpu_memory_mb": 2048 if device == "cuda" else 0,
            },
            "asset_manifest_sha256": "a" * 64,
            "allowed_asset_sources": ["https://assets.example"],
        }
    )


def _qualification_config():
    return QualificationRunConfig(
        image="example.invalid/plugin@sha256:" + "b" * 64,
        command=("custom-detector", "qualify", "--cycles", "2"),
        gpu_uuid="GPU-12345678-1234-1234-1234-123456789abc",
        memory_mb=4096,
        cpus=2,
        env_file=Path("/private/state/.qualify-1/qualify.env"),
        context_file=Path("/private/state/.qualify-1/context.json"),
        output_dir=Path("/private/state/.qualify-1/output"),
        data_target="/var/lib/yoao-plugin",
        assets_dir=Path("/private/state/data/assets"),
        fixture_file=Path("/private/fixtures/sample.jpg"),
    )


def test_inspect_image_labels_verifies_digest_before_reading_labels(monkeypatch):
    image = "example.invalid/plugin@sha256:" + "b" * 64
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        out = _image_inspect_payload(
            image,
            repo_digests=["example.invalid/plugin@sha256:" + "c" * 64],
            labels={"bad": "ignored"},
        )
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr("yoao.container_runtime.subprocess.run", run)
    with pytest.raises(SDKError, match="approved digest"):
        Docker().inspect_image_labels(image)
    assert len(calls) == 1

    calls.clear()
    with pytest.raises(SDKError, match="sha256 digest"):
        Docker().inspect_image_labels("example.invalid/plugin:latest")
    assert calls == []


def test_inspect_image_manifest_reads_cpu_and_cuda_labels(monkeypatch):
    image = "example.invalid/plugin@sha256:" + "b" * 64
    image_id = "sha256:" + "d" * 64
    labels: dict[str, str] | None = {
        PLUGIN_MANIFEST_OCI_LABEL: _oci_manifest_label(device="cpu")
    }

    def run(argv, **kwargs):
        if argv[1:3] == ["image", "inspect"]:
            out = _image_inspect_payload(image, image_id=image_id, labels=labels)
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr("yoao.container_runtime.subprocess.run", run)
    manifest, observed_id, qcmd = Docker().inspect_image_manifest(image)
    assert observed_id == image_id
    assert manifest.artifact == image
    assert manifest.resource.device == "cpu"
    assert qcmd is None
    assert Docker().inspect_image_metadata(image).prepare_assets_command is None

    labels = {
        PLUGIN_MANIFEST_OCI_LABEL: _oci_manifest_label(device="cuda"),
        PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL: json.dumps(
            ["custom-detector", "qualify"]
        ),
        PLUGIN_PREPARE_ASSETS_COMMAND_OCI_LABEL: json.dumps(
            ["custom-detector", "prepare-assets", "--bundle", "default"]
        ),
    }
    inspected = Docker().inspect_image_metadata(image)
    assert inspected.image_id == image_id
    assert inspected.manifest.resource.device == "cuda"
    assert inspected.qualification_command == ("custom-detector", "qualify")
    assert inspected.prepare_assets_command == (
        "custom-detector",
        "prepare-assets",
        "--bundle",
        "default",
    )
    manifest, observed_id, qcmd = Docker().inspect_image_manifest(image)
    assert observed_id == image_id
    assert manifest.resource.device == "cuda"
    assert qcmd == ("custom-detector", "qualify")

    labels = None
    assert Docker().inspect_image_labels(image) == (image_id, {})
    with pytest.raises(SDKError, match=PLUGIN_MANIFEST_OCI_LABEL):
        Docker().inspect_image_manifest(image)

    labels = {PLUGIN_MANIFEST_OCI_LABEL: '{"artifact":"' + image + '"}'}
    with pytest.raises(SDKError, match=PLUGIN_MANIFEST_OCI_LABEL):
        Docker().inspect_image_manifest(image)

    labels = {
        PLUGIN_MANIFEST_OCI_LABEL: _oci_manifest_label(device="cuda"),
        PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL: json.dumps(["sh", "-c", "id"]),
    }
    with pytest.raises(SDKError, match=PLUGIN_QUALIFICATION_COMMAND_OCI_LABEL):
        Docker().inspect_image_manifest(image)

    labels = {
        PLUGIN_MANIFEST_OCI_LABEL: _oci_manifest_label(device="cuda"),
        PLUGIN_PREPARE_ASSETS_COMMAND_OCI_LABEL: json.dumps(["sh", "-c", "id"]),
    }
    with pytest.raises(SDKError, match=PLUGIN_PREPARE_ASSETS_COMMAND_OCI_LABEL):
        Docker().inspect_image_metadata(image)


def test_run_qualification_isolates_container_and_mounts_only_declared_paths(
    monkeypatch,
):
    qcfg = _qualification_config()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        out = (
            _image_inspect_payload(qcfg.image)
            if argv[1:3] == ["image", "inspect"]
            else ""
        )
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr("yoao.container_runtime.subprocess.run", run)
    monkeypatch.setattr(Docker, "_user", staticmethod(lambda: "1000:1000"))

    Docker().run_qualification(qcfg)
    assert len(calls) == 2
    assert calls[0] == [
        "docker",
        "image",
        "inspect",
        "--format",
        "{{json .}}",
        qcfg.image,
    ]
    cmd = calls[1]
    assert cmd[:4] == ["docker", "container", "run", "--rm"]
    assert cmd[cmd.index("--network") + 1] == "none"
    assert cmd[cmd.index("--user") + 1] == "1000:1000"
    assert cmd[cmd.index("--gpus") + 1] == f"device={qcfg.gpu_uuid}"
    assert cmd[cmd.index("--memory") + 1] == "4096m"
    assert cmd[cmd.index("--cpus") + 1] == "2"
    assert cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert cmd[cmd.index("--security-opt") + 1] == "no-new-privileges"
    assert cmd[cmd.index("--env-file") + 1] == str(qcfg.env_file)
    assert cmd[cmd.index("--entrypoint") + 1] == "custom-detector"
    assert cmd[cmd.index("--") + 1 :] == [qcfg.image, "qualify", "--cycles", "2"]

    mounts = [cmd[index + 1] for index, token in enumerate(cmd) if token == "--mount"]
    assert mounts == [
        f"type=bind,src={qcfg.context_file},dst=/var/lib/yoao-qualification/context.json,readonly",
        f"type=bind,src={qcfg.output_dir},dst=/var/lib/yoao-qualification/output",
        f"type=bind,src={qcfg.assets_dir},dst={qcfg.data_target}/assets,readonly",
        f"type=bind,src={qcfg.fixture_file},dst=/var/lib/yoao-qualification/fixture,readonly",
    ]
    joined = " ".join(cmd)
    for forbidden in ("/data/token", "gpu-keyring.json", "docker.sock"):
        assert forbidden not in joined
    assert not any(
        mount.startswith("type=bind,src=/private/state/data,") for mount in mounts
    )


def test_qualification_run_config_rejects_unsafe_inputs():
    qcfg = _qualification_config()
    for bad in (
        replace(qcfg, image="example.invalid/plugin:latest"),
        replace(qcfg, command=("bash", "-c", "id")),
        replace(qcfg, command=("BASH", "-c", "id")),
        replace(qcfg, command=("python3", "-c", "import os")),
        replace(qcfg, command=("python3", "-Ic", "import os")),
        replace(qcfg, gpu_uuid="all"),
        replace(qcfg, memory_mb=0),
        replace(qcfg, cpus=0),
        replace(qcfg, env_file=Path("relative/env")),
        replace(qcfg, context_file=Path("/tmp/a,b/context.json")),
        replace(qcfg, output_dir=Path("/var/run/docker.sock")),
        replace(qcfg, data_target="relative/target"),
        replace(qcfg, assets_dir=Path("/var/run/docker.sock")),
        replace(qcfg, fixture_file=Path("/tmp/fix,ture.jpg")),
    ):
        with pytest.raises(SDKError):
            bad.validate()

    for bad_entry in (
        ("BASH", "-c", "id"),
        ("python3", "-Ic", "import os"),
        ("python3", "-cimport os"),
    ):
        with pytest.raises(SDKError, match="entrypoint"):
            replace(config(), entrypoint=bad_entry).validate()
