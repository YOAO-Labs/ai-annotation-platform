from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_annotation._plugin_docker import ContainerConfig, Docker
from ai_annotation.errors import AAPError


def config():
    return ContainerConfig(
        name="aap-plugin-123", installation_id="123", owner_hash="a" * 64,
        image="example.invalid/plugin@sha256:" + "b" * 64,
        gpu_uuid="GPU-12345678-1234-1234-1234-123456789abc", network="approved", host_port=9080,
        container_port=8000, memory_mb=4096, cpus=2, env_file=Path("/private/env"),
        data_dir=Path("/private/data"), data_target="/data", qualification_file=Path("/private/report.json"),
    )


def test_create_uses_one_gpu_and_no_shell_or_socket(monkeypatch):
    cfg = config()
    calls = []
    exists = False
    running = False

    def run(argv, **kwargs):
        nonlocal exists, running
        assert kwargs["check"] is False
        calls.append(argv)
        if argv[1:4] == ["container", "ls", "-a"]:
            out = "abc" if exists else ""
        elif argv[1:3] == ["container", "create"]:
            exists = True
            out = "abc"
        elif "{{json .Config.Labels}}" in argv:
            out = json.dumps({"aap.installation": cfg.installation_id, "aap.owner": cfg.owner_hash, "aap.image": cfg.image, "aap.config": cfg.identity()})
        elif "{{.Config.Image}}" in argv:
            out = cfg.image
        elif "{{json .HostConfig}}" in argv:
            out = json.dumps({"Privileged": False, "NetworkMode": cfg.network,
                "Memory": cfg.memory_mb * 1024 * 1024, "NanoCpus": int(cfg.cpus * 1_000_000_000),
                "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
                "DeviceRequests": [{"DeviceIDs": [cfg.gpu_uuid], "Capabilities": [["gpu"]]}],
                "PortBindings": {f"{cfg.container_port}/tcp": [{"HostPort": str(cfg.host_port), "HostIp": ""}]}})
        elif "{{json .Mounts}}" in argv:
            out = json.dumps([
                {"Type": "bind", "Source": str(cfg.data_dir), "Destination": cfg.data_target, "RW": True},
                {"Type": "bind", "Source": str(cfg.qualification_file), "Destination": "/var/lib/aap-libreyolo-qualification/report.json", "RW": False},
            ])
        elif "{{.Config.User}}" in argv:
            out = "1000:1000"
        elif "{{json .Config.Entrypoint}}" in argv:
            out = json.dumps(["aap-libreyolo", "serve", "--host", "0.0.0.0", "--port", "8000"])
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
        elif argv[1:3] == ["container", "start"]:
            running, out = True, ""
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr("ai_annotation._plugin_docker.subprocess.run", run)
    monkeypatch.setattr(Docker, "_user", staticmethod(lambda: "1000:1000"))
    docker = Docker()
    docker.ensure_started(cfg)
    docker.ensure_started(cfg)
    creates = [x for x in calls if x[1:3] == ["container", "create"]]
    assert len(creates) == 1
    assert creates[0][creates[0].index("--gpus") + 1] == "device=GPU-12345678-1234-1234-1234-123456789abc"
    assert creates[0][-1] == cfg.image
    assert not any("docker.sock" in arg for arg in creates[0])
    assert creates[0][-2:] == ["--", cfg.image]
    assert any("report.json,readonly" in arg for arg in creates[0])


def test_unknown_container_is_not_adopted_or_stopped(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        out = "other" if "ls" in argv else '{"aap.installation":"other"}'
        return SimpleNamespace(returncode=0, stdout=out, stderr="")
    monkeypatch.setattr("ai_annotation._plugin_docker.subprocess.run", run)
    with pytest.raises(AAPError, match="another installation"):
        Docker().stop_and_remove(config())
    assert not any("stop" in argv or "rm" in argv for argv in calls)


def test_rejects_floating_image_and_nonphysical_gpu():
    with pytest.raises(AAPError):
        replace(config(), image="example.invalid/plugin:latest").validate()
    with pytest.raises(AAPError):
        replace(config(), gpu_uuid="all").validate()


def test_rejects_digest_mismatch_before_creating_container(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='["example.invalid/plugin@sha256:' + "c" * 64 + '"]', stderr="")
    monkeypatch.setattr("ai_annotation._plugin_docker.subprocess.run", run)
    with pytest.raises(AAPError, match="approved digest"):
        Docker().ensure_started(config())
    assert not any("create" in argv or "start" in argv for argv in calls)
