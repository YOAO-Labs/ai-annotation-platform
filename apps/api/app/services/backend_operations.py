"""Private file queue shared by the API and an explicitly installed host runner."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid

PROVIDERS = {
    "gsam2": "grounded-sam2-backend",
    "sam3": "sam3-backend",
    "yolo": "yolo-backend",
    "onnxtools": "onnxtools-backend",
    "rapidocr": "rapidocr-backend",
}
TERMINAL = {"succeeded", "failed", "interrupted"}


def now():
    return datetime.now(UTC).isoformat()


def operations_directory():
    from app.config import settings

    parents = Path(__file__).resolve().parents
    default = (
        parents[4] / "data/backend-manager"
        if len(parents) > 4
        else Path("/run/aap-backend-manager")
    )
    return Path(
        getattr(settings, "ml_backend_operations_dir", None)
        or os.environ.get("ML_BACKEND_OPERATIONS_DIR")
        or default
    )


class OperationConflict(Exception):
    def __init__(self, job_id):
        self.job_id = job_id


class ManagerUnavailable(Exception):
    pass


class TargetChanged(Exception):
    pass


class JobStore:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()

    def prepare(self):
        if self.directory.resolve() != self.directory:
            raise ValueError("Private queue must not use symlinks")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if os.geteuid() not in {0, info.st_uid}:
                raise PermissionError("Private queue owner mismatch")
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)

    @contextmanager
    def lock(self, name="queue.lock", blocking=True):
        self.prepare()
        fd = os.open(
            self.directory / name,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise PermissionError("Queue lock must be a regular file")
            if os.geteuid() == 0:
                info = self.directory.stat()
                os.fchown(fd, info.st_uid, info.st_gid)
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            yield
        finally:
            os.close(fd)

    def read(self, name):
        fd = os.open(self.directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_uid != self.directory.stat().st_uid
            ):
                raise PermissionError("Unsafe private queue file")
            raw = stream.read(1048577)
            if len(raw) > 1048576:
                raise ValueError("Queue document exceeds limit")
            return json.loads(raw)

    def write(self, name, value):
        self.prepare()
        fd, path = tempfile.mkstemp(prefix=".write-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                if os.geteuid() == 0:
                    info = self.directory.stat()
                    os.fchown(stream.fileno(), info.st_uid, info.st_gid)
                json.dump(value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, self.directory / name)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(path).unlink(missing_ok=True)

    def jobs(self):
        if not self.directory.exists():
            return []
        jobs = []
        for path in self.directory.glob("*.json"):
            try:
                job_id = str(uuid.UUID(path.stem))
                job = self.read(f"{job_id}.json")
                self.validate_job(job, job_id)
                jobs.append(job)
            except (KeyError, TypeError) as exc:
                raise ValueError("Invalid private queue document") from exc
            except ValueError:
                if path.stem == "heartbeat":
                    continue
                raise
        return sorted(jobs, key=lambda j: j["created_at"], reverse=True)

    @staticmethod
    def validate_job(job, job_id):
        if (
            not isinstance(job, dict)
            or job.get("id") != job_id
            or job.get("provider") not in PROVIDERS
            or job.get("status") not in TERMINAL | {"queued", "running"}
        ):
            raise ValueError("Invalid private queue document")
        if job.get("phase") not in {
            "queued",
            "validating",
            "building",
            "stopping",
            "recreating",
            "checking",
            "complete",
        }:
            raise ValueError("Invalid job phase")
        for key in ("created_at", "updated_at"):
            datetime.fromisoformat(job[key])
        if (
            not isinstance(job.get("deployment"), str)
            or not isinstance(job.get("runner_id"), str)
            or not isinstance(job.get("submitted"), bool)
            or not isinstance(job.get("events"), list)
            or (job.get("error") is not None and not isinstance(job["error"], str))
            or not isinstance(job.get("requires_inspection", False), bool)
        ):
            raise ValueError("Invalid job metadata")
        for event in job["events"]:
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("message"), str)
                or not isinstance(event.get("at"), str)
                or event.get("kind") not in {"info", "success", "error"}
            ):
                raise ValueError("Invalid job event")

    def heartbeat(self):
        try:
            heartbeat = self.read("heartbeat.json")
            if (
                not isinstance(heartbeat, dict)
                or not isinstance(heartbeat.get("services"), list)
                or not isinstance(heartbeat.get("deployment"), str)
                or not isinstance(heartbeat.get("runner_id"), str)
            ):
                return None
            if not 0 <= time.time() - heartbeat["timestamp"] < 15:
                return None
            for service in heartbeat["services"]:
                if (
                    not isinstance(service, dict)
                    or not isinstance(service.get("provider"), str)
                    or service["provider"] not in PROVIDERS
                    or not isinstance(service.get("can_rebuild"), bool)
                    or (
                        service.get("reason") is not None
                        and not isinstance(service["reason"], str)
                    )
                ):
                    return None
            return heartbeat
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def status(self):
        heartbeat = self.heartbeat()
        try:
            jobs = self.jobs()
        except (OSError, ValueError, KeyError, TypeError):
            return {
                "available": False,
                "reason": "维护队列损坏或权限异常，请部署管理员检查私有目录。",
                "deployment": None,
                "runner_id": None,
                "jobs": [],
                "services": [
                    {"provider": p, "can_rebuild": False, "reason": "维护队列不可用。"}
                    for p in PROVIDERS
                ],
            }
        active = next((j for j in jobs if j["status"] not in TERMINAL), None)
        uncertain = next((j for j in jobs if j.get("requires_inspection")), None)
        reason = (
            None
            if heartbeat
            else "宿主机维护程序未运行或心跳已过期；请先由部署管理员安装并启动。"
        )
        blocker = reason or (
            "存在待核查的中断任务，请在宿主机确认容器状态后恢复维护程序。"
            if uncertain
            else "已有维护任务正在进行。"
            if active
            else None
        )
        service_states = {
            s.get("provider"): s
            for s in (heartbeat or {}).get("services", [])
            if isinstance(s, dict)
        }
        return {
            "available": bool(heartbeat),
            "reason": reason,
            "deployment": heartbeat.get("deployment") if heartbeat else None,
            "runner_id": heartbeat.get("runner_id") if heartbeat else None,
            "services": [
                {
                    "provider": p,
                    "can_rebuild": not blocker
                    and service_states.get(p, {}).get("can_rebuild") is True,
                    "reason": blocker
                    or service_states.get(p, {}).get("reason")
                    or (
                        None
                        if service_states.get(p, {}).get("can_rebuild") is True
                        else "宿主机尚未登记可维护的服务容器。"
                    ),
                }
                for p in PROVIDERS
            ],
            "jobs": jobs[:30],
        }

    def reserve(self, provider, *, expected_deployment, expected_runner_id):
        if provider not in PROVIDERS:
            raise ValueError("Unknown provider")
        with self.lock():
            heartbeat = self.heartbeat()
            if not heartbeat:
                raise ManagerUnavailable()
            if (
                heartbeat["deployment"] != expected_deployment
                or heartbeat["runner_id"] != expected_runner_id
            ):
                raise TargetChanged()
            if not any(
                s.get("provider") == provider and s.get("can_rebuild") is True
                for s in heartbeat["services"]
                if isinstance(s, dict)
            ):
                raise ManagerUnavailable()
            for job in self.jobs():
                if job["status"] not in TERMINAL or job.get("requires_inspection"):
                    raise OperationConflict(job["id"])
            job = {
                "id": str(uuid.uuid4()),
                "provider": provider,
                "status": "queued",
                "phase": "queued",
                "created_at": now(),
                "updated_at": now(),
                "events": [],
                "error": None,
                "deployment": heartbeat["deployment"],
                "runner_id": heartbeat["runner_id"],
                "submitted": False,
            }
            self.write(f"{job['id']}.json", job)
            return job

    def publish(self, job_id):
        with self.lock():
            job = self.read(f"{uuid.UUID(job_id)}.json")
            job["submitted"] = True
            self.write(f"{job['id']}.json", job)
            return job

    def abort_reservation(self, job_id):
        with self.lock():
            job = self.read(f"{uuid.UUID(job_id)}.json")
            if not job.get("submitted"):
                (self.directory / f"{job['id']}.json").unlink()

    def event(
        self, job, phase, message, *, status="running", kind="info", uncertain=False
    ):
        job.update(phase=phase, status=status, updated_at=now())
        job["events"] = [
            *job["events"][-49:],
            {"at": now(), "message": message, "kind": kind},
        ]
        if status in {"failed", "interrupted"}:
            job["error"] = message
        if uncertain:
            job["requires_inspection"] = True
        self.write(f"{job['id']}.json", job)
