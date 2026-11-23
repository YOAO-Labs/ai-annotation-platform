#!/usr/bin/env python3
"""Restart primary-checkout development apps, excluding shared infrastructure."""

import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import time
import urllib.request
import urllib.error

RECOVERY_WORKER = "celery-worker-jobs-recovery"
RECOVERY_COMMAND = [
    "/bin/sh",
    "-ec",
    "python -m app.workers.jobs_dispatch; "
    "exec celery -A app.workers.celery_app worker -l info -Q jobs.recovery "
    "--concurrency=1 --prefetch-multiplier=1",
]

WORKERS = (
    "celery-worker",
    "celery-worker-maintenance",
    "celery-worker-gpu",
    "celery-worker-cpu",
    "celery-worker-export",
    "celery-worker-image-pyramid",
    "celery-worker-gpu-control",
    RECOVERY_WORKER,
    "celery-beat",
)


def command(*args, timeout=30):
    return subprocess.check_output(
        args, text=True, timeout=timeout, stderr=subprocess.PIPE
    )


def docker(*args, timeout=30):
    return command("docker", "--context", "default", *args, timeout=timeout)


def validate(containers, project_dir):
    found = {}
    for container in containers:
        labels = container["Config"].get("Labels") or {}
        service = labels.get("com.docker.compose.service")
        if service not in WORKERS:
            continue
        files = labels.get("com.docker.compose.project.config_files", "").split(",")
        if (
            labels.get("com.docker.compose.project") != "ai-annotation-platform"
            or labels.get("com.docker.compose.project.working_dir") != str(project_dir)
            or labels.get("com.docker.compose.oneoff", "").lower() != "false"
            or str(project_dir / "docker-compose.yml") not in files
            or not set(files)
            <= {
                str(project_dir / "docker-compose.yml"),
                str(project_dir / "docker-compose.ml.yml"),
                str(project_dir / "docker-compose.ml-yolo.yml"),
                str(project_dir / "docker-compose.ml-onnxtools.yml"),
                str(project_dir / "docker-compose.ml-rapidocr.yml"),
            }
            or service in found
        ):
            raise RuntimeError("Development worker identity mismatch; restart refused")
        environment = dict(
            item.split("=", 1)
            for item in container["Config"].get("Env", [])
            if "=" in item
        )
        if environment.get("ALEMBIC_AUTO_UPGRADE") != "false":
            raise RuntimeError(
                "Automatic migration enabled or unspecified; recreate development workers before scheduled restart"
            )
        dispatch = "JOBS_DISPATCH_DATABASE_URL"
        publication = "JOBS_PUBLICATION_DATABASE_URL"
        if service == RECOVERY_WORKER:
            if (
                container["Config"].get("Cmd") != RECOVERY_COMMAND
                or any(
                    bool(environment.get(key)) == bool(environment.get(f"{key}_FILE"))
                    for key in (dispatch, publication)
                )
                or environment.get("DATABASE_URL")
                or environment.get("MIGRATION_DATABASE_URL")
            ):
                raise RuntimeError("Development Jobs recovery configuration mismatch")
        elif environment.get(dispatch) or environment.get(f"{dispatch}_FILE"):
            raise RuntimeError(
                "Jobs claim credential belongs only to the recovery worker"
            )
        if service == "celery-beat" and (
            environment.get(publication) or environment.get(f"{publication}_FILE")
        ):
            raise RuntimeError("Beat must not hold Jobs publication credentials")
        found[service] = container["Id"]
    if set(found) != set(WORKERS):
        missing = ", ".join(s for s in WORKERS if s not in found)
        raise RuntimeError(f"Missing development workers; restart refused: {missing}")
    return [found[s] for s in WORKERS]


def http_ok(url):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=5) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def run(args):
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.state_dir / "restart.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Development restart already running; skipped.", flush=True)
            return
        if (args.state_dir / "maintenance").exists():
            print("Development maintenance enabled; skipped.", flush=True)
            return
        ids = docker(
            "ps",
            "-aq",
            "--filter",
            "label=com.docker.compose.project=ai-annotation-platform",
        ).split()
        if not ids:
            raise RuntimeError("Development containers missing")
        workers = validate(
            json.loads(docker("inspect", *ids)), args.project_dir.resolve()
        )
        print(
            f"Validated {len(workers)} development worker/beat containers; shared services excluded.",
            flush=True,
        )
        if args.check:
            return
        validate(json.loads(docker("inspect", *workers)), args.project_dir.resolve())
        if (args.state_dir / "maintenance").exists():
            print("Development maintenance enabled; skipped.", flush=True)
            return
        print(
            "Restarting development workers (120-second graceful stop timeout).",
            flush=True,
        )
        docker("restart", "--time", "120", *workers, timeout=120 * len(workers) + 30)
        if (args.state_dir / "maintenance").exists():
            print(
                "Development maintenance enabled during worker restart; host restart skipped.",
                flush=True,
            )
            return
        print("Restarting systemd-owned development API/Web.", flush=True)
        command(
            "systemctl",
            "--user",
            "restart",
            "aap-development-api.service",
            "aap-development-web.service",
            timeout=100,
        )
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            containers = json.loads(docker("inspect", *workers))
            ready = all(
                c["State"]["Status"] == "running"
                and c["State"].get("Health", {}).get("Status", "healthy") == "healthy"
                for c in containers
            )
            if (
                ready
                and http_ok("http://127.0.0.1:8000/health/db")
                and http_ok("http://127.0.0.1:3000/")
            ):
                print(
                    f"Development API/Web and all {len(workers)} worker/beat containers ready.",
                    flush=True,
                )
                return
            time.sleep(3)
        raise RuntimeError(
            "Development readiness timed out; inspect service journal and worker health"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    try:
        run(parser.parse_args())
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(
            str(error) if isinstance(error, RuntimeError) else type(error).__name__,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
