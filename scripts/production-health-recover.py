#!/usr/bin/env python3
"""Recover only existing LAN production containers; never deploy or signal host PIDs."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import ssl
import subprocess
import time
import urllib.error
import urllib.request

PROJECT = "aap-production"
SERVICES = (
    "redis-production",
    "api",
    "web",
    "gateway",
    "celery-worker",
    "celery-worker-maintenance",
    "celery-worker-gpu",
    "celery-worker-cpu",
    "celery-worker-export",
    "celery-worker-image-pyramid",
    "celery-worker-gpu-control",
    "celery-beat",
)
THRESHOLD = 3
COOLDOWN = 900
MAX_HOURLY = 3


def docker(*args, timeout=25):
    result = subprocess.run(
        ["docker", "--context", "default", *args],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=True,
    )
    return result.stdout


def inspect(cid):
    return json.loads(docker("inspect", cid))[0]


def owned(container, service, project_dir):
    labels = container["Config"].get("Labels") or {}
    return (
        labels.get("com.docker.compose.project") == PROJECT
        and labels.get("com.docker.compose.service") == service
        and labels.get("com.docker.compose.oneoff", "").lower() == "false"
        and labels.get("com.docker.compose.project.working_dir") == str(project_dir)
        and labels.get("com.docker.compose.project.config_files")
        == str(project_dir / "docker-compose.lan-prod.yml")
    )


def state_signature(container):
    state = container["State"]
    return (
        state["Status"],
        state.get("StartedAt"),
        state.get("Health", {}).get("Status"),
    )


def healthy(container):
    state = container["State"]
    return (
        state["Status"] == "running"
        and state.get("Health", {}).get("Status", "healthy") == "healthy"
    )


def api_responds(cid):
    # Use a dependency-free route; any HTTP response proves the server is alive.
    code = """import urllib.request, urllib.error
try:
    urllib.request.urlopen('http://127.0.0.1:8000/openapi.json', timeout=5)
except urllib.error.HTTPError:
    pass
"""
    try:
        docker("exec", cid, "python", "-c", code, timeout=10)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def ingress_ok(container, ca_file):
    context = ssl.create_default_context(cafile=str(ca_file))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    ports = container["NetworkSettings"]["Ports"]
    for port, path in (("3030", "/healthz"), ("8080", "/health/db")):
        bindings = ports.get(port + "/tcp") or []
        if len(bindings) != 1:
            return False
        binding = bindings[0]
        host = binding["HostIp"]
        if binding["HostPort"] != port or host in ("0.0.0.0", "::", ""):
            return False
        try:
            with opener.open(f"https://{host}:{port}{path}", timeout=8) as response:
                if response.status != 200:
                    return False
        except (urllib.error.URLError, TimeoutError, OSError):
            return False
    return True


def classify(service, container, containers, ca_file):
    state = container["State"]
    status = state["Status"]
    if status in ("exited", "created"):
        return "start", status
    if status != "running":
        return None, status  # Docker owns restarting; paused containers stay paused.
    health = state.get("Health", {}).get("Status")
    if health == "starting":
        return None, "starting"
    if service == "gateway":
        if not all(healthy(containers[s]) for s in ("api", "web")):
            return None, "upstream unhealthy; gateway recovery suppressed"
        return (
            (None, "healthy")
            if ingress_ok(container, ca_file)
            else ("restart", "HTTPS probe failed")
        )
    if health == "unhealthy":
        if service == "api" and api_responds(container["Id"]):
            return None, "dependency health degraded; API responds"
        if service.startswith("celery-worker") and not healthy(
            containers["redis-production"]
        ):
            return None, "broker unhealthy; worker recovery suppressed"
        return "restart", "unhealthy"
    return None, "healthy"


def run(args):
    project_dir = args.project_dir.resolve()
    state_dir = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_dir / "health.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another production health check is running; skipped.")
            return 0
        if (state_dir / "maintenance").exists():
            print("Production maintenance enabled; recovery paused.")
            return 0
        # Validate CA before any mutation, even when gateway is stopped.
        ssl.create_default_context(cafile=str(args.ca_file))
        ids = docker(
            "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"
        ).split()
        containers = {}
        for cid in ids:
            container = inspect(cid)
            labels = container["Config"].get("Labels") or {}
            service = labels.get("com.docker.compose.service")
            if (
                labels.get("com.docker.compose.oneoff", "").lower() == "true"
                or service == "migrate"
            ):
                continue
            if (
                service not in SERVICES
                or not owned(container, service, project_dir)
                or service in containers
            ):
                raise RuntimeError(
                    "Production container identity/replica mismatch; no recovery performed"
                )
            containers[service] = container
        missing = set(SERVICES) - containers.keys()
        if missing:
            raise RuntimeError(
                "Missing production services (manual deployment required): "
                + ", ".join(sorted(missing))
            )
        state_file = state_dir / "health-state.json"
        history = json.loads(state_file.read_text()) if state_file.exists() else {}
        now = time.time()
        degraded = False
        for service in SERVICES:
            container = containers[service]
            action, reason = classify(service, container, containers, args.ca_file)
            print(f"{service}: {reason}", flush=True)
            record = history.setdefault(service, {})
            identity = [container["Id"], container["State"].get("StartedAt")]
            if record.get("identity") != identity:
                record.update(identity=identity, failures=0)
            # Require consecutive scheduled observations, not stale failures after downtime.
            consecutive = 0 <= now - record.get("observed", 0) <= 300
            record["failures"] = (
                (record.get("failures", 0) + 1 if consecutive else 1) if action else 0
            )
            record["observed"] = now
            attempts = [t for t in record.get("attempts", []) if now - t < 3600]
            record["attempts"] = attempts
            degraded |= reason != "healthy"
            if not action or record["failures"] < THRESHOLD:
                continue
            if attempts and (
                now - attempts[-1] < COOLDOWN or len(attempts) >= MAX_HOURLY
            ):
                print(f"{service}: recovery rate limited", flush=True)
                continue
            if args.check:
                print(f"{service}: would {action} (check only)", flush=True)
                continue
            current = inspect(container["Id"])
            if not owned(current, service, project_dir) or state_signature(
                current
            ) != state_signature(container):
                print(f"{service}: state changed during check; deferred", flush=True)
                continue
            if (state_dir / "maintenance").exists():
                return 0
            # Persist attempt before mutation so interrupted runs cannot cause restart loops.
            record["attempts"].append(now)
            save_state(state_file, history)
            print(f"{service}: {action} {container['Id'][:12]}", flush=True)
            if action == "restart":
                docker("restart", "--time", "60", container["Id"], timeout=85)
            else:
                docker("start", container["Id"], timeout=85)
            # The next timer invocation verifies health, allowing normal startup grace.
            return 1
        if not args.check:
            save_state(state_file, history)
        return 1 if degraded else 0


def save_state(path, history):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(history, indent=2) + "\n")
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--ca-file", required=True, type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Read-only diagnosis; do not update failure history or containers",
    )
    args = parser.parse_args()
    try:
        return run(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        # Subprocess output may contain configuration; never dump it into the journal.
        print(
            f"Production health check failed: {type(error).__name__}: "
            + (
                str(error)
                if isinstance(error, RuntimeError)
                else "inspect runtime/configuration"
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
