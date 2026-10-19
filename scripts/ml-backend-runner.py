#!/usr/bin/env python3
"""Restricted, explicitly deployed host runner; never accepts command text from jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
from app.services.backend_operations import JobStore, PROVIDERS, TERMINAL  # noqa: E402

PORTS = {"gsam2": 8001, "sam3": 8002, "yolo": 8003, "onnxtools": 8004, "rapidocr": 8005}
MODEL_MOUNTS = {
    "gsam2": {"/app/checkpoints", "/app/.cache/huggingface"},
    "sam3": {"/app/checkpoints", "/app/.cache/huggingface"},
    "yolo": {"/app/checkpoints"},
    "onnxtools": {"/app/models"},
    "rapidocr": {"/app/models"},
}
# Read process/code identity without importing GPU libraries or printing environment.
PROBE = """import ast,hashlib,json,pathlib
p=pathlib.Path('/app')
t=ast.parse((p/'main.py').read_text())
shutdown=any(isinstance(n,ast.Await) and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Attribute) and isinstance(n.value.func.value,ast.Name) and n.value.func.value.id=='_gpu_lifecycle' and n.value.func.attr=='shutdown' for n in ast.walk(t))
print(json.dumps({'argv':pathlib.Path('/proc/1/cmdline').read_bytes().decode().split('\\0')[:-1],'shutdown':shutdown,'lifecycle_sha256':hashlib.sha256((p/'gpu_lifecycle.py').read_bytes()).hexdigest()}))
"""


class UnsafeRuntime(RuntimeError):
    pass


class CommandFailure(RuntimeError):
    pass


class TrackingTimeout(RuntimeError):
    pass


class Runner:
    def __init__(
        self, root, project, compose_files, store, *, timeout=1800, runner_id=None
    ):
        self.root = Path(root).resolve()
        self.project = project
        self.files = tuple(Path(p).resolve() for p in compose_files)
        allowed = {
            self.root / name
            for name in (
                "docker-compose.yml",
                "docker-compose.ml.yml",
                "docker-compose.prod.yml",
                "docker-compose.hostvols.yml",
                "docker-compose.lan-prod.yml",
            )
        }
        if (
            not self.files
            or not set(self.files) <= allowed
            or self.root / "docker-compose.ml.yml" not in self.files
        ):
            raise ValueError("Only deployment-owned Compose files are permitted")
        if not project or any(
            c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in project
        ):
            raise ValueError("Invalid fixed Compose project")
        self.store = store
        self.timeout = timeout
        self.runner_id = runner_id or str(uuid.uuid4())
        self.services = [
            {"provider": p, "can_rebuild": False, "reason": "正在核对宿主机部署。"}
            for p in PROVIDERS
        ]

    def docker(self, *args):
        return ["docker", "--context", "default", *args]

    def compose(self, *args):
        command = self.docker(
            "compose",
            "--project-directory",
            str(self.root),
            "--project-name",
            self.project,
        )
        for path in self.files:
            command += ["--file", str(path)]
        command += [
            "--env-file",
            str(
                self.root
                / (
                    ".env.production"
                    if self.root / "docker-compose.prod.yml" in self.files
                    else ".env"
                )
            ),
        ]
        return [*command, *args]

    def command(self, args, *, logfile=None, timeout=30):
        if logfile is None:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout, check=False
            )
            if result.returncode:
                raise CommandFailure("Docker command failed")
            return result.stdout
        # The stop client is deliberately never terminated on our tracking deadline.
        # Docker's stop request has an infinite grace period; only the operator may
        # decide how to recover an indefinitely draining runtime.
        process = subprocess.Popen(
            args, stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TrackingTimeout(
                "Host tracking deadline expired; command left running"
            ) from None
        if code:
            raise CommandFailure("Docker command failed")
        return ""

    def inspect(self, container_id):
        return json.loads(self.command(self.docker("inspect", container_id)))[0]

    def locate(self, provider):
        ids = self.command(
            self.docker(
                "ps",
                "-aq",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
                "--filter",
                f"label=com.docker.compose.service={PROVIDERS[provider]}",
            )
        ).split()
        if len(ids) != 1:
            raise UnsafeRuntime("Expected exactly one existing deployment container")
        return self.inspect(ids[0])

    def validate(self, provider, container, *, probe=True):
        labels = container["Config"].get("Labels") or {}
        if (
            labels.get("com.docker.compose.project") != self.project
            or labels.get("com.docker.compose.service") != PROVIDERS[provider]
            or labels.get("com.docker.compose.project.working_dir") != str(self.root)
            or labels.get("com.docker.compose.oneoff", "").lower() != "false"
            or set(labels.get("com.docker.compose.project.config_files", "").split(","))
            != {str(p) for p in self.files}
        ):
            raise UnsafeRuntime("Container deployment identity mismatch")
        config = container["Config"]
        environment = dict(
            value.split("=", 1) for value in config.get("Env", []) if "=" in value
        )
        if (
            config.get("Cmd")
            or any(k.startswith("UVICORN_") for k in environment)
            or environment.get("WEB_CONCURRENCY", "1") != "1"
            or config.get("StopSignal", "SIGTERM") not in {"", "SIGTERM", "15"}
        ):
            raise UnsafeRuntime(
                "Custom process or shutdown overrides are not supported"
            )
        entrypoint = config.get("Entrypoint") or []
        direct = [
            "uvicorn",
            "main:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(PORTS[provider]),
            "--workers",
            "1",
        ]
        expected = (
            [
                "/bin/bash",
                "-c",
                f"python /app/scripts/download_checkpoints.py{' primary' if provider == 'gsam2' else ''} && exec uvicorn main:app --host 0.0.0.0 --port {PORTS[provider]} --workers 1",
            ]
            if provider in {"gsam2", "sam3"}
            else ["python3", "-m", *direct]
            if provider in {"onnxtools", "rapidocr"}
            else direct
        )
        if entrypoint != expected:
            raise UnsafeRuntime("Entrypoint is not the supported single-worker runtime")
        state = container["State"]
        if state["Status"] not in {"running", "exited", "created"}:
            raise UnsafeRuntime("Container transition already in progress")
        if probe and state["Status"] == "running":
            if state.get("Health", {}).get("Status") != "healthy":
                raise UnsafeRuntime(
                    "Runtime must finish startup before safe maintenance"
                )
            observed = json.loads(
                self.command(
                    self.docker("exec", container["Id"], "python3", "-c", PROBE)
                )
            )
            args = observed["argv"]
            while args and Path(args[0]).name.startswith("python"):
                args = args[1:]
            if args[:1] == ["-m"]:
                args = args[1:]
            if (
                not args
                or Path(args[0]).name != "uvicorn"
                or args[1:] != direct[1:]
                or not observed["shutdown"]
            ):
                raise UnsafeRuntime(
                    "PID 1 or graceful shutdown hook differs from supported runtime"
                )
            expected_hash = hashlib.sha256(
                (
                    self.root / "apps" / PROVIDERS[provider] / "gpu_lifecycle.py"
                ).read_bytes()
            ).hexdigest()
            if observed["lifecycle_sha256"] != expected_hash:
                raise UnsafeRuntime(
                    "Runtime lacks the verified lifecycle shutdown implementation"
                )
        mounts = {
            m["Destination"]: (m["Type"], m.get("Name") or m["Source"], m["RW"])
            for m in container.get("Mounts", [])
            if m["Destination"] in MODEL_MOUNTS[provider]
        }
        if set(mounts) != MODEL_MOUNTS[provider]:
            raise UnsafeRuntime("Persistent model mounts are incomplete")
        if any(
            m["Destination"].startswith("/app")
            and m["Destination"] not in MODEL_MOUNTS[provider]
            for m in container.get("Mounts", [])
        ):
            raise UnsafeRuntime("Unexpected source mount may replace shutdown code")
        return mounts

    def heartbeat(self):
        self.store.write(
            "heartbeat.json",
            {
                "timestamp": time.time(),
                "deployment": self.project,
                "runner_id": self.runner_id,
                "services": self.services,
            },
        )

    def refresh_services(self):
        services = []
        for provider in PROVIDERS:
            try:
                self.validate(provider, self.locate(provider))
                reason = None
            except Exception:
                reason = (
                    "未找到可安全维护的当前部署容器，请检查容器、启动状态和进程配置。"
                )
            services.append(
                {"provider": provider, "can_rebuild": reason is None, "reason": reason}
            )
        self.services = services

    def recover(self, acknowledged=()):
        with self.store.lock():
            for job in self.store.jobs():
                if job["status"] not in TERMINAL:
                    uncertain = job["status"] == "running"
                    self.store.event(
                        job,
                        job["phase"],
                        "宿主机维护程序曾中断；未自动重试，请先核查实际容器。"
                        if uncertain
                        else "未执行的请求已安全取消，可重新提交。",
                        status="interrupted",
                        kind="error",
                        uncertain=uncertain,
                    )
                if job["id"] in acknowledged:
                    job.pop("requires_inspection", None)
                    self.store.write(f"{job['id']}.json", job)

    def run_job(self, job):
        with self.store.lock():
            current = self.store.read(f"{job['id']}.json")
            if current["status"] != "queued" or not current.get("submitted"):
                return
            if (
                current["runner_id"] != self.runner_id
                or current["deployment"] != self.project
                or time.time() - datetime_epoch(current["created_at"]) > 300
            ):
                self.store.event(
                    current,
                    "queued",
                    "请求已过期或不属于当前维护进程，请重新提交。",
                    status="interrupted",
                    kind="error",
                )
                return
            job = current
            self.store.event(job, "validating", "正在核对部署、容器和优雅退出能力。")
        provider = job["provider"]
        if provider not in PROVIDERS:
            raise UnsafeRuntime("Unknown provider")
        stopped = False
        try:
            old = self.locate(provider)
            mounts = self.validate(provider, old)
            self.store.event(job, "building", "正在构建目标镜像；现有容器继续服务。")
            log_path = self.store.directory / f"{job['id']}.log"
            fd = os.open(
                log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "w") as logfile:
                self.command(
                    self.compose("build", PROVIDERS[provider]),
                    logfile=logfile,
                    timeout=self.timeout,
                )
                current = self.locate(provider)
                if (
                    current["Id"] != old["Id"]
                    or self.validate(provider, current) != mounts
                ):
                    raise UnsafeRuntime("Container changed while image was building")
                self.store.event(
                    job,
                    "stopping",
                    "停止接受新请求并等待现有推理和后台构建完成；不强制终止。",
                )
                stopped = True
                if current["State"]["Status"] == "running":
                    self.command(
                        self.docker(
                            "stop",
                            "--signal",
                            "SIGTERM",
                            "--timeout",
                            "-1",
                            current["Id"],
                        ),
                        logfile=logfile,
                        timeout=self.timeout,
                    )
                exited = self.inspect(old["Id"])
                self.validate(provider, exited, probe=False)
                if (
                    exited["State"]["Status"] not in {"exited", "created"}
                    or exited["State"].get("OOMKilled")
                    or exited["State"].get("ExitCode", 0) != 0
                ):
                    raise UnsafeRuntime("Container did not exit cleanly")
                self.store.event(
                    job,
                    "recreating",
                    "优雅退出已完成，正在重建同一服务容器并保留模型卷。",
                )
                self.command(
                    self.compose(
                        "up",
                        "-d",
                        "--no-deps",
                        "--no-build",
                        "--force-recreate",
                        PROVIDERS[provider],
                    ),
                    logfile=logfile,
                    timeout=self.timeout,
                )
            self.store.event(
                job, "checking", "正在检查容器健康；认证和业务状态仍需平台探活确认。"
            )
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                new = self.locate(provider)
                if self.validate(provider, new, probe=False) != mounts:
                    raise UnsafeRuntime(
                        "Persistent model mount changed during recreation"
                    )
                if new["State"].get("Health", {}).get("Status") == "healthy":
                    self.validate(provider, new)
                    self.store.event(
                        job,
                        "complete",
                        "目标容器已健康；空闲追踪会话缓存已清空，持久模型数据保留。请等待平台重新探活。",
                        status="succeeded",
                        kind="success",
                    )
                    return
                time.sleep(2)
            raise TrackingTimeout()
        except Exception as exc:
            uncertain = stopped or isinstance(exc, TrackingTimeout)
            message = (
                "维护结果待核查：等待超时或停止后的步骤失败。未强制终止容器，请部署管理员核查后恢复。"
                if uncertain
                else "构建或安全校验失败，未停止原容器。旧服务若不具备已验证的优雅退出能力，需部署管理员首次升级；详细诊断保存在宿主机私有日志。"
            )
            self.store.event(
                job,
                job["phase"],
                message,
                status="interrupted" if uncertain else "failed",
                kind="error",
                uncertain=uncertain,
            )

    def run(self, acknowledged=(), once=False):
        with self.store.lock("runner.lock", blocking=False):
            self.recover(acknowledged)
            stop = threading.Event()

            def pulse():
                while not stop.is_set():
                    self.heartbeat()
                    stop.wait(2)

            thread = threading.Thread(target=pulse, daemon=True)
            thread.start()
            try:
                self.refresh_services()
                refreshed_at = time.monotonic()
                while True:
                    for reserved in self.store.jobs():
                        if (
                            reserved["status"] == "queued"
                            and not reserved.get("submitted")
                            and time.time() - datetime_epoch(reserved["created_at"])
                            > 30
                        ):
                            with self.store.lock():
                                latest = self.store.read(f"{reserved['id']}.json")
                                if not latest.get("submitted"):
                                    self.store.event(
                                        latest,
                                        "queued",
                                        "请求未完成审计提交，已安全取消；可重新提交。",
                                        status="interrupted",
                                        kind="error",
                                    )
                    if time.monotonic() - refreshed_at > 10:
                        self.refresh_services()
                        refreshed_at = time.monotonic()
                    if not any(j.get("requires_inspection") for j in self.store.jobs()):
                        for job in reversed(self.store.jobs()):
                            if job["status"] == "queued" and job.get("submitted"):
                                self.run_job(job)
                                break
                    if once:
                        return
                    time.sleep(1)
            finally:
                stop.set()
                thread.join(timeout=3)
                (self.store.directory / "heartbeat.json").unlink(missing_ok=True)


def datetime_epoch(value):
    from datetime import datetime

    return datetime.fromisoformat(value).timestamp()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--compose-file", type=Path, action="append", required=True)
    parser.add_argument("--operations-dir", type=Path, required=True)
    parser.add_argument("--phase-timeout", type=int, default=1800)
    parser.add_argument(
        "--acknowledge-interrupted", type=uuid.UUID, action="append", default=[]
    )
    args = parser.parse_args()
    if not 30 <= args.phase_timeout <= 86400:
        parser.error("phase timeout must be 30..86400 seconds")
    runner = Runner(
        args.root,
        args.project,
        args.compose_file,
        JobStore(args.operations_dir),
        timeout=args.phase_timeout,
    )
    runner.run([str(v) for v in args.acknowledge_interrupted])


if __name__ == "__main__":
    main()
