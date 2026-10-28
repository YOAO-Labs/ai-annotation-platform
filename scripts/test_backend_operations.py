"""Focused, DB-free maintenance tests. Docker commands are always mocked."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))
from app.services.backend_operations import JobStore, OperationConflict, TargetChanged  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "backend_runner", ROOT / "scripts/ml-backend-runner.py"
)
runner_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner_module)


class FakeRunner(runner_module.Runner):
    def __init__(self, store):
        super().__init__(
            ROOT,
            "maintenance-test",
            [ROOT / "docker-compose.yml", ROOT / "docker-compose.ml.yml"],
            store,
        )
        self.commands = []
        self.failure = None
        self.container = {
            "Id": "old-id",
            "Config": {
                "Entrypoint": [
                    "uvicorn",
                    "main:app",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8003",
                    "--workers",
                    "1",
                ],
                "Cmd": None,
                "Env": [],
                "Labels": {
                    "com.docker.compose.project": self.project,
                    "com.docker.compose.service": "yolo-backend",
                    "com.docker.compose.project.working_dir": str(ROOT),
                    "com.docker.compose.project.config_files": ",".join(
                        str(p) for p in self.files
                    ),
                    "com.docker.compose.oneoff": "False",
                },
            },
            "State": {
                "Status": "running",
                "Health": {"Status": "healthy"},
                "ExitCode": 0,
            },
            "Mounts": [
                {
                    "Destination": "/app/checkpoints",
                    "Type": "volume",
                    "Name": "maintenance-test_yolo_checkpoints",
                    "Source": "/private/volume",
                    "RW": True,
                }
            ],
        }
        self.probe = {
            "argv": list(self.container["Config"]["Entrypoint"]),
            "shutdown": True,
            "lifecycle_sha256": hashlib.sha256(
                (ROOT / "apps/_shared/backend_runtime/src/aap_backend_runtime/gpu_lifecycle.py").read_bytes()
            ).hexdigest(),
        }
        self.services = [{"provider": "yolo", "can_rebuild": True, "reason": None}]
        self.heartbeat()

    def command(self, args, **kwargs):
        self.commands.append(args)
        if "inspect" in args:
            return json.dumps([self.container])
        if "ps" in args:
            return self.container["Id"]
        if "exec" in args:
            return json.dumps(self.probe)
        if "build" in args and self.failure == "build":
            raise runner_module.CommandFailure()
        if "stop" in args:
            if self.failure == "stop":
                raise runner_module.TrackingTimeout()
            self.container["State"]["Status"] = "exited"
        if "up" in args:
            self.container["Id"] = "new-id"
            self.container["State"]["Status"] = "running"
            if self.failure == "mount":
                self.container["Mounts"][0]["Name"] = "foreign-volume"
        return ""

    def job(self):
        return self.store.publish(
            self.store.reserve(
                "yolo",
                expected_deployment=self.runner.project
                if hasattr(self, "runner")
                else self.project,
                expected_runner_id=self.runner.runner_id
                if hasattr(self, "runner")
                else self.runner_id,
            )["id"]
        )


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = JobStore(Path(self.temp.name) / "queue")
        self.runner = FakeRunner(self.store)

    def test_private_atomic_queue_and_duplicate_conflict(self):
        job = self.store.reserve(
            "yolo",
            expected_deployment=self.runner.project
            if hasattr(self, "runner")
            else self.project,
            expected_runner_id=self.runner.runner_id
            if hasattr(self, "runner")
            else self.runner_id,
        )
        self.assertFalse(job["submitted"])
        self.assertEqual(self.store.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            (self.store.directory / f"{job['id']}.json").stat().st_mode & 0o777, 0o600
        )
        with self.assertRaises(OperationConflict):
            self.store.reserve(
                "yolo",
                expected_deployment=self.runner.project
                if hasattr(self, "runner")
                else self.project,
                expected_runner_id=self.runner.runner_id
                if hasattr(self, "runner")
                else self.runner_id,
            )
        self.assertFalse(self.store.status()["services"][2]["can_rebuild"])
        self.store.abort_reservation(job["id"])
        self.assertEqual(self.store.jobs(), [])

    def test_confirmation_is_bound_to_runner_and_deployment(self):
        for deployment, runner_id in [
            ("another-project", self.runner.runner_id),
            (self.runner.project, "old-runner"),
        ]:
            with self.assertRaises(TargetChanged):
                self.store.reserve(
                    "yolo", expected_deployment=deployment, expected_runner_id=runner_id
                )
        self.assertEqual(self.store.jobs(), [])

    def test_missing_runner_and_undeployed_provider_are_disabled(self):
        status = self.store.status()
        self.assertTrue(
            next(s for s in status["services"] if s["provider"] == "yolo")[
                "can_rebuild"
            ]
        )
        self.assertFalse(
            next(s for s in status["services"] if s["provider"] == "sam3")[
                "can_rebuild"
            ]
        )
        heartbeat = self.store.read("heartbeat.json")
        heartbeat["timestamp"] = time.time() - 20
        self.store.write("heartbeat.json", heartbeat)
        self.assertFalse(self.store.status()["available"])

    def test_malformed_and_symlink_queue_fail_closed(self):
        path = self.store.directory / f"{uuid.uuid4()}.json"
        path.write_text('{"provider": []}')
        path.chmod(0o600)
        self.assertFalse(self.store.status()["available"])
        path.unlink()
        (self.store.directory / "queue.lock").symlink_to("heartbeat.json")
        with self.assertRaises(OSError):
            with self.store.lock():
                pass

    def test_fifo_lock_never_blocks(self):
        os.mkfifo(self.store.directory / "queue.lock", 0o600)
        with self.assertRaises(PermissionError):
            with self.store.lock():
                pass

    def test_malformed_heartbeat_and_job_metadata_fail_closed(self):
        heartbeat = self.store.read("heartbeat.json")
        heartbeat["services"] = [{"provider": [], "can_rebuild": True}]
        self.store.write("heartbeat.json", heartbeat)
        self.assertFalse(self.store.status()["available"])
        self.runner.heartbeat()
        job = self.runner.job()
        job["error"] = []
        self.store.write(f"{job['id']}.json", job)
        self.assertFalse(self.store.status()["available"])

    def test_build_before_infinite_grace_stop_and_volume_preservation(self):
        job = self.runner.job()
        self.runner.run_job(job)
        result = self.store.read(f"{job['id']}.json")
        self.assertEqual(result["status"], "succeeded")
        actions = [
            args
            for args in self.runner.commands
            if "build" in args or "stop" in args or "up" in args
        ]
        self.assertIn("build", actions[0])
        self.assertEqual(
            actions[1][-6:],
            ["stop", "--signal", "SIGTERM", "--timeout", "-1", "old-id"],
        )
        self.assertEqual(
            actions[2][-6:],
            ["up", "-d", "--no-deps", "--no-build", "--force-recreate", "yolo-backend"],
        )
        self.assertNotIn("auth", json.dumps(result))
        self.assertFalse(
            any(
                "kill" in args or "down" in args or "-v" in args
                for args in self.runner.commands
            )
        )

    def test_all_five_current_dockerfiles_and_shutdown_sources_are_supported(self):
        for provider, service in runner_module.PROVIDERS.items():
            with self.subTest(provider=provider):
                container = deepcopy(self.runner.container)
                container["Config"]["Labels"]["com.docker.compose.service"] = service
                source = (ROOT / "apps" / service / "Dockerfile").read_text()
                entrypoint = next(
                    line.removeprefix("ENTRYPOINT ")
                    for line in source.splitlines()
                    if line.startswith("ENTRYPOINT ")
                )
                container["Config"]["Entrypoint"] = json.loads(entrypoint)
                container["Mounts"] = [
                    {
                        "Destination": mount,
                        "Type": "volume",
                        "Name": "fixed-model-volume",
                        "Source": "/models",
                        "RW": True,
                    }
                    for mount in runner_module.MODEL_MOUNTS[provider]
                ]
                self.runner.probe = {
                    "argv": [
                        "uvicorn",
                        "main:app",
                        "--host",
                        "0.0.0.0",
                        "--port",
                        str(runner_module.PORTS[provider]),
                        "--workers",
                        "1",
                    ],
                    "shutdown": True,
                    "lifecycle_sha256": hashlib.sha256(
                        (ROOT / "apps/_shared/backend_runtime/src/aap_backend_runtime/gpu_lifecycle.py" if provider == "yolo" else ROOT / "apps" / service / "gpu_lifecycle.py").read_bytes()
                    ).hexdigest(),
                }
                self.runner.validate(provider, container)

    def test_build_failure_does_not_stop_old_container(self):
        self.runner.failure = "build"
        job = self.runner.job()
        self.runner.run_job(job)
        self.assertEqual(self.store.read(f"{job['id']}.json")["status"], "failed")
        self.assertFalse(
            any("stop" in args or "up" in args for args in self.runner.commands)
        )

    def test_stop_timeout_never_recreates_and_requires_host_inspection(self):
        self.runner.failure = "stop"
        job = self.runner.job()
        self.runner.run_job(job)
        result = self.store.read(f"{job['id']}.json")
        self.assertEqual(result["status"], "interrupted")
        self.assertTrue(result["requires_inspection"])
        self.assertFalse(any("up" in args for args in self.runner.commands))
        self.assertFalse(self.store.status()["services"][2]["can_rebuild"])

    def test_changed_model_mount_fails_without_resume(self):
        self.runner.failure = "mount"
        job = self.runner.job()
        self.runner.run_job(job)
        self.assertTrue(self.store.read(f"{job['id']}.json")["requires_inspection"])

    def test_process_environment_or_lifecycle_mismatch_refuses(self):
        for key in ("identity", "environment", "pid", "hash"):
            with self.subTest(key=key):
                container = deepcopy(self.runner.container)
                probe = deepcopy(self.runner.probe)
                if key == "identity":
                    container["Config"]["Labels"]["com.docker.compose.project"] = (
                        "foreign"
                    )
                elif key == "environment":
                    container["Config"]["Env"] = ["UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN=1"]
                elif key == "pid":
                    self.runner.probe["argv"][-1] = "2"
                else:
                    self.runner.probe["lifecycle_sha256"] = "bad"
                with self.assertRaises(runner_module.UnsafeRuntime):
                    self.runner.validate("yolo", container)
                self.runner.probe = probe

    def test_restart_marks_running_uncertain_without_replay(self):
        job = self.runner.job()
        self.store.event(job, "stopping", "wait")
        self.runner.recover()
        self.assertTrue(self.store.read(f"{job['id']}.json")["requires_inspection"])
        self.runner.recover([job["id"]])
        self.assertFalse(
            self.store.read(f"{job['id']}.json").get("requires_inspection")
        )
        self.assertFalse(any("stop" in c for c in self.runner.commands))


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from fastapi import FastAPI
        from app.api.v1 import admin_backend_operations as api
        from app.deps import get_current_user, get_db

        self.api = api
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = JobStore(Path(self.temp.name) / "queue")
        self.runner = FakeRunner(self.store)
        self.user = SimpleNamespace(role="super_admin", id=uuid.uuid4())
        self.payload = {
            "provider": "yolo",
            "expected_deployment": self.runner.project,
            "expected_runner_id": self.runner.runner_id,
        }
        self.db = SimpleNamespace(commit=AsyncMock())
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        )
        self.addAsyncCleanup(self.client.aclose)
        self.patcher = patch.object(api, "get_store", return_value=self.store)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.audit = patch.object(api.AuditService, "log", new_callable=AsyncMock)
        self.audit_mock = self.audit.start()
        self.addCleanup(self.audit.stop)

    async def test_admin_submission_audited_and_extra_commands_rejected(self):
        response = await self.client.post(
            "/admin/ml-backend-operations/jobs", json=self.payload
        )
        self.assertEqual(response.status_code, 202, response.text)
        data = response.json()
        self.assertNotIn("runner_id", data)
        self.assertNotIn("submitted", data)
        self.audit_mock.assert_awaited_once()
        self.db.commit.assert_awaited_once()
        self.assertEqual(
            (
                await self.client.get(f"/admin/ml-backend-operations/jobs/{data['id']}")
            ).status_code,
            200,
        )
        duplicate = await self.client.post(
            "/admin/ml-backend-operations/jobs", json=self.payload
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(
            (
                await self.client.post(
                    "/admin/ml-backend-operations/jobs",
                    json={"provider": "yolo", "command": "evil"},
                )
            ).status_code,
            422,
        )

    async def test_stale_confirmation_has_no_unrelated_job_id(self):
        payload = {**self.payload, "expected_runner_id": "old-runner"}
        response = await self.client.post(
            "/admin/ml-backend-operations/jobs", json=payload
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["error_code"], "backend_manager_target_changed"
        )
        self.assertNotIn("job_id", response.json()["detail"])
        self.assertEqual(self.store.jobs(), [])

    async def test_non_admin_cannot_read_or_submit(self):
        self.user.role = "employee"
        for method, path in [
            ("GET", "/status"),
            ("POST", "/jobs"),
            ("GET", f"/jobs/{uuid.uuid4()}"),
        ]:
            response = await self.client.request(
                method,
                "/admin/ml-backend-operations" + path,
                json=self.payload if method == "POST" else None,
            )
            self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.jobs(), [])

    async def test_failed_audit_cannot_publish_runnable_job(self):
        self.db.commit.side_effect = RuntimeError("DB failed")
        response = await self.client.post(
            "/admin/ml-backend-operations/jobs", json=self.payload
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.store.jobs(), [])


if __name__ == "__main__":
    unittest.main()
