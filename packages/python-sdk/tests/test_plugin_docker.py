from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from ai_annotation._plugin_docker import ContainerConfig, Docker
from ai_annotation.errors import AAPError


def config():
    return ContainerConfig(
        name="aap-plugin-123",
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
        data_target="/data",
        qualification_file=Path("/private/report.json"),
    )


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
            out = "abc"
        elif "{{json .Config.Labels}}" in argv:
            out = json.dumps(
                {
                    "aap.installation": cfg.installation_id,
                    "aap.owner": cfg.owner_hash,
                    "aap.image": cfg.image,
                    "aap.config": cfg.identity(),
                }
            )
        elif "{{.Config.Image}}" in argv:
            out = cfg.image
        elif "{{json .HostConfig}}" in argv:
            out = json.dumps(
                {
                    "Privileged": False,
                    "PidMode": "",
                    "NetworkMode": cfg.network,
                    "Memory": cfg.memory_mb * 1024 * 1024,
                    "NanoCpus": int(cfg.cpus * 1_000_000_000),
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges"],
                    "DeviceRequests": [
                        {"DeviceIDs": [cfg.gpu_uuid], "Capabilities": [["gpu"]]}
                    ],
                    "PortBindings": {
                        f"{cfg.container_port}/tcp": [
                            {"HostPort": str(cfg.host_port), "HostIp": ""}
                        ]
                    },
                }
            )
        elif "{{json .Mounts}}" in argv:
            out = json.dumps(
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
                        "Destination": "/var/lib/aap-libreyolo-qualification/report.json",
                        "RW": False,
                    },
                ]
            )
        elif "{{.Config.User}}" in argv:
            out = "1000:1000"
        elif "{{json .Config.Entrypoint}}" in argv:
            out = json.dumps(
                [inspected_script, "serve", "--host", "0.0.0.0", "--port", "8000"]
            )
        elif "{{json .Config.Cmd}}" in argv:
            out = "null"
        elif "{{json .RepoDigests}}" in argv:
            out = json.dumps([cfg.image])
        elif "{{.Id}}" in argv and argv[1] == "container":
            out = "c" * 64
        elif "{{.Id}}" in argv or "{{.Image}}" in argv:
            out = "sha256:" + "d" * 64
        elif "{{.State.Running}}" in argv:
            out = "true" if running else "false"
        elif "{{.State.StartedAt}}" in argv:
            out = "2026-10-29T12:00:00Z"
        elif argv[1:3] == ["container", "start"]:
            running, out = True, ""
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr("ai_annotation._plugin_docker.subprocess.run", run)
    monkeypatch.setattr(Docker, "_user", staticmethod(lambda: "1000:1000"))
    docker = Docker()
    if keyring_rw:
        with pytest.raises(AAPError):
            docker.ensure_started(cfg)
        assert not any(call[1:3] == ["container", "start"] for call in calls)
        return
    if inspected_script != cfg.entrypoint[0]:
        with pytest.raises(AAPError, match="entrypoint"):
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
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        out = "c" * 64 if "ls" in argv else '{"aap.installation":"other"}'
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr("ai_annotation._plugin_docker.subprocess.run", run)
    with pytest.raises(AAPError, match="another installation"):
        Docker().stop_and_remove(config(), "c" * 64, "original")
    assert not any("stop" in argv or "rm" in argv for argv in calls)


def test_candidate_intent_rejects_unknown_container(monkeypatch):
    cfg = replace(config(), candidate_token="e" * 64)
    docker = Docker()
    calls = []

    def run(*args):
        calls.append(args)
        if args[:3] == ("container", "ls", "-a"):
            return "c" * 64
        if "{{json .Config.Labels}}" in args:
            return json.dumps(
                {
                    "aap.installation": cfg.installation_id,
                    "aap.owner": cfg.owner_hash,
                    "aap.image": cfg.image,
                    "aap.config": cfg.identity(),
                    "aap.candidate": "f" * 64,
                }
            )
        raise AssertionError(args)

    monkeypatch.setattr(docker, "_run", run)
    with pytest.raises(AAPError, match="another installation"):
        docker.create_candidate(cfg)
    assert not any(
        args[:2] in (("container", "start"), ("container", "create")) for args in calls
    )


def test_candidate_does_not_restart_exited_process_or_replaced_id(monkeypatch):
    cfg = replace(config(), candidate_token="e" * 64)
    docker = Docker()
    monkeypatch.setattr(docker, "_verify_image", lambda _: None)
    monkeypatch.setattr(
        docker, "_inspect", lambda _: {"id": "c" * 64, "name": cfg.name}
    )
    calls = []

    def run(*args):
        calls.append(args)
        if "{{json .State}}" in args:
            return json.dumps(
                {
                    "Running": False,
                    "Restarting": False,
                    "Paused": False,
                    "Dead": False,
                    "StartedAt": "2026-10-29T12:00:00Z",
                    "FinishedAt": "2026-10-29T12:01:00Z",
                }
            )
        raise AssertionError(args)

    monkeypatch.setattr(docker, "_run", run)
    with pytest.raises(AAPError, match="exited"):
        docker.start_candidate(cfg, "c" * 64, may_start=True)
    with pytest.raises(AAPError, match="ID differs"):
        docker.start_candidate(cfg, "d" * 64, may_start=True)
    assert not any(args[:2] == ("container", "start") for args in calls)


def test_running_candidate_with_prior_exit_is_not_adopted(monkeypatch):
    cfg = replace(config(), candidate_token="e" * 64)
    docker = Docker()
    monkeypatch.setattr(docker, "_verify_image", lambda _: None)
    monkeypatch.setattr(
        docker, "_inspect", lambda _: {"id": "c" * 64, "name": cfg.name}
    )
    calls = []

    def run(*args):
        calls.append(args)
        if "{{json .State}}" in args:
            return json.dumps(
                {
                    "Running": True,
                    "Restarting": False,
                    "Paused": False,
                    "Dead": False,
                    "StartedAt": "2026-10-29T12:02:00Z",
                    "FinishedAt": "2026-10-29T12:01:00Z",
                }
            )
        raise AssertionError(args)

    monkeypatch.setattr(docker, "_run", run)
    with pytest.raises(AAPError, match="previously exited"):
        docker.start_candidate(cfg, "c" * 64, may_start=True)
    assert not any(args[:2] == ("container", "start") for args in calls)


def test_rejects_floating_image_and_nonphysical_gpu():
    with pytest.raises(AAPError):
        replace(config(), image="example.invalid/plugin:latest").validate()
    with pytest.raises(AAPError):
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
        with pytest.raises(AAPError, match="entrypoint"):
            replace(cfg, entrypoint=entry).validate()


def test_rejects_digest_mismatch_before_creating_container(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout='["example.invalid/plugin@sha256:' + "c" * 64 + '"]',
            stderr="",
        )

    monkeypatch.setattr("ai_annotation._plugin_docker.subprocess.run", run)
    with pytest.raises(AAPError, match="approved digest"):
        Docker().ensure_started(config())
    assert not any("create" in argv or "start" in argv for argv in calls)


@pytest.mark.parametrize("pid_mode", [None, "host", "container:" + "d" * 64, "private"])
def test_inspect_rejects_unapproved_pid_mode(monkeypatch, pid_mode):
    cfg = config()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:4] == ["container", "ls", "-a"]:
            out = "c" * 64
        elif "{{json .Config.Labels}}" in argv:
            out = json.dumps(
                {
                    "aap.installation": cfg.installation_id,
                    "aap.owner": cfg.owner_hash,
                    "aap.image": cfg.image,
                    "aap.config": cfg.identity(),
                }
            )
        elif "{{.Config.Image}}" in argv:
            out = cfg.image
        elif "{{json .HostConfig}}" in argv:
            host = {
                "Privileged": False,
                "NetworkMode": cfg.network,
                "Memory": cfg.memory_mb * 1024 * 1024,
                "NanoCpus": int(cfg.cpus * 1_000_000_000),
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "DeviceRequests": [
                    {"DeviceIDs": [cfg.gpu_uuid], "Capabilities": [["gpu"]]}
                ],
            }
            if pid_mode is not None:
                host["PidMode"] = pid_mode
            out = json.dumps(host)
        elif "{{json .Mounts}}" in argv:
            out = "[]"
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr("ai_annotation._plugin_docker.subprocess.run", run)
    with pytest.raises(AAPError, match="resources"):
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
    monkeypatch.setattr(docker, "_run", lambda *args: calls.append(args))
    with pytest.raises(AAPError, match="proven container"):
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

    def run(*args):
        nonlocal running
        calls.append(args)
        if args[:2] == ("container", "inspect"):
            return json.dumps(
                {
                    "Running": running,
                    "Restarting": False,
                    "Paused": False,
                    "Dead": False,
                    "StartedAt": "original",
                    "FinishedAt": "previous",
                }
            )
        if args[:2] == ("container", "stop"):
            running = False
        return ""

    monkeypatch.setattr(docker, "_run", run)
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

    def run(*args):
        calls.append(args)
        if args[:2] == ("container", "inspect"):
            return json.dumps(
                {
                    "Running": True,
                    "Restarting": False,
                    "Paused": False,
                    "Dead": False,
                    "StartedAt": "restarted",
                    "FinishedAt": "original-finished",
                }
            )
        raise AssertionError(args)

    monkeypatch.setattr(docker, "_run", run)
    with pytest.raises(AAPError, match="process identity"):
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
        monkeypatch.setattr(docker, "_run", lambda *_args: "true")
        cfg = replace(config(), host_port=server.server_address[1])
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
